"""Field verification agent: cross-document matching using deterministic rules + fuzzy matching."""
from __future__ import annotations

from .. import config
from ..normalize import name_similarity, norm_alnum, ocr_confusable_equal
from ..schemas import (DOC_LABELS, STATUS_MATCH, STATUS_MISMATCH, STATUS_REVIEW, Finding, evidence)
from .base import Agent, get_field

# (check_id, title, (doc_a, field_a), (doc_b, field_b), kind, severity)
CHECKS = [
    ("REG_CLAIM_RC", "Vehicle registration: claim form vs RC", ("claim_form", "vehicle_reg_no"), ("rc", "vehicle_reg_no"), "id", "high"),
    ("REG_CLAIM_POLICY", "Vehicle registration: claim form vs policy", ("claim_form", "vehicle_reg_no"), ("policy", "vehicle_reg_no"), "id", "high"),
    ("REG_RC_POLICY", "Vehicle registration: RC vs policy", ("rc", "vehicle_reg_no"), ("policy", "vehicle_reg_no"), "id", "high"),
    ("CHASSIS_RC_POLICY", "Chassis number: RC vs policy", ("rc", "chassis_no"), ("policy", "chassis_no"), "id", "high"),
    ("POLICY_NO_CLAIM_POLICY", "Policy number: claim form vs policy", ("claim_form", "policy_number"), ("policy", "policy_number"), "id", "high"),
    ("DL_NO_CLAIM_DL", "Licence number: claim form vs licence", ("claim_form", "driving_licence_no"), ("driving_licence", "dl_no"), "id", "medium"),
    ("NAME_CLAIM_RC", "Name: claimant vs RC owner", ("claim_form", "claimant_name"), ("rc", "owner_name"), "name", "high"),
    ("NAME_CLAIM_POLICY", "Name: claimant vs policy insured", ("claim_form", "claimant_name"), ("policy", "insured_name"), "name", "high"),
    ("NAME_RC_POLICY", "Name: RC owner vs policy insured", ("rc", "owner_name"), ("policy", "insured_name"), "name", "high"),
    ("NAME_CLAIM_DL", "Name: claimant vs licence holder", ("claim_form", "claimant_name"), ("driving_licence", "holder_name"), "name", "high"),
]


class FieldVerificationAgent(Agent):
    name = "Field verification"

    def run(self, ctx):
        out = []
        for check_id, title, (da, fa), (db, fb), kind, sev in CHECKS:
            pa, va = get_field(ctx, da, fa)
            pb, vb = get_field(ctx, db, fb)
            if not (pa and pb):
                continue  # a missing document is reported by the confidence/evidence step
            ev = [evidence(ctx.extractions, da, fa), evidence(ctx.extractions, db, fb)]
            if va is None or vb is None:
                unread = [DOC_LABELS[d] for d, v in ((da, va), (db, vb)) if v is None]
                out.append(Finding(check_id, title, STATUS_REVIEW, "low", self.name,
                                   f"Could not read this field from: {', '.join(unread)}. The comparison could not be completed.",
                                   "Check the source document manually or ask the claimant for a clearer copy.", ev))
                continue
            status, detail = self._compare(kind, va.value, vb.value)
            if status == STATUS_MISMATCH and min(va.confidence, vb.confidence) < config.LOW_CONF_DOWNGRADE:
                status, detail = STATUS_REVIEW, "values differ but at least one was read with low OCR confidence"
            msg, action = self._explain(status, kind, detail, DOC_LABELS[da], va.value, DOC_LABELS[db], vb.value)
            out.append(Finding(check_id, title, status, sev if status != STATUS_MATCH else "info", self.name, msg, action, ev))
        return out

    @staticmethod
    def _compare(kind, a, b):
        if kind == "id":
            if norm_alnum(a) == norm_alnum(b):
                return STATUS_MATCH, "identical"
            if ocr_confusable_equal(a, b):
                return STATUS_REVIEW, "differs only by characters OCR often confuses (e.g. O/0, I/1, S/5)"
            return STATUS_MISMATCH, "different values"
        sim = name_similarity(a, b)
        if sim >= config.NAME_MATCH:
            return STATUS_MATCH, f"similarity {sim:.0%}"
        if sim >= config.NAME_REVIEW:
            return STATUS_REVIEW, f"similar but not identical ({sim:.0%}); may be initials, spelling or order"
        return STATUS_MISMATCH, f"similarity only {sim:.0%}"

    @staticmethod
    def _explain(status, kind, detail, la, a, lb, b):
        what = "value" if kind == "id" else "name"
        if status == STATUS_MATCH:
            return f"{la} and {lb} agree ({a}).", ""
        if status == STATUS_REVIEW:
            return (f"{la} shows '{a}' and {lb} shows '{b}': {detail}.",
                    "Compare the original documents to confirm whether this is a reading error or a real difference.")
        return (f"{la} shows '{a}' but {lb} shows '{b}' ({detail}).",
                f"Inspect both documents and confirm the correct {what} with the claimant before proceeding.")
