# Python 3.11 (متوافق مع PTB v20)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# تحديث النظام وتثبيت أدوات التحويل
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-common libreoffice-writer libreoffice-calc libreoffice-impress \
    poppler-utils ghostscript ffmpeg fonts-dejavu-core \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# المتطلبات
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

# نسخ الكود
COPY . /app

# Render يمرّر PORT تلقائياً. نضع افتراضي فقط.
ENV PORT=10000

# شغّل البوت
CMD ["python", "bot.py"]
