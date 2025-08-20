# -*- coding: utf-8 -*-
import asyncio, logging, os, re, shutil, tempfile, uuid, time, json
from collections import defaultdict, deque
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv
from PIL import Image

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, ChatMember,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import telegram  # لعرض نسخة المكتبة في اللوج

# ===================== إعدادات عامة =====================
ENV_PATH = Path('.env')
if ENV_PATH.exists():
    load_dotenv(ENV_PATH, override=False)

BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN مفقود')

PORT = int(os.getenv('PORT', '10000'))
TG_LIMIT_MB = int(os.getenv('TG_LIMIT_MB', os.getenv('MAX_SEND_MB', '49')))
TG_LIMIT_BYTES = TG_LIMIT_MB * 1024 * 1024

OWNER_ID = int(os.getenv('OWNER_ID', '0') or 0)
ADMINS = {OWNER_ID} if OWNER_ID else set()

OPS_PER_MINUTE = int(os.getenv('OPS_PER_MINUTE', '10'))

# قناة الاشتراك الإجباري (username مع @ أو id -100...)
SUB_TARGET = (os.getenv('SUB_CHANNEL', '').strip() or '')
SUB_CHAT_ID: Optional[int] = None
SUB_USERNAME: Optional[str] = None  # مثل ferpoks

# اسم مستخدم المدير لزر التراسل (بدون @)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '').strip()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
log = logging.getLogger('convbot')
log.info(f"PTB version at runtime: {telegram.__version__}")

# ===================== حالات وتشخيص =====================
# الطلبات تُعرّف بتوكن داخل callback لتفادي التعارض
PENDING: dict[str, dict] = {}
BIN = {"soffice": None, "pdftoppm": None, "ffmpeg": None, "gs": None}
USER_QPS: dict[int, deque] = defaultdict(deque)
BANNED: set[int] = set()
STATS = {"ok": 0, "fail": 0, "bytes_in": 0, "bytes_out": 0, "started_at": int(time.time())}
ACTIVE = {"office": 0, "pdf": 0, "media": 0, "image": 0}

# حفظ اللغة المختارة + كل المستخدمين
DATA_DIR = Path("data"); DATA_DIR.mkdir(exist_ok=True)
USERS_JSON = DATA_DIR / "users.json"
try:
    USERS = json.loads(USERS_JSON.read_text(encoding="utf-8"))
except Exception:
    USERS = {}

def save_users():
    try:
        USERS_JSON.write_text(json.dumps(USERS, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ===================== الامتدادات والدوال =====================
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

def ext_of(filename: Optional[str]) -> str:
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

def which(*names: str) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p: return p
    return None

# ===================== الترجمة واللغة =====================
LANGS = {
    "ar": {
        "choose_lang": "اختر اللغة:",
        "start": "👋 أهلاً! أنا بوت تحويل الملفات.\nأرسل أي ملف كـ *مستند* وسأعرض التحويلات المتاحة.",
        "help": "أرسل أي ملف (كمستند).\nالمدعوم:\n• Office → PDF\n• PDF → DOCX | صور (ZIP)\n• صور: JPG/PNG/WEBP ↔️ | صورة → PDF\n• صوت MP3/WAV/OGG — فيديو → MP4",
        "must_join": "🚸 للاستخدام يجب الاشتراك أولاً بالقناة ثم اضغط «تحقّقت».",
        "join_btn": "🔔 اشترك بالقناة",
        "check_btn": "تحقّقت ✅",
        "send_file": "أرسل ملفًا *كمستند* من فضلك.",
        "pick_conv": "📎 الملف: `{}`\nاختر التحويل:",
        "canceled": "أُلغي الطلب ✅",
        "conv_done": "✔️ تم التحويل",
        "too_big": "❌ الناتج أكبر من حد تيليجرام ({mb}MB).",
        "unknown": "صيغة غير معروفة.",
        "rate_limited": "⏳ محاولات كثيرة جدًا. جرّب بعد دقيقة.",
        "contact": "📬 تواصل مع الإدارة",
        "menu_start": "▶️ ابدأ",
        "menu_help": "ℹ️ مساعدة",
        "admin_only": "🚫 هذا الأمر للمدير فقط.",
    },
    "en": {
        "choose_lang": "Choose your language:",
        "start": "👋 Welcome! I'm a file converter bot.\nSend any *document* and I’ll show available conversions.",
        "help": "Send any file (as *document*).\nSupported:\n• Office → PDF\n• PDF → DOCX | Images (ZIP)\n• Images: JPG/PNG/WEBP ↔ | Image → PDF\n• Audio MP3/WAV/OGG — Video → MP4",
        "must_join": "🚸 You must join the channel first, then press “I joined”.",
        "join_btn": "🔔 Join Channel",
        "check_btn": "I joined ✅",
        "send_file": "Please send a *document* file.",
        "pick_conv": "📎 File: `{}`\nChoose a conversion:",
        "canceled": "Request cancelled ✅",
        "conv_done": "✔️ Converted",
        "too_big": "❌ Output is larger than Telegram limit ({mb}MB).",
        "unknown": "Unknown file type.",
        "rate_limited": "⏳ Too many attempts. Try again in a minute.",
        "contact": "📬 Contact admin",
        "menu_start": "▶️ Start",
        "menu_help": "ℹ️ Help",
        "admin_only": "🚫 Admin only.",
    }
}

def user_lang(uid:int) -> str:
    code = USERS.get(str(uid), {}).get("lang")
    return code if code in ("ar","en") else "ar"

def set_user_lang(uid:int, code:str):
    USERS.setdefault(str(uid), {})["lang"] = code
    save_users()

def t(uid:int, key:str) -> str:
    return LANGS[user_lang(uid)].get(key, key)

def menu_keyboard(uid:int):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t(uid,"menu_start")), KeyboardButton(t(uid,"menu_help"))]],
        resize_keyboard=True
    )

