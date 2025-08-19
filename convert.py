# convert.py
# -*- coding: utf-8 -*-
import asyncio, shutil, re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from PIL import Image

SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.\- ]+")

DOC_EXTS = {"doc", "docx", "odt", "rtf"}
PPT_EXTS = {"ppt", "pptx", "odp"}
XLS_EXTS = {"xls", "xlsx", "ods"}
IMG_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "tiff"}
AUD_EXTS = {"mp3", "wav", "ogg", "m4a"}
VID_EXTS = {"mp4", "mov", "mkv", "avi", "webm"}
ALL_OFFICE = DOC_EXTS | PPT_EXTS | XLS_EXTS

def safe_name(name: str, fallback: str = "file") -> str:
    name = (name or "").strip() or fallback
    return SAFE_CHARS.sub("_", name)[:200]

def ext_of(filename: str | None) -> str:
    return Path(filename).suffix.lower().lstrip('.') if filename else ""

def which(*names: str) -> str | None:
    import shutil as _sh
    for n in names:
        p = _sh.which(n)
        if p: return p
    return None

async def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    import asyncio as _a, subprocess as _s
    proc = await _a.create_subprocess_exec(*cmd, stdout=_a.subprocess.PIPE, stderr=_a.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors='ignore'), err.decode(errors='ignore')

def detect_bins() -> dict:
    return {
        "soffice": which('soffice','libreoffice','lowriter'),
        "pdftoppm": which('pdftoppm'),
        "ffmpeg": which('ffmpeg'),
        "gs": which('gs','ghostscript'),
    }

def kind_for_extension(ext: str) -> str:
    if ext in IMG_EXTS: return 'image'
    if ext in AUD_EXTS: return 'audio'
    if ext in VID_EXTS: return 'video'
    if ext in ALL_OFFICE: return 'office'
    if ext == 'pdf': return 'pdf'
    return 'unknown'

async def office_to_pdf(in_path: Path, out_dir: Path, bins: dict) -> Path:
    if not bins.get("soffice"):
        raise RuntimeError('LibreOffice غير متوفر.')
    cmd = [bins["soffice"], '--headless','--nologo','--nofirststartwizard','--convert-to','pdf','--outdir', str(out_dir), str(in_path)]
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
    import asyncio as _a
    await _a.to_thread(_convert)
    return out_path

async def image_to_pdf(in_path: Path, out_dir: Path, dpi: int = 150) -> Path:
    out_path = out_dir / (in_path.stem + '.pdf')
    def _do():
        im = Image.open(in_path)
        if im.mode in ("RGBA","P"): im = im.convert("RGB")
        im.save(out_path, "PDF", resolution=float(dpi))
    import asyncio as _a
    await _a.to_thread(_do)
    return out_path

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
    import asyncio as _a
    await _a.to_thread(_do)
    return out_path

async def pdf_to_images_zip_parts(in_path: Path, out_dir: Path, fmt: str, limit_bytes: int, bins: dict) -> list[Path]:
    if not bins.get("pdftoppm"):
        raise RuntimeError('Poppler غير متوفر (pdftoppm).')
    from pdf2image import convert_from_path
    import asyncio as _a
    pages = await _a.to_thread(convert_from_path, str(in_path), dpi=150)
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
        if current and current_size + s > int(limit_bytes*0.95):
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

async def audio_convert_ffmpeg(in_path: Path, out_dir: Path, target_ext: str, bins: dict) -> Path:
    if not bins.get("ffmpeg"):
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
    cmd = [bins["ffmpeg"], '-y','-i',str(in_path), *args, str(out_path)]
    code, out, err = await run_cmd(cmd)
    if code != 0: raise RuntimeError(f"FFmpeg فشل: {err or out}")
    return out_path

async def video_to_mp4_ffmpeg(in_path: Path, out_dir: Path, bins: dict) -> Path:
    if not bins.get("ffmpeg"):
        raise RuntimeError('FFmpeg غير متوفر.')
    out_path = out_dir / (in_path.stem + '.mp4')
    cmd = [bins["ffmpeg"], '-y','-i',str(in_path),
           '-c:v','libx264','-preset','veryfast','-crf','23',
           '-c:a','aac','-b:a','128k', str(out_path)]
    code, out, err = await run_cmd(cmd)
    if code != 0: raise RuntimeError(f"FFmpeg فشل: {err or out}")
    return out_path

# ===== Shrinkers =====
async def shrink_pdf(in_path: Path, out_dir: Path, bins: dict) -> Path | None:
    if not bins.get("gs"): return None
    for preset in ('/ebook','/screen'):
        out = out_dir / (in_path.stem + f'.min.pdf')
        cmd = [bins["gs"], '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4',
               f'-dPDFSETTINGS={preset}', '-dNOPAUSE', '-dQUIET', '-dBATCH',
               f'-sOutputFile={str(out)}', str(in_path)]
        code, _, _ = await run_cmd(cmd)
        if code==0 and out.exists() and out.stat().st_size < in_path.stat().st_size:
            return out
    return None

