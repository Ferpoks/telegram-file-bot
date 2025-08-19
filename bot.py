# -*- coding: utf-8 -*-
import asyncio, logging, os, re, shutil, tempfile, uuid, time
from collections import defaultdict, deque
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from aiohttp import web
from dotenv import load_dotenv
from PIL import Image

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ===================== إعدادات عامة =====================
ENV_PATH = Path('.env')
if ENV_PATH.exists():
    # لا نسمح للـ .env أن يطغى على متغيرات Render
    load_dotenv(ENV_PATH, override=False)

BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN مفقود في المتغيرات البيئية')

PORT = int(os.getenv('PORT', '10000'))

# حد رفع تيليجرام للبوت (MB). غيّره من env أو بأمر /setlimit
TG_LIMIT_MB = int(os.getenv('TG_LIMIT_MB', os.getenv('MAX_SEND_MB', '49')))
TG_LIMIT_BYTES = TG_LIMIT_MB * 1024 * 1024

# مدير/أدمن
OWNER_ID = int(os.getenv('OWNER_ID', '0') or 0)
ADMINS = {OWNER_ID} if OWNER_ID else set()

# التوازي وحدّ العمليات
MAX_CONCURRENCY = int(os.getenv('MAX_CONCURRENCY', '2'))
OPS_PER_MINUTE = int(os.getenv('OPS_PER_MINUTE', '10'))

# سجلات عامة
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger('convbot')

# ===================== حالات وتشخيص =====================
PENDING: dict[str, dict] = {}                  # آخر ملف بانتظار الاختيار
BIN = {"soffice": None, "pdftoppm": None, "ffmpeg": None, "gs": None}  # مسارات الأدوات
sem = asyncio.Semaphore(MAX_CONCURRENCY)       # حد التوازي
USER_QPS: dict[int, deque] = defaultdict(deque)  # حد العمليات/دقيقة لكل مستخدم
BANNED: set[int] = set()

STATS = {
    "ok": 0, "fail": 0,
    "bytes_in": 0, "bytes_out": 0,
    "started_at": int(time.time())
}

# ===================== الامتدادات والدوال المساعدة =====================
DOC_EXTS = {"doc", "docx", "odt", "rtf"}
PPT_EXTS = {"ppt", "pptx", "odp"}
XLS_EXTS = {"xls", "xlsx", "ods"}
IMG_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "tiff"}
AUD_EXTS = {"mp3", "wav", "ogg", "m4a"}
VID_EXTS = {"mp4", "mov", "mkv", "avi", "webm"}
ALL_OFFICE = DOC_EXTS | PPT_EXTS | XLS_EXTS
SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.\- ]+")

def safe_name(name: str, fallback: str = "file") -> str:
    name = (name or "").strip() or fallback
    return SAFE_CHARS.sub("_", name)[:200]

def ext_of(filename: str | None) -> str:
    return Path(filename).suffix.lower().lstrip('.') if filename else ""

def size_ok(path: Path) -> bool:
    return path.stat().st_size <= TG_LIMIT_BYTES

def fmt_bytes(n:int)->str:
    x = float(n)
    for u in ['B','KB','MB','GB','TB']:
        if x < 1024:
            return f"{x:.1f}{u}"
        x /= 1024
    return f"{x:.1f}PB"

async def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors='ignore'), err.decode(errors='ignore')

def which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p: return p
    return None

# ===================== صلاحيات/حدود =====================
def is_admin(uid: int) -> bool:
    return uid in ADMINS

def is_banned(uid: int) -> bool:
    return uid in BANNED

def allow(uid: int) -> bool:
    dq = USER_QPS[uid]
    now = time.time()
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= OPS_PER_MINUTE:
        return False
    dq.append(now)
    return True

# ===================== كشف النوع وبناء الأزرار =====================
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
        # لا نظهر التحويل إن لم تتوفر LibreOffice
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
        targets = ['JPG','PNG','WEBP']
        row2 = [InlineKeyboardButton(f'إلى {t}', callback_data=f'c:{t}') for t in targets if t.lower()!=ext]
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

