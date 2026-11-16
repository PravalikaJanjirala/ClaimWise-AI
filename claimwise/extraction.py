"""Structured field extraction from OCR output.

Primary path: deterministic "Label: value" parsing with fuzzy label matching and per-field
confidence (OCR word confidence, reduced when the value fails format validation).

Optional path: an LLM fills ONLY fields the primary path could not read reliably. Its output is
validated with the same format rules, capped at a lower confidence, and clearly marked source="llm".
"""
from __future__ import annotations
import json
import logging
import re
from difflib import SequenceMatcher, get_close_matches

from . import config
from .normalize import clean_value
from .ocr import ocr_file, OcrLine
from .schemas import DOC_TITLES, FIELD_SPECS, Extraction, FieldValue

log = logging.getLogger(__name__)
LLM_CONF_CAP = 0.75


def _key(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _split_line(line: OcrLine):
    """Split an OCR line into (label, value text, value words) at the first colon."""
    for i, (w, c) in enumerate(line.words):
        if ":" in w:
            before, _, after = w.partition(":")
            label = " ".join([x for x, _ in line.words[:i]] + [before]).strip()
            val_words = ([(after, c)] if after else []) + line.words[i + 1:]
            return label, " ".join(x for x, _ in val_words), val_words
    return None


def _match_label(label: str, specs):
    cands = {_key(lab): (f, k) for f, lab, k in specs}
    m = get_close_matches(_key(label), list(cands), n=1, cutoff=0.82)
    return cands[m[0]] if m else None


def detect_doc_type(lines) -> str | None:
    head = [_key(l.text) for l in lines[:6]]
    best, best_r = None, 0.0
    for doc_type, title in DOC_TITLES.items():
        t = _key(title)
        for h in head:
            r = SequenceMatcher(None, h, t).ratio()
            if r > best_r:
                best, best_r = doc_type, r
    return best if best_r >= 0.75 else None


def extract_from_ocr(doc_type: str, ocr) -> Extraction:
    ex = Extraction(doc_type=doc_type, detected_type=detect_doc_type(ocr.lines),
                    mean_ocr_conf=ocr.mean_conf, engine=ocr.engine, text=ocr.text)
    if ex.detected_type and ex.detected_type != doc_type:
        ex.warnings.append(f"Document looks like '{ex.detected_type}' but was uploaded as '{doc_type}'.")
    elif ex.detected_type is None:
        ex.warnings.append("Document title not recognised; check that the right document was uploaded.")
    specs = FIELD_SPECS[doc_type]
    for line in ocr.lines:
        parsed = _split_line(line)
        if not parsed:
            continue
        label, value_text, words = parsed
        match = _match_label(label, specs)
        if not match or not value_text.strip():
            continue
        fname, kind = match
        cleaned, ok = clean_value(kind, value_text)
        conf = sum(c for _, c in words) / len(words) if words else 0.0
        conf = max(0.0, min(1.0, conf * (1.0 if ok else 0.6)))
        prev = ex.fields.get(fname)
        if prev is None or conf > prev.confidence:
            ex.fields[fname] = FieldValue(value=cleaned, raw=value_text.strip(), confidence=round(conf, 3),
                                          source="ocr", format_ok=ok)
    return ex


# ----------------------------------------------------------------------------- optional LLM fill
def _llm_call(doc_type: str, text: str, wanted: list[str]) -> dict:
    """Call the Anthropic Messages API directly over HTTPS (no SDK dependency)."""
    import os
    import requests
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {}
    prompt = (
        "Extract fields from this OCR text of a motor insurance document. "
        f"Document type: {doc_type}. Return ONLY a JSON object with exactly these keys: {wanted}. "
        "Use null when a field is absent or unreadable. Do not guess or invent values.\n\nOCR TEXT:\n" + text
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": config.LLM_MODEL, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json().get("content", []))
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {}


def llm_fill(ex: Extraction) -> Extraction:
    specs = FIELD_SPECS[ex.doc_type]
    wanted = [f for f, _, _ in specs if f not in ex.fields or ex.fields[f].confidence < 0.6]
    if not wanted:
        return ex
    try:
        got = _llm_call(ex.doc_type, ex.text, wanted)
    except Exception as e:  # network/API/JSON problems must never break the pipeline
        ex.warnings.append(f"LLM-assisted extraction skipped: {type(e).__name__}")
        return ex
    kinds = {f: k for f, _, k in specs}
    for fname in wanted:
        raw = got.get(fname)
        if raw in (None, ""):
            continue
        cleaned, ok = clean_value(kinds[fname], str(raw))
        if not ok:
            continue  # reject values that fail format validation
        ex.fields[fname] = FieldValue(value=cleaned, raw=str(raw), confidence=LLM_CONF_CAP, source="llm", format_ok=True)
    return ex


def extract(doc_type: str, path, use_llm: bool | None = None) -> Extraction:
    ex = extract_from_ocr(doc_type, ocr_file(path))
    if config.USE_LLM if use_llm is None else use_llm:
        ex = llm_fill(ex)
    return ex
