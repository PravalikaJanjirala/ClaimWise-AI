"""Synthetic motor-claim generator with planted mismatches and ground-truth labels.

All data is fictional. Every rendered page carries a visible "SYNTHETIC SAMPLE" footer.
"""
from __future__ import annotations
import copy
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .schemas import DOC_TITLES, FIELD_SPECS

FIRST = ["Ramesh", "Sita", "Anil", "Priya", "Kiran", "Lakshmi", "Suresh", "Anita", "Vikram", "Meena", "Arjun", "Divya",
         "Rahul", "Sneha", "Mohan", "Kavya", "Naveen", "Pooja", "Harish", "Swathi"]
LAST = ["Reddy", "Sharma", "Kumar", "Rao", "Naidu", "Patel", "Singh", "Iyer", "Gupta", "Verma", "Chowdary", "Nair", "Joshi", "Das"]
MAKES = ["Maruti Swift VXI", "Hyundai i20 Sportz", "Tata Nexon XZ", "Honda City ZX", "Kia Seltos HTK", "Mahindra XUV300 W6"]
PLACES = ["Madhapur, Hyderabad", "Gachibowli, Hyderabad", "Secunderabad", "Kukatpally, Hyderabad", "LB Nagar, Hyderabad",
          "Uppal, Hyderabad", "Banjara Hills, Hyderabad"]
STATES = ["TS", "AP", "KA", "MH", "TN"]
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
VIN = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"

EXPECT = {
    "clean": (set(), "FAST_LANE"),
    "reg_mismatch": ({"REG_CLAIM_RC", "REG_CLAIM_POLICY"}, "EXCEPTION_QUEUE"),
    "name_mismatch": ({"NAME_CLAIM_RC", "NAME_CLAIM_POLICY", "NAME_CLAIM_DL"}, "EXCEPTION_QUEUE"),
    "chassis_mismatch": ({"CHASSIS_RC_POLICY", "CARRIER_RECORD"}, "EXCEPTION_QUEUE"),
    "policy_expired": ({"POLICY_PERIOD"}, "MANUAL_REVIEW"),
    "amount_exceeds_idv": ({"AMOUNT_VS_IDV"}, "MANUAL_REVIEW"),
    "estimate_diff": ({"ESTIMATE_VS_CLAIM"}, "EXCEPTION_QUEUE"),
    "dl_expired": ({"DL_VALIDITY"}, "MANUAL_REVIEW"),
    "missing_doc": ({"DOC_MISSING_DRIVING_LICENCE"}, "MANUAL_REVIEW"),
    "duplicate_file": ({"DUP_FILE", "SIMILAR_CLAIM"}, "MANUAL_REVIEW"),
    "similar_claim": ({"SIMILAR_CLAIM"}, "EXCEPTION_QUEUE"),
}
CYCLE = ["reg_mismatch", "name_mismatch", "chassis_mismatch", "policy_expired", "amount_exceeds_idv",
         "estimate_diff", "dl_expired", "missing_doc", "duplicate_file", "similar_claim"]


@dataclass
class Case:
    case_id: str
    mutation: str
    quality: str
    truth: dict                       # doc_type -> {field: canonical value}
    docs: dict                        # doc_type -> (filename, bytes)
    expected_flags: set
    expected_lane: str
    carrier: dict = field(default_factory=dict)
    base: str | None = None


# ----------------------------------------------------------------------------- rendering
def _font(bold: bool, size: int):
    names = (["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_doc(doc_type: str, lines: list, footer: str) -> Image.Image:
    w, row_h, top = 1240, 74, 190
    img = Image.new("RGB", (w, top + row_h * len(lines) + 120), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 120], fill=(226, 232, 245))
    d.text((60, 36), DOC_TITLES[doc_type], font=_font(True, 44), fill=(20, 30, 70))
    y = top
    for label, value in lines:
        lf, vf = _font(True, 32), _font(False, 32)
        d.text((60, y), f"{label}:", font=lf, fill=(20, 20, 20))
        d.text((60 + d.textlength(f"{label}: ", font=lf), y), str(value), font=vf, fill=(0, 0, 0))
        y += row_h
    d.line([60, img.height - 85, w - 60, img.height - 85], fill=(150, 150, 150), width=2)
    d.text((60, img.height - 70), footer, font=_font(False, 22), fill=(110, 110, 110))
    return img


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    img = img.rotate(rng.uniform(-1.2, 1.2), resample=Image.BICUBIC, fillcolor="white", expand=False)
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.7, 1.1)))
    noise = Image.effect_noise(img.size, 14).convert("RGB")
    return ImageChops.add(img, noise, scale=2.0, offset=-64)


