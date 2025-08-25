#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import io
import sys
import json
import math
import time
import uuid
import asyncio
import logging
import tempfile
import shutil
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile, BotCommand, ChatMember, Chat, MessageEntity,
)
from telegram.ext import (
    ApplicationBuilder, Application, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    filters,
)

# =========================
# إعداد السجلات
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("convbot")

# =========================
# متغيرات البيئة
# =========================
BOT_TOKEN        = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USERNAME   = os.getenv("ADMIN_USERNAME", "").lstrip("@")
OWNER_ID         = int(os.getenv("OWNER_ID", "0") or 0)
SUB_CHANNEL      = os.getenv("SUB_CHANNEL", "").strip()  # مثال: @ferpokss أو رابط t.me/...
PUBLIC_URL       = os.getenv("PUBLIC_URL", "-").strip()

# حدود/تزامن (لو ما وجدت، تعاريف آمنة)
CONC_IMAGE  = max(1, int(os.getenv("CONC_IMAGE",  "3")))
CONC_MEDIA  = max(1, int(os.getenv("CONC_MEDIA",  "2")))
CONC_OFFICE = max(1, int(os.getenv("CONC_OFFICE", "2")))
CONC_PDF    = max(1, int(os.getenv("CONC_PDF",    "3")))

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    log.error("❌ BOT_TOKEN غير موجود في المتغيرات.")
    sys.exit(1)

