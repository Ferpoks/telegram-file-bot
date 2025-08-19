# -*- coding: utf-8 -*-
import asyncio, logging, os, re, shutil, tempfile, uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from aiohttp import web
from dotenv import load_dotenv
from PIL import Image

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ===== إعدادات عامة =====
ENV_PATH = Path('.env')
if ENV_PATH.exists():
    # لا نسمح للـ .env أن يطغى على متغيرات Render
    load_dotenv(ENV_PATH, override=False)

BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN مفقود في المتغيرات البيئية')

PORT = int(os.getenv('PORT', '10000'))
MAX_SEND_MB = int(os.getenv('MAX_SEND_MB', '48'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger('convbot')

PENDING: dict[str, dict] = {}

# ===== امتدادات =====
DOC_EXTS = {"doc", "docx", "odt", "rtf"}
PPT_EXTS = {"ppt", "pptx", "odp"}
XLS_EXTS = {"xls", "xlsx", "ods"}
IMG_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "tiff"}
AUD_EXTS = {"mp3", "wav", "ogg", "m4a"}
VID_EXTS = {"mp4", "mov", "mkv", "avi", "webm"}
ALL_OFFICE = DOC_EXTS | PPT_EXTS | XLS_EXTS
SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.\- ]+")

# حالة الأدوات (تتعبّى عند التشغيل)
BIN = {"soffice": None, "pdftoppm": None, "ffmpeg": None}

def safe_name(name: str, fallback: str = "file") -> str:
    name = (name or "").strip() or fallback
    name = SAFE_CHARS.sub("_", name)
    return name[:200]

def ext_of(filename: str | None) -> str:
    return Path(filename).suffix.lower().lstrip('.') if filename else ""

def size_ok(path: Path) -> bool:
    return path.stat().st_size <= MAX_SEND_MB * 1024 * 1024

async def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors='ignore'), err.decode(errors='ignore')

def which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None

# ===== كشف النوع =====
def kind_for_extension(ext: str) -> str:
    if ext in IMG_EXTS: return 'image'
    if ext in AUD_EXTS: return 'audio'
    if ext in VID_EXTS: return 'video'
    if ext in ALL_OFFICE: return 'office'
    if ext == 'pdf': return 'pdf'
    return 'unknown'

def options_for(kind: str, ext: str) -> list[list[InlineKeyboardButton]]:
    btns: list[list[InlineKeyboardButton]] = []
    if kind == 'office':
        # لا نظهر الخيار لو LibreOffice غير متوفر
        if BIN["soffice"]:
            btns.append([InlineKeyboardButton('تحويل إلى PDF', callback_data='c:PDF')])
    elif kind == 'pdf':
        btns.append([InlineKeyboardButton('PDF → DOCX', callback_data='c:DOCX')])
        btns.append([
            InlineKeyboardButton('PDF → صور PNG (ZIP)', callback_data='c:PNGZIP'),
            InlineKeyboardButton('PDF → صور JPG (ZIP)', callback_data='c:JPGZIP'),
        ])
    elif kind == 'image':
        row1 = [InlineKeyboardButton('إلى PDF', callback_data='c:PDF')]
        targets = ['JPG', 'PNG', 'WEBP']
        row2 = [InlineKeyboardButton(f'إلى {t}', callback_data=f'c:{t}') for t in targets if t.lower() != ext]
        btns.append(row1); 
        if row2: btns.append(row2)
    elif kind == 'audio':
        if BIN["ffmpeg"]:
            row = [InlineKeyboardButton(f'إلى {t}', callback_data=f'c:{t}') for t in ['MP3','WAV','OGG'] if t.lower()!=ext]
            if row: btns.append(row)
    elif kind == 'video':
        if BIN["ffmpeg"]:
            btns.append([InlineKeyboardButton('إلى MP4', callback_data='c:MP4')])
    return btns

# ===== وظائف التحويل =====
async def office_to_pdf(in_path: Path, out_dir: Path) -> Path:
    if not BIN["soffice"]:
        raise RuntimeError('لا يمكن تحويل Office→PDF لأن LibreOffice غير مثبت على الخادم.')
    cmd = [BIN["soffice"], '--headless', '--nologo', '--nofirststartwizard', '--convert-to', 'pdf',
           '--outdir', str(out_dir), str(in_path)]
    code, out, err = await run_cmd(cmd)
    if code != 0: raise RuntimeError(f"LibreOffice فشل: {err or out}")
    out_path = out_dir / (in_path.stem + '.pdf')
    if not out_path.exists():
        c = list(out_dir.glob(in_path.stem + '*.pdf'))
        if c: out_path = c[0]
    return out_path

async def pdf_to_docx(in_path: Path, out_dir: Path) -> Path:
    from pdf2docx import Converter
    out_path = out_dir / (in_path.stem + '.docx')
    def _convert():
        cv = Converter(str(in_path))
        try: cv.convert(str(out_path), start=0, end=None)
        finally: cv.close()
    await asyncio.to_thread(_convert); return out_path

