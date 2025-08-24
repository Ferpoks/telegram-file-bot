# bot.py
# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx
from pdf2image import convert_from_path
from pdf2docx import parse as pdf2docx_parse
from PIL import Image
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
    BotCommand,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= إعدادات عامة =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("convbot")

# بيئة
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").lstrip("@")
SUB_CHANNEL = os.getenv("SUB_CHANNEL", "").strip()  # @user أو user أو t.me/...
PUBLIC_URL = (os.getenv("PUBLIC_URL", "") or "").strip().rstrip("/")
PORT = int(os.getenv("PORT", os.getenv("WEB_CONCURRENCY", "10000")))

# MODE: webhook | polling
MODE = (os.getenv("MODE", "").strip().lower() or ("webhook" if PUBLIC_URL else "polling"))

# حدود حجم تيليجرام
TG_LIMIT_MB = int(os.getenv("TG_LIMIT_MB", "49"))
TG_LIMIT = TG_LIMIT_MB * 1024 * 1024

# التوازي
CONC_IMAGE = int(os.getenv("CONC_IMAGE", "20"))
CONC_PDF   = int(os.getenv("CONC_PDF", "20"))
CONC_MEDIA = int(os.getenv("CONC_MEDIA", "20"))
CONC_OFFICE= int(os.getenv("CONC_OFFICE", "20"))

# PDF.co اختياري
PDFCO_API_KEY = os.getenv("PDFCO_API_KEY", "").strip()

WORK_ROOT = Path("/tmp/convbot")
WORK_ROOT.mkdir(parents=True, exist_ok=True)

# برامج النظام
BIN = {
    "soffice": shutil.which("soffice"),
    "pdftoppm": shutil.which("pdftoppm"),
    "ffmpeg": shutil.which("ffmpeg"),
    "gs": shutil.which("gs"),
}
log.info("[bin] soffice=%s, pdftoppm=%s, ffmpeg=%s, gs=%s (limit=%dMB)",
         BIN["soffice"], BIN["pdftoppm"], BIN["ffmpeg"], BIN["gs"], TG_LIMIT_MB)

SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.\- ]+")

# لغات
USER_LANG: Dict[int, str] = {}

# Semaphores
SEM_IMAGE = asyncio.Semaphore(CONC_IMAGE)
SEM_PDF   = asyncio.Semaphore(CONC_PDF)
SEM_MEDIA = asyncio.Semaphore(CONC_MEDIA)
SEM_OFFICE= asyncio.Semaphore(CONC_OFFICE)

# اشتراك القناة
CHANNEL_CHAT_ID: Optional[int] = None
CHANNEL_USERNAME_LINK: Optional[str] = None  # t.me/<user>

