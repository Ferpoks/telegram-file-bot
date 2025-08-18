# -*- coding: utf-8 -*-
import os, re, json, time, base64, hashlib, logging, asyncio, sqlite3, tempfile, socket, threading, shutil
from pathlib import Path
from html import escape as _escape

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# ========= Optional: OpenAI =========
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ========= Telegram =========
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ChatMemberStatus, ChatAction
from telegram.error import BadRequest

# ========= Others =========
import aiohttp
from dotenv import load_dotenv
from PIL import Image
try:
    import yt_dlp
except Exception:
    yt_dlp = None
try:
    import whois as pywhois
except Exception:
    pywhois = None
try:
    import dns.resolver as dnsresolver
    import dns.exception as dnsexception
except Exception:
    dnsresolver = None

# -------------------- ENV --------------------
if Path(".env").exists() and not os.getenv("RENDER"):
    load_dotenv(".env", override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").lstrip("@")

MAIN_CHANNEL_USERNAMES = [u.strip().lstrip("@") for u in (os.getenv("MAIN_CHANNELS","").split(",")) if u.strip()]
MAIN_CHANNEL_LINK = f"https://t.me/{MAIN_CHANNEL_USERNAMES[0]}" if MAIN_CHANNEL_USERNAMES else ""

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
SERVE_HEALTH = os.getenv("SERVE_HEALTH","1") == "1"

WELCOME_ANIMATION = (os.getenv("WELCOME_ANIMATION") or "").strip()  # gif/mp4/file_id (تجنّب webp كأنيميشن)
WELCOME_PHOTO = (os.getenv("WELCOME_PHOTO") or "").strip()          # بديل ثابت

# AI
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_VISION = os.getenv("OPENAI_VISION","0") == "1"
AI_ENABLED = bool(OPENAI_API_KEY) and (OpenAI is not None)
_openai_client = None

REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()
REPLICATE_MODEL_OWNER = os.getenv("REPLICATE_MODEL_OWNER", "stability-ai")
REPLICATE_MODEL_NAME  = os.getenv("REPLICATE_MODEL_NAME",  "stable-diffusion-xl-base-1.0")
REPLICATE_MODEL_VER   = os.getenv("REPLICATE_MODEL_VER",   "").strip()

DARK_GPT_URL = os.getenv("DARK_GPT_URL","https://flowgpt.com/chat/M0GRwnsc2MY0DdXPPmF4X")

# Paylink
PAY_WEBHOOK_ENABLE = os.getenv("PAY_WEBHOOK_ENABLE","1") == "1"
PAY_WEBHOOK_SECRET = (os.getenv("PAY_WEBHOOK_SECRET") or "").strip()
PAYLINK_API_BASE   = os.getenv("PAYLINK_API_BASE","https://restapi.paylink.sa/api").rstrip("/")
PAYLINK_API_ID     = (os.getenv("PAYLINK_API_ID") or "").strip()
PAYLINK_API_SECRET = (os.getenv("PAYLINK_API_SECRET") or "").strip()
PAYLINK_CHECKOUT_BASE = (os.getenv("PAYLINK_CHECKOUT_BASE") or "").strip()
VIP_PRICE_SAR = float(os.getenv("VIP_PRICE_SAR","10") or "10")

# Security tools keys
URLSCAN_API_KEY = (os.getenv("URLSCAN_API_KEY") or "").strip()
KICKBOX_API_KEY = (os.getenv("KICKBOX_API_KEY") or "").strip()
IPINFO_TOKEN    = (os.getenv("IPINFO_TOKEN") or "").strip()

# Courses
COURSE_PYTHON_URL = os.getenv("COURSE_PYTHON_URL","")
COURSE_CYBER_URL  = os.getenv("COURSE_CYBER_URL","")
COURSE_EH_URL     = os.getenv("COURSE_EH_URL","https://www.mediafire.com/folder/r26pp5mpduvnx/%D8%AF%D9%88%D8%B1%D8%A9_%D8%A7%D9%84%D9%87%D8%A7%D9%83%D8%B1_%D8%A7%D9%84%D8%A7%D8%AE%D9%84%D8%A7%D9%82%D9%8A_%D8%B9%D8%A8%D8%AF%D8%A7%D9%84%D8%B1%D8%AD%D9%85%D9%86_%D9%88%D8%B5%D9%81%D9%8A")
COURSE_ECOM_URL   = os.getenv("COURSE_ECOM_URL","https://drive.google.com/drive/folders/1-UADEMHUswoCyo853FdTu4R4iuUx_f3I?hl=ar")

# Services -> Games & Subs
GAMES_LINKS = [
    ("G2A",    os.getenv("GAMES_LINK_G2A","https://www.g2a.com/")),
    ("Kinguin",os.getenv("GAMES_LINK_KINGUIN","https://www.kinguin.net/")),
    ("GAMIVO", os.getenv("GAMES_LINK_GAMIVO","https://www.gamivo.com/")),
    ("Eneba",  os.getenv("GAMES_LINK_ENEBA","https://www.eneba.com/")),
]

TMP_DIR = Path(os.getenv("TMP_DIR","/tmp"))
DB_PATH = os.getenv("DB_PATH","/tmp/bot.db")

MAX_UPLOAD_MB = 47
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# ------------- helpers -------------
def _ensure_openai():
    global _openai_client
    if _openai_client is None and AI_ENABLED and OpenAI is not None:
        try:
            _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        except Exception as e:
            log.error("[openai] init failed: %s", e)

def admin_button_url() -> str:
    if OWNER_USERNAME:
        return f"tg://resolve?domain={OWNER_USERNAME}"
    if OWNER_ID:
        return f"tg://user?id={OWNER_ID}"
    return "https://t.me/"

# ========= Health/Webhook server in a separate Thread =========
from aiohttp import web

def _public_url(path: str) -> str:
    base = PUBLIC_BASE_URL or (f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME','')}" if os.getenv("RENDER_EXTERNAL_HOSTNAME") else "")
    return (base or "").rstrip("/") + path

async def _aio_health(_):
    return web.json_response({"ok": True})

def _find_ref(obj):
    if not obj: return None
    if isinstance(obj, dict):
        for k in ("orderNumber","merchantOrderNumber","merchantOrderNo","ref","reference","customerRef","customerReference"):
            v = obj.get(k)
            if isinstance(v,str) and re.fullmatch(r"\d{6,}-\d{9,}", v): return v
        for v in obj.values():
            r = _find_ref(v)
            if r: return r
    if isinstance(obj, (list,tuple)):
        for v in obj:
            r = _find_ref(v)
            if r: return r
    if isinstance(obj, str):
        m = re.search(r"(\d{6,}-\d{9,})", obj)
        if m: return m.group(1)
    return None

async def _payhook(request: web.Request):
    if PAY_WEBHOOK_SECRET and request.headers.get("X-PL-Secret") != PAY_WEBHOOK_SECRET:
        return web.json_response({"ok": False, "error": "bad secret"}, status=401)
    try:
        data = await request.json()
    except Exception:
        data = {"raw": await request.text()}
    ref = _find_ref(data)
    if not ref:
        return web.json_response({"ok": False, "error": "no-ref"}, status=200)
    activated = payments_mark_paid_by_ref(ref, raw=data)
    log.info("[payhook] ref=%s -> %s", ref, activated)
    return web.json_response({"ok": True, "ref": ref, "activated": bool(activated)})

def run_health_server_threaded():
    """يشغّل aiohttp في ثريد مستقل (لوب مختلف) لتفادي تضارب حلقات asyncio مع run_polling."""
    if not SERVE_HEALTH:
        return
    port = int(os.getenv("PORT","10000"))

    def _thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = web.Application()
        app.router.add_get("/", _aio_health)
        app.router.add_get("/health", _aio_health)
        if PAY_WEBHOOK_ENABLE:
            app.router.add_post("/payhook", _payhook)
            app.router.add_get("/payhook", _aio_health)
        runner = web.AppRunner(app)
        async def _start():
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            log.info("[http] serving on 0.0.0.0:%d", port)
        loop.run_until_complete(_start())
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(runner.cleanup())
            loop.close()

    threading.Thread(target=_thread, daemon=True).start()

# ffmpeg presence (optional)
def ffmpeg_path():
    p = shutil.which("ffmpeg")
    return p

if ffmpeg_path():
    log.info("[ffmpeg] FOUND at %s", ffmpeg_path())
else:
    log.warning("[ffmpeg] missing")

# ------------- i18n -------------
def T(key: str, lang: str | None = None, **kw) -> str:
    AR = {
        "start_pick_lang": "اختر لغتك:",
        "lang_ar": "العربية",
        "lang_en": "English",
        "hello_name": "مرحباً {name} 👋\nهذا بوت فيربوكس — عندك: أدوات الذكاء الاصطناعي، الأمن، الخدمات، الدورات، والدفع للـVIP.",
        "main_menu": "👇 القائمة الرئيسية",
        "btn_myinfo": "👤 معلوماتي",
        "btn_lang": "🌐 تغيير اللغة",
        "btn_vip": "⭐ حساب VIP",
        "btn_contact": "📨 تواصل مع الإدارة",
        "btn_sections": "📂 الأقسام",
        "gate_join": "🔐 انضم للقناة أولاً لاستخدام البوت:",
        "verify": "✅ تحقّق",
        "back": "↩️ رجوع",
        "sections": "📂 الأقسام",
        "sec_ai": "🤖 أدوات الذكاء الاصطناعي (VIP)",
        "sec_security": "🛡️ الأمن (VIP)",
        "sec_services": "🧰 الخدمات",
        "sec_unban": "🚫 فك الباند",
        "sec_courses": "🎓 الدورات",
        "sec_darkgpt": "🕶️ Dark GPT (VIP)",
        "vip_status_on": "⭐ حسابك VIP (مدى الحياة).",
        "vip_status_off": "⚡ ترقية إلى VIP مدى الحياة",
        "verify_done": "👌 تم التحقق.",
        "not_verified": "❗️ لم يتم التحقق بعد.",
        "contact_admin": "تواصل مع الإدارة:",
        "choose_option": "اختر خياراً:",
        "myinfo": "👤 الاسم: {name}\n🆔 المعرف: {uid}\n🌐 اللغة: {lng}",

        "page_ai": "🤖 أدوات الذكاء الاصطناعي:",
        "btn_ai_chat": "💬 دردشة",
        "btn_ai_write": "✍️ كتابة (إعلانات/منشورات)",
        "btn_ai_translate": "🌐 ترجمة تلقائية (AR ↔ EN)",
        "btn_ai_image": "🖼️ توليد صور",

        "page_security": "🛡️ أدوات الأمن:",
        "btn_urlscan": "🔗 فحص رابط",
        "btn_emailcheck": "📧 فحص إيميل",
        "btn_geolookup": "🛰️ موقع IP/دومين",

        "page_services": "🧰 الخدمات:",
        "btn_games": "🎮 الألعاب والاشتراكات",

        "page_courses": "🎓 الدورات:",
        "course_python": "بايثون من الصفر",
        "course_cyber": "الأمن السيبراني من الصفر",
        "course_eh": "الهكر الأخلاقي",
        "course_ecom": "التجارة الإلكترونية",

        "vip_only": "🚫 هذه الميزة متاحة لمشتركي VIP فقط.",
    }
    EN = {
        "start_pick_lang": "Pick your language:",
        "lang_ar": "العربية",
        "lang_en": "English",
        "hello_name": "Welcome {name}! 👋\nThis is Ferpoks Bot — you’ll find: AI tools, Security, Services, Courses and VIP payments.",
        "main_menu": "👇 Main menu",
        "btn_myinfo": "👤 My info",
        "btn_lang": "🌐 Change language",
        "btn_vip": "⭐ VIP Account",
        "btn_contact": "📨 Contact Admin",
        "btn_sections": "📂 Sections",
        "gate_join": "🔐 Please join the channel first:",
        "verify": "✅ Verify",
        "back": "↩️ Back",
        "sections": "📂 Sections",
        "sec_ai": "🤖 AI Tools (VIP)",
        "sec_security": "🛡️ Security (VIP)",
        "sec_services": "🧰 Services",
        "sec_unban": "🚫 Unban",
        "sec_courses": "🎓 Courses",
        "sec_darkgpt": "🕶️ Dark GPT (VIP)",
        "vip_status_on": "⭐ Your VIP is active (lifetime).",
        "vip_status_off": "⚡ Upgrade to lifetime VIP",
        "verify_done": "👌 Verified.",
        "not_verified": "❗️ Not verified yet.",
        "contact_admin": "Contact admin:",
        "choose_option": "Choose an option:",
        "myinfo": "👤 Name: {name}\n🆔 ID: {uid}\n🌐 Lang: {lng}",

        "page_ai": "🤖 AI Tools:",
        "btn_ai_chat": "💬 Chat",
        "btn_ai_write": "✍️ Writing (Ads/Posts)",
        "btn_ai_translate": "🌐 Auto Translate (AR ↔ EN)",
        "btn_ai_image": "🖼️ Image Gen",

        "page_security": "🛡️ Security tools:",
        "btn_urlscan": "🔗 URL Scan",
        "btn_emailcheck": "📧 Email Check",
        "btn_geolookup": "🛰️ IP/Domain Geo",

        "page_services": "🧰 Services:",
        "btn_games": "🎮 Games & Subscriptions",

        "page_courses": "🎓 Courses:",
        "course_python": "Python from Zero",
        "course_cyber": "Cybersecurity from Zero",
        "course_eh": "Ethical Hacking",
        "course_ecom": "E-commerce",

        "vip_only": "🚫 VIP only feature.",
    }

    if lang not in ("ar","en"): lang = "ar"
    D = AR if lang=="ar" else EN
    s = D.get(key, key)
    try:
        kw = {k:_escape(str(v)) for k,v in kw.items()}
        return s.format(**kw)
    except Exception:
        return s

# ------------- DB -------------
_db_lock = threading.RLock()
def _db():
    conn = getattr(_db, "_conn", None)
    if conn is not None: return conn
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _db._conn = conn
    log.info("[db] %s", DB_PATH)
    return conn

def migrate_db():
    with _db_lock:
        _db().execute("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          premium INTEGER DEFAULT 0,
          verified_ok INTEGER DEFAULT 0,
          verified_at INTEGER DEFAULT 0,
          vip_forever INTEGER DEFAULT 0,
          vip_since INTEGER DEFAULT 0,
          pref_lang TEXT DEFAULT 'ar'
        );""")
        _db().execute("""
        CREATE TABLE IF NOT EXISTS ai_state (
          user_id TEXT PRIMARY KEY,
          mode TEXT,
          extra TEXT,
          updated_at INTEGER
        );""")
        _db().execute("""
        CREATE TABLE IF NOT EXISTS payments (
          ref TEXT PRIMARY KEY,
          user_id TEXT,
          amount REAL,
          provider TEXT,
          status TEXT,
          created_at INTEGER,
          paid_at INTEGER,
          raw TEXT
        );""")
        _db().commit()

def init_db():
    migrate_db()

def user_get(uid) -> dict:
    uid = str(uid)
    with _db_lock:
        c = _db().cursor()
        c.execute("SELECT * FROM users WHERE id=?", (uid,))
        r = c.fetchone()
        if not r:
            _db().execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (uid,))
            _db().commit()
            return {"id": uid, "premium":0, "verified_ok":0, "verified_at":0, "vip_forever":0, "vip_since":0, "pref_lang":"ar"}
        return dict(r)

def user_is_vip(uid) -> bool:
    u = user_get(uid)
    return bool(u.get("premium") or u.get("vip_forever"))

def user_grant(uid):
    now = int(time.time())
    with _db_lock:
        _db().execute("UPDATE users SET premium=1, vip_forever=1, vip_since=COALESCE(NULLIF(vip_since,0),?) WHERE id=?", (now, str(uid)))
        _db().commit()

def user_set_verify(uid, ok=True):
    with _db_lock:
        _db().execute("UPDATE users SET verified_ok=?, verified_at=? WHERE id=?", (1 if ok else 0, int(time.time()), str(uid)))
        _db().commit()

def prefs_set_lang(uid, lang):
    with _db_lock:
        _db().execute("UPDATE users SET pref_lang=? WHERE id=?", (lang, str(uid))); _db().commit()

def ai_set_mode(uid, mode, extra=None):
    with _db_lock:
        _db().execute(
            "INSERT INTO ai_state (user_id,mode,extra,updated_at) VALUES (?,?,?,strftime('%s','now')) "
            "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode, extra=excluded.extra, updated_at=strftime('%s','now')",
            (str(uid), mode, json.dumps(extra or {}, ensure_ascii=False))
        ); _db().commit()

def ai_get_mode(uid):
    with _db_lock:
        c = _db().cursor()
        c.execute("SELECT mode,extra FROM ai_state WHERE user_id=?", (str(uid),))
        r = c.fetchone()
        if not r: return None, {}
        try:
            extra = json.loads(r["extra"] or "{}")
        except Exception:
            extra = {}
        return r["mode"], extra

# payments
def payments_new_ref(uid) -> str:
    return f"{uid}-{int(time.time())}"

def payments_create(uid, amount, provider="paylink", ref=None) -> str:
    ref = ref or payments_new_ref(uid)
    with _db_lock:
        _db().execute("INSERT OR REPLACE INTO payments (ref,user_id,amount,provider,status,created_at) VALUES (?,?,?,?,?,?)",
                      (ref, str(uid), amount, provider, "pending", int(time.time())))
        _db().commit()
    return ref

def payments_status(ref) -> str | None:
    with _db_lock:
        c = _db().cursor()
        c.execute("SELECT status FROM payments WHERE ref=?", (ref,))
        r = c.fetchone()
        return r["status"] if r else None

def payments_mark_paid_by_ref(ref, raw=None) -> bool:
    with _db_lock:
        c = _db().cursor()
        c.execute("SELECT user_id,status FROM payments WHERE ref=?", (ref,))
        r = c.fetchone()
        if not r: return False
        if r["status"] == "paid":
            user_grant(r["user_id"]); return True
        _db().execute("UPDATE payments SET status='paid', paid_at=?, raw=? WHERE ref=?",
                      (int(time.time()), json.dumps(raw, ensure_ascii=False) if raw is not None else None, ref))
        _db().commit()
    user_grant(r["user_id"])
    return True

# Paylink API
_paylink_token = None
_paylink_token_exp = 0
async def paylink_auth_token():
    global _paylink_token, _paylink_token_exp
    now = time.time()
    if _paylink_token and _paylink_token_exp > now + 10:
        return _paylink_token
    url = f"{PAYLINK_API_BASE}/auth"
    payload = {"apiId": PAYLINK_API_ID, "secretKey": PAYLINK_API_SECRET, "persistToken": False}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, timeout=20) as r:
            data = await r.json(content_type=None)
    if "token" in data:
        _paylink_token = data["token"]; _paylink_token_exp = now + 9*60; return _paylink_token
    raise RuntimeError(f"paylink auth failed: {data}")

async def paylink_create_invoice(order_number: str, amount: float, client_name: str):
    token = await paylink_auth_token()
    url = f"{PAYLINK_API_BASE}/addInvoice"
    body = {
        "orderNumber": order_number,
        "amount": amount,
        "clientName": client_name or "Telegram User",
        "clientMobile": "0500000000",
        "currency": "SAR",
        "callBackUrl": _public_url("/payhook"),
        "displayPending": False,
        "note": f"VIP via Telegram #{order_number}",
        "products": [{"title": "VIP Access (Lifetime)", "price": amount, "qty": 1, "isDigital": True}]
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=headers, timeout=30) as r:
            data = await r.json(content_type=None)
    pay_url = data.get("url") or data.get("mobileUrl") or data.get("qrUrl")
    if not pay_url: raise RuntimeError(f"paylink addInvoice failed: {data}")
    return pay_url, data

def _build_checkout_link(ref: str) -> str:
    base = PAYLINK_CHECKOUT_BASE.strip()
    if not base: return ""
    if "{ref}" in base: return base.format(ref=ref)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}ref={ref}"

# ------------- Security helpers -------------
def md5_hex(s: str) -> str:
    return hashlib.md5(s.strip().lower().encode()).hexdigest()

async def http_head(url: str) -> int | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.head(url, allow_redirects=True, timeout=15) as r:
                return r.status
    except Exception:
        return None

def resolve_ip(host: str) -> str | None:
    try:
        infos = socket.getaddrinfo(host, None)
        for _,_,_,_,sa in infos:
            ip = sa[0]
            if ":" not in ip: return ip
        return infos[0][4][0] if infos else None
    except Exception:
        return None

async def fetch_geo(query: str) -> dict | None:
    url = f"http://ip-api.com/json/{query}?fields=status,message,country,regionName,city,isp,org,as,query,lat,lon,timezone,zip,reverse"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=15) as r:
                return await r.json(content_type=None)
    except Exception:
        return {"status":"fail","message":"network error"}

def fmt_geo(data: dict) -> str:
    if not data or data.get("status")!="success":
        return f"⚠️ {data.get('message','lookup failed') if data else 'lookup failed'}"
    parts = [
        f"🔎 query: <code>{_escape(str(data.get('query','')))}</code>",
        f"🌍 {data.get('country','?')} — {data.get('regionName','?')}",
        f"🏙️ {data.get('city','?')} — {data.get('zip','-')}",
        f"⏰ {data.get('timezone','-')}",
        f"📡 ISP/ORG: {data.get('isp','-')} / {data.get('org','-')}",
        f"🛰️ AS: {data.get('as','-')}",
        f"📍 {data.get('lat','?')}, {data.get('lon','?')}",
    ]
    if data.get("reverse"): parts.append(f"🔁 Reverse: {_escape(str(data['reverse']))}")
    return "\n".join(parts)

async def urlscan_lookup(u: str) -> str:
    if not URLSCAN_API_KEY:
        return "ℹ️ ضع URLSCAN_API_KEY لتفعيل الفحص."
    try:
        headers = {"API-Key": URLSCAN_API_KEY, "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as s:
            async with s.post("https://urlscan.io/api/v1/scan/", headers=headers, json={"url": u, "visibility":"unlisted"}, timeout=30) as r:
                data = await r.json(content_type=None)
        out = []
        if "result" in data: out.append(f"urlscan: {data['result']}")
        if "message" in data: out.append(f"msg: {data['message']}")
        return "\n".join(out) or "urlscan: submitted."
    except Exception as e:
        return f"urlscan error: {e}"

async def kickbox_lookup(email: str) -> str:
    if not KICKBOX_API_KEY:
        return "ℹ️ ضع KICKBOX_API_KEY لتفعيل فحص الإيميل."
    try:
        params = {"email": email, "apikey": KICKBOX_API_KEY}
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.kickbox.com/v2/verify", params=params, timeout=20) as r:
                data = await r.json(content_type=None)
        return f"Kickbox: result={data.get('result')} reason={data.get('reason')}"
    except Exception as e:
        return f"kickbox error: {e}"

def is_valid_email(e: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}", e or ""))

def whois_domain(domain: str) -> dict | None:
    if pywhois is None: return {"error":"python-whois غير مثبت"}
    try:
        w = pywhois.whois(domain)
        return {
            "domain_name": str(getattr(w,"domain_name",None)),
            "registrar": getattr(w,"registrar",None),
            "creation_date": str(getattr(w,"creation_date",None)),
            "expiration_date": str(getattr(w,"expiration_date",None)),
            "emails": getattr(w,"emails",None)
        }
    except Exception as e:
        return {"error": f"whois error: {e}"}

async def osint_email(email: str) -> str:
    if not is_valid_email(email):
        return "⚠️ صيغة الإيميل غير صحيحة."
    local, domain = email.split("@",1)
    # MX
    if dnsresolver:
        try:
            answers = dnsresolver.resolve(domain,"MX")
            mx_hosts = [str(r.exchange).rstrip(".") for r in answers]
            mx_txt = ", ".join(mx_hosts[:5]) if mx_hosts else "لا يوجد"
        except dnsexception.DNSException:
            mx_txt = "لا يوجد (فشل الاستعلام)"
    else:
        mx_txt = "dnspython غير مثبت"
    # gravatar
    g_url = f"https://www.gravatar.com/avatar/{md5_hex(email)}?d=404"
    g_st = await http_head(g_url)
    grav = "✅ موجود" if g_st and 200 <= g_st < 300 else "❌ غير موجود"
    # whois
    w = whois_domain(domain)
    w_txt = "WHOIS: غير متاح" if not w else (f"WHOIS: {w['error']}" if w.get("error") else f"WHOIS:\n- Registrar: {w.get('registrar')}\n- Created: {w.get('creation_date')}\n- Expires: {w.get('expiration_date')}")
    # geo
    ip = resolve_ip(domain)
    geo_txt = fmt_geo(await fetch_geo(ip)) if ip else "⚠️ تعذّر حلّ IP للدومين."
    return "\n".join([f"📧 {email}", f"📮 MX: {mx_txt}", f"🖼️ Gravatar: {grav}", w_txt, "\n"+geo_txt])

async def link_scan(u: str) -> str:
    if not re.search(r"https?://", u or ""):
        return "⚠️ أرسل رابط يبدأ بـ http:// أو https://"
    host = re.sub(r"^https?://", "", u).split("/")[0]
    issues = []
    st = await http_head(u); issues.append(f"🔎 HTTP: {st if st is not None else 'n/a'}")
    if not u.startswith("https://"): issues.append("❗️ الرابط بدون HTTPS")
    try:
        us = await urlscan_lookup(u); issues.append(us)
    except Exception:
        pass
    ip = resolve_ip(host)
    geo_txt = fmt_geo(await fetch_geo(ip)) if ip else "⚠️ تعذّر حلّ IP."
    return f"🔗 <code>{_escape(u)}</code>\nالمضيف: <code>{_escape(host)}</code>\n" + "\n".join(issues) + f"\n\n{geo_txt}"

# ------------- AI -------------
def contains_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))

def ai_chat_reply(text: str) -> str:
    if not AI_ENABLED or OpenAI is None:
        return "🧠 الذكاء الاصطناعي غير مفعّل."
    _ensure_openai()
    msgs = [
        {"role":"system","content":"أجب بإيجاز وبأسلوب مهني."},
        {"role":"user","content": text}
    ]
    try:
        r = _openai_client.chat.completions.create(model=OPENAI_CHAT_MODEL, messages=msgs, temperature=0.6)
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.error("[ai-chat] %s", e)
        return "⚠️ تعذّر الرد حالياً."

async def ai_write(prompt: str) -> str:
    if not AI_ENABLED or OpenAI is None:
        return "🧠 الذكاء الاصطناعي غير مفعّل."
    _ensure_openai()
    sys = "أنت كاتب إعلانات محترف. اكتب نصًا تسويقيًا واضحًا ومقنعًا مع CTA واستخدام عناوين قصيرة."
    try:
        r = _openai_client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[{"role":"system","content":sys},{"role":"user","content":prompt}],
            temperature=0.7
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.error("[ai-write] %s", e)
        return "⚠️ تعذّر التوليد حالياً."

async def ai_auto_translate(text: str) -> str:
    """إذا عربي -> إنجليزي، وإلا إنجليزي -> عربي. ويُظهر الاتجاه بوضوح."""
    if not AI_ENABLED or OpenAI is None:
        return "🧠 الذكاء الاصطناعي غير مفعّل."
    _ensure_openai()
    src_ar = contains_arabic(text)
    target = "English" if src_ar else "Arabic"
    try:
        r = _openai_client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[{"role":"system","content":"Translate accurately while preserving meaning."},
                      {"role":"user","content": f"Source:\n{text}\n\nTranslate to {target} only."}],
            temperature=0.0
        )
        out = (r.choices[0].message.content or "").strip()
        if src_ar:
            return f"🇦🇪 AR → 🇬🇧 EN\n\n{out}"
        else:
            return f"🇬🇧 EN → 🇦🇪 AR\n\n{out}"
    except Exception as e:
        log.error("[ai-translate] %s", e)
        return "⚠️ تعذّر الترجمة حالياً."

# ------------- Telegram UI -------------
def gate_kb(lang="ar"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 " + ("الانضمام للقناة" if lang=="ar" else "Join Channel"), url=MAIN_CHANNEL_LINK or "https://t.me/")],
        [InlineKeyboardButton(T("verify", lang=lang), callback_data="verify")]
    ])

def main_menu_kb(uid, lang="ar"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T("btn_myinfo", lang=lang), callback_data="myinfo")],
        [InlineKeyboardButton(T("btn_lang", lang=lang), callback_data="pick_lang")],
        [InlineKeyboardButton(T("btn_vip", lang=lang), callback_data="vip")],
        [InlineKeyboardButton(T("btn_contact", lang=lang), url=admin_button_url())],
        [InlineKeyboardButton(T("btn_sections", lang=lang), callback_data="sections")]
    ])

def sections_kb(lang="ar"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(T("sec_ai", lang=lang), callback_data="sec_ai")],
        [InlineKeyboardButton(T("sec_security", lang=lang), callback_data="sec_security")],
        [InlineKeyboardButton(T("sec_services", lang=lang), callback_data="sec_services")],
        [InlineKeyboardButton(T("sec_unban", lang=lang), callback_data="sec_unban")],
        [InlineKeyboardButton(T("sec_courses", lang=lang), callback_data="sec_courses")],
        [InlineKeyboardButton(T("sec_darkgpt", lang=lang), callback_data="sec_darkgpt")],
        [InlineKeyboardButton(T("back", lang=lang), callback_data="back_home")]
    ])

def ai_stop_kb(lang="ar"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔚 " + ("إنهاء" if lang=="ar" else "Stop"), callback_data="ai_stop")],
        [InlineKeyboardButton(T("back", lang=lang), callback_data="sections")]
    ])

async def safe_edit(q, text=None, kb=None):
    try:
        if text is not None:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        elif kb is not None:
            await q.edit_message_reply_markup(reply_markup=kb)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.warning("safe_edit: %s", e)

# membership check
ALLOWED_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
try: ALLOWED_STATUSES.add(ChatMemberStatus.OWNER)
except: pass
try: ALLOWED_STATUSES.add(ChatMemberStatus.CREATOR)
except: pass

CHANNEL_ID = None
_member_cache = {}
async def resolve_channel_id(bot):
    global CHANNEL_ID
    CHANNEL_ID = None
    for u in MAIN_CHANNEL_USERNAMES:
        try:
            chat = await bot.get_chat(f"@{u}")
            CHANNEL_ID = chat.id
            log.info("[startup] channel @%s -> %s", u, CHANNEL_ID); break
        except Exception as e:
            log.warning("[startup] get_chat @%s failed: %s", u, e)

async def is_member(context, user_id: int, force=False, retries=3, backoff=0.7) -> bool:
    now = time.time()
    if not force:
        c = _member_cache.get(user_id)
        if c and c[1] > now: return c[0]
    targets = [CHANNEL_ID] if CHANNEL_ID is not None else [f"@{u}" for u in MAIN_CHANNEL_USERNAMES]
    for attempt in range(1, retries+1):
        for t in targets:
            try:
                cm = await context.bot.get_chat_member(t, user_id)
                ok = getattr(cm,"status",None) in ALLOWED_STATUSES
                if ok:
                    _member_cache[user_id] = (True, now+60); user_set_verify(user_id, True); return True
            except Exception as e:
                log.warning("[is_member] try#%d %s  %s", attempt, t, e)
        if attempt < retries: await asyncio.sleep(backoff*attempt)
    _member_cache[user_id] = (False, now+60)
    user_set_verify(user_id, False); return False

async def must_join_or_vip(context, uid) -> bool:
    return user_is_vip(uid) or await is_member(context, uid, retries=3, backoff=0.7)

# ------------- Handlers -------------
async def on_startup(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("[startup] delete_webhook: %s", e)
    await resolve_channel_id(app.bot)
    try:
        await app.bot.set_my_commands([BotCommand("start","Start"), BotCommand("help","Help")], scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(
            [
                BotCommand("start","Start"), BotCommand("help","Help"),
                BotCommand("id","Your ID"), BotCommand("grant","Grant VIP"),
                BotCommand("revoke","Revoke VIP"), BotCommand("vipinfo","VIP Info"),
                BotCommand("refreshcmds","Refresh Cmds"), BotCommand("aidiag","AI diag"),
                BotCommand("libdiag","Lib versions"), BotCommand("paylist","Payments"), BotCommand("restart","Restart")
            ],
            scope=BotCommandScopeChat(chat_id=OWNER_ID)
        )
    except Exception as e:
        log.warning("[startup] set_my_commands: %s", e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    uid = update.effective_user.id
    u = user_get(uid)
    lang = u.get("pref_lang","ar")
    name = (update.effective_user.full_name or "").strip() or "صديقي"
    greet = T("hello_name", lang=lang, name=name)
    summary = (
        "• 🤖 الذكاء الاصطناعي (VIP)\n"
        "• 🛡️ الأمن (VIP)\n"
        "• 🧰 الخدمات (تشمل 🎮 الألعاب والاشتراكات)\n"
        "• 🎓 الدورات\n"
        "• ⭐ الترقية إلى VIP مدى الحياة"
        if lang=="ar" else
        "• 🤖 AI tools (VIP)\n"
        "• 🛡️ Security (VIP)\n"
        "• 🧰 Services (incl. 🎮 Games & Subs)\n"
        "• 🎓 Courses\n"
        "• ⭐ Upgrade to lifetime VIP"
    )
    text = f"{greet}\n\n{summary}\n\n{T('main_menu',lang=lang)}"
    sent_media = False
    try:
        if WELCOME_ANIMATION:
            await context.bot.send_animation(update.effective_chat.id, WELCOME_ANIMATION)
            sent_media = True
        elif WELCOME_PHOTO:
            await context.bot.send_photo(update.effective_chat.id, WELCOME_PHOTO)
            sent_media = True
    except Exception as e:
        log.warning("[welcome media] %s", e)
    if sent_media:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=main_menu_kb(uid, lang))
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=main_menu_kb(uid, lang))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_get(uid).get("pref_lang","ar")
    await update.message.reply_text(T("main_menu", lang=lang), reply_markup=main_menu_kb(uid, lang))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    q = update.callback_query; uid = q.from_user.id
    u = user_get(uid); lang = u.get("pref_lang","ar")
    await q.answer()

    if q.data in ("set_lang_ar","set_lang_en"):
        new = "ar" if q.data.endswith("_ar") else "en"
        prefs_set_lang(uid, new)
        await safe_edit(q, T("main_menu", lang=new), kb=main_menu_kb(uid, new)); return

    if q.data == "pick_lang":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T("lang_ar", lang=lang), callback_data="set_lang_ar"),
             InlineKeyboardButton(T("lang_en", lang=lang), callback_data="set_lang_en")],
            [InlineKeyboardButton(T("back", lang=lang), callback_data="back_home")]
        ])
        await safe_edit(q, T("start_pick_lang", lang=lang), kb=kb); return

    if q.data == "verify":
        ok = await is_member(context, uid, force=True)
        await safe_edit(q, T("verify_done", lang=lang) if ok else T("not_verified", lang=lang), kb=main_menu_kb(uid, lang)); return

    if not await must_join_or_vip(context, uid):
        await safe_edit(q, T("gate_join", lang=lang), kb=gate_kb(lang)); return

    if q.data == "myinfo":
        await safe_edit(q, T("myinfo", lang=lang, name=q.from_user.full_name, uid=uid, lng=lang.upper()), kb=main_menu_kb(uid, lang)); return
    if q.data == "back_home":
        await safe_edit(q, T("main_menu", lang=lang), kb=main_menu_kb(uid, lang)); return

    # VIP
    if q.data == "vip":
        if user_is_vip(uid) or uid == OWNER_ID:
            await safe_edit(q, T("vip_status_on", lang=lang), kb=main_menu_kb(uid, lang)); return
        ref = payments_create(uid, VIP_PRICE_SAR, "paylink")
        try:
            if PAYLINK_API_ID and PAYLINK_API_SECRET:
                url, _ = await paylink_create_invoice(ref, VIP_PRICE_SAR, q.from_user.full_name or "Telegram User")
            else:
                url = _build_checkout_link(ref) or "https://paylink.sa"
            txt = f"{T('vip_status_off',lang=lang)} — {VIP_PRICE_SAR:.2f} SAR\n🔖 ref: <code>{_escape(ref)}</code>"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Pay", url=url)],
                [InlineKeyboardButton("✅ Check", callback_data=f"verify_pay_{ref}")],
                [InlineKeyboardButton(T("back", lang=lang), callback_data="back_home")]
            ])
            await safe_edit(q, txt, kb=kb)
        except Exception as e:
            log.error("[vip] %s", e)
            await safe_edit(q, "⚠️ تعذّر إنشاء رابط الدفع حالياً.", kb=main_menu_kb(uid, lang))
        return

    if q.data.startswith("verify_pay_"):
        ref = q.data.split("_",2)[2]
        st = payments_status(ref)
        if st == "paid" or user_is_vip(uid):
            await safe_edit(q, T("vip_status_on", lang=lang), kb=main_menu_kb(uid, lang))
        else:
            await safe_edit(q, T("not_verified", lang=lang)+f"\nref=<code>{_escape(ref)}</code>", kb=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Check", callback_data=f"verify_pay_{ref}")],
                [InlineKeyboardButton(T("back", lang=lang), callback_data="back_home")]
            ]))
        return

    # sections
    if q.data == "sections":
        await safe_edit(q, T("sections", lang=lang), kb=sections_kb(lang)); return

    # AI (VIP only)
    if q.data == "sec_ai":
        if not user_is_vip(uid) and uid != OWNER_ID:
            await safe_edit(q, T("vip_only", lang=lang), kb=sections_kb(lang)); return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T("btn_ai_chat", lang=lang), callback_data="ai_chat")],
            [InlineKeyboardButton(T("btn_ai_write", lang=lang), callback_data="ai_write")],
            [InlineKeyboardButton(T("btn_ai_translate", lang=lang), callback_data="ai_translate")],
            [InlineKeyboardButton(T("btn_ai_image", lang=lang), callback_data="ai_image")],
            [InlineKeyboardButton(T("back", lang=lang), callback_data="sections")]
        ])
        await safe_edit(q, T("page_ai", lang=lang), kb=kb); return

    if q.data == "ai_chat":
        ai_set_mode(uid, "ai_chat"); await safe_edit(q, "✳️ أرسل رسالتك…", kb=ai_stop_kb(lang)); return
    if q.data == "ai_write":
        ai_set_mode(uid, "writer"); await safe_edit(q, "✳️ اكتب وصف الحملة/المنتج وسأصيغ إعلانًا جذابًا.", kb=ai_stop_kb(lang)); return
    if q.data == "ai_translate":
        ai_set_mode(uid, "translate"); await safe_edit(q, "✳️ أرسل النص — سيتم اكتشاف اللغة تلقائيًا (AR ↔ EN).", kb=ai_stop_kb(lang)); return
    if q.data == "ai_image":
        ai_set_mode(uid, "image_ai"); await safe_edit(q, "✳️ اكتب وصف الصورة المطلوب توليدها.", kb=ai_stop_kb(lang)); return
    if q.data == "ai_stop":
        ai_set_mode(uid, None); await safe_edit(q, T("main_menu", lang=lang), kb=main_menu_kb(uid, lang)); return

    # Security (VIP only)
    if q.data == "sec_security":
        if not user_is_vip(uid) and uid != OWNER_ID:
            await safe_edit(q, T("vip_only", lang=lang), kb=sections_kb(lang)); return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T("btn_urlscan", lang=lang), callback_data="sec_security_url")],
            [InlineKeyboardButton(T("btn_emailcheck", lang=lang), callback_data="sec_security_email")],
            [InlineKeyboardButton(T("btn_geolookup", lang=lang), callback_data="sec_security_geo")],
            [InlineKeyboardButton(T("back", lang=lang), callback_data="sections")]
        ])
        await safe_edit(q, T("page_security", lang=lang), kb=kb); return

    if q.data == "sec_security_url":
        ai_set_mode(uid, "link_scan"); await safe_edit(q, "🛡️ أرسل الرابط للفحص.", kb=ai_stop_kb(lang)); return
    if q.data == "sec_security_email":
        ai_set_mode(uid, "email_check"); await safe_edit(q, "✉️ أرسل الإيميل للفحص.", kb=ai_stop_kb(lang)); return
    if q.data == "sec_security_geo":
        ai_set_mode(uid, "geo_ip"); await safe_edit(q, "📍 أرسل IP أو دومين.", kb=ai_stop_kb(lang)); return

    # Services
    if q.data == "sec_services":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(T("btn_games", lang=lang), callback_data="serv_games")],
            [InlineKeyboardButton(T("back", lang=lang), callback_data="sections")]
        ])
        await safe_edit(q, T("page_services", lang=lang), kb=kb); return

    if q.data == "serv_games":
        rows = [[InlineKeyboardButton(name, url=url)] for name,url in GAMES_LINKS]
        rows.append([InlineKeyboardButton(T("back", lang=lang), callback_data="sec_services")])
        await safe_edit(q, "🎮 أفضل مواقع شراء الألعاب والاشتراكات:", kb=InlineKeyboardMarkup(rows)); return

    # Unban (رسائل قوية)
    if q.data == "sec_unban":
        rows = [
            [InlineKeyboardButton("Instagram", callback_data="unban_instagram")],
            [InlineKeyboardButton("Facebook", callback_data="unban_facebook")],
            [InlineKeyboardButton("Telegram", callback_data="unban_telegram")],
            [InlineKeyboardButton("Epic Games", callback_data="unban_epic")],
            [InlineKeyboardButton(T("back", lang=lang), callback_data="sections")]
        ]
        await safe_edit(q, "اختر المنصة لعرض رسالة قوية لإرسالها للدعم:", kb=InlineKeyboardMarkup(rows)); return

    if q.data.startswith("unban_"):
        key = q.data.split("_",1)[1]
        strong = {
            "instagram": ("Instagram Support Appeal",
                          "Hello Instagram Support,\n\nMy account has been restricted/disabled in error. I strictly adhere to the Community Guidelines and believe this action was triggered by an automated system. I respectfully request a manual review and reinstatement.\n\nI am ready to provide any required verification or additional information. Thank you for your time."),
            "facebook": ("Facebook Support Appeal",
                         "Hello Facebook Support,\n\nMy account was mistakenly restricted/disabled. I fully comply with the Community Standards and believe this was an automated false positive. Please conduct a manual review and restore access.\n\nI can provide identity or evidence if needed. Thank you."),
            "telegram": ("Telegram Support Appeal",
                         "Hello Telegram Support,\n\nMy account/channel appears to be limited due to a false positive. I comply with the Terms of Service and local laws. Please manually review my case and lift the restriction.\n\nThanks for your help."),
            "epic": ("Epic Games Support Appeal",
                     "Hello Epic Games Support,\n\nMy account was banned by mistake. I respect all your policies and never intended to violate any rule. Please review my case manually and remove the ban. I can verify ownership or provide any evidence required.\n\nThank you.")
        }
        title, msg = strong.get(key, ("Support Appeal",""))
        link = {
            "instagram":"https://help.instagram.com/contact/606967319425038",
            "facebook":"https://www.facebook.com/help/contact/260749603972907",
            "telegram":"https://telegram.org/support",
            "epic":"https://www.epicgames.com/help/en-US/c4059"
        }.get(key,"")
        await safe_edit(q, f"📋 <b>{_escape(title)}</b>\n<code>{_escape(msg)}</code>\n\n🔗 {link}", kb=InlineKeyboardMarkup([[InlineKeyboardButton(T("back", lang=lang), callback_data="sec_unban")]])); return

    # Courses
    if q.data == "sec_courses":
        courses = [
            (T("course_python", lang=lang), COURSE_PYTHON_URL),
            (T("course_cyber",  lang=lang), COURSE_CYBER_URL),
            (T("course_eh",     lang=lang), COURSE_EH_URL),
            (T("course_ecom",   lang=lang), COURSE_ECOM_URL),
        ]
        rows = [[InlineKeyboardButton(title, url=url)] for title,url in courses if url]
        rows.append([InlineKeyboardButton(T("back", lang=lang), callback_data="sections")])
        await safe_edit(q, T("page_courses", lang=lang), kb=InlineKeyboardMarkup(rows)); return

    # Dark GPT (VIP only)
    if q.data == "sec_darkgpt":
        if not user_is_vip(uid) and uid != OWNER_ID:
            await safe_edit(q, T("vip_only", lang=lang), kb=sections_kb(lang)); return
        await safe_edit(q, f"{T('sec_darkgpt',lang=lang)}\n{_escape(DARK_GPT_URL)}", kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("Open", url=DARK_GPT_URL)],
            [InlineKeyboardButton(T("back", lang=lang), callback_data="sections")]
        ])); return

# messages guard
async def guard_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = user_get(uid); lang = u.get("pref_lang","ar")
    if not await must_join_or_vip(context, uid):
        await update.message.reply_text(T("gate_join", lang=lang), reply_markup=gate_kb(lang)); return

    mode, extra = ai_get_mode(uid)
    msg = update.message

    if msg.text and not msg.text.startswith("/"):
        text = msg.text.strip()
        if mode == "ai_chat":
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            await update.message.reply_text(ai_chat_reply(text), reply_markup=ai_stop_kb(lang)); return
        if mode == "writer":
            out = await ai_write(text); await update.message.reply_text(out, parse_mode="HTML"); return
        if mode == "translate":
            out = await ai_auto_translate(text); await update.message.reply_text(out, parse_mode="HTML"); return
        if mode == "link_scan":
            out = await link_scan(text); await update.message.reply_text(out, parse_mode="HTML", disable_web_page_preview=True); return
        if mode == "email_check":
            out = await osint_email(text); await update.message.reply_text(out, parse_mode="HTML"); return
        if mode == "geo_ip":
            target = text
            if re.fullmatch(r"[a-zA-Z0-9.-]+\.[A-Za-z]{2,63}", target or ""):
                ip = resolve_ip(target); target = ip or target
            data = await fetch_geo(target); await update.message.reply_text(fmt_geo(data), parse_mode="HTML"); return

    if not mode:
        await update.message.reply_text(T("main_menu", lang=lang), reply_markup=main_menu_kb(uid, lang))

# owner cmds
async def cmd_id(update, context):
    if update.effective_user.id == OWNER_ID:
        await update.message.reply_text(str(update.effective_user.id))

async def grant(update, context):
    if update.effective_user.id != OWNER_ID: return
    if not context.args: await update.message.reply_text("Usage: /grant <user_id>"); return
    user_grant(context.args[0]); await update.message.reply_text("✅ granted")

async def revoke(update, context):
    if update.effective_user.id != OWNER_ID: return
    if not context.args: await update.message.reply_text("Usage: /revoke <user_id>"); return
    with _db_lock: _db().execute("UPDATE users SET premium=0, vip_forever=0 WHERE id=?", (str(context.args[0]),)); _db().commit()
    await update.message.reply_text("❌ revoked")

async def vipinfo(update, context):
    if update.effective_user.id != OWNER_ID: return
    if not context.args: await update.message.reply_text("Usage: /vipinfo <user_id>"); return
    await update.message.reply_text(json.dumps(user_get(context.args[0]), ensure_ascii=False, indent=2))

async def refresh_cmds(update, context):
    if update.effective_user.id != OWNER_ID: return
    await on_startup(context.application); await update.message.reply_text("✅ refreshed")

async def aidiag(update, context):
    if update.effective_user.id != OWNER_ID: return
    k = bool(OPENAI_API_KEY); await update.message.reply_text(f"AI_ENABLED={AI_ENABLED} key={'yes' if k else 'no'} model={OPENAI_CHAT_MODEL}")

async def libdiag(update, context):
    if update.effective_user.id != OWNER_ID: return
    try:
        from importlib.metadata import version, PackageNotFoundError
        def v(p):
            try: return version(p)
            except PackageNotFoundError: return "not-installed"
        msg = (f"python-telegram-bot={v('python-telegram-bot')}\n"
               f"aiohttp={v('aiohttp')}\nPillow={v('Pillow')}\nyt-dlp={v('yt-dlp')}\n"
               f"python-whois={v('python-whois')}\ndnspython={v('dnspython')}\npython={os.sys.version.split()[0]}")
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(str(e))

async def paylist(update, context):
    if update.effective_user.id != OWNER_ID: return
    with _db_lock:
        c = _db().cursor()
        c.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT 20")
        rows = [dict(x) for x in c.fetchall()]
    if not rows: await update.message.reply_text("no payments"); return
    txt = []
    for r in rows:
        ts = time.strftime('%Y-%m-%d %H:%M', time.gmtime(r.get('created_at') or 0))
        txt.append(f"{r['ref']}  user={r['user_id']}  {r['status']}  at={ts}")
    await update.message.reply_text("\n".join(txt))

async def restart_cmd(update, context):
    if update.effective_user.id == OWNER_ID:
        await update.message.reply_text("🔄 restarting…"); os._exit(0)

async def on_error(update, context):
    log.error("error: %s", getattr(context, "error", "unknown"))

# ------------- Runner -------------
def main():
    init_db()
    run_health_server_threaded()  # سيرفر الصحة/الويبهوك على ثريد مستقل

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(on_startup)   # ← الإصلاح هنا: إضافتها قبل build()
        .build()
    )

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("vipinfo", vipinfo))
    app.add_handler(CommandHandler("refreshcmds", refresh_cmds))
    app.add_handler(CommandHandler("aidiag", aidiag))
    app.add_handler(CommandHandler("libdiag", libdiag))
    app.add_handler(CommandHandler("paylist", paylist))
    app.add_handler(CommandHandler("restart", restart_cmd))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guard_messages))

    app.add_error_handler(on_error)

    app.run_polling()

if __name__ == "__main__":
    main()