async def image_to_pdf(in_path: Path, out_dir: Path) -> Path:
    out_path = out_dir / (in_path.stem + '.pdf')
    def _do():
        im = Image.open(in_path)
        if im.mode in ("RGBA","P"): im = im.convert("RGB")
        im.save(out_path, "PDF", resolution=150.0)
    await asyncio.to_thread(_do); return out_path

async def image_to_image(in_path: Path, out_dir: Path, target_ext: str) -> Path:
    out_path = out_dir / (in_path.stem + f'.{target_ext}')
    def _do():
        im = Image.open(in_path); fmt = target_ext.upper()
        if fmt in ("JPG","JPEG"):
            if im.mode in ("RGBA","P"): im = im.convert("RGB")
            im.save(out_path, "JPEG", quality=90, optimize=True)
        elif fmt=="PNG": im.save(out_path, "PNG", optimize=True)
        elif fmt=="WEBP":
            if im.mode in ("RGBA","P"): im = im.convert("RGB")
            im.save(out_path, "WEBP", quality=90, method=4)
        else: im.save(out_path)
    await asyncio.to_thread(_do); return out_path

async def pdf_to_images_zip(in_path: Path, out_dir: Path, fmt: str='png') -> Path:
    if not BIN["pdftoppm"]:
        raise RuntimeError('لا يمكن PDF→صور لأن Poppler (pdftoppm) غير مثبت.')
    from pdf2image import convert_from_path
    pages = await asyncio.to_thread(convert_from_path, str(in_path), dpi=150)
    outs = []
    for i, im in enumerate(pages, 1):
        out_img = out_dir / f"{in_path.stem}_{i:03d}.{fmt}"
        if fmt.lower()=='jpg':
            im = im.convert('RGB'); im.save(out_img, 'JPEG', quality=90, optimize=True)
        else:
            im.save(out_img, fmt.upper())
        outs.append(out_img)
    zip_path = out_dir / f"{in_path.stem}_images_{fmt}.zip"
    with ZipFile(zip_path, 'w', ZIP_DEFLATED) as zf:
        for p in outs: zf.write(p, arcname=p.name)
    return zip_path

async def audio_convert_ffmpeg(in_path: Path, out_dir: Path, target_ext: str) -> Path:
    if not BIN["ffmpeg"]:
        raise RuntimeError('لا يمكن تحويل الصوت/الفيديو لأن FFmpeg غير مثبت.')
    target_ext = target_ext.lower()
    out_path = out_dir / (in_path.stem + f'.{target_ext}')
    if target_ext=='mp3': args = ['-vn','-c:a','libmp3lame','-q:a','2']
    elif target_ext=='wav': args = ['-vn','-c:a','pcm_s16le']
    elif target_ext=='ogg': args = ['-vn','-c:a','libvorbis','-q:a','5']
    else: raise RuntimeError('صيغة صوت غير مدعومة')
    code, out, err = await run_cmd([BIN["ffmpeg"],'-y','-i',str(in_path),*args,str(out_path)])
    if code != 0: raise RuntimeError(f"FFmpeg فشل: {err or out}")
    return out_path

async def video_to_mp4_ffmpeg(in_path: Path, out_dir: Path) -> Path:
    if not BIN["ffmpeg"]:
        raise RuntimeError('لا يمكن تحويل الفيديو لأن FFmpeg غير مثبت.')
    out_path = out_dir / (in_path.stem + '.mp4')
    cmd = [BIN["ffmpeg"], '-y','-i',str(in_path), '-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-b:a','128k', str(out_path)]
    code, out, err = await run_cmd(cmd)
    if code != 0: raise RuntimeError(f"FFmpeg فشل: {err or out}")
    return out_path

# ===== Handlers =====
HELP_TEXT = ("أرسل أي ملف (كـ *مستند* وليس صورة مضغوطة)\n"
"المدعوم:\n"
"• DOC/DOCX/RTF/ODT/PPT/PPTX/XLS/XLSX → PDF\n"
"• PDF → DOCX / PNG(ZIP) / JPG(ZIP)\n"
"• صور JPG/PNG/WEBP ↔ JPG/PNG/WEBP / صورة → PDF\n"
"• صوت MP3/WAV/OGG / فيديو → MP4\n")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 أهلاً! أنا بوت تحويل الملفات.\n\n"+HELP_TEXT, disable_web_page_preview=True)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, disable_web_page_preview=True)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg: return
    if msg.document:
        file_id = msg.document.file_id; file_name = msg.document.file_name or 'file'
    elif msg.photo:
        file_id = msg.photo[-1].file_id; file_name = 'photo.jpg'
    elif msg.audio:
        file_id = msg.audio.file_id; file_name = msg.audio.file_name or 'audio'
    elif msg.video:
        file_id = msg.video.file_id; file_name = msg.video.file_name or 'video'
    else:
        await msg.reply_text('أرسل الملف كـ *مستند* من فضلك.'); return

    ext = ext_of(file_name); kind = kind_for_extension(ext)
    if kind == 'unknown':
        await msg.reply_text('صيغة غير معروفة.'); return

    token = uuid.uuid4().hex[:10]
    PENDING[token] = {'file_id': file_id, 'file_name': file_name, 'ext': ext, 'kind': kind}

    kb = options_for(kind, ext)
    if not kb:
        await msg.reply_text('لا تحويلات متاحة لهذه الصيغة/البيئة حالياً.'); return

    await msg.reply_text(
        f"📎 الملف: `{safe_name(file_name)}`\nاختر التحويل المطلوب:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('إلغاء', callback_data='c:CANCEL')]]+kb),
        parse_mode='Markdown'
    )