# ترجمات
T = {
    "ar": {
        "start_title": "👋 أهلاً بك!",
        "start_desc": ("أنا بوت تحويل ملفات. اختر لغتك ثم أرسل أي ملف وسأعرض لك صيغ التحويل المتاحة.\n\n"
                       "المدعوم: صور PNG/JPG/WEBP ⇄ PDF، PDF ⇢ صور/ DOCX، صوت ⇄ MP3/WAV/OGG، فيديو ⇢ MP4.\n"
                       "تحويل أوفيس → PDF متاح إذا كان LibreOffice موجوداً أو لديك مفتاح PDF.co."),
        "choose_lang": "اختر اللغة:",
        "btn_ar": "العربية 🇸🇦",
        "btn_en": "English 🇬🇧",
        "help": "للمساعدة والتواصل مع الإدارة: @{admin}\nقناة التحديثات: {chan}\n\nأرسل ملفك فقط.",
        "must_join": "🚫 لا يمكنك استخدام البوت قبل الاشتراك في القناة:",
        "join_btn": "الانضمام للقناة",
        "joined_ok": "✅ تم التحقق من الاشتراك. أرسل ملفك الآن.",
        "file_too_big": "❌ الملف أكبر من الحد المسموح ({mb}MB).",
        "choose_action": "ماذا تريد أن أفعل بهذا الملف؟",
        "working": "⏳ يتم التحويل، انتظر من فضلك…",
        "failed": "❌ حدث خطأ أثناء التحويل: {err}",
        "sent": "✅ تم الإرسال.",
        "admin_only": "هذا الأمر للمدير فقط.",
        "formats_title": "صيَغ التحويل المتاحة حالياً:",
        "stats": "📊 إحصائيات سريعة:\nمستخدمون فريدون تقريباً: {u}\nمحاولات تحويل: {c}",
        "lang_saved": "✅ تم ضبط اللغة على العربية.",
        "lang_prompt": "↪️ اختر لغتك من الأزرار.",
    },
    "en": {
        "start_title": "👋 Welcome!",
        "start_desc": ("I'm a file converter bot. Pick your language, then send a file and I'll show you the available conversions.\n\n"
                       "Supported: Images PNG/JPG/WEBP ⇄ PDF, PDF ⇢ images/DOCX, audio ⇄ MP3/WAV/OGG, video ⇢ MP4.\n"
                       "Office → PDF is available if LibreOffice exists or you set a PDF.co API key."),
        "choose_lang": "Choose a language:",
        "btn_ar": "العربية 🇸🇦",
        "btn_en": "English 🇬🇧",
        "help": "For help/contact admin: @{admin}\nUpdates channel: {chan}\n\nJust send your file.",
        "must_join": "🚫 You must join the channel before using the bot:",
        "join_btn": "Join channel",
        "joined_ok": "✅ Subscription verified. Send your file.",
        "file_too_big": "❌ File exceeds allowed limit ({mb}MB).",
        "choose_action": "What do you want to do with this file?",
        "working": "⏳ Converting, please wait…",
        "failed": "❌ Conversion error: {err}",
        "sent": "✅ Sent.",
        "admin_only": "This command is admin-only.",
        "formats_title": "Currently supported conversions:",
        "stats": "📊 Quick stats:\nApprox unique users: {u}\nConversions: {c}",
        "lang_saved": "✅ Language set to English.",
        "lang_prompt": "↪️ Pick your language via buttons.",
    },
}

def lang_of(update: Update) -> str:
    uid = update.effective_user.id if update.effective_user else 0
    return USER_LANG.get(uid, "ar")

def tr(update: Update, key: str, **kw) -> str:
    return T.get(lang_of(update), T["ar"]).get(key, key).format(**kw)

def reply_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/start"), KeyboardButton("/help")]],
        resize_keyboard=True
    )

def clean_name(name: str) -> str:
    safe = SAFE_CHARS.sub("_", name)
    return safe[:128] or "file"

async def ensure_joined(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    global CHANNEL_CHAT_ID, CHANNEL_USERNAME_LINK
    if not CHANNEL_CHAT_ID:
        return True
    user = update.effective_user
    if not user:
        return True
    try:
        member = await ctx.bot.get_chat_member(CHANNEL_CHAT_ID, user.id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception as e:
        log.warning("ensure_joined error: %s", e)

    btn = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton(tr(update, "join_btn"), url=f"https://t.me/{CHANNEL_USERNAME_LINK}")
    )
    await update.effective_message.reply_text(tr(update, "must_join"), reply_markup=btn)
    return False

def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing")

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # أوامر
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("lang", cmd_lang))
    application.add_handler(CommandHandler("formats", cmd_formats))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("debugsub", cmd_debugsub))

    # كول باك
    application.add_handler(CallbackQueryHandler(cb_lang, pattern=r"^lang:(ar|en)$"))
    application.add_handler(CallbackQueryHandler(cb_convert, pattern=r"^conv:.+"))

    # استقبال ملفات (تصحيح: filters.Document.ALL)
    file_filter = (filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO)
    application.add_handler(MessageHandler(file_filter, on_file))

    return application

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T["ar"]["btn_ar"], callback_data="lang:ar"),
         InlineKeyboardButton(T["en"]["btn_en"], callback_data="lang:en")]
    ])
    await update.effective_message.reply_text(
        f"<b>{T['ar']['start_title']}</b>\n\n{T['ar']['start_desc']}\n\n"
        f"<b>{T['en']['start_title']}</b>\n\n{T['en']['start_desc']}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )

async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(T["ar"]["btn_ar"], callback_data="lang:ar"),
         InlineKeyboardButton(T["en"]["btn_en"], callback_data="lang:en")]
    ])
    await update.effective_message.reply_text(tr(update, "lang_prompt"), reply_markup=kb)