# ===================== وظائف التحويل الأساسية =====================
async def office_to_pdf(in_path: Path, out_dir: Path) -> Path:
    if not BIN["soffice"]:
        raise RuntimeError('لا يمكن Office→PDF لأن LibreOffice غير مثبت.')
    cmd = [BIN["soffice"], '--headless','--nologo','--nofirststartwizard','--convert-to','pdf','--outdir', str(out_dir), str(in_path)]
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

async def image_to_pdf(in_path: Path, out_dir: Path, dpi: int = 150) -> Path:
    out_path = out_dir / (in_path.stem + '.pdf')
    def _do():
        im = Image.open(in_path)
        if im.mode in ("RGBA","P"): im = im.convert("RGB")
        im.save(out_path, "PDF", resolution=float(dpi))
    await asyncio.to_thread(_do); return out_path

async def image_to_image(in_path: Path, out_dir: Path, target_ext: str, max_side: int | None = None, quality: int = 90) -> Path:
    out_path = out_dir / (in_path.stem + f'.{target_ext}')
    def _do():
        im = Image.open(in_path)
        if max_side:
            w, h = im.size
            scale = max(w, h) / max_side
            if scale > 1:
                im = im.resize((int(w/scale), int(h/scale)))
        fmt = target_ext.upper()
        if fmt in ("JPG","JPEG"):
            if im.mode in ("RGBA","P"): im = im.convert("RGB")
            im.save(out_path, "JPEG", quality=quality, optimize=True)
        elif fmt=="PNG":
            im.save(out_path, "PNG", optimize=True)
        elif fmt=="WEBP":
            if im.mode in ("RGBA","P"): im = im.convert("RGB")
            im.save(out_path, "WEBP", quality=quality, method=4)
        else:
            im.save(out_path)
    await asyncio.to_thread(_do); return out_path

# PDF → صور (ZIP) مع تقسيم إلى أجزاء ≤ الحد
async def pdf_to_images_zip_parts(in_path: Path, out_dir: Path, fmt: str='png') -> list[Path]:
    if not BIN["pdftoppm"]:
        raise RuntimeError('لا يمكن PDF→صور لأن Poppler (pdftoppm) غير مثبت.')
    from pdf2image import convert_from_path
    pages = await asyncio.to_thread(convert_from_path, str(in_path), dpi=150)
    imgs = []
    for i, im in enumerate(pages, 1):
        out_img = out_dir / f"{in_path.stem}_{i:03d}.{fmt}"
        if fmt.lower()=='jpg':
            im = im.convert('RGB'); im.save(out_img, 'JPEG', quality=90, optimize=True)
        else:
            im.save(out_img, fmt.upper())
        imgs.append(out_img)
    parts: list[Path] = []
    part_idx = 1
    current: list[Path] = []
    current_size = 0
    for p in imgs:
        s = p.stat().st_size
        if current and current_size + s > TG_LIMIT_BYTES*0.95:
            z = out_dir / f"{in_path.stem}_images_{fmt}_part{part_idx}.zip"
            with ZipFile(z,'w',ZIP_DEFLATED) as zf:
                for f in current: zf.write(f, arcname=f.name)
            parts.append(z); part_idx += 1; current = [p]; current_size = s
        else:
            current.append(p); current_size += s
    if current:
        z = out_dir / f"{in_path.stem}_images_{fmt}_part{part_idx}.zip"
        with ZipFile(z,'w',ZIP_DEFLATED) as zf:
            for f in current: zf.write(f, arcname=f.name)
        parts.append(z)
    return parts

# ===================== تخفيض الحجم =====================
async def shrink_pdf(in_path: Path, out_dir: Path) -> Path | None:
    if not BIN["gs"]:
        return None
    for preset in ('/ebook','/screen'):
        out = out_dir / (in_path.stem + f'.min.pdf')
        cmd = [BIN["gs"], '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4',
               f'-dPDFSETTINGS={preset}', '-dNOPAUSE', '-dQUIET', '-dBATCH',
               f'-sOutputFile={str(out)}', str(in_path)]
        code, _, _ = await run_cmd(cmd)
        if code==0 and out.exists() and out.stat().st_size < in_path.stat().st_size:
            return out
    return None

