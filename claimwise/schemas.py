"""Shared data structures and the document field catalogue."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional

REQUIRED_DOCS = ("claim_form", "rc", "driving_licence", "policy")
DOC_LABELS = {
    "claim_form": "Claim form",
    "rc": "Registration Certificate (RC)",
    "driving_licence": "Driving licence",
    "policy": "Insurance policy",
}
DOC_TITLES = {
    "claim_form": "MOTOR INSURANCE CLAIM FORM",
    "rc": "CERTIFICATE OF REGISTRATION",
    "driving_licence": "DRIVING LICENCE",
    "policy": "MOTOR INSURANCE POLICY SCHEDULE",
}
# (field name, printed label, kind)
FIELD_SPECS = {
    "claim_form": [
        ("claimant_name", "Claimant Name", "name"),
        ("policy_number", "Policy Number", "policy_no"),
        ("vehicle_reg_no", "Vehicle Registration No", "reg"),
        ("driving_licence_no", "Driving Licence No", "dl"),
        ("incident_date", "Date of Incident", "date"),
        ("incident_location", "Place of Incident", "text"),
        ("claimed_amount", "Amount Claimed", "amount"),
        ("repair_estimate", "Garage Estimate", "amount"),
    ],
    "rc": [
        ("owner_name", "Owner Name", "name"),
        ("vehicle_reg_no", "Registration No", "reg"),
        ("chassis_no", "Chassis No", "chassis"),
        ("make_model", "Make / Model", "text"),
        ("registration_date", "Date of Registration", "date"),
    ],
    "driving_licence": [
        ("holder_name", "Holder Name", "name"),
        ("dl_no", "Licence No", "dl"),
        ("dob", "Date of Birth", "date"),
        ("valid_upto", "Valid Upto", "date"),
        ("vehicle_class", "Vehicle Class", "text"),
    ],
    "policy": [
        ("policy_number", "Policy Number", "policy_no"),
        ("insured_name", "Insured Name", "name"),
        ("vehicle_reg_no", "Vehicle Registration No", "reg"),
        ("chassis_no", "Chassis No", "chassis"),
        ("policy_start", "Policy Start Date", "date"),
        ("policy_end", "Policy End Date", "date"),
        ("idv", "Insured Declared Value", "amount"),
        ("coverage_type", "Coverage Type", "text"),
    ],
}
FIELD_LABELS = {(d, f): lab for d, specs in FIELD_SPECS.items() for f, lab, _ in specs}
FIELD_KINDS = {(d, f): k for d, specs in FIELD_SPECS.items() for f, _, k in specs}

STATUS_MATCH, STATUS_MISMATCH, STATUS_MISSING = "MATCH", "MISMATCH", "MISSING"
STATUS_REVIEW, STATUS_CANDIDATE = "REVIEW", "CANDIDATE"
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

LANE_FAST, LANE_EXCEPTION, LANE_MANUAL = "FAST_LANE", "EXCEPTION_QUEUE", "MANUAL_REVIEW"
LANE_TEXT = {
    LANE_FAST: "No discrepancies found. Ready for routine human review.",
    LANE_EXCEPTION: "Discrepancies found. Review the flagged items before deciding.",
    LANE_MANUAL: "Critical issue or missing/duplicate documents. Detailed manual review required.",
}
LANE_PRIORITY = {LANE_MANUAL: 0, LANE_EXCEPTION: 1, LANE_FAST: 2}


@dataclass
class FieldValue:
    value: str
    raw: str
    confidence: float
    source: str = "ocr"       # "ocr" | "llm"
    format_ok: bool = True


@dataclass
class Extraction:
    doc_type: str
    detected_type: Optional[str] = None
    fields: dict = field(default_factory=dict)   # name -> FieldValue
    mean_ocr_conf: float = 0.0
    engine: str = ""
    warnings: list = field(default_factory=list)
    text: str = ""

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        fields = {k: FieldValue(**v) for k, v in d.get("fields", {}).items()}
        return Extraction(doc_type=d["doc_type"], detected_type=d.get("detected_type"), fields=fields,
                          mean_ocr_conf=d.get("mean_ocr_conf", 0.0), engine=d.get("engine", ""),
                          warnings=d.get("warnings", []), text=d.get("text", ""))


@dataclass
class Finding:
    check_id: str
    title: str
    status: str
    severity: str
    agent: str
    message: str
    action: str = ""
    evidence: list = field(default_factory=list)  # [{doc, doc_type, field, value, confidence}]

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return Finding(**d)


@dataclass
class ClaimContext:
    claim_id: str
    documents: dict            # doc_type -> {"path","sha256","filename"}
    extractions: dict          # doc_type -> Extraction
    db: object = None
    policy_service: object = None


def evidence(extractions, doc_type, field_name):
    """Build one evidence record for an extracted field."""
    ex = extractions.get(doc_type)
    fv = ex.fields.get(field_name) if ex else None
    return {
        "doc": DOC_LABELS[doc_type], "doc_type": doc_type, "field_key": field_name,
        "field": FIELD_LABELS.get((doc_type, field_name), field_name),
        "value": fv.value if fv else "(not readable)",
        "confidence": round(fv.confidence, 2) if fv else 0.0,
    }