def _encode(img: Image.Image, quality: str):
    buf = io.BytesIO()
    if quality == "degraded":
        img.save(buf, "JPEG", quality=70)
        return ".jpg", buf.getvalue()
    img.save(buf, "PNG", optimize=True)
    return ".png", buf.getvalue()


# ----------------------------------------------------------------------------- truth
def _name(rng):
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def _fmt_date(d: date, rng):
    return rng.choice([d.strftime("%d-%m-%Y"), d.strftime("%d/%m/%Y"), d.strftime("%d-%b-%Y")])


def _fmt_amt(a):
    return f"Rs. {a:,.0f}/-"


def _alter_digits(reg, rng):
    head, tail = reg[:-4], reg[-4:]
    while True:
        new = f"{rng.randint(1000, 9999)}"
        if sum(x != y for x, y in zip(new, tail)) >= 2:
            return head + new


def _alter_chassis(ch, rng):
    chars = list(ch)
    for i in rng.sample(range(5, 17), 3):
        chars[i] = rng.choice([c for c in VIN if c != chars[i] and c not in "0123456789"] or "A")
    return "".join(chars)


def base_truth(rng):
    owner, state, rto = _name(rng), rng.choice(STATES), rng.randint(1, 40)
    reg = f"{state}{rto:02d}{rng.choice(LETTERS)}{rng.choice(LETTERS)}{rng.randint(1000, 9999)}"
    chassis = "".join(rng.choice(VIN) for _ in range(17))
    start = date(2025, 5, 1) + timedelta(days=rng.randint(0, 132))
    end = start + timedelta(days=364)
    incident = start + timedelta(days=rng.randint(30, 330))
    idv = rng.randrange(300000, 900001, 5000)
    amount = rng.randrange(15000, 120001, 500)
    pol = f"POL-MTR-{start.year}-{rng.randint(0, 999999):06d}"
    dl = f"{state}{rto:02d}{rng.randint(2005, 2022)}{rng.randint(0, 9999999):07d}"
    return {
        "claim_form": {"claimant_name": owner, "policy_number": pol, "vehicle_reg_no": reg, "driving_licence_no": dl,
                       "incident_date": incident.isoformat(), "incident_location": rng.choice(PLACES),
                       "claimed_amount": float(amount), "repair_estimate": float(amount)},
        "rc": {"owner_name": owner, "vehicle_reg_no": reg, "chassis_no": chassis, "make_model": rng.choice(MAKES),
               "registration_date": (start - timedelta(days=rng.randint(200, 2000))).isoformat()},
        "driving_licence": {"holder_name": owner, "dl_no": dl,
                            "dob": date(rng.randint(1970, 2000), rng.randint(1, 12), rng.randint(1, 28)).isoformat(),
                            "valid_upto": (incident + timedelta(days=rng.randint(400, 3000))).isoformat(), "vehicle_class": "LMV"},
        "policy": {"policy_number": pol, "insured_name": owner, "vehicle_reg_no": reg, "chassis_no": chassis,
                   "policy_start": start.isoformat(), "policy_end": end.isoformat(), "idv": float(idv),
                   "coverage_type": "COMPREHENSIVE"},
    }


def carrier_record(truth):
    p = truth["policy"]
    return {p["policy_number"]: {"insured_name": p["insured_name"], "vehicle_reg_no": p["vehicle_reg_no"],
                                 "chassis_no": p["chassis_no"], "policy_start": p["policy_start"], "policy_end": p["policy_end"],
                                 "idv": p["idv"], "coverage_type": p["coverage_type"], "status": "ACTIVE"}}


def _display(doc_type, fname, kind, value, rng):
    if kind == "date":
        return _fmt_date(date.fromisoformat(value), rng)
    if kind == "amount":
        return _fmt_amt(value)
    if kind == "reg" and doc_type == "rc" and rng.random() < 0.5:
        return f"{value[:2]} {value[2:4]} {value[4:-4]} {value[-4:]}"
    return value


def render_case_docs(cid, truth, quality, rng):
    docs = {}
    for doc_type, values in truth.items():
        lines = [(label, _display(doc_type, f, kind, values[f], rng)) for f, label, kind in FIELD_SPECS[doc_type]]
        img = render_doc(doc_type, lines, f"SYNTHETIC SAMPLE - NOT A REAL DOCUMENT  |  Scan ref: {cid}-{doc_type}")
        if quality == "degraded":
            img = degrade(img, rng)
        ext, data = _encode(img, quality)
        docs[doc_type] = (f"{doc_type}{ext}", data)
    return docs