async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    await query.answer()
    data = (query.data or '')
    if not data.startswith('c:'): return
    choice = data.split(':',1)[1]
    if choice == 'CANCEL':
        try: await query.edit_message_text('أُلغيَ الطلب ✅')
        except: pass
        return
    if not PENDING:
        await query.edit_message_text('انتهت صلاحية الطلب.'); return
    token, meta = next(reversed(list(PENDING.items())))
    file_id, file_name, ext, kind = meta['file_id'], meta['file_name'], meta['ext'], meta['kind']

    await query.edit_message_text('⏳ جارٍ التحويل...')
    workdir = Path(tempfile.mkdtemp(prefix='convbot_'))
    try:
        try: await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        except: pass

        in_path = workdir / safe_name(file_name or 'file')
        tgfile = await context.bot.get_file(file_id)
        await tgfile.download_to_drive(str(in_path))

        out_path: Path | None = None
        if kind == 'office' and choice == 'PDF':
            out_path = await office_to_pdf(in_path, workdir)
        elif kind == 'pdf' and choice == 'DOCX':
            out_path = await pdf_to_docx(in_path, workdir)
        elif kind == 'pdf' and choice == 'PNGZIP':
            out_path = await pdf_to_images_zip(in_path, workdir, fmt='png')
        elif kind == 'pdf' and choice == 'JPGZIP':
            out_path = await pdf_to_images_zip(in_path, workdir, fmt='jpg')
        elif kind == 'image' and choice == 'PDF':
            out_path = await image_to_pdf(in_path, workdir)
        elif kind == 'image' and choice in {'JPG','PNG','WEBP'}:
            out_path = await image_to_image(in_path, workdir, target_ext=choice.lower())
        elif kind == 'audio' and choice in {'MP3','WAV','OGG'}:
            out_path = await audio_convert_ffmpeg(in_path, workdir, target_ext=choice.lower())
        elif kind == 'video' and choice == 'MP4':
            out_path = await video_to_mp4_ffmpeg(in_path, workdir)
        else:
            raise RuntimeError('هذا التحويل غير مدعوم.')

        if not out_path or not out_path.exists():
            raise RuntimeError('فشل إنشاء الملف الناتج')
        if not size_ok(out_path):
            raise RuntimeError('حجم الملف الناتج أكبر من الحد المسموح.')

        # أرسل الملف الصحيح (افتحه كـ rb)
        with open(out_path, 'rb') as fh:
            await query.message.reply_document(document=InputFile(fh, filename=out_path.name), caption='✔️ تم التحويل')
        await query.edit_message_text('تم الإرسال ✅')
    except Exception as e:
        log.exception('conversion error')
        try: await query.edit_message_text(f'❌ فشل التحويل: {e}')
        except: pass
    finally:
        try: shutil.rmtree(workdir, ignore_errors=True)
        except: pass
        PENDING.pop(token, None)

# ===== خادم صحة + تشخيص =====
async def make_web_app() -> web.Application:
    app = web.Application()
    async def health(_): return web.json_response({"ok": True, "service": "converter-bot"})
    async def diag(_): return web.json_response({"soffice": BIN["soffice"], "pdftoppm": BIN["pdftoppm"], "ffmpeg": BIN["ffmpeg"]})
    app.router.add_get('/health', health); app.router.add_get('/', health); app.router.add_get('/diag', diag)
    return app

async def on_startup_ptb(app: Application) -> None:
    # اكتشاف الأدوات
    BIN["soffice"]  = which('soffice','libreoffice','lowriter')
    BIN["pdftoppm"] = which('pdftoppm')
    BIN["ffmpeg"]   = which('ffmpeg')
    log.info(f"[bin] soffice={BIN['soffice']}, pdftoppm={BIN['pdftoppm']}, ffmpeg={BIN['ffmpeg']}")

    # تشغيل خادم HTTP
    webapp = await make_web_app()
    runner = web.AppRunner(webapp); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT); await site.start()
    app.bot_data['web_runner'] = runner

    try: await app.bot.delete_webhook(drop_pending_updates=True)
    except: pass
    try:
        me = await app.bot.get_me()
        log.info(f"[bot] started as @{me.username} (id={me.id})")
    except: pass
    log.info(f"[http] serving on 0.0.0.0:{PORT}")

async def on_shutdown_ptb(app: Application) -> None:
    runner: web.AppRunner | None = app.bot_data.get('web_runner')
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
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO, handle_file))
    application.add_handler(CallbackQueryHandler(on_choice, pattern=r'^c:'))
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception('Unhandled error: %s', context.error)
    application.add_error_handler(on_error)
    return application

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()

