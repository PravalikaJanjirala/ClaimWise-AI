"""Photo/scan quality check, used (a) at upload time to tell the customer to retake a bad photo
and (b) inside the pipeline to record the result for the officer.

Checks: blur (Laplacian variance), brightness, contrast, resolution. Pure Pillow + NumPy, no OCR, so it is fast.
Thresholds were calibrated on synthetic documents; tune them on real phone photos before relying on them
(see README, "Image quality check").
"""
from __future__ import annotations
import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

BLUR_MIN_LAPVAR = 100.0     # below: blurry (sharp scans ~1400-2000; readable noisy scans ~280-400)
DARK_MAX_MEAN = 90.0        # mean grey level below: too dark
WASHED_MIN_MEAN = 250.0     # mean above (with almost no ink): washed out / glare
LOW_CONTRAST_GAP = 70.0     # paper level (median) minus ink level (2nd percentile) below: faded / low contrast
                            # (sharp scans ~225-235, readable noisy scans ~85-110, heavily faded <= ~70)
MIN_LONG_SIDE = 900         # pixels
ANALYSIS_WIDTH = 1000

TIPS = {
    "blurry": "hold the phone steady, tap the screen to focus, and retake the photo",
    "dark": "move to a brighter spot and avoid shadows over the document",
    "washed_out": "avoid flash or glare and photograph the document flat",
    "low_contrast": "use even lighting and place the document on a plain dark surface",
    "low_resolution": "use your camera's normal or high quality setting and fill the frame with the document",
    "unreadable_file": "upload the file again; it could not be opened as an image",
}
WORDS = {"blurry": "blurry", "dark": "too dark", "washed_out": "washed out or overexposed",
         "low_contrast": "faded or low contrast", "low_resolution": "too small / low resolution",
         "unreadable_file": "not a readable image"}


@dataclass
class QualityResult:
    ok: bool
    issues: list = field(default_factory=list)   # [{"code","message","tip"}]
    metrics: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return ", ".join(i["message"] for i in self.issues)

    @property
    def advice(self) -> str:
        return "; ".join(dict.fromkeys(i["tip"] for i in self.issues))


def _issue(code):
    return {"code": code, "message": WORDS[code], "tip": TIPS[code]}


def analyse_image(img: Image.Image) -> QualityResult:
    w, h = img.size
    g = img.convert("L")
    if g.width != ANALYSIS_WIDTH:
        g = g.resize((ANALYSIS_WIDTH, max(1, int(g.height * ANALYSIS_WIDTH / g.width))), Image.LANCZOS)
    a = np.asarray(g, dtype=float)
    lap = -4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    lapvar, mean = float(lap.var()), float(a.mean())
    p5, gap = float(np.percentile(a, 5)), float(np.median(a) - np.percentile(a, 2))
    metrics = {"sharpness": round(lapvar, 1), "mean_brightness": round(mean, 1),
               "ink_paper_gap": round(gap, 1), "width": w, "height": h}
    codes = []
    if max(w, h) < MIN_LONG_SIDE:
        codes.append("low_resolution")
    if mean < DARK_MAX_MEAN:
        codes.append("dark")
    elif mean > WASHED_MIN_MEAN and p5 > 235:
        codes.append("washed_out")
    elif gap < LOW_CONTRAST_GAP:
        codes.append("low_contrast")
    if lapvar < BLUR_MIN_LAPVAR and "dark" not in codes and "low_contrast" not in codes:
        codes.append("blurry")   # a dark or faded photo also has low sharpness; report the root cause
    return QualityResult(ok=not codes, issues=[_issue(c) for c in codes], metrics=metrics)


def check_image_quality(source, filename: str | None = None) -> QualityResult:
    """source: path, or raw bytes (then pass filename). PDFs are skipped (digital PDFs have a text layer)."""
    name = (filename or (str(source) if not isinstance(source, (bytes, bytearray)) else "")).lower()
    if name.endswith(".pdf"):
        return QualityResult(ok=True, metrics={"skipped": "pdf"})
    try:
        data = bytes(source) if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            return analyse_image(im.convert("RGB"))
    except (UnidentifiedImageError, OSError, ValueError):
        return QualityResult(ok=False, issues=[_issue("unreadable_file")])
