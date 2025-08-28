# bot.py
# ————————————————————————————————————————————————————————————
# بوت مكتبة الكورسات — مع شاشة ترحيب وزر "Start" ورسالة توضيحية
# وزر "المساعدة/Help" للتواصل مع الإدارة + قوائم عربية/إنجليزية
# دعم التحقق من الاشتراك + إرسال PDF/ZIP/RAR + قائمة قابلة للتعديل
# ————————————————————————————————————————————————————————————
import os
import json
import logging
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# ===================== إعدادات عامة =====================
TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TOKEN") or ""
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # مثال: @my_channel
OWNER_USERNAME = (os.getenv("OWNER_USERNAME") or os.getenv("ADMIN_USERNAME") or "").lstrip("@")

CATALOG_PATH = "assets/catalog.json"
BASE_DIR = Path(__file__).parent.resolve()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("courses-bot")

# لغة المستخدم وحالات الواجهة
USER_LANG: dict[int, str] = {}              # user_id -> 'ar' | 'en'
KB_SENT: set[int] = set()                   # من أُرسلت لهم لوحة الأزرار السفلية
MENU_MSG: dict[int, tuple[int, int]] = {}   # user_id -> (chat_id, message_id)

# ===================== النصوص (AR/EN) =====================
L = {
    "ar": {
        "intro": (
            "أهلًا بك! هذا البوت يوفّر مكتبة كورسات وملفات (PDF/ZIP/RAR) بعدّة أقسام.\n"
            "▪️ اشترك في القناة إن لزم\n"
            "▪️ اضغط ▶️ *بدء* للبدء\n"
            "▪️ يمكنك تغيير اللغة في أي وقت\n\nاستمتع 🤍"
        ),
        "welcome": "مرحبًا بك في مكتبة الكورسات 📚\nاختر القسم:",
        "back": "رجوع",
        "contact": "المطور 🧑‍💻",
        "contact_short": "🆘 المساعدة",
        "must_join": "الرجاء الاشتراك في القناة أولًا ثم اضغط ✅ تم الاشتراك",
        "joined": "✅ تم التحقق — يمكنك المتابعة الآن.",
        "verify": "✅ تم الاشتراك",
        "join_channel": "🔔 الذهاب إلى القناة",
        "missing": "⚠️ لم أجد الملف في السيرفر:\n",
        "change_language": "🌍 تغيير اللغة | Change Language",
        "start": "▶️ بدء",
        "myinfo": "🪪 معلوماتي",
        "greet": "👋 الترحيب",
        "help_text_contact": "للتواصل مع الإدارة:",
        "greet_text": "أهلًا وسهلًا! استمتع بالتصفح 🤍",
        "info_fmt": "اسم: {name}\nيوزر: @{user}\nمعرّف: {uid}\nاللغة: {lang}",
        "sections": {
            "prog": "💻 البرمجة",
            "design": "🎨 التصميم",
            "security": "🛡️ الأمن",
            "languages": "🗣️ اللغات",
            "marketing": "📈 التسويق",
            "maintenance": "🔧 الصيانة",
            "office": "🗂️ البرامج المكتبية",
        },
        "dev": "المطور 🧑‍💻",
    },
    "en": {
        "intro": (
            "Welcome! This bot provides a library of courses and files (PDF/ZIP/RAR) across sections.\n"
            "▪️ Join the channel if required\n"
            "▪️ Press ▶️ *Start* to begin\n"
            "▪️ You can switch language anytime\n\nEnjoy 🤍"
        ),
        "welcome": "Welcome to the courses library 📚\nPick a category:",
        "back": "Back",
        "contact": "Admin 🧑‍💻",
        "contact_short": "🆘 Help",
        "must_join": "Please join the channel first, then press ✅ Joined",
        "joined": "✅ Verified — you can continue.",
        "verify": "✅ Joined",
        "join_channel": "🔔 Go to channel",
        "missing": "⚠️ File not found on server:\n",
        "change_language": "🌍 Change Language | تغيير اللغة",
        "start": "▶️ Start",
        "myinfo": "🪪 My info",
        "greet": "👋 Welcome",
        "help_text_contact": "Contact the admin:",
        "greet_text": "Hi there! Enjoy browsing 🤍",
        "info_fmt": "Name: {name}\nUser: @{user}\nUser ID: {uid}\nLang: {lang}",
        "sections": {
            "prog": "💻 Programming",
            "design": "🎨 Design",
            "security": "🛡️ Security",
            "languages": "🗣️ Languages",
            "marketing": "📈 Marketing",
            "maintenance": "🔧 Maintenance",
            "office": "🗂️ Office apps",
        },
        "dev": "Developer 🧑‍💻",
    },
}

ALLOWED_EXTS = {".pdf", ".zip", ".rar"}

