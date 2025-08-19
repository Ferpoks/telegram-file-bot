# tasks.py
# -*- coding: utf-8 -*-
import os, json, tempfile, asyncio
from pathlib import Path

from telegram import Bot, InputFile
from convert import convert_with_limits, detect_bins, safe_name

# مفاتيح إحصاءات في Redis (اختياري لو احتجت ترقيتها)
STAT_OK_KEY = "stats:ok"
STAT_FAIL_KEY = "stats:fail"
STAT_BIN_KEY = "stats:bytes_out"

async def _do(spec: dict):
    bot = Bot(token=os.environ["BOT_TOKEN"])  # PTB v21 async bot
    bins = detect_bins()
    limit_mb = int(spec.get("limit_mb", os.getenv("TG_LIMIT_MB", "49")))
    chat_id = spec["chat_id"]
    reply_to = spec.get("reply_to_message_id")
    file_id = spec["file_id"]
    file_name = spec.get("file_name", "file")
    kind = spec["kind"]
    choice = spec["choice"]

    with tempfile.TemporaryDirectory(prefix="convjob_") as tmpd:
        tmp = Path(tmpd)
        in_path = tmp / safe_name(file_name)
        # نزّل الملف من تيليجرام
        tgfile = await bot.get_file(file_id)
        await tgfile.download_to_drive(str(in_path))

        # نفّذ التحويل
        outs = await convert_with_limits(kind, choice, in_path, tmp, limit_mb=limit_mb, bins=bins)

        if not outs:
            await bot.send_message(chat_id=chat_id, text=f"❌ الناتج أكبر من حد تيليجرام ({limit_mb}MB).", reply_to_message_id=reply_to)
            return

        # أرسل النتائج (قد تكون عدة أجزاء)
        for idx, p in enumerate(outs, 1):
            cap = '✔️ تم التحويل' + (f' (جزء {idx}/{len(outs)})' if len(outs)>1 else '')
            with open(p, 'rb') as fh:
                await bot.send_document(chat_id=chat_id, document=InputFile(fh, filename=p.name),
                                        caption=cap, reply_to_message_id=reply_to)

def process_job(spec: dict):
    """واجهة متزامنة لـ RQ؛ تشغّل اللوب وتنفّذ المهمة."""
    return asyncio.run(_do(spec))