def build_case(rng, idx, mutation, quality="clean", base: Case | None = None) -> Case:
    cid = f"S{idx:03d}"
    flags, lane = EXPECT[mutation]
    if mutation == "duplicate_file":
        return Case(cid, mutation, "n/a", copy.deepcopy(base.truth), dict(base.docs), set(flags), lane, dict(base.carrier), base.case_id)
    if mutation == "similar_claim":
        truth = copy.deepcopy(base.truth)
        cf = truth["claim_form"]
        cf["incident_date"] = (date.fromisoformat(cf["incident_date"]) - timedelta(days=1)).isoformat()
        amt = float(rng.randrange(15000, 120001, 500))
        cf["claimed_amount"] = cf["repair_estimate"] = amt
        carrier = dict(base.carrier)
    else:
        truth = base_truth(rng)
        carrier = carrier_record(truth)
    t = truth
    if mutation == "reg_mismatch":
        t["claim_form"]["vehicle_reg_no"] = _alter_digits(t["claim_form"]["vehicle_reg_no"], rng)
    elif mutation == "name_mismatch":
        old = t["claim_form"]["claimant_name"]
        new = _name(rng)
        while new.split()[0] == old.split()[0] or new.split()[1] == old.split()[1]:
            new = _name(rng)
        t["claim_form"]["claimant_name"] = new
    elif mutation == "chassis_mismatch":
        t["policy"]["chassis_no"] = _alter_chassis(t["policy"]["chassis_no"], rng)
    elif mutation == "policy_expired":
        end = date.fromisoformat(t["policy"]["policy_end"])
        t["claim_form"]["incident_date"] = (end + timedelta(days=rng.randint(15, 40))).isoformat()
    elif mutation == "amount_exceeds_idv":
        amt = float(round((t["policy"]["idv"] + rng.randint(10000, 50000)) / 500) * 500)
        t["claim_form"]["claimed_amount"] = t["claim_form"]["repair_estimate"] = amt
    elif mutation == "estimate_diff":
        t["claim_form"]["claimed_amount"] = float(round(t["claim_form"]["repair_estimate"] * 1.25 / 100) * 100)
    elif mutation == "dl_expired":
        inc = date.fromisoformat(t["claim_form"]["incident_date"])
        t["driving_licence"]["valid_upto"] = (inc - timedelta(days=rng.randint(30, 200))).isoformat()
    docs = render_case_docs(cid, t, quality, rng)
    if mutation == "missing_doc":
        docs.pop("driving_licence")
        t = {k: v for k, v in t.items() if k != "driving_licence"}
    return Case(cid, mutation, quality, t, docs, set(flags), lane, carrier, base.case_id if base else None)


def build_dataset(n=40, seed=7):
    """n >= 20. First 10 are clean; then the 10 mutation types cycle. Duplicates/similar reuse earlier clean cases."""
    rng = random.Random(seed)
    cases: list[Case] = []
    n_clean = max(10, n - 3 * len(CYCLE)) if n < 40 else 10
    plan = ["clean"] * n_clean
    while len(plan) < n:
        plan.append(CYCLE[(len(plan) - n_clean) % len(CYCLE)])
    next_base = 0
    for i, m in enumerate(plan):
        quality = "degraded" if i % 3 == 2 else "clean"
        base = None
        if m in ("duplicate_file", "similar_claim"):
            clean_cases = [c for c in cases if c.mutation == "clean"]
            base = clean_cases[next_base % len(clean_cases)]
            next_base += 1
        cases.append(build_case(rng, i + 1, m, quality, base))
    return cases


def write_cases(cases, out_dir):
    out = Path(out_dir)
    carrier, labels = {}, {}
    for c in cases:
        folder = out / f"{c.case_id}_{c.mutation}"
        folder.mkdir(parents=True, exist_ok=True)
        for doc_type, (name, data) in c.docs.items():
            (folder / name).write_bytes(data)
        carrier.update(c.carrier)
        labels[c.case_id] = {"mutation": c.mutation, "quality": c.quality, "expected_flags": sorted(c.expected_flags),
                             "expected_lane": c.expected_lane, "base": c.base, "truth": c.truth}
    (out / "labels.json").write_text(json.dumps(labels, indent=1))
    return carrier
