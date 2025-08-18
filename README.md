# Telegram File Converter Bot

بوت تحويل ملفات يعمل على Render + python-telegram-bot v21.

## المدعوم
- DOC/DOCX/RTF/ODT/PPT/PPTX/XLS/XLSX → PDF (LibreOffice)
- PDF → DOCX (pdf2docx)
- صورة ↔ صورة (JPG/PNG/WEBP) + صورة → PDF (Pillow)
- PDF → صور PNG/JPG (ZIP) (pdf2image + poppler)
- صوت mp3/wav/ogg ↔ mp3/wav/ogg (FFmpeg)
- فيديو → MP4 (FFmpeg)

## النشر على Render (Native)
1) اربط المستودع.
2) ضع في **Build Command**:
   ```bash
   bash render-build.sh
