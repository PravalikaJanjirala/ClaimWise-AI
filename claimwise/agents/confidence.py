"""Confidence & evidence component: consolidates findings, scores, and suggests a review lane.

The lane and score are ADVISORY. They order the employee's work queue; they never approve or reject.
"""
from __future__ import annotations

from .. import config
from ..schemas import (DOC_LABELS, FIELD_LABELS, LANE_EXCEPTION, LANE_FAST, LANE_MANUAL, LANE_TEXT, REQUIRED_DOCS,
                       SEVERITY_ORDER, STATUS_CANDIDATE, STATUS_MATCH, STATUS_MISMATCH, STATUS_MISSING, STATUS_REVIEW,
                       Finding, evidence)
from .base import Agent

PENALTY = {"critical": 30, "high": 20, "medium": 10, "low": 4, "info": 0}


class ConfidenceEvidenceAgent(Agent):
    name = "Confidence & evidence"

    def run(self, ctx, findings: list[Finding]):
        extra = []
        # A low-confidence read that agrees with an independent source is corroborated: no extra prompt needed.
        corroborated = {(e["doc_type"], e["field_key"]) for f in findings
                        if f.status == STATUS_MATCH and (f.agent == "Field verification" or f.check_id == "CARRIER_RECORD")
                        for e in f.evidence}
        for doc in REQUIRED_DOCS:
            if doc not in ctx.extractions:
                extra.append(Finding(f"DOC_MISSING_{doc.upper()}", f"{DOC_LABELS[doc]} provided", STATUS_MISSING, "high", self.name,
                                     f"{DOC_LABELS[doc]} was not provided.",
                                     f"Request the {DOC_LABELS[doc].lower()} from the claimant."))
        for doc, ex in ctx.extractions.items():
            for w in ex.warnings:
                if "uploaded as" in w or "title not recognised" in w:
                    extra.append(Finding(f"DOC_TYPE_{doc.upper()}", f"{DOC_LABELS[doc]} looks correct", STATUS_REVIEW, "medium", self.name,
                                         w, "Check that the right document was uploaded in this slot."))
            for fname, fv in ex.fields.items():
                if fv.confidence < config.FIELD_CONF_REVIEW and not (fv.format_ok and (doc, fname) in corroborated):
                    extra.append(Finding(f"LOW_CONFIDENCE:{doc}.{fname}",
                                         f"Low extraction confidence: {FIELD_LABELS[(doc, fname)]}", STATUS_REVIEW, "low", self.name,
                                         f"'{fv.value}' was read from {DOC_LABELS[doc]} with confidence {fv.confidence:.0%}"
                                         + (" (format check failed)." if not fv.format_ok else "."),
                                         "Verify this value against the original document.", [evidence(ctx.extractions, doc, fname)]))
        allf = findings + extra
        issues = [f for f in allf if f.status != STATUS_MATCH]
        score = max(0, 100 - sum(PENALTY.get(f.severity, 0) for f in issues))
        return allf, self.summarise(allf, score)

    @staticmethod
    def lane_for(findings):
        critical = any(f.severity == "critical" and f.status in (STATUS_MISMATCH, STATUS_MISSING) for f in findings)
        missing = any(f.status == STATUS_MISSING for f in findings)
        dup_file = any(f.check_id == "DUP_FILE" and f.status == STATUS_CANDIDATE for f in findings)
        if critical or missing or dup_file:
            return LANE_MANUAL
        if any(f.status != STATUS_MATCH for f in findings):
            return LANE_EXCEPTION
        return LANE_FAST

    def summarise(self, findings, score):
        lane = self.lane_for(findings)
        issues = sorted([f for f in findings if f.status != STATUS_MATCH],
                        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.check_id))
        actions = []
        for f in issues:
            if f.action and f.action not in actions:
                actions.append(f.action)
        counts = {s: sum(1 for f in findings if f.status == s)
                  for s in (STATUS_MATCH, STATUS_MISMATCH, STATUS_REVIEW, STATUS_MISSING, STATUS_CANDIDATE)}
        return {"lane": lane, "lane_text": LANE_TEXT[lane], "score": score, "counts": counts,
                "top_issues": [f.title for f in issues[:5]], "recommended_actions": actions,
                "note": "Advisory only. The final decision is made by an authorised insurance employee."}