async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":")[1]
    USER_LANG[q.from_user.id] = choice
    await q.edit_message_text(T[choice]["lang_saved"], reply_markup=None)
    await q.message.reply_text(
        f"<b>{T[choice]['start_title']}</b>\n\n{T[choice]['start_desc']}",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_kb()
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, ctx):
        return
    chan = f"@{CHANNEL_USERNAME_LINK}" if CHANNEL_USERNAME_LINK else "-"
    txt = tr(update, "help", admin=ADMIN_USERNAME or "admin", chan=chan)
    await update.effective_message.reply_text(txt, reply_markup=reply_kb())

async def cmd_formats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text(tr(update, "admin_only"))
        return
    lines = []
    lines.append("• Images: PNG ⇄ JPG ⇄ WEBP, Image → PDF")
    lines.append("• PDF → Images (PNG/JPG) [ZIP], PDF → DOCX")
    if BIN["soffice"] or PDFCO_API_KEY:
        lines.append("• Office (DOC/DOCX/RTF/ODT/PPT/PPTX/XLS/XLSX) → PDF")
    else:
        lines.append("• Office → PDF (غير متاح حالياً: يحتاج LibreOffice أو PDF.co)")
    if BIN["ffmpeg"]:
        lines.append("• Audio ⇄ MP3/WAV/OGG, Video → MP4")
    await update.effective_message.reply_text(f"{tr(update, 'formats_title')}\n\n" + "\n".join(lines))

STATS_U = set()
STATS_C = 0

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text(tr(update, "admin_only"))
        return
    await update.effective_message.reply_text(tr(update, "stats", u=len(STATS_U), c=STATS_C))

async def cmd_debugsub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text(tr(update, "admin_only"))
        return
    await update.effective_message.reply_text(
        f"MODE={MODE}\nPUBLIC_URL={PUBLIC_URL or '-'}\nSUB_CHANNEL=@{CHANNEL_USERNAME_LINK or '-'}\nCHAT_ID={CHANNEL_CHAT_ID}"
    )

def detect_kind(filename: str, mime: Optional[str]) -> str:
    ext = Path(filename).suffix.lower().strip(".")
    if ext in ("png", "jpg", "jpeg", "webp"):
        return "image"
    if ext in ("pdf",):
        return "pdf"
    if ext in ("mp3", "wav", "ogg", "m4a", "flac", "aac"):
        return "audio"
    if ext in ("mp4", "mov", "mkv", "avi", "webm"):
        return "video"
    if ext in ("doc", "docx", "rtf", "odt", "ppt", "pptx", "xls", "xlsx"):
        return "office"
    if mime:
        if mime.startswith("image/"): return "image"
        if mime.startswith("audio/"): return "audio"
        if mime.startswith("video/"): return "video"
        if mime == "application/pdf": return "pdf"
    return "other"

def conv_options(kind: str) -> list:
    opts = []
    if kind == "image":
        opts = [("IMG→PDF", "img2pdf"), ("PNG", "to_png"), ("JPG", "to_jpg"), ("WEBP", "to_webp")]
    elif kind == "pdf":
        opts = [("PDF→JPG (ZIP)", "pdf2jpg"), ("PDF→PNG (ZIP)", "pdf2png"), ("PDF→DOCX", "pdf2docx")]
    elif kind == "audio":
        opts = [("MP3", "to_mp3"), ("WAV", "to_wav"), ("OGG", "to_ogg")]
    elif kind == "video":
        opts = [("MP4 (H264/AAC)", "to_mp4")]
    elif kind == "office":
        if BIN["soffice"] or PDFCO_API_KEY:
            opts = [("Office→PDF", "office2pdf")]
    return opts