# ===================== تحميل الكتالوج =====================
def load_catalog() -> dict:
    cat_file = BASE_DIR / CATALOG_PATH
    if not cat_file.exists():
        alt = BASE_DIR / "catalog.json"
        if alt.exists():
            cat_file = alt
    log.info("📘 Using catalog file: %s", cat_file.as_posix())
    with cat_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
    stats = {k: (len(v) if isinstance(v, list) else len(v.get("children", [])))
             for k, v in data.items()}
    log.info("📦 Catalog on start: %s", stats)
    return data

CATALOG = load_catalog()

# ===================== Health server =====================
class Healthz(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Healthz).serve_forever()

def start_health_thread():
    Thread(target=start_health_server, daemon=True).start()
    log.info("🌐 Health server on 0.0.0.0:%s", os.getenv("PORT", "10000"))

# ===================== أدوات لغة/قوائم =====================
def ulang(update: Update) -> str:
    uid = update.effective_user.id if update.effective_user else 0
    return USER_LANG.get(uid, "ar")

def t(update: Update, key: str) -> str:
    return L[ulang(update)].get(key, key)

def section_label(update: Update, key: str) -> str:
    return L[ulang(update)]["sections"].get(key, key)

def bottom_keyboard(update: Update) -> ReplyKeyboardMarkup:
    s = L[ulang(update)]["sections"]
    rows = [
        [KeyboardButton(s["prog"]), KeyboardButton(s["design"])],
        [KeyboardButton(s["security"]), KeyboardButton(s["languages"])],
        [KeyboardButton(s["marketing"]), KeyboardButton(s["maintenance"])],
        [KeyboardButton(s["office"])],
        [KeyboardButton(L[ulang(update)]["change_language"]),
         KeyboardButton(L[ulang(update)]["contact_short"])],
        [KeyboardButton(L[ulang(update)]["start"])],  # زر البدء دائمًا موجود بالأسفل
        [KeyboardButton(L[ulang(update)]["myinfo"]),
         KeyboardButton(L[ulang(update)]["greet"])],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def contact_inline_button(update: Update):
    if OWNER_USERNAME:
        return InlineKeyboardButton(L[ulang(update)]["dev"], url=f"https://t.me/{OWNER_USERNAME}")
    return None

def main_menu_inline(update: Update) -> InlineKeyboardMarkup:
    order = ["prog", "design", "security", "languages", "marketing", "maintenance", "office"]
    rows, row = [], []
    for key in order:
        if key in CATALOG:
            row.append(InlineKeyboardButton(section_label(update, key), callback_data=f"cat|{key}"))
            if len(row) == 2:
                rows.append(row); row = []
    if row: rows.append(row)
    rows.append([
        InlineKeyboardButton("🇸🇦 عربي", callback_data="lang|ar"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang|en"),
    ])
    btn = contact_inline_button(update)
    if btn:
        rows.append([btn])
    return InlineKeyboardMarkup(rows)

def build_section_kb(section: str, update: Update) -> InlineKeyboardMarkup:
    items = CATALOG.get(section, [])
    rows = []
    for itm in items:
        if "children" in itm:
            title = itm.get("title", "Series")
            rows.append([InlineKeyboardButton(f"📚 {title}", callback_data=f"series|{section}")])
        else:
            title = itm.get("title", "file")
            path = itm.get("path", "")
            rows.append([InlineKeyboardButton(f"📄 {title}", callback_data=f"file|{path}")])
    rows.append([InlineKeyboardButton(L[ulang(update)]["back"], callback_data="back|main")])
    return InlineKeyboardMarkup(rows)

def build_series_kb(section: str, update: Update) -> InlineKeyboardMarkup:
    series = None
    for itm in CATALOG.get(section, []):
        if "children" in itm:
            series = itm["children"]; break
    rows = []
    if series:
        for child in series:
            rows.append([InlineKeyboardButton(f"📘 {child.get('title','part')}",
                                              callback_data=f"file|{child.get('path','')}")])
    rows.append([InlineKeyboardButton(L[ulang(update)]["back"], callback_data=f"cat|{section}")])
    return InlineKeyboardMarkup(rows)

# ===================== اشتراك القناة =====================
async def ensure_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user.id)
        status = getattr(member, "status", "left")
        if status in ("left", "kicked"):
            kb = [
                [InlineKeyboardButton(L[ulang(update)]["join_channel"],
                                      url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
                [InlineKeyboardButton(L[ulang(update)]["verify"], callback_data="verify")],
            ]
            await update.effective_message.reply_text(
                L[ulang(update)]["must_join"], reply_markup=InlineKeyboardMarkup(kb)
            )
            return False
        return True
    except Exception:
        # إذا كانت القناة خاصة أو الوصول محدود، نسمح بالمتابعة
        return True

# ===================== مطابقة مرنة للمسارات =====================
def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())

def resolve_relaxed(rel_path: str) -> Path | None:
    """يحاول إيجاد الملف حتى لو تغيّر اسم الحروف الكبيرة/المسافات أو تغيّر المجلد الفرعي داخل assets."""
    rel_path = rel_path.strip().replace("\\", "/")
    p = (BASE_DIR / rel_path).resolve()
    if p.exists():
        return p

    target = Path(rel_path)
    target_dir = (BASE_DIR / target.parent).resolve()
    target_stem_norm = _norm(target.stem)

    search_dirs = []
    if target_dir.exists():
        search_dirs.append(target_dir)
    assets_dir = BASE_DIR / "assets" / target.parent.name
    if assets_dir.exists() and assets_dir not in search_dirs:
        search_dirs.append(assets_dir)
    just_assets = BASE_DIR / "assets"
    if just_assets.exists() and just_assets not in search_dirs:
        search_dirs.append(just_assets)

    exts = {".pdf", ".zip", ".rar"}
    for d in search_dirs:
        try:
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in exts and _norm(f.stem) == target_stem_norm:
                    return f.resolve()
        except Exception:
            continue

    try:
        for f in (BASE_DIR / "assets").rglob("*"):
            if f.is_file() and f.suffix.lower() in exts and _norm(f.stem) == target_stem_norm:
                return f.resolve()
    except Exception:
        pass

    return None

# ===================== إرسال الملفات =====================
async def send_book(update: Update, context: ContextTypes.DEFAULT_TYPE, rel_path: str):
    fs_path = resolve_relaxed(rel_path)
    if not fs_path:
        log.warning("Missing file: %s", rel_path)
        await update.effective_message.reply_text(L[ulang(update)]["missing"] + rel_path)
        return
    if not str(fs_path).startswith(str(BASE_DIR)):
        log.warning("Blocked path traversal: %s -> %s", rel_path, fs_path)
        await update.effective_message.reply_text(L[ulang(update)]["missing"] + rel_path)
        return

    try:
        with fs_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(f, filename=fs_path.name),
            )
    except Exception as e:
        log.error("Failed to send %s: %s", fs_path, e, exc_info=True)
        await update.effective_message.reply_text(L[ulang(update)]["missing"] + rel_path)

# ===================== رسالة القائمة القابلة للتعديل =====================
async def set_menu_message(user_id: int, chat_id: int, message_id: int):
    MENU_MSG[user_id] = (chat_id, message_id)

def get_menu_message(user_id: int):
    return MENU_MSG.get(user_id)

async def ensure_menu_exists(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pair = get_menu_message(uid)
    if not pair:
        msg = await update.effective_message.reply_text(
            t(update, "welcome"),
            reply_markup=main_menu_inline(update),
        )
        await set_menu_message(uid, msg.chat.id, msg.message_id)

async def menu_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, kb: InlineKeyboardMarkup):
    uid = update.effective_user.id
    pair = get_menu_message(uid)
    if not pair:
        msg = await update.effective_message.reply_text(text, reply_markup=kb)
        await set_menu_message(uid, msg.chat.id, msg.message_id)
        return
    chat_id, msg_id = pair
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        msg = await update.effective_message.reply_text(text, reply_markup=kb)
        await set_menu_message(uid, msg.chat.id, msg.message_id)
    except Exception:
        msg = await update.effective_message.reply_text(text, reply_markup=kb)
        await set_menu_message(uid, msg.chat.id, msg.message_id)

# ===================== شاشة الترحيب + الدخول =====================
def landing_kb(update: Update) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(L[ulang(update)]["start"], callback_data="go|start")]]
    rows.append([
        InlineKeyboardButton("🇸🇦 عربي", callback_data="lang|ar"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang|en"),
    ])
    btn = contact_inline_button(update)
    if btn:
        rows.append([btn])
    return InlineKeyboardMarkup(rows)

async def landing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يُعرض عند دخول المستخدم: رسالة توضيحية + زر Start، مع إظهار لوحة الأزرار السفلية (Start + Help)."""
    uid = update.effective_user.id
    USER_LANG.setdefault(uid, USER_LANG.get(uid, "ar"))

    # إظهار لوحة الأزرار السفلية مرة واحدة (تحتوي على زر بدء + المساعدة)
    if uid not in KB_SENT:
        KB_SENT.add(uid)
        await update.effective_message.reply_text(
            L[ulang(update)]["welcome"],
            reply_markup=bottom_keyboard(update),
        )

    # رسالة ترحيب توضيحية مع زر بدء
    await update.effective_message.reply_text(
        L[ulang(update)]["intro"],
        reply_markup=landing_kb(update),
        parse_mode="Markdown",
    )

async def enter_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الدخول للواجهة الرئيسية (الاشتراك + القائمة + تثبيت رسالة القائمة للتعديل)."""
    uid = update.effective_user.id
    USER_LANG.setdefault(uid, USER_LANG.get(uid, "ar"))

    if not await ensure_membership(update, context):
        return

    if uid not in KB_SENT:
        KB_SENT.add(uid)
        await update.effective_message.reply_text(
            t(update, "welcome"),
            reply_markup=bottom_keyboard(update),
        )

    await ensure_menu_exists(update, context)
    await menu_edit(update, context, t(update, "welcome"), main_menu_inline(update))

# ===================== أوامر/معالجات =====================
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CATALOG
    try:
        CATALOG = load_catalog()
        await update.effective_message.reply_text("✅ تم إعادة تحميل الكاتالوج.")
        await menu_edit(update, context, t(update, "welcome"), main_menu_inline(update))
    except Exception as e:
        await update.effective_message.reply_text(f"❌ خطأ في إعادة التحميل: {e}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """زر /help يرسل طريقة التواصل (نفس زر المساعدة في الأسفل)."""
    if OWNER_USERNAME:
        await update.effective_message.reply_text(
            f"{L[ulang(update)]['help_text_contact']} https://t.me/{OWNER_USERNAME}",
            reply_markup=bottom_keyboard(update),
            disable_web_page_preview=True,
        )
    else:
        await update.effective_message.reply_text(
            "ضع OWNER_USERNAME في متغيرات البيئة لتمكين رابط التواصل.",
            reply_markup=bottom_keyboard(update),
        )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    kind, _, rest = data.partition("|")

    # حفظ رسالة القائمة إن كانت هذه هي الرسالة
    try:
        await set_menu_message(update.effective_user.id, q.message.chat.id, q.message.message_id)
    except Exception:
        pass

    if kind == "go":  # go|start
        await enter_app(update, context); return

    if kind == "verify":
        await q.edit_message_text(t(update, "welcome"), reply_markup=main_menu_inline(update))
        await update.effective_message.reply_text(
            L[ulang(update)]["joined"], reply_markup=bottom_keyboard(update)
        )
        return

    if kind == "lang":
        USER_LANG[update.effective_user.id] = "ar" if rest == "ar" else "en"
        # إعادة رسم شاشة الترحيب مع اللغة الجديدة
        await q.edit_message_text(L[ulang(update)]["intro"], reply_markup=landing_kb(update), parse_mode="Markdown")
        return

    if not await ensure_membership(update, context):
        return

    if kind == "back" and rest == "main":
        await q.edit_message_text(t(update, "welcome"), reply_markup=main_menu_inline(update))
        return

    if kind == "cat":
        section = rest
        await q.edit_message_text(section_label(update, section), reply_markup=build_section_kb(section, update))
        return

    if kind == "series":
        section = rest
        await q.edit_message_text(section_label(update, section), reply_markup=build_series_kb(section, update))
        return

    if kind == "file":
        await send_book(update, context, rest)
        return

def label_to_section_map(lang: str) -> dict[str, str]:
    return {v: k for k, v in L[lang]["sections"].items()}

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    lang = USER_LANG.get(uid, "ar")

    # زر البدء من اللوحة السفلية
    if text == L[lang]["start"]:
        await enter_app(update, context); return

    # تغيير اللغة
    if text == L[lang]["change_language"]:
        USER_LANG[uid] = ("en" if lang == "ar" else "ar")
        await landing(update, context)  # أعِد عرض شاشة الترحيب + لوحة الأزرار
        return

    # المساعدة (التواصل مع الإدارة)
    if text == L[lang]["contact_short"]:
        await cmd_help(update, context); return

    # معلوماتي + الترحيب
    if text == L[lang]["myinfo"]:
        name = (update.effective_user.full_name or "-")
        user = (update.effective_user.username or "-")
        msg = L[lang]["info_fmt"].format(name=name, user=user, uid=update.effective_user.id, lang=lang)
        await update.effective_message.reply_text(msg, reply_markup=bottom_keyboard(update))
        return

    if text == L[lang]["greet"]:
        await update.effective_message.reply_text(L[lang]["greet_text"], reply_markup=bottom_keyboard(update))
        return

    # خرائط الأقسام (باللغتين)
    for l in ("ar", "en"):
        sec_map = label_to_section_map(l)
        if text in sec_map:
            if not await ensure_membership(update, context):
                return
            key = sec_map[text]
            await menu_edit(update, context, section_label(update, key), build_section_kb(key, update))
            return

# ===================== التشغيل =====================
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set")
    start_health_thread()

    app = ApplicationBuilder().token(TOKEN).build()

    # /start: شاشة ترحيب + زر بدء + إظهار لوحة الأزرار السفلية (Start + Help)
    app.add_handler(CommandHandler("start", landing))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("🤖 Telegram bot starting…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