# =========================
# أدوات مساعدة للنظام
# =========================
async def run_cmd(cmd: list, cwd: Optional[str] = None, env: Optional[dict] = None) -> Tuple[int, bytes, bytes]:
    """
    تشغيل أمر نظامي وإرجاع (code, stdout, stderr)
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env or os.environ.copy()
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


def find_bin(*names) -> Optional[str]:
    """
    العثور على مسار تنفيذ إحدى الأدوات بالترتيب
    """
    for name in names:
        for p in os.environ.get("PATH", "").split(os.pathsep):
            cand = os.path.join(p, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


# =========================
# ضغط الملفات (محسّن)
# =========================
def _map_quality_to_gs_params(percent: int) -> Tuple[int, int]:
    """
    تحويل نسبة الضغط (10..90) إلى dpis و JPEGQ مناسبة.
    كلما زادت النسبة = جودة أعلى/ضغط أقل.
    """
    p = max(10, min(90, int(percent)))
    dpi = int(72 + (300 - 72) * (p / 90.0))     # 10%≈72dpi .. 90%≈300dpi
    jpeg_q = int(40 + (85 - 40) * (p / 90.0))   # 10%≈40 .. 90%≈85
    return dpi, jpeg_q


async def compress_pdf(in_path: str, percent: int, workdir: str) -> str:
    """
    ضغط PDF عبر Ghostscript بدون تحويل الصفحات لصور.
    يحافظ على المتجهات والخطوط، ويضغط الصور داخل الملف فقط.
    """
    gs = find_bin("gs", "ghostscript")
    if not gs:
        raise FileNotFoundError("لم يتم العثور على ghostscript (gs) في الخادم.")

    dpi, jpeg_q = _map_quality_to_gs_params(percent)
    base = os.path.splitext(os.path.basename(in_path))[0]
    out_path = os.path.join(workdir, f"{base}_compressed_{percent}.pdf")

    cmd = [
        gs, "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.6",
        "-dNOPAUSE", "-dQUIET", "-dBATCH",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dFastWebView=true",

        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",

        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",

        f"-dColorImageResolution={dpi}",
        f"-dGrayImageResolution={dpi}",
        f"-dMonoImageResolution={dpi}",

        "-dEncodeColorImages=true",
        "-dAutoFilterColorImages=false",
        "-dColorImageFilter=/DCTEncode",
        f"-dJPEGQ={jpeg_q}",

        "-dEncodeGrayImages=true",
        "-dAutoFilterGrayImages=false",
        "-dGrayImageFilter=/DCTEncode",

        "-dUseFastColor=true",
        "-dColorConversionStrategy=/LeaveColorUnchanged",

        "-sOutputFile=" + out_path,
        in_path,
    ]
    code, out, err = await run_cmd(cmd)
    if code != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"فشل ضغط PDF (gs)، كود={code}\n{(err or b'').decode('utf-8','ignore')}")
    return out_path


def _jpeg_quality_from_percent(percent: int) -> int:
    p = max(10, min(90, int(percent)))
    return int(35 + (90 - 35) * (p / 90.0))  # 10%≈35 .. 90%≈90


async def compress_image(in_path: str, percent: int, workdir: str) -> str:
    """
    ضغط الصور: JPEG يخفض quality، PNG كمّ ألوان + optimize.
    """
    ext = os.path.splitext(in_path)[1].lower()
    base = os.path.splitext(os.path.basename(in_path))[0]
    out_path = os.path.join(workdir, f"{base}_compressed_{percent}{ext}")

    with Image.open(in_path) as im:
        fmt = im.format
        if fmt == "JPEG" or ext in (".jpg", ".jpeg"):
            q = _jpeg_quality_from_percent(percent)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out_path, "JPEG", quality=q, optimize=True, progressive=True)
            return out_path

        if fmt == "PNG" or ext == ".png":
            colors = max(32, int(256 * (percent / 100.0)))
            if im.mode not in ("RGB", "L", "P"):
                im = im.convert("RGBA")
            try:
                im8 = im.convert("P", palette=Image.ADAPTIVE, colors=colors)
                im8.save(out_path, "PNG", optimize=True, compress_level=9)
                if os.path.getsize(out_path) >= os.path.getsize(in_path):
                    im.save(out_path, "PNG", optimize=True, compress_level=9)
            except Exception:
                im.save(out_path, "PNG", optimize=True, compress_level=9)
            return out_path

        # صيغ أخرى: ننسخ كما هي (لن نحاول ضغط غير مدعوم كي لا يزيد الحجم)
        shutil.copy2(in_path, out_path)
        return out_path


async def compress_any(in_path: str, percent: int, workdir: str) -> str:
    ext = os.path.splitext(in_path)[1].lower()
    if ext == ".pdf":
        return await compress_pdf(in_path, percent, workdir)
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return await compress_image(in_path, percent, workdir)
    # غير ذلك: نعيد الملف كما هو (لمنع زيادة الحجم)
    base = os.path.splitext(os.path.basename(in_path))[0]
    out_path = os.path.join(workdir, f"{base}_compressed_{percent}{ext}")
    shutil.copy2(in_path, out_path)
    return out_path


# =========================
# أدوات تليجرام/قناة
# =========================
@dataclass
class SubChannelState:
    username: Optional[str] = None  # مثل @ferpokss
    chat_id: Optional[int] = None   # id سلبي للقنوات

SUB_STATE = SubChannelState()

async def resolve_sub_channel(bot) -> None:
    """
    نحاول تحويل SUB_CHANNEL (@username أو رابط) إلى chat_id كي نستخدمه في getChatMember بثبات.
    """
    if not SUB_CHANNEL:
        return
    uname = SUB_CHANNEL
    if uname.startswith("https://t.me/"):
        uname = "@" + uname.rsplit("/", 1)[-1]
    if not uname.startswith("@"):
        uname = "@" + uname
    try:
        chat: Chat = await bot.get_chat(uname)
        SUB_STATE.username = uname
        SUB_STATE.chat_id = chat.id
        log.info("[sub] channel resolved: %s (id=%s)", uname, chat.id)
    except Exception as e:
        log.warning("تعذر حل القناة %s: %s", SUB_CHANNEL, e)


async def ensure_joined(user_id: int, bot) -> bool:
    """
    التحقق من عضوية المستخدم في القناة (إن تم تحديدها).
    """
    if not SUB_STATE.chat_id:
        # لا توجد قناة مطلوبة
        return True
    try:
        member: ChatMember = await bot.get_chat_member(SUB_STATE.chat_id, user_id)
        st = member.status
        if st in ("left", "kicked"):
            return False
        return True
    except Exception as e:
        log.warning("ensure_joined error: %s", e)
        return False


# =========================
# لوحات / نصوص
# =========================
def start_keyboard():
    kb = [
        [
            InlineKeyboardButton("🇸🇦 عربي", callback_data="lang_ar"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        ]
    ]
    return InlineKeyboardMarkup(kb)


def compress_ask_kb():
    kb = [
        [InlineKeyboardButton("10%", callback_data="cmp_10"),
         InlineKeyboardButton("20%", callback_data="cmp_20"),
         InlineKeyboardButton("30%", callback_data="cmp_30")],
        [InlineKeyboardButton("50%", callback_data="cmp_50"),
         InlineKeyboardButton("70%", callback_data="cmp_70"),
         InlineKeyboardButton("90%", callback_data="cmp_90")],
    ]
    return InlineKeyboardMarkup(kb)


START_AR = (
    "أهلًا! 👋\n"
    "اختر لغتك ثم أرسل ملفًا، وبعدها اضغط **ضغط** واختر النسبة."
)
START_EN = (
    "Hi! 👋\n"
    "Pick your language, send a file, then choose **Compress** and a quality level."
)

HELP_AR = lambda: (
    "ℹ️ **طريقة الاستخدام**\n"
    "1) أرسل ملف PDF/صورة…\n"
    "2) اضغط زر **ضغط** واختر نسبة.\n"
    f"\nللتواصل مع الإدارة: @{ADMIN_USERNAME}" if ADMIN_USERNAME else ""
)
HELP_EN = lambda: (
    "ℹ️ **How to use**\n"
    "1) Send a PDF/image…\n"
    "2) Tap **Compress** and pick a level.\n"
    f"\nContact admin: @{ADMIN_USERNAME}" if ADMIN_USERNAME else ""
)


# =========================
# Health server (HTTPServer)
# =========================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        # اسكات لوجات HTTPServer
        return


def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), _HealthHandler)
        log.info("[health] serving on 0.0.0.0:%d", PORT)
        server.serve_forever()
    except Exception as e:
        log.warning("health server error: %s", e)


# =========================
# Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "اختر اللغة / Choose language:", reply_markup=start_keyboard()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ar")
    text = HELP_AR() if lang == "ar" else HELP_EN()
    await update.effective_message.reply_markdown(text)


async def on_lang_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    if q.data == "lang_ar":
        context.user_data["lang"] = "ar"
        await q.edit_message_text(START_AR)
    else:
        context.user_data["lang"] = "en"
        await q.edit_message_text(START_EN)


# نخزن آخر ملف للمستخدم كي نضغطه بعد اختيار النسبة
async def _remember_last_file(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, file_name: str):
    context.user_data["last_file"] = {"id": file_id, "name": file_name}
    # نعرض أزرار الضغط
    await update.effective_message.reply_text(
        "اختر نسبة الضغط:", reply_markup=compress_ask_kb()
    )

# استقبال المستندات
async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update.effective_user.id, context.bot):
        if SUB_STATE.username:
            await update.effective_message.reply_text(
                f"رجاءً اشترك بالقناة أولًا: {SUB_STATE.username}"
            )
        else:
            await update.effective_message.reply_text("الخدمة تتطلب الاشتراك بالقناة.")
        return
    doc = update.effective_message.document
    if not doc:
        return
    await _remember_last_file(update, context, doc.file_id, doc.file_name or "file.bin")

# الصور
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update.effective_user.id, context.bot):
        if SUB_STATE.username:
            await update.effective_message.reply_text(
                f"رجاءً اشترك بالقناة أولًا: {SUB_STATE.username}"
            )
        else:
            await update.effective_message.reply_text("الخدمة تتطلب الاشتراك بالقناة.")
        return
    photo = update.effective_message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    # نعطي اسم افتراضي
    await _remember_last_file(update, context, photo.file_id, f"photo_{photo.file_id}.jpg")

# فيديو/صوت (نخزن فقط – الضغط العام لن يغيرها الآن)
async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update.effective_user.id, context.bot):
        if SUB_STATE.username:
            await update.effective_message.reply_text(
                f"رجاءً اشترك بالقناة أولًا: {SUB_STATE.username}"
            )
        else:
            await update.effective_message.reply_text("الخدمة تتطلب الاشتراك بالقناة.")
        return
    v = update.effective_message.video
    if not v:
        return
    await _remember_last_file(update, context, v.file_id, v.file_name or f"video_{v.file_id}.mp4")

async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined(update.effective_user.id, context.bot):
        if SUB_STATE.username:
            await update.effective_message.reply_text(
                f"رجاءً اشترك بالقناة أولًا: {SUB_STATE.username}"
            )
        else:
            await update.effective_message.reply_text("الخدمة تتطلب الاشتراك بالقناة.")
        return
    a = update.effective_message.audio
    if not a:
        return
    await _remember_last_file(update, context, a.file_id, a.file_name or f"audio_{a.file_id}.mp3")


# تنفيذ الضغط بعد اختيار النسبة
async def on_compress_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    if not q.data.startswith("cmp_"):
        return
    try:
        percent = int(q.data.split("_", 1)[1])
    except Exception:
        await q.edit_message_text("نسبة غير صحيحة.")
        return

    meta = context.user_data.get("last_file")
    if not meta:
        await q.edit_message_text("أرسل ملفًا أولًا ثم اختر نسبة الضغط.")
        return

    # تنزيل الملف
    file = await context.bot.get_file(meta["id"])
    with tempfile.TemporaryDirectory(prefix="convbot_") as workdir:
        in_path = os.path.join(workdir, meta["name"])
        await file.download_to_drive(in_path)

        try:
            await q.edit_message_text("⏳ يتم التنفيذ، انتظر من فضلك…")
            out_path = await compress_any(in_path, percent, workdir)

            # إحصاء قبل/بعد
            try:
                before = os.path.getsize(in_path)
                after  = os.path.getsize(out_path)
            except Exception:
                before = after = 0

            cap = f"تم الإرسال ✅"
            if before and after:
                delta = after - before
                sign = "⬇️" if delta < 0 else "⬆️"
                ratio = (1 - (after / before)) * 100 if before else 0
                cap = f"{sign} الحجم: قبل {before//1024}KB → بعد {after//1024}KB (تغيير {ratio:.1f}%)"

            await context.bot.send_document(
                chat_id=q.message.chat_id,
                document=InputFile(out_path),
                caption=cap
            )
            await q.edit_message_text("تم التنفيذ ✅")
        except Exception as e:
            log.exception("compress error")
            await q.edit_message_text(f"❌ فشل الضغط: {e}")


# =========================
# إنشاء التطبيق وتسجيل الأوامر
# =========================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # أوامر للمستخدم
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))

    # اختيار لغة
    app.add_handler(CallbackQueryHandler(on_lang_choice, pattern=r"^lang_"))
    # اختيار نسبة ضغط
    app.add_handler(CallbackQueryHandler(on_compress_choice, pattern=r"^cmp_\d+$"))

    # استقبال أنواع الملفات (نتعامل معها بنفس الآلية)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO,        on_photo))
    app.add_handler(MessageHandler(filters.VIDEO,        on_video))
    app.add_handler(MessageHandler(filters.AUDIO,        on_audio))

    return app


async def _set_bot_commands(app: Application):
    cmds = [
        BotCommand("start", "ابدأ | Start"),
        BotCommand("help",  "مساعدة | Help"),
    ]
    try:
        await app.bot.set_my_commands(cmds, language_code="ar")
        await app.bot.set_my_commands(cmds, language_code="en")
        await app.bot.set_my_commands(cmds)
    except Exception as e:
        log.warning("set_my_commands: %s", e)


def main():
    log.info("PTB version at runtime: 22.x")
    log.info("CONFIG: MODE=polling PUBLIC_URL=%s PORT=%s", PUBLIC_URL, PORT)

    # Health server على منفذ Render
    threading.Thread(target=start_health_server, daemon=True).start()

    app = build_app()

    async def _startup():
        await resolve_sub_channel(app.bot)
        await _set_bot_commands(app)

    app.post_init = _startup  # سيُستدعى بعد init تلقائيًا

    # تشغيل Polling (يدير الحدث بنفسه)
    app.run_polling(drop_pending_updates=True)

# ============== ENTRY ==============
if __name__ == "__main__":
    main()