@dataclass
class Job:
    user_id: int
    kind: str
    file_path: Path
    file_name: str

JOBS: Dict[str, Job] = {}

async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update, ctx):
        return
    if update.effective_user:
        STATS_U.add(update.effective_user.id)

    msg = update.effective_message

    if msg.document:
        tgfile = msg.document
        size = tgfile.file_size or 0
        fname = tgfile.file_name or "file"
        mime = tgfile.mime_type
        file_id = tgfile.file_id
    elif msg.photo:
        tgfile = msg.photo[-1]
        size = tgfile.file_size or 0
        fname = "photo.jpg"
        mime = "image/jpeg"
        file_id = tgfile.file_id
    elif msg.video:
        tgfile = msg.video
        size = tgfile.file_size or 0
        fname = tgfile.file_name or "video.mp4"
        mime = tgfile.mime_type
        file_id = tgfile.file_id
    elif msg.audio:
        tgfile = msg.audio
        size = tgfile.file_size or 0
        fname = tgfile.file_name or "audio"
        mime = tgfile.mime_type
        file_id = tgfile.file_id
    else:
        return

    if size > TG_LIMIT:
        await msg.reply_text(tr(update, "file_too_big", mb=TG_LIMIT_MB))
        return

    kind = detect_kind(fname, mime)
    options = conv_options(kind)
    if not options:
        await msg.reply_text("لا توجد تحويلات مناسبة لهذا النوع حالياً.")
        return

    tmpd = Path(tempfile.mkdtemp(prefix=f"u{update.effective_user.id}_", dir=WORK_ROOT))
    in_path = tmpd / (SAFE_CHARS.sub("_", fname)[:128] or "file")
    fobj = await ctx.bot.get_file(file_id)
    await fobj.download_to_drive(in_path.as_posix())

    token = os.urandom(6).hex()
    JOBS[token] = Job(update.effective_user.id, kind, in_path, in_path.name)

    kb, row = [], []
    for text, code in options:
        row.append(InlineKeyboardButton(text, callback_data=f"conv:{token}:{code}"))
        if len(row) == 3:
            kb.append(row); row = []
    if row: kb.append(row)

    await msg.reply_text(tr(update, "choose_action"), reply_markup=InlineKeyboardMarkup(kb))

async def run_cmd(cmd: list, cwd=None, timeout=600) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
    )
    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, out_b.decode("utf-8", "ignore"), err_b.decode("utf-8", "ignore")

async def image_to_pdf(in_path: Path, out_path: Path):
    with Image.open(in_path) as im:
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im.save(out_path, "PDF")

async def image_convert(in_path: Path, out_path: Path):
    with Image.open(in_path) as im:
        if out_path.suffix.lower() in (".jpg", ".jpeg") and im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im.save(out_path)

async def pdf_to_images_zip(in_path: Path, fmt: str, out_zip: Path):
    images = convert_from_path(in_path.as_posix(), fmt=fmt)
    d = out_zip.parent / (out_zip.stem + "_pages")
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for i, im in enumerate(images, 1):
        p = d / f"page_{i:03d}.{fmt}"
        im.save(p)
        files.append(p)
    import zipfile
    with zipfile.ZipFile(out_zip, "w") as z:
        for p in files:
            z.write(p, arcname=p.name)

async def pdf_to_docx(in_path: Path, out_path: Path):
    pdf2docx_parse(in_path.as_posix(), out_path.as_posix())

