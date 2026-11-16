"""Getting text out of a PDF or image: embedded text first, OCR (Tesseract) when there is none."""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

log = logging.getLogger(__name__)
MIN_TEXT_CHARS = 80  # below this a PDF is treated as a scan


class TextResult:
    def __init__(self, text: str, method: str):
        self.text = text
        self.method = method  # "pdf" or "ocr" or "none"


def pdf_text(data: bytes, max_pages: int = 10) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:max_pages])
    except Exception:  # damaged/encrypted PDF
        log.info("PDF text extraction failed", exc_info=True)
        return ""


def _ocr_langs() -> str:
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return "eng"
    langs = [x.strip() for x in out.splitlines()[1:]]
    return "+".join(x for x in ("deu", "eng") if x in langs) or "eng"


def prepare_photo(data: bytes, longest: int = 3000) -> bytes:
    """Phone photos: turn upright (EXIF), grayscale, sensible size, more contrast."""
    from io import BytesIO

    from PIL import Image, ImageOps
    try:
        im = Image.open(BytesIO(data))
        im = ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001  not an image Pillow knows: let Tesseract try the original
        return data
    im = ImageOps.autocontrast(im.convert("L"), cutoff=1)
    scale = longest / max(im.size)
    if scale < 1 or max(im.size) < 1500:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    out = BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def ocr(data: bytes, is_pdf: bool, max_pages: int = 5) -> str:
    """OCR with Tesseract. PDFs are rasterised with pdftoppm (300 dpi) first."""
    if not shutil.which("tesseract"):
        log.warning("tesseract not installed, OCR skipped")
        return ""
    langs = _ocr_langs()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if is_pdf:
            src = tmpdir / "in.pdf"
            src.write_bytes(data)
            subprocess.run(["pdftoppm", "-r", "300", "-gray", "-png", "-l", str(max_pages), str(src),
                            str(tmpdir / "page")], check=True, timeout=180, capture_output=True)
            images = sorted(tmpdir.glob("page*.png"))
        else:
            img = tmpdir / "in.png"
            img.write_bytes(prepare_photo(data))
            images = [img]
        parts = []
        for image in images:
            res = subprocess.run(["tesseract", str(image), "-", "-l", langs, "--psm", "4"],
                                 capture_output=True, text=True, timeout=180)
            parts.append(res.stdout)
        return "\n".join(parts)


def get_text(data: bytes, is_pdf: bool) -> TextResult:
    if is_pdf:
        text = pdf_text(data)
        if len(text.strip()) >= MIN_TEXT_CHARS:
            return TextResult(text, "pdf")
    try:
        text = ocr(data, is_pdf)
    except (OSError, subprocess.SubprocessError):
        log.warning("OCR failed", exc_info=True)
        text = ""
    return TextResult(text, "ocr" if text.strip() else "none")