async def shrink_video(in_path: Path, out_dir: Path, bins: dict, limit_bytes: int) -> Path | None:
    if not bins.get("ffmpeg"): return None
    trials = [
        ['-vf','scale=\'min(1280,iw)\':-2','-c:v','libx264','-preset','veryfast','-crf','28','-c:a','aac','-b:a','96k'],
        ['-vf','scale=\'min(854,iw)\':-2','-c:v','libx264','-preset','veryfast','-crf','30','-c:a','aac','-b:a','96k'],
    ]
    src = in_path
    for i, args in enumerate(trials,1):
        out = out_dir / (in_path.stem + f'.r{i}.mp4')
        code, _, _ = await run_cmd([bins["ffmpeg"],'-y','-i',str(src), *args, str(out)])
        if code==0 and out.exists():
            src = out
            if out.stat().st_size <= limit_bytes: return out
    if src!=in_path and src.stat().st_size <= limit_bytes:
        return src
    return None

async def shrink_image(in_path: Path, out_dir: Path, ext: str, limit_bytes: int) -> Path | None:
    for max_side, q in [(2000,85),(1400,75)]:
        out = await image_to_image(in_path, out_dir, target_ext=ext, max_side=max_side, quality=q)
        if out.stat().st_size <= limit_bytes: return out
    return None

async def shrink_audio(in_path: Path, out_dir: Path, ext: str, bins: dict, limit_bytes: int) -> Path | None:
    if not bins.get("ffmpeg"): return None
    out = out_dir / (in_path.stem + ('.mp3' if ext=='wav' else f'.{ext}'))
    if ext=='mp3': args = ['-vn','-c:a','libmp3lame','-q:a','5']
    elif ext=='ogg': args = ['-vn','-c:a','libvorbis','-q:a','3']
    elif ext=='wav': args = ['-vn','-c:a','libmp3lame','-q:a','5']
    else: return None
    code, _, _ = await run_cmd([which('ffmpeg') or 'ffmpeg','-y','-i',str(in_path), *args, str(out)])
    return out if code==0 and out.exists() and out.stat().st_size <= limit_bytes else None

# ===== واجهة رئيسية للوركر =====
async def convert_with_limits(kind: str, choice: str, in_path: Path, workdir: Path, limit_mb: int, bins: dict) -> list[Path]:
    limit_bytes = limit_mb * 1024 * 1024
    out_paths: list[Path] = []

    if kind == 'office' and choice == 'PDF':
        out_paths = [await office_to_pdf(in_path, workdir, bins)]
    elif kind == 'pdf' and choice == 'DOCX':
        out_paths = [await pdf_to_docx(in_path, workdir)]
    elif kind == 'pdf' and choice == 'PNGZIP':
        out_paths = await pdf_to_images_zip_parts(in_path, workdir, fmt='png', limit_bytes=limit_bytes, bins=bins)
    elif kind == 'pdf' and choice == 'JPGZIP':
        out_paths = await pdf_to_images_zip_parts(in_path, workdir, fmt='jpg', limit_bytes=limit_bytes, bins=bins)
    elif kind == 'image' and choice == 'PDF':
        out_paths = [await image_to_pdf(in_path, workdir)]
    elif kind == 'image' and choice in {'JPG','PNG','WEBP'}:
        out_paths = [await image_to_image(in_path, workdir, target_ext=choice.lower())]
    elif kind == 'audio' and choice in {'MP3','WAV','OGG'}:
        out_paths = [await audio_convert_ffmpeg(in_path, workdir, target_ext=choice.lower(), bins=bins)]
    elif kind == 'video' and choice == 'MP4':
        out_paths = [await video_to_mp4_ffmpeg(in_path, workdir, bins)]
    else:
        raise RuntimeError('تحويل غير مدعوم.')

    fixed: list[Path] = []
    for p in out_paths:
        if p.stat().st_size <= limit_bytes:
            fixed.append(p); continue
        # محاولة تخفيض
        if p.suffix.lower()=='.pdf':
            shr = await shrink_pdf(p, workdir, bins)
            if shr and shr.stat().st_size <= limit_bytes: fixed.append(shr)
        elif p.suffix.lower()=='.mp4':
            shr = await shrink_video(p, workdir, bins, limit_bytes)
            if shr and shr.stat().st_size <= limit_bytes: fixed.append(shr)
        elif p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
            shr = await shrink_image(p, workdir, p.suffix.lstrip('.'), limit_bytes)
            if shr and shr.stat().st_size <= limit_bytes: fixed.append(shr)
        elif p.suffix.lower() in {'.mp3','.wav','.ogg'}:
            shr = await shrink_audio(p, workdir, p.suffix.lstrip('.'), bins, limit_bytes)
            if shr and shr.stat().st_size <= limit_bytes: fixed.append(shr)

    return [p for p in (fixed or out_paths) if p.stat().st_size <= limit_bytes]