# ===================== اشتراك: حلّ القناة + تحقق =====================
def _to_username(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith('@'): return s[1:]
    if s.startswith('https://t.me/'): return s.rsplit('/', 1)[-1]
    return s

async def resolve_subchat_id(bot) -> Optional[int]:
    """يحاول مرة واحدة جلب chat_id من username أو id، ويخزّنه."""
    global SUB_CHAT_ID, SUB_USERNAME
    if not SUB_TARGET:
        return None
    if SUB_CHAT_ID is not None:
        return SUB_CHAT_ID
    # جرّب باسم مستخدم
    uname = _to_username(SUB_TARGET)
    try:
        if uname:
            chat = await bot.get_chat(f"@{uname}")
            SUB_CHAT_ID = chat.id
            SUB_USERNAME = chat.username
            return SUB_CHAT_ID
    except Exception as e:
        log.warning("resolve_subchat_id by username failed: %s", e)
    # جرّب كـ id (مثل -100...)
    try:
        chat = await bot.get_chat(SUB_TARGET)
        SUB_CHAT_ID = chat.id
        SUB_USERNAME = chat.username
        return SUB_CHAT_ID
    except Exception as e:
        log.warning("resolve_subchat_id by id failed: %s", e)
        SUB_CHAT_ID = None
        return None

async def ensure_joined(bot, uid:int) -> bool:
    """True إذا مشترك، False إذا غير ذلك أو فشلنا في الوصول للقناة."""
    if not SUB_TARGET:
        return True
    chat_id = await resolve_subchat_id(bot)
    if chat_id is None:
        log.warning("ensure_joined: cannot resolve channel (bot probably not admin in the channel).")
        return False
    try:
        member: ChatMember = await bot.get_chat_member(chat_id, uid)
        return member.status not in ("left","kicked")
    except Exception as e:
        log.warning("ensure_joined error: %s", e)
        return False

async def gate_or_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يرجع True إذا مسموح، وإلا يرسل رسالة الاشتراك ويرجع False."""
    if not SUB_TARGET:
        return True
    uid = update.effective_user.id if update.effective_user else 0
    ok = await ensure_joined(context.bot, uid)
    if ok:
        return True
    uname = SUB_USERNAME or _to_username(SUB_TARGET)
    join_url = f"https://t.me/{uname}" if uname else None
    lang = user_lang(uid)
    buttons = []
    if join_url:
        buttons.append([InlineKeyboardButton(LANGS[lang]["join_btn"], url=join_url)])
    buttons.append([InlineKeyboardButton(LANGS[lang]["check_btn"], callback_data="chk:join")])
    await (update.effective_message or update.message).reply_text(
        LANGS[lang]["must_join"], reply_markup=InlineKeyboardMarkup(buttons)
    )
    return False

# ========== أمر تشخيص الاشتراك ==========
async def debugsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    lines = [f"SUB_TARGET = {SUB_TARGET!r}"]
    try:
        cid = await resolve_subchat_id(context.bot)
        lines.append(f"resolved chat_id = {cid}")
        lines.append(f"resolved username = {SUB_USERNAME}")
    except Exception as e:
        lines.append(f"resolve error: {e}")

    try:
        cid = SUB_CHAT_ID or SUB_TARGET
        m = await context.bot.get_chat_member(cid, uid)
        lines.append(f"get_chat_member = OK, status={m.status}")
    except Exception as e:
        lines.append(f"get_chat_member = ERROR: {e}")

    await update.message.reply_text("\n".join(lines))

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
    dq.append(now); return True

# ===================== كشف النوع وبناء الأزرار =====================
def kind_for_extension(ext: str) -> str:
    if ext in IMG_EXTS: return 'image'
    if ext in AUD_EXTS: return 'audio'
    if ext in VID_EXTS: return 'video'
    if ext in ALL_OFFICE: return 'office'
    if ext == 'pdf': return 'pdf'
    return 'unknown'

def cb_build(token: str, code: str) -> str:
    return f'c:{token}:{code}'

def options_for(kind: str, ext: str, token: str) -> list[list[InlineKeyboardButton]]:
    btns: list[list[InlineKeyboardButton]] = []
    if kind == 'office':
        if BIN["soffice"]:
            btns.append([InlineKeyboardButton('تحويل إلى PDF', callback_data=cb_build(token,'PDF'))])
    elif kind == 'pdf':
        btns.append([InlineKeyboardButton('PDF → DOCX', callback_data=cb_build(token,'DOCX'))])
        btns.append([
            InlineKeyboardButton('PDF → صور PNG (ZIP)', callback_data=cb_build(token,'PNGZIP')),
            InlineKeyboardButton('PDF → صور JPG (ZIP)',  callback_data=cb_build(token,'JPGZIP')),
        ])
    elif kind == 'image':
        row1 = [InlineKeyboardButton('إلى PDF', callback_data=cb_build(token,'PDF'))]
        targets = ['JPG','PNG','WEBP']
        row2 = [InlineKeyboardButton(f'إلى {t}', callback_data=cb_build(token,t)) for t in targets if t.lower()!=ext]
        btns.append(row1)
        if row2: btns.append(row2)
    elif kind == 'audio':
        if BIN["ffmpeg"]:
            row = [InlineKeyboardButton(f'إلى {t}', callback_data=cb_build(token,t)) for t in ['MP3','WAV','OGG'] if t.lower()!=ext]
            if row: btns.append(row)
    elif kind == 'video':
        if BIN["ffmpeg"]:
            btns.append([InlineKeyboardButton('إلى MP4', callback_data=cb_build(token,'MP4'))])
    return btns

# ===================== “توازي آمن” حسب النوع =====================
CONC_OFFICE = int(os.getenv('CONC_OFFICE', '4'))   # ثقيل
CONC_PDF    = int(os.getenv('CONC_PDF',    '6'))   # متوسط
CONC_MEDIA  = int(os.getenv('CONC_MEDIA',  '4'))   # ثقيل (صوت/فيديو)
CONC_IMAGE  = int(os.getenv('CONC_IMAGE',  '6'))   # خفيف

sem_office = asyncio.Semaphore(CONC_OFFICE)
sem_pdf    = asyncio.Semaphore(CONC_PDF)
sem_media  = asyncio.Semaphore(CONC_MEDIA)
sem_image  = asyncio.Semaphore(CONC_IMAGE)

def select_sem(kind: str) -> tuple[asyncio.Semaphore, str]:
    if kind == 'office': return sem_office, 'office'
    if kind == 'pdf':    return sem_pdf, 'pdf'
    if kind in ('audio','video'): return sem_media, 'media'
    if kind == 'image':  return sem_image, 'image'
    return sem_pdf, 'pdf'

# ===================== وظائف التحويل =====================
async def office_to_pdf(in_path: Path, out_dir: Path) -> Path:
    if not BIN["soffice"]:
        raise RuntimeError('LibreOffice غير متاح.')
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

async def image_to_image(in_path: Path, out_dir: Path, target_ext: str, max_side: Optional[int] = None, quality: int = 92) -> Path:
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
            im.save(out_path, "WEBP", quality=quality, method=6)
        else:
            im.save(out_path)
    await asyncio.to_thread(_do); return out_path

async def pdf_to_images_zip_parts(in_path: Path, out_dir: Path, fmt: str='png') -> list[Path]:
    if not BIN["pdftoppm"]:
        raise RuntimeError('Poppler غير متاح (pdftoppm).')
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

async def audio_convert_ffmpeg(in_path: Path, out_dir: Path, target_ext: str) -> Path:
    if not BIN["ffmpeg"]:
        raise RuntimeError('FFmpeg غير متوفر.')
    target_ext = target_ext.lower()
    out_path = out_dir / (in_path.stem + f'.{target_ext}')
    if target_ext=='mp3':
        args = ['-vn','-c:a','libmp3lame','-q:a','2']
    elif target_ext=='wav':
        args = ['-vn','-c:a','pcm_s16le']
    elif target_ext=='ogg':
        args = ['-vn','-c:a','libvorbis','-q:a','5']
    else:
        raise RuntimeError('صيغة صوت غير مدعومة')
    code, out, err = await run_cmd([BIN["ffmpeg"], '-y','-i',str(in_path), *args, str(out_path)])
    if code != 0: raise RuntimeError(f"FFmpeg فشل: {err or out}")
    return out_path

async def video_to_mp4_ffmpeg(in_path: Path, out_dir: Path) -> Path:
    if not BIN["ffmpeg"]:
        raise RuntimeError('FFmpeg غير متوفر.')
    out_path = out_dir / (in_path.stem + '.mp4')
    cmd = [BIN["ffmpeg"], '-y','-i',str(in_path),
           '-c:v','libx264','-preset','veryfast','-crf','23',
           '-c:a','aac','-b:a','128k', str(out_path)]
    code, out, err = await run_cmd(cmd)
    if code != 0: raise RuntimeError(f"FFmpeg فشل: {err or out}")
    return out_path

# ===================== تخفيض الحجم عند الحاجة =====================
async def shrink_pdf(in_path: Path, out_dir: Path) -> Optional[Path]:
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

async def shrink_video(in_path: Path, out_dir: Path) -> Optional[Path]:
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

async def shrink_image(in_path: Path, out_dir: Path, ext: str) -> Optional[Path]:
    for max_side, q in [(2000,85),(1400,75)]:
        out = await image_to_image(in_path, out_dir, target_ext=ext, max_side=max_side, quality=q)
        if size_ok(out): return out
    return None

async def shrink_audio(in_path: Path, out_dir: Path, ext: str) -> Optional[Path]:
    if not BIN["ffmpeg"]:
        return None
    out = out_dir / (in_path.stem + ('.mp3' if ext=='wav' else f'.{ext}'))
    if ext=='mp3': args = ['-vn','-c:a','libmp3lame','-q:a','5']
    elif ext=='ogg': args = ['-vn','-c:a','libvorbis','-q:a','3']
    elif ext=='wav': args = ['-vn','-c:a','libmp3lame','-q:a','5']
    else: return None
    code, _, _ = await run_cmd([BIN["ffmpeg"],'-y','-i',str(in_path), *args, str(out)])
    return out if code==0 and out.exists() else None

# ===================== Handlers: start/help/lang =====================
async def choose_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("العربية", callback_data="lang:ar"),
         InlineKeyboardButton("English", callback_data="lang:en")]
    ])
    await (update.message or update.callback_query.message).reply_text(
        "اختر اللغة:\nChoose language:", reply_markup=kb
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ✅ دائمًا نعرض اختيار اللغة عند /start
    await choose_lang(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not await gate_or_prompt(update, context):
        return
    kb = None
    if ADMIN_USERNAME:
        url = f"https://t.me/{ADMIN_USERNAME}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(t(uid,"contact"), url=url)]])
    await update.message.reply_text(t(uid,"help"), reply_markup=kb or menu_keyboard(uid), disable_web_page_preview=True)

async def on_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    try:
        _, code = (q.data or "").split(':',1)
    except:
        return
    if code not in ("ar","en"): return
    uid = q.from_user.id
    set_user_lang(uid, code)
    if SUB_TARGET:
        ok = await ensure_joined(context.bot, uid)
        if not ok:
            uname = SUB_USERNAME or _to_username(SUB_TARGET)
            join_url = f"https://t.me/{uname}" if uname else None
            buttons = []
            if join_url:
                buttons.append([InlineKeyboardButton(LANGS[code]["join_btn"], url=join_url)])
            buttons.append([InlineKeyboardButton(LANGS[code]["check_btn"], callback_data="chk:join")])
            return await q.edit_message_text(LANGS[code]["must_join"], reply_markup=InlineKeyboardMarkup(buttons))
    await q.edit_message_text(LANGS[code]["start"])
    try:
        await q.message.reply_text(LANGS[code]["help"], reply_markup=menu_keyboard(uid))
    except:
        pass

async def on_check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    await q.answer()
    uid = q.from_user.id
    if await ensure_joined(context.bot, uid):
        await q.edit_message_text(t(uid,"start"))
        try: await q.message.reply_text(t(uid,"help"), reply_markup=menu_keyboard(uid))
        except: pass
    else:
        await q.answer("لم يتم العثور على اشتراك. تأكد أنك مشترك ثم اضغط مجددًا.", show_alert=True)

# ===================== استقبال الملفات =====================
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg: return
    uid = msg.from_user.id if msg.from_user else 0

    if is_banned(uid):
        await msg.reply_text("🚫 تم حظرك."); return
    if not allow(uid):
        await msg.reply_text(t(uid,"rate_limited")); return
    if not await gate_or_prompt(update, context):
        return

    # خزّن المستخدم لو جديد (لازم للبث لاحقًا)
    USERS.setdefault(str(uid), {}).setdefault("lang", "ar"); save_users()

    if msg.document:
        file_id = msg.document.file_id; file_name = msg.document.file_name or 'file'
    elif msg.photo:
        file_id = msg.photo[-1].file_id; file_name = 'photo.jpg'
    elif msg.audio:
        file_id = msg.audio.file_id; file_name = msg.audio.file_name or 'audio'
    elif msg.video:
        file_id = msg.video.file_id; file_name = msg.video.file_name or 'video'
    else:
        await msg.reply_text(t(uid,"send_file")); return

    ext = ext_of(file_name); kind = kind_for_extension(ext)
    if kind == 'unknown':
        await msg.reply_text(t(uid,"unknown")); return

    token = uuid.uuid4().hex[:10]
    PENDING[token] = {'file_id': file_id, 'file_name': file_name, 'ext': ext, 'kind': kind, 'uid': uid, 'ts': time.time()}

    kb = options_for(kind, ext, token)
    if not kb:
        await msg.reply_text('لا تحويلات متاحة لهذه الصيغة/البيئة حالياً.'); return

    cancel_btn = [[InlineKeyboardButton('إلغاء', callback_data=f'c:{token}:CANCEL')]]
    await msg.reply_text(
        t(uid,"pick_conv").format(safe_name(file_name)),
        reply_markup=InlineKeyboardMarkup(cancel_btn + kb),
        parse_mode='Markdown'
    )

async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    await query.answer()
    data = (query.data or '')
    if data.startswith('chk:'):
        return await on_check_join(update, context)
    if not data.startswith('c:'): return

    # c:{token}:{choice}
    try:
        _, token, choice = data.split(':', 2)
    except ValueError:
        return

    uid = query.from_user.id
    meta = PENDING.get(token)
    if not meta:
        try: await query.edit_message_text('⏳ انتهت صلاحية الطلب.')
        except: pass
        return
    if meta.get('uid') != uid:
        await query.answer("هذا الطلب ليس لك.", show_alert=True)
        return

    if choice == 'CANCEL':
        PENDING.pop(token, None)
        try: await query.edit_message_text(t(uid,"canceled"))
        except: pass
        return
    if not await ensure_joined(context.bot, uid):
        return await gate_or_prompt(update, context)

    file_id, file_name, ext, kind = meta['file_id'], meta['file_name'], meta['ext'], meta['kind']

    await query.edit_message_text('⏳ جارٍ التحويل...')
    workdir = Path(tempfile.mkdtemp(prefix='convbot_'))
    try:
        sem, cat = select_sem(kind)
        async with sem:
            ACTIVE[cat] += 1
            try:
                try: await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                except: pass

                in_path = workdir / safe_name(file_name or 'file')
                tgfile = await context.bot.get_file(file_id)
                await tgfile.download_to_drive(str(in_path))
                try: STATS["bytes_in"] += in_path.stat().st_size
                except: pass

                out_paths: list[Path] = []

                if kind == 'office' and choice == 'PDF':
                    out_paths = [await office_to_pdf(in_path, workdir)]
                elif kind == 'pdf' and choice == 'DOCX':
                    out_paths = [await pdf_to_docx(in_path, workdir)]
                elif kind == 'pdf' and choice == 'PNGZIP':
                    out_paths = await pdf_to_images_zip_parts(in_path, workdir, fmt='png')
                elif kind == 'pdf' and choice == 'JPGZIP':
                    out_paths = await pdf_to_images_zip_parts(in_path, workdir, fmt='jpg')
                elif kind == 'image' and choice == 'PDF':
                    out_paths = [await image_to_pdf(in_path, workdir)]
                elif kind == 'image' and choice in {'JPG','PNG','WEBP'}:
                    out_paths = [await image_to_image(in_path, workdir, target_ext=choice.lower())]
                elif kind == 'audio' and choice in {'MP3','WAV','OGG'}:
                    out_paths = [await audio_convert_ffmpeg(in_path, workdir, target_ext=choice.lower())]
                elif kind == 'video' and choice == 'MP4':
                    out_paths = [await video_to_mp4_ffmpeg(in_path, workdir)]
                else:
                    raise RuntimeError('تحويل غير مدعوم.')

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
                    raise RuntimeError(t(uid,"too_big").format(mb=TG_LIMIT_MB))

                for idx, p in enumerate(to_send, 1):
                    cap = t(uid,"conv_done") + (f' (جزء {idx}/{len(to_send)})' if len(to_send)>1 else '')
                    with open(p, 'rb') as fh:
                        await query.message.reply_document(document=InputFile(fh, filename=p.name), caption=cap)
                    try: STATS["bytes_out"] += p.stat().st_size
                    except: pass

                STATS["ok"] += 1
                await query.edit_message_text('تم الإرسال ✅')
            finally:
                ACTIVE[cat] -= 1

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
        return await update.message.reply_text(t(uid,"admin_only"))
    up = int(time.time()) - STATS["started_at"]
    await update.message.reply_text(
        "🛠️ لوحة المدير\n"
        f"- أدوات: soffice={bool(BIN['soffice'])}, pdftoppm={bool(BIN['pdftoppm'])}, ffmpeg={bool(BIN['ffmpeg'])}, gs={bool(BIN['gs'])}\n"
        f"- الحد: {TG_LIMIT_MB}MB | OPS/min: {OPS_PER_MINUTE}\n"
        f"- تشغيل منذ: {up//3600}h {(up%3600)//60}m\n"
        f"- محظورون: {len(BANNED)}\n"
        f"- توازي: office={CONC_OFFICE}, pdf={CONC_PDF}, media={CONC_MEDIA}, image={CONC_IMAGE}\n"
        f"- نشط: office={ACTIVE['office']}, pdf={ACTIVE['pdf']}, media={ACTIVE['media']}, image={ACTIVE['image']}"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.message.reply_text(t(uid,"admin_only"))
    ok, fail = STATS["ok"], STATS["fail"]
    await update.message.reply_text(
        "📈 الإحصاءات\n"
        f"- نجاح: {ok} | فشل: {fail}\n"
        f"- دخل: {fmt_bytes(STATS['bytes_in'])} | خرج: {fmt_bytes(STATS['bytes_out'])}"
    )

async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    if not context.args: return await update.message.reply_text("استخدم: /setlimit 49")
    try:
        mb = int(context.args[0]); 
        if mb < 1: raise ValueError()
        global TG_LIMIT_MB, TG_LIMIT_BYTES
        TG_LIMIT_MB = mb; TG_LIMIT_BYTES = mb * 1024 * 1024
        await update.message.reply_text(f"✅ تم ضبط الحد إلى {mb}MB")
    except:
        await update.message.reply_text("قيمة غير صالحة.")

async def setops_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    if not context.args: return await update.message.reply_text("استخدم: /setops <محاولات بالدقيقة>")
    try:
        n = int(context.args[0]); 
        if n < 1: raise ValueError()
        global OPS_PER_MINUTE; OPS_PER_MINUTE = n
        await update.message.reply_text(f"✅ OPS_PER_MINUTE = {n}")
    except:
        await update.message.reply_text("قيمة غير صالحة.")

async def setconc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    if len(context.args) != 2:
        return await update.message.reply_text("استخدم: /setconc <office|pdf|media|image> <N>")
    kind, n = context.args[0].lower(), context.args[1]
    try:
        n = int(n); 
        if n < 0: raise ValueError()
    except:
        return await update.message.reply_text("قيمة غير صالحة.")
    global CONC_OFFICE, CONC_PDF, CONC_MEDIA, CONC_IMAGE, sem_office, sem_pdf, sem_media, sem_image
    if kind == 'office':
        CONC_OFFICE = n; sem_office = asyncio.Semaphore(CONC_OFFICE)
    elif kind == 'pdf':
        CONC_PDF = n; sem_pdf = asyncio.Semaphore(CONC_PDF)
    elif kind == 'media':
        CONC_MEDIA = n; sem_media = asyncio.Semaphore(CONC_MEDIA)
    elif kind == 'image':
        CONC_IMAGE = n; sem_image = asyncio.Semaphore(CONC_IMAGE)
    else:
        return await update.message.reply_text("النوع غير معروف.")
    await update.message.reply_text(f"✅ setconc {kind} = {n} (سيُطبّق على المهام الجديدة)")

async def active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    await update.message.reply_text(
        "🔎 النشاط الحالي\n"
        f"- office: {ACTIVE['office']}/{CONC_OFFICE}\n"
        f"- pdf:    {ACTIVE['pdf']}/{CONC_PDF}\n"
        f"- media:  {ACTIVE['media']}/{CONC_MEDIA}\n"
        f"- image:  {ACTIVE['image']}/{CONC_IMAGE}"
    )

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    if not context.args: return await update.message.reply_text("استخدم: /ban <user_id>")
    try:
        BANNED.add(int(context.args[0])); await update.message.reply_text("تم الحظر ✅")
    except: await update.message.reply_text("user_id غير صالح.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    if not context.args: return await update.message.reply_text("استخدم: /unban <user_id>")
    try:
        BANNED.discard(int(context.args[0])); await update.message.reply_text("تم إلغاء الحظر ✅")
    except: await update.message.reply_text("user_id غير صالح.")

async def banlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    if not BANNED:
        return await update.message.reply_text("لا يوجد محظورون.")
    await update.message.reply_text("المحظورون:\n" + "\n".join(map(str, sorted(BANNED))))

async def setsub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    global SUB_TARGET, SUB_CHAT_ID, SUB_USERNAME
    if not context.args:
        return await update.message.reply_text(f"SUB_CHANNEL الحالي: {SUB_TARGET!r} (chat_id={SUB_CHAT_ID})")
    SUB_TARGET = " ".join(context.args).strip()
    SUB_CHAT_ID = None; SUB_USERNAME = None
    await update.message.reply_text(f"✅ SUB_CHANNEL = {SUB_TARGET}. سيُعاد حلّها تلقائيًا.")

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if uid != OWNER_ID:  # فقط المالك يضيف/يحذف مشرفين
        return await update.message.reply_text("هذا الأمر للمالك فقط.")
    if not context.args: return await update.message.reply_text("استخدم: /addadmin <user_id>")
    try:
        ADMINS.add(int(context.args[0])); await update.message.reply_text("تمت إضافة المشرف ✅")
    except: await update.message.reply_text("user_id غير صالح.")

async def deladmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if uid != OWNER_ID:
        return await update.message.reply_text("هذا الأمر للمالك فقط.")
    if not context.args: return await update.message.reply_text("استخدم: /deladmin <user_id>")
    try:
        ADMINS.discard(int(context.args[0])); await update.message.reply_text("تم حذف المشرف ✅")
    except: await update.message.reply_text("user_id غير صالح.")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    text = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("استخدم: /broadcast <نص>")
    ok = 0; fail = 0
    for k in list(USERS.keys()):
        try:
            chat_id = int(k)
            await context.bot.send_message(chat_id, text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await update.message.reply_text(f"تم الإرسال: {ok} | فشل: {fail}")

async def adminhelp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid): return await update.message.reply_text(t(uid,"admin_only"))
    await update.message.reply_text(
        "أوامر المدير:\n"
        "/admin – لوحة معلومات\n"
        "/stats – إحصاءات\n"
        "/active – النشاط الحالي\n"
        "/setconc <office|pdf|media|image> <N> – تعديل التوازي\n"
        "/setops <N> – تعديل محاولات/دقيقة\n"
        "/setlimit <MB> – حد حجم الإرسال\n"
        "/setsub <@channel|-100id> – قناة الاشتراك\n"
        "/addadmin <user_id>, /deladmin <user_id>\n"
        "/ban <user_id>, /unban <user_id>, /banlist\n"
        "/broadcast <نص>\n"
        "/formats – الصيغ المدعومة (عرض فقط)\n"
        "/debugsub – تشخيص الاشتراك"
    )

# ========= /formats للمدير فقط =========
async def formats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return await update.message.reply_text(t(uid,"admin_only"))
    await update.message.reply_text(
        "🧾 الصيغ المدعومة (Admin):\n"
        "• Office → PDF (DOC/DOCX/RTF/ODT/PPT/PPTX/XLS/XLSX)\n"
        "• PDF → DOCX | صور (PNG/JPG داخل ZIP)\n"
        "• صور: JPG/PNG/WEBP ↔ | صورة → PDF\n"
        "• صوت: MP3/WAV/OGG — فيديو: إلى MP4\n"
        f"• حد تيليجرام الحالي: {TG_LIMIT_MB}MB",
        disable_web_page_preview=True
    )

# ===================== خادم صحة + تشخيص =====================
async def make_web_app() -> web.Application:
    app = web.Application()
    async def health(_): return web.json_response({"ok": True, "service": "converter-bot"})
    async def diag(_): return web.json_response({
        "soffice": BIN["soffice"], "pdftoppm": BIN["pdftoppm"], "ffmpeg": BIN["ffmpeg"], "gs": BIN["gs"],
        "limit_mb": TG_LIMIT_MB, "sub_target": SUB_TARGET, "sub_chat_id": SUB_CHAT_ID,
        "conc": {"office": CONC_OFFICE, "pdf": CONC_PDF, "media": CONC_MEDIA, "image": CONC_IMAGE},
        "active": ACTIVE,
        "ptb_version": telegram.__version__,
    })
    app.router.add_get('/health', health)
    app.router.add_get('/', health)
    app.router.add_get('/diag', diag)
    return app

async def on_startup_ptb(app: Application) -> None:
    BIN["soffice"]  = which('soffice','libreoffice','lowriter')
    BIN["pdftoppm"] = which('pdftoppm')
    BIN["ffmpeg"]   = which('ffmpeg')
    BIN["gs"]       = which('gs','ghostscript')
    log.info(f"[bin] soffice={BIN['soffice']}, pdftoppm={BIN['pdftoppm']}, ffmpeg={BIN['ffmpeg']}, gs={BIN['gs']} (limit={TG_LIMIT_MB}MB)")

    webapp = await make_web_app()
    runner = web.AppRunner(webapp); await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT); await site.start()
    app.bot_data['web_runner'] = runner

    # polling
    try: await app.bot.delete_webhook(drop_pending_updates=True)
    except: pass

    try:
        await app.bot.set_my_commands([
            BotCommand("start","Start / ابدأ"),
            BotCommand("help","Help / مساعدة"),
        ])
        await app.bot.set_my_commands([
            BotCommand("start","ابدأ"),
            BotCommand("help","مساعدة"),
        ], language_code="ar")
        await app.bot.set_my_commands([
            BotCommand("start","Start"),
            BotCommand("help","Help"),
        ], language_code="en")
    except Exception:
        pass

    log.info(f"[http] serving on 0.0.0.0:{PORT}")

async def on_shutdown_ptb(app: Application) -> None:
    runner: Optional[web.AppRunner] = app.bot_data.get('web_runner')
    if runner: await runner.cleanup()

# ===================== قائمة سفلية (start/help) بالنص =====================
async def text_shortcuts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    txt = (update.message.text or "").strip()
    if txt in (LANGS['ar']["menu_help"], LANGS['en']["menu_help"]):
        return await help_cmd(update, context)
    if txt in (LANGS['ar']["menu_start"], LANGS['en']["menu_start"]):
        return await start(update, context)

# ===================== البناء والتشغيل =====================
def build_app() -> Application:
    application = (Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(on_startup_ptb)
        .post_shutdown(on_shutdown_ptb)
        .build())
    # المستخدم
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_shortcuts))
    # اللغة والاشتراك
    application.add_handler(CallbackQueryHandler(on_lang, pattern=r'^lang:'))
    application.add_handler(CallbackQueryHandler(on_check_join, pattern=r'^chk:'))
    # الملفات والتحويل
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO, handle_file))
    application.add_handler(CallbackQueryHandler(on_choice, pattern=r'^c:'))
    # المدير
    application.add_handler(CommandHandler('adminhelp', adminhelp_cmd))
    application.add_handler(CommandHandler('admin', admin_cmd))
    application.add_handler(CommandHandler('stats', stats_cmd))
    application.add_handler(CommandHandler('active', active_cmd))
    application.add_handler(CommandHandler('setconc', setconc_cmd))
    application.add_handler(CommandHandler('setops', setops_cmd))
    application.add_handler(CommandHandler('setlimit', setlimit_cmd))
    application.add_handler(CommandHandler('ban', ban_cmd))
    application.add_handler(CommandHandler('unban', unban_cmd))
    application.add_handler(CommandHandler('banlist', banlist_cmd))
    application.add_handler(CommandHandler('setsub', setsub_cmd))
    application.add_handler(CommandHandler('addadmin', addadmin_cmd))
    application.add_handler(CommandHandler('deladmin', deladmin_cmd))
    application.add_handler(CommandHandler('broadcast', broadcast_cmd))
    application.add_handler(CommandHandler('formats', formats_cmd))
    # تشخيص الاشتراك
    application.add_handler(CommandHandler('debugsub', debugsub_cmd))
    # أخطاء عامة
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception('Unhandled error: %s', context.error)
    application.add_error_handler(on_error)
    return application

def main() -> None:
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