async def shrink_video(in_path: Path, out_dir: Path) -> Path | None:
    if not BIN["ffmpeg"]:
        return None
    trials = [
        ['-vf','scale=\'min(1280,iw)\':-2','-c:v','libx264','-preset','veryfast','-crf','28','-c:a','aac','-b:a','96k'],
        ['-vf','scale=\'min(854,iw)\':-2','-c:v','libx264','-preset','veryfast','-crf','30','-c:a','aac','-b:a','96k'],
    ]
    src = in_path
    for i, args in enumerate(trials,1):
        out = out_dir / (in_path.stem + f'.r{i}.mp4')
        code, _, _ = await run_cmd([BIN["ffmpeg"],'-y','-i',str(src), *args, str(out)])
        if code==0 and out.exists():
            src = out
            if size_ok(out): return out
    return src if src!=in_path and size_ok(src) else None

async def shrink_image(in_path: Path, out_dir: Path, ext: str) -> Path | None:
    for max_side, q in [(2000,85),(1400,75)]:
        out = await image_to_image(in_path, out_dir, target_ext=ext, max_side=max_side, quality=q)
        if size_ok(out): return out
    return None

async def shrink_audio(in_path: Path, out_dir: Path, ext: str) -> Path | None:
    if not BIN["ffmpeg"]:
        return None
    out = out_dir / (in_path.stem + ('.mp3' if ext=='wav' else f'.{ext}'))
    if ext=='mp3': args = ['-vn','-c:a','libmp3lame','-q:a','5']
    elif ext=='ogg': args = ['-vn','-c:a','libvorbis','-q:a','3']
    elif ext=='wav': args = ['-vn','-c:a','libmp3lame','-q:a','5']
    else: return None
    code, _, _ = await run_cmd([BIN["ffmpeg"],'-y','-i',str(in_path), *args, str(out)])
    return out if code==0 and out.exists() else None

# ===================== نصوص واجهة =====================
FORMATS_TEXT = (
    "✅ المدعوم حاليًا:\n"
    "• Office → PDF (DOC/DOCX/RTF/ODT/PPT/PPTX/XLS/XLSX)\n"
    "• PDF → DOCX | صور (PNG/JPG داخل ZIP)\n"
    "• صور JPG/PNG/WEBP ↔ بين بعض | صورة → PDF\n"
    "• صوت: MP3/WAV/OGG — فيديو: إلى MP4\n"
)

HELP_TEXT = ("أرسل أي ملف (كـ *مستند* وليس صورة مضغوطة)\n" + FORMATS_TEXT)

