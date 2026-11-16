"""OCR layer: Tesseract for images/scanned PDFs, text layer for digital PDFs."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from . import config


class OCRUnavailable(RuntimeError):
    """Raised when Tesseract is not installed."""


@dataclass
class OcrLine:
    text: str
    words: list  # [(word, confidence 0..1)]


@dataclass
class OcrResult:
    lines: list
    text: str
    mean_conf: float
    engine: str


def _prep(img: Image.Image) -> Image.Image:
    img = img.convert("L")
    if img.width < 1400:
        scale = 1400 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(img)


def _ocr_image(img: Image.Image) -> OcrResult:
    try:
        import pytesseract
    except ImportError as e:  # pragma: no cover
        raise OCRUnavailable("pytesseract is not installed (pip install pytesseract)") from e
    try:
        data = pytesseract.image_to_data(
            _prep(img), lang=config.OCR_LANG, config="--psm 6", output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractNotFoundError as e:
        raise OCRUnavailable("Tesseract binary not found. Install it (e.g. apt install tesseract-ocr).") from e
    groups: dict = {}
    for i, word in enumerate(data["text"]):
        word = word.strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append((word, conf / 100.0))
    lines = [OcrLine(" ".join(w for w, _ in ws), ws) for _, ws in sorted(groups.items())]
    confs = [c for ln in lines for _, c in ln.words]
    return OcrResult(lines, "\n".join(l.text for l in lines), sum(confs) / len(confs) if confs else 0.0, "tesseract")


def _pdf(path: Path) -> OcrResult:
    import pdfplumber
    with pdfplumber.open(str(path)) as pdf:
        pages = pdf.pages[:2]
        text = "\n".join((p.extract_text() or "") for p in pages)
        if len(text.strip()) >= 20:  # digital PDF with a text layer
            lines = [OcrLine(t, [(w, 0.99) for w in t.split()]) for t in text.splitlines() if t.strip()]
            return OcrResult(lines, "\n".join(l.text for l in lines), 0.99, "pdf-text-layer")
        img = pages[0].to_image(resolution=200).original  # scanned PDF: rasterise then OCR
    return _ocr_image(img)


def ocr_file(path) -> OcrResult:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf(path)
    with Image.open(path) as im:
        im.load()
        return _ocr_image(im)
