# bot_web.py
# -*- coding: utf-8 -*-
import os, json, time, uuid, asyncio, logging
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import web
from redis import Redis
from rq import Queue

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram import BotCommand

from convert import detect_bins, kind_for_extension, ext_of, safe_name

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger("web")

# ====== إعدادات ======
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN مفقود")
PORT = int(os.getenv('PORT', '10000'))
PUBLIC_URL = os.getenv('PUBLIC_URL', '')
USE_WEBHOOK = os.getenv('USE_WEBHOOK', '1') == '1'

OWNER_ID = int(os.getenv('OWNER_ID', '0') or 0)
ADMINS = {OWNER_ID} if OWNER_ID else set()

OPS_PER_MINUTE = int(os.getenv('OPS_PER_MINUTE', '10'))
TG_LIMIT_MB = int(os.getenv('TG_LIMIT_MB', os.getenv('MAX_SEND_MB','49')))
TG_LIMIT_BYTES = TG_LIMIT_MB * 1024 * 1024

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis = Redis.from_url(REDIS_URL)
queue = Queue("conversions", connection=redis)

# anti-spam per user
USER_QPS: dict[int, deque] = defaultdict(deque)
def allow(uid:int)->bool:
    dq = USER_QPS[uid]
    now = time.time()
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= OPS_PER_MINUTE:
        return False
    dq.append(now)
    return True

# bins (لعرض الأزرار بشكل واقعي)
BINS = detect_bins()

FORMATS_TEXT = (
    "✅ المدعوم:\n"
    "• Office → PDF (DOC/DOCX/RTF/ODT/PPT/PPTX/XLS/XLSX)\n"
    "• PDF → DOCX | صور (PNG/JPG داخل ZIP)\n"
    "• صور JPG/PNG/WEBP ↔ بين بعض | صورة → PDF\n"
    "• صوت: MP3/WAV/OGG — فيديو: إلى MP4\n"
)

def options_for(kind: str, ext: str) -> list[list[InlineKeyboardButton]]:
    btns: list[list[InlineKeyboardButton]] = []
    if kind == 'office':
        if BINS.get("soffice"):
            btns.append([InlineKeyboardButton('تحويل إلى PDF', callback_data='c:PDF')])
    elif kind == 'pdf':
        btns.append([InlineKeyboardButton('PDF → DOCX', callback_data='c:DOCX')])
        btns.append([
            InlineKeyboardButton('PDF → صور PNG (ZIP)', callback_data='c:PNGZIP'),
            InlineKeyboardButton('PDF → صور JPG (ZIP)', callback_data='c:JPGZIP'),
        ])
    elif kind == 'image':
        row1 = [InlineKeyboardButton('إلى PDF', callback_data='c:PDF')]
        targets = ['JPG','PNG','WEBP']
        row2 = [InlineKeyboardButton(f'إلى {t}', callback_data=f'c:{t}') for t in targets if t.lower()!=ext]
        btns.append(row1); 
        if row2: btns.append(row2)
    elif kind == 'audio':
        if BINS.get("ffmpeg"):
            row = [InlineKeyboardButton(f'إلى {t}', callback_data=f'c:{t}') for t in ['MP3','WAV','OGG'] if t.lower()!=ext]
            if row: btns.append(row)
    elif kind == 'video':
        if BINS.get("ffmpeg"):
            btns.append([InlineKeyboardButton('إلى MP4', callback_data='c:MP4')])
    return btns

# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 المساعدة", callback_data="menu:help"),
         InlineKeyboardButton("🧾 الصيغ", callback_data="menu:formats")]
    ])
    await update.message.reply_text(
        "👋 أهلاً! أنا بوت تحويل الملفات.\n"
        "أرسل أي ملف كـ *مستند* وسأعرض لك التحويلات المتاحة.\n",
        reply_markup=kb, disable_web_page_preview=True
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ مساعدة:\n"+FORMATS_TEXT, disable_web_page_preview=True)

async def formats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧾 الصيغ:\n"+FORMATS_TEXT, disable_web_page_preview=True)

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    data = q.data or ""
    if data == "menu:help":
        await q.edit_message_text("ℹ️ مساعدة:\n"+FORMATS_TEXT)
    elif data == "menu:formats":
        await q.edit_message_text("🧾 الصيغ:\n"+FORMATS_TEXT)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    uid = msg.from_user.id if msg.from_user else 0
    if not allow(uid):
        await msg.reply_text("⏳ محاولات كثيرة جدًا. جرّب بعد دقيقة.")
        return

    if msg.document:
        file_id = msg.document.file_id; file_name = msg.document.file_name or 'file'
    elif msg.photo:
        file_id = msg.photo[-1].file_id; file_name = 'photo.jpg'
    elif msg.audio:
        file_id = msg.audio.file_id; file_name = msg.audio.file_name or 'audio'
    elif msg.video:
        file_id = msg.video.file_id; file_name = msg.video.file_name or 'video'
    else:
        await msg.reply_text('📎 أرسل الملف كـ *مستند* من فضلك.'); return

    ext = ext_of(file_name); kind = kind_for_extension(ext)
    if kind == 'unknown':
        await msg.reply_text('صيغة غير معروفة.'); return

    # خزّن الميتا مؤقتًا في Redis
    token = uuid.uuid4().hex[:10]
    meta = {"file_id": file_id, "file_name": file_name, "ext": ext, "kind": kind}
    redis.setex(f"pending:{token}", 600, json.dumps(meta))

    kb = options_for(kind, ext)
    if not kb:
        await msg.reply_text('لا تحويلات متاحة لهذه الصيغة/البيئة حالياً.'); return

    # ضمّن التوكن في الـ callback_data
    kb = [[InlineKeyboardButton('إلغاء', callback_data=f'x:{token}')]] + \
         [[InlineKeyboardButton(btn.text, callback_data=f'c:{btn.callback_data.split(":")[1]}:{token}') for btn in row] for row in kb]

    await msg.reply_text(
        f"📎 الملف: `{safe_name(file_name)}`\nاختر التحويل المطلوب:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode='Markdown'
    )

async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    data = q.data or ''
    if data.startswith('x:'):
        try: await q.edit_message_text('أُلغيَ الطلب ✅')
        except: pass
        return
    if not data.startswith('c:'):
        return
    # c:CHOICE:TOKEN
    try:
        _, choice, token = data.split(':', 2)
    except:
        return await q.edit_message_text("طلب غير صالح.")

    raw = redis.get(f"pending:{token}")
    if not raw:
        return await q.edit_message_text("⏳ انتهت صلاحية الطلب. أعد إرسال الملف.")
    meta = json.loads(raw)

    # ضع المهمة في طابور RQ
    spec = {
        "chat_id": q.message.chat_id,
        "reply_to_message_id": q.message.message_id,
        "file_id": meta["file_id"],
        "file_name": meta["file_name"],
        "ext": meta["ext"],
        "kind": meta["kind"],
        "choice": choice,
        "limit_mb": TG_LIMIT_MB,
    }
    job = queue.enqueue("tasks.process_job", spec, job_timeout=1800)  # 30 دقيقة سقف

    await q.edit_message_text(f"🧺 تم إدراج مهمتك في قائمة الانتظار (Job: {job.id[:8]}). سنرسل النتيجة هنا عند الانتهاء ✅")

# ====== Aiohttp health/diag ======
async def make_web_app() -> web.Application:
    app = web.Application()
    async def ping(_): return web.json_response({"ok": True})
    async def diag(_): return web.json_response({"bins": detect_bins(), "limit_mb": TG_LIMIT_MB})
    app.router.add_get('/health', ping)
    app.router.add_get('/', ping)
    app.router.add_get('/diag', diag)
    return app

async def on_startup_ptb(app: Application) -> None:
    # commands list
    try:
        await app.bot.set_my_commands([
            BotCommand("start","بدء الاستخدام"),
            BotCommand("help","المساعدة"),
            BotCommand("formats","الصيغ المدعومة"),
        ])
    except Exception: pass
    # http server
    webapp = await make_web_app()
    runner = web.AppRunner(webapp); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT); await site.start()
    app.bot_data['web_runner'] = runner

    if USE_WEBHOOK:
        if not PUBLIC_URL:
            raise RuntimeError("PUBLIC_URL مطلوب عند USE_WEBHOOK=1")
        await app.bot.set_webhook(url=f"{PUBLIC_URL}/{BOT_TOKEN}", drop_pending_updates=True)
        log.info(f"[webhook] set to {PUBLIC_URL}/{BOT_TOKEN}")
    else:
        try: await app.bot.delete_webhook(drop_pending_updates=True)
        except Exception: pass

async def on_shutdown_ptb(app: Application) -> None:
    runner = app.bot_data.get('web_runner')
    if runner: await runner.cleanup()

def build_app() -> Application:
    application = (Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(on_startup_ptb)
        .post_shutdown(on_shutdown_ptb)
        .build())
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('formats', formats_cmd))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO, handle_file))
    application.add_handler(CallbackQueryHandler(on_choice, pattern=r'^(c:|x:)'))
    application.add_handler(CallbackQueryHandler(on_menu, pattern=r'^menu:'))
    return application

def main():
    app = build_app()
    if USE_WEBHOOK:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN)
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