# ===================== Handlers (مستخدم) =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 المساعدة", callback_data="menu:help"),
         InlineKeyboardButton("🧾 الصيغ", callback_data="menu:formats")]
    ])
    await update.message.reply_text(
        "👋 أهلاً! أنا بوت تحويل الملفات.\n"
        "أرسل أي ملف كـ *مستند* وسأعرض لك التحويلات المتاحة.\n",
        reply_markup=kb, disable_web_page_preview=True
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("ℹ️ مساعدة:\n" + HELP_TEXT, disable_web_page_preview=True)

async def formats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🧾 الصيغ:\n" + FORMATS_TEXT, disable_web_page_preview=True)

async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q: return
    await q.answer()
    data = q.data or ""
    if data == "menu:help":
        await q.edit_message_text("ℹ️ مساعدة:\n" + HELP_TEXT)
    elif data == "menu:formats":
        await q.edit_message_text("🧾 الصيغ:\n" + FORMATS_TEXT)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg: return
    uid = msg.from_user.id if msg.from_user else 0

    if is_banned(uid):
        await msg.reply_text("🚫 تم حظرك من استخدام البوت.")
        return
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
        async with sem:
            try: await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
            except: pass

            in_path = workdir / safe_name(file_name or 'file')
            tgfile = await context.bot.get_file(file_id)
            await tgfile.download_to_drive(str(in_path))
            try: STATS["bytes_in"] += in_path.stat().st_size
            except: pass

            out_paths: list[Path] = []  # قد نرسل عدة ملفات

            # 1) التحويل
            if kind == 'office' and choice == 'PDF':
                out = await office_to_pdf(in_path, workdir); out_paths = [out]
            elif kind == 'pdf' and choice == 'DOCX':
                out = await pdf_to_docx(in_path, workdir); out_paths = [out]
            elif kind == 'pdf' and choice == 'PNGZIP':
                out_paths = await pdf_to_images_zip_parts(in_path, workdir, fmt='png')
            elif kind == 'pdf' and choice == 'JPGZIP':
                out_paths = await pdf_to_images_zip_parts(in_path, workdir, fmt='jpg')
            elif kind == 'image' and choice == 'PDF':
                out = await image_to_pdf(in_path, workdir); out_paths = [out]
            elif kind == 'image' and choice in {'JPG','PNG','WEBP'}:
                out = await image_to_image(in_path, workdir, target_ext=choice.lower()); out_paths = [out]
            elif kind == 'audio' and choice in {'MP3','WAV','OGG'}:
                out = await audio_convert_ffmpeg(in_path, workdir, target_ext=choice.lower()); out_paths = [out]
            elif kind == 'video' and choice == 'MP4':
                out = await video_to_mp4_ffmpeg(in_path, workdir); out_paths = [out]
            else:
                raise RuntimeError('هذا التحويل غير مدعوم.')

            # 2) تقليل الحجم إن لزم
            fixed: list[Path] = []
            for p in out_paths:
                if size_ok(p):
                    fixed.append(p); continue
                if p.suffix.lower()=='.pdf':
                    shr = await shrink_pdf(p, workdir)
                    if shr and size_ok(shr): fixed.append(shr)
                elif p.suffix.lower()=='.mp4':
                    shr = await shrink_video(p, workdir)
                    if shr and size_ok(shr): fixed.append(shr)
                elif p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
                    shr = await shrink_image(p, workdir, p.suffix.lstrip('.'))
                    if shr and size_ok(shr): fixed.append(shr)
                elif p.suffix.lower() in {'.mp3','.wav','.ogg'}:
                    shr = await shrink_audio(p, workdir, p.suffix.lstrip('.'))
                    if shr and size_ok(shr): fixed.append(shr)

            to_send = [p for p in (fixed or out_paths) if size_ok(p)]
            if not to_send:
                raise RuntimeError(f'الملف الناتج أكبر من حد تيليجرام ({TG_LIMIT_MB}MB). جرّب ملف أصغر أو تحويلًا آخر.')

            # 3) الإرسال
            for idx, p in enumerate(to_send, 1):
                cap = '✔️ تم التحويل' + (f' (جزء {idx}/{len(to_send)})' if len(to_send)>1 else '')
                with open(p, 'rb') as fh:
                    await query.message.reply_document(document=InputFile(fh, filename=p.name), caption=cap)
                try: STATS["bytes_out"] += p.stat().st_size
                except: pass

            STATS["ok"] += 1
            await query.edit_message_text('تم الإرسال ✅')

    except Exception as e:
        STATS["fail"] += 1
        log.exception('conversion error')
        try: await query.edit_message_text(f'❌ فشل التحويل: {e}')
        except: pass
    finally:
        try: shutil.rmtree(workdir, ignore_errors=True)
        except: pass
        PENDING.pop(token, None)

# ===================== Handlers (مدير) =====================
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.message.reply_text("🚫 هذا الأمر للمدير فقط.")
    up = int(time.time()) - STATS["started_at"]
    await update.message.reply_text(
        "🛠️ لوحة المدير\n"
        f"- أدوات: soffice={bool(BIN['soffice'])}, pdftoppm={bool(BIN['pdftoppm'])}, ffmpeg={bool(BIN['ffmpeg'])}, gs={bool(BIN['gs'])}\n"
        f"- الحد الحالي: {TG_LIMIT_MB}MB | التوازي: {MAX_CONCURRENCY} | OPS/min: {OPS_PER_MINUTE}\n"
        f"- تشغيل منذ: {up//3600}h {(up%3600)//60}m\n"
        f"- محظورون: {len(BANNED)}"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok, fail = STATS["ok"], STATS["fail"]
    await update.message.reply_text(
        "📈 الإحصاءات\n"
        f"- نجاح: {ok} | فشل: {fail}\n"
        f"- دخل: {fmt_bytes(STATS['bytes_in'])} | خرج: {fmt_bytes(STATS['bytes_out'])}"
    )

async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.message.reply_text("🚫 هذا الأمر للمدير فقط.")
    if not context.args:
        return await update.message.reply_text("استخدم: /setlimit 49")
    try:
        mb = int(context.args[0])
        if mb < 1: raise ValueError()
        global TG_LIMIT_MB, TG_LIMIT_BYTES
        TG_LIMIT_MB = mb
        TG_LIMIT_BYTES = mb * 1024 * 1024
        await update.message.reply_text(f"✅ تم ضبط الحد إلى {mb}MB")
    except:
        await update.message.reply_text("قيمة غير صالحة.")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text("🚫 للمَدير فقط.")
    if not context.args: return await update.message.reply_text("استخدم: /ban <user_id>")
    try:
        BANNED.add(int(context.args[0]))
        await update.message.reply_text("تم الحظر ✅")
    except: await update.message.reply_text("user_id غير صالح.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text("🚫 للمَدير فقط.")
    if not context.args: return await update.message.reply_text("استخدم: /unban <user_id>")
    try:
        BANNED.discard(int(context.args[0]))
        await update.message.reply_text("تم إلغاء الحظر ✅")
    except: await update.message.reply_text("user_id غير صالح.")

# ===================== خادم صحة + تشخيص =====================
async def make_web_app() -> web.Application:
    app = web.Application()
    async def health(_): return web.json_response({"ok": True, "service": "converter-bot"})
    async def diag(_): return web.json_response({"soffice": BIN["soffice"], "pdftoppm": BIN["pdftoppm"], "ffmpeg": BIN["ffmpeg"], "gs": BIN["gs"], "limit_mb": TG_LIMIT_MB})
    app.router.add_get('/health', health)
    app.router.add_get('/', health)
    app.router.add_get('/diag', diag)
    return app

async def on_startup_ptb(app: Application) -> None:
    # اكتشاف الأدوات
    BIN["soffice"]  = which('soffice','libreoffice','lowriter')
    BIN["pdftoppm"] = which('pdftoppm')
    BIN["ffmpeg"]   = which('ffmpeg')
    BIN["gs"]       = which('gs','ghostscript')
    log.info(f"[bin] soffice={BIN['soffice']}, pdftoppm={BIN['pdftoppm']}, ffmpeg={BIN['ffmpeg']}, gs={BIN['gs']} (limit={TG_LIMIT_MB}MB)")

    # خادم HTTP
    webapp = await make_web_app()
    runner = web.AppRunner(webapp); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT); await site.start()
    app.bot_data['web_runner'] = runner

    # نظافة polling
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
    # أوامر المستخدم
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('formats', formats_cmd))
    # أوامر المدير
    application.add_handler(CommandHandler('admin', admin_cmd))
    application.add_handler(CommandHandler('stats', stats_cmd))
    application.add_handler(CommandHandler('setlimit', setlimit_cmd))
    application.add_handler(CommandHandler('ban', ban_cmd))
    application.add_handler(CommandHandler('unban', unban_cmd))
    # ملفات وقوائم
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO, handle_file))
    application.add_handler(CallbackQueryHandler(on_choice, pattern=r'^c:'))
    application.add_handler(CallbackQueryHandler(on_menu, pattern=r'^menu:'))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception('Unhandled error: %s', context.error)
    application.add_error_handler(on_error)
    return application

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
