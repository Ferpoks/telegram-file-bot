FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

# أدوات النظام + خطوط (ليشتغل التحويل داخل الحاوية)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libreoffice ffmpeg poppler-utils ghostscript \
      fonts-noto fonts-noto-cjk fonts-dejavu && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app
EXPOSE 10000

CMD ["python", "bot.py"]
