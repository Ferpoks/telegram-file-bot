# Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# أدوات التحويل
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-common libreoffice-writer libreoffice-calc libreoffice-impress \
    poppler-utils ghostscript ffmpeg \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN python -m pip install --upgrade pip && pip install -r requirements.txt

COPY . /app

# بيئة افتراضية لRender
ENV PORT=10000
CMD ["python", "bot.py"]