async def office_to_pdf(in_path: Path, out_path: Path):
    if BIN["soffice"]:
        cmd = [BIN["soffice"], "--headless", "--convert-to", "pdf",
               "--outdir", out_path.parent.as_posix(), in_path.as_posix()]
        code, out, err = await run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"LibreOffice failed: {err or out}")
        cand = out_path.parent / (in_path.stem + ".pdf")
        if cand.exists():
            cand.rename(out_path)
        if not out_path.exists():
            raise RuntimeError("output not found")
        return

    if PDFCO_API_KEY:
        ext = in_path.suffix.lower().lstrip(".")
        url = f"https://api.pdf.co/v1/pdf/convert/from/{ext}"
        headers = {"x-api-key": PDFCO_API_KEY}
        files = {"file": (in_path.name, in_path.read_bytes())}
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers=headers, files=files)
            r.raise_for_status()
            jr = r.json()
            if not jr.get("success"):
                raise RuntimeError(jr.get("message", "pdfco failed"))
            link = jr.get("url")
            if not link:
                raise RuntimeError("pdfco: no url in response")
            pdf = await client.get(link)
            pdf.raise_for_status()
            out_path.write_bytes(pdf.content)
        return

    raise RuntimeError("Office→PDF غير متاح: لا يوجد LibreOffice ولا PDF.co API")

async def cb_convert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global STATS_C
    q = update.callback_query
    await q.answer()
    try:
        _, token, code = q.data.split(":")
    except Exception:
        return

    job = JOBS.pop(token, None)
    if not job:
        await q.edit_message_text("انتهت صلاحية هذه العملية، أعد إرسال الملف.")
        return
    if job.user_id != q.from_user.id:
        await q.edit_message_text("هذه العملية ليست لك.")
        return

    await q.edit_message_text(tr(update, "working"))

    try:
        out_path = await do_convert(job, code)
        await update.effective_chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        with out_path.open("rb") as f:
            await update.effective_chat.send_document(
                document=InputFile(f, filename=out_path.name),
                caption=tr(update, "sent"),
            )
    except Exception as e:
        log.exception("conversion error")
        await update.effective_chat.send_message(tr(update, "failed", err=str(e)[:200]))
    finally:
        try:
            if job.file_path.exists():
                job.file_path.unlink(missing_ok=True)
            if job.file_path.parent.exists():
                shutil.rmtree(job.file_path.parent, ignore_errors=True)
        except Exception:
            pass

    STATS_C += 1

async def do_convert(job: Job, code: str) -> Path:
    ext_map = {
        "to_png": ".png", "to_jpg": ".jpg", "to_webp": ".webp",
        "img2pdf": ".pdf", "pdf2jpg": ".zip", "pdf2png": ".zip",
        "pdf2docx": ".docx", "to_mp3": ".mp3", "to_wav": ".wav",
        "to_ogg": ".ogg", "to_mp4": ".mp4", "office2pdf": ".pdf",
    }
    out_path = job.file_path.parent / (Path(job.file_name).stem + ext_map.get(code, ".out"))

    if job.kind == "image":
        async with SEM_IMAGE:
            if code == "img2pdf":
                await image_to_pdf(job.file_path, out_path)
            elif code in ("to_png", "to_jpg", "to_webp"):
                await image_convert(job.file_path, out_path)
            else:
                raise RuntimeError("Unsupported image conversion")

    elif job.kind == "pdf":
        async with SEM_PDF:
            if code == "pdf2jpg":
                await pdf_to_images_zip(job.file_path, "jpg", out_path)
            elif code == "pdf2png":
                await pdf_to_images_zip(job.file_path, "png", out_path)
            elif code == "pdf2docx":
                await pdf_to_docx(job.file_path, out_path)
            else:
                raise RuntimeError("Unsupported pdf conversion")

    elif job.kind == "audio":
        if not BIN["ffmpeg"]:
            raise RuntimeError("ffmpeg غير متوفر")
        async with SEM_MEDIA:
            if code in ("to_mp3", "to_wav", "to_ogg"):
                acodec = {"to_mp3": "libmp3lame", "to_wav": "pcm_s16le", "to_ogg": "libvorbis"}[code]
                cmd = [BIN["ffmpeg"], "-y", "-i", job.file_path.as_posix(),
                       "-vn", "-acodec", acodec, out_path.as_posix()]
                code_, out, err = await run_cmd(cmd)
                if code_ != 0:
                    raise RuntimeError(err or out)
            else:
                raise RuntimeError("Unsupported audio conversion")

    elif job.kind == "video":
        if not BIN["ffmpeg"]:
            raise RuntimeError("ffmpeg غير متوفر")
        async with SEM_MEDIA:
            if code == "to_mp4":
                cmd = [BIN["ffmpeg"], "-y", "-i", job.file_path.as_posix(),
                       "-c:v", "libx264", "-preset", "veryfast",
                       "-c:a", "aac", "-movflags", "+faststart",
                       out_path.as_posix()]
                code_, out, err = await run_cmd(cmd, timeout=1800)
                if code_ != 0:
                    raise RuntimeError(err or out)
            else:
                raise RuntimeError("Unsupported video conversion")

    elif job.kind == "office":
        async with SEM_OFFICE:
            if code == "office2pdf":
                await office_to_pdf(job.file_path, out_path)
            else:
                raise RuntimeError("Unsupported office conversion")

    else:
        raise RuntimeError("نوع ملف غير مدعوم.")

    if not out_path.exists():
        raise RuntimeError("لم يُنتج ملف ناتج.")
    return out_path.rename(out_path.with_name(SAFE_CHARS.sub("_", out_path.name)[:128] or "out"))

