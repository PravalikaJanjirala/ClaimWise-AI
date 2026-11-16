"""Normalisation and comparison helpers for extracted values."""
import re
from datetime import date, datetime
from difflib import SequenceMatcher

_TITLES = {"MR", "MRS", "MS", "SHRI", "SMT", "SRI", "DR", "KUM", "KUMARI", "MISS"}
_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%d-%B-%Y")
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})

VALIDATORS = {
    "reg": re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}$"),
    "dl": re.compile(r"^[A-Z]{2}\d{13}$"),
    "chassis": re.compile(r"^[A-Z0-9]{17}$"),
    "policy_no": re.compile(r"^[A-Z0-9\-/]{8,25}$"),
}


def norm_alnum(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def norm_name(s):
    s = re.sub(r"[^A-Za-z ]", " ", s or "").upper()
    return " ".join(t for t in s.split() if t not in _TITLES)


def name_similarity(a, b):
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    sa, sb = " ".join(sorted(na.split())), " ".join(sorted(nb.split()))
    return max(SequenceMatcher(None, na, nb).ratio(), SequenceMatcher(None, sa, sb).ratio())


def ocr_confusable_equal(a, b):
    """True when two IDs differ only by characters OCR commonly confuses (O/0, I/1, S/5, B/8...)."""
    a, b = norm_alnum(a), norm_alnum(b)
    return a != b and len(a) == len(b) and a.translate(_CONFUSABLE) == b.translate(_CONFUSABLE)


def parse_date(s):
    s = re.sub(r"\s+", " ", (s or "").strip().rstrip(".,;"))
    s = re.sub(r"\s*([-/.])\s*", r"\1", s)
    cands = [s]
    if not re.search(r"[A-Za-z]{3}", s):
        cands.append(s.translate(_DIGIT_FIX))
    for c in cands:
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(c, fmt).date()
            except ValueError:
                pass
    return None


def parse_amount(s):
    t = (s or "").upper()
    t = re.sub(r"RS\.?|INR|₹|/-|,|\s", "", t)
    if t and sum(ch.isdigit() for ch in t) >= len(t) * 0.6:
        t = t.translate(_DIGIT_FIX)
    try:
        return float(t)
    except ValueError:
        return None


def iso(d):
    return d.isoformat() if isinstance(d, date) else None


_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
_TO_ALPHA = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}


def _convert(s, types):
    out = []
    for ch, t in zip(s, types):
        if t == "D":
            ch = _TO_DIGIT.get(ch, ch)
            if not ch.isdigit():
                return None
        else:
            ch = _TO_ALPHA.get(ch, ch)
            if not ch.isalpha():
                return None
        out.append(ch)
    return "".join(out)


def repair_id(kind, s):
    """Format-aware OCR repair for structured IDs. Only changes characters that the field's known
    format makes unambiguous (digit positions vs letter positions). Returns the (possibly) repaired string."""
    if kind == "chassis":
        # The VIN standard never uses I, O or Q, so these can only be misread 1 / 0 / 0.
        return s.translate(str.maketrans({"O": "0", "I": "1", "Q": "0"}))
    if VALIDATORS.get(kind) and VALIDATORS[kind].match(s):
        return s
    if kind == "dl":
        r = _convert(s, "AA" + "D" * (len(s) - 2)) if len(s) == 15 else None
        return r if r and VALIDATORS["dl"].match(r) else s
    if kind == "reg":
        best = None
        for rto in (2, 1):
            for series in (3, 2, 1, 0):
                types = "AA" + "D" * rto + "A" * series + "DDDD"
                if len(types) == len(s):
                    r = _convert(s, types)
                    if r and VALIDATORS["reg"].match(r):
                        changes = sum(a != b for a, b in zip(r, s))
                        if best is None or changes < best[0]:
                            best = (changes, r)
        return best[1] if best else s
    return s


def clean_value(kind, raw):
    """Return (clean string, format_ok) for a raw OCR value."""
    raw = (raw or "").strip()
    if kind in ("reg", "dl", "chassis"):
        v = repair_id(kind, norm_alnum(raw))
        return v, bool(VALIDATORS[kind].match(v))
    if kind == "policy_no":
        v = re.sub(r"\s+", "", raw.upper())
        return v, bool(VALIDATORS["policy_no"].match(v))
    if kind == "date":
        d = parse_date(raw)
        return (iso(d) or raw), d is not None
    if kind == "amount":
        a = parse_amount(raw)
        return (f"{a:.2f}" if a is not None else raw), a is not None
    if kind == "name":
        letters = sum(ch.isalpha() for ch in raw)
        return re.sub(r"\s+", " ", raw), letters >= 3 and letters >= 0.8 * max(len(raw.replace(" ", "")), 1)
    return re.sub(r"\s+", " ", raw), bool(raw)