async def resolve_channel(bot) -> None:
    global CHANNEL_CHAT_ID, CHANNEL_USERNAME_LINK
    val = SUB_CHANNEL.strip()
    if not val:
        return
    if val.startswith("http"):
        m = re.search(r"t\.me/([A-Za-z0-9_]+)", val)
        user = m.group(1) if m else val
    else:
        user = val.lstrip("@")
    try:
        chat = await bot.get_chat(f"@{user}")
        CHANNEL_CHAT_ID = chat.id
        CHANNEL_USERNAME_LINK = user
        log.info("[sub] channel resolved: @%s (id=%s)", user, CHANNEL_CHAT_ID)
    except Exception as e:
        log.warning("resolve_channel failed for %s: %s", val, e)
        CHANNEL_CHAT_ID = None
        CHANNEL_USERNAME_LINK = None

async def on_startup(app: Application):
    await resolve_channel(app.bot)
    await app.bot.set_my_commands([
        BotCommand("start", "Start / اختر اللغة"),
        BotCommand("help", "Help / المساعدة"),
        BotCommand("lang", "Language / تغيير اللغة"),
    ])

# -------- health server للـ Web Service في وضع polling --------
def start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return  # لا نكتب في اللوق

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = json.dumps({"ok": True, "mode": MODE}).encode()
            self.wfile.write(body)

    def _serve():
        httpd = HTTPServer(("0.0.0.0", PORT), Handler)
        log.info("[health] serving on 0.0.0.0:%s", PORT)
        httpd.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()

# ---------- تشغيل Polling/Webhook بشكل صريح (متوافق مع Python 3.13) ----------
async def _run_polling(app: Application):
    await app.initialize()
    await on_startup(app)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await app.updater.wait_until_closed()
    await app.stop()
    await app.shutdown()

async def _run_webhook(app: Application, path: str):
    await app.initialize()
    await on_startup(app)
    await app.start()
    await app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=path,
        webhook_url=f"{PUBLIC_URL}/{path}",
    )
    await app.updater.wait_until_closed()
    await app.stop()
    await app.shutdown()

def main() -> None:
    app = build_app()

    if MODE == "webhook" and PUBLIC_URL:
        log.info("PTB version at runtime: 22.x")
        log.info("CONFIG: MODE=webhook PUBLIC_URL=%s PORT=%s", PUBLIC_URL, PORT)
        asyncio.run(_run_webhook(app, "webhook"))
        return

    # polling + health server للبورت الخاص بـ Render
    log.info("PTB version at runtime: 22.x")
    log.info("CONFIG: MODE=polling PUBLIC_URL=%s PORT=%s", PUBLIC_URL or "-", PORT)
    start_health_server()
    asyncio.run(_run_polling(app))

if __name__ == "__main__":
    main()

