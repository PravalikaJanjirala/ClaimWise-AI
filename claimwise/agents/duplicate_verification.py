"""Duplicate verification agent.

Two DIFFERENT checks (kept separate on purpose):
  DUP_FILE       identical file bytes (SHA-256) previously submitted under another claim.
  SIMILAR_CLAIM  a different claim with the same vehicle and a nearby incident date.
Neither is proof of fraud: both are prompts for a human to check for a legitimate resubmission.
"""
from __future__ import annotations
from datetime import timedelta

from .. import config
from ..normalize import parse_date
from ..schemas import DOC_LABELS, STATUS_CANDIDATE, STATUS_MATCH, Finding, evidence
from .base import Agent, get_field


class DuplicateVerificationAgent(Agent):
    name = "Duplicate verification"

    def run(self, ctx):
        out = []
        hits = []
        for doc_type, d in ctx.documents.items():
            for prior in ctx.db.find_documents_by_hash(d["sha256"], exclude_claim=ctx.claim_id):
                hits.append((doc_type, prior["claim_id"]))
        if hits:
            desc = "; ".join(f"{DOC_LABELS[d]} (also in claim {c})" for d, c in hits)
            out.append(Finding("DUP_FILE", "Identical file submitted before", STATUS_CANDIDATE, "high", self.name,
                               f"Byte-identical files were already submitted under another claim: {desc}. "
                               "This can be a legitimate resubmission or reuse of the same document.",
                               "Open the earlier claim and confirm whether this resubmission is legitimate."))
        else:
            out.append(Finding("DUP_FILE", "Identical file submitted before", STATUS_MATCH, "info", self.name,
                               "No byte-identical file was found in earlier claims.", ""))

        _, reg = get_field(ctx, "claim_form", "vehicle_reg_no")
        _, inc = get_field(ctx, "claim_form", "incident_date")
        inc_d = parse_date(inc.value) if inc else None
        if reg and inc_d:
            window = (inc_d - timedelta(days=config.SIMILAR_DAYS), inc_d + timedelta(days=config.SIMILAR_DAYS))
            sims = ctx.db.find_similar_claims(reg.value, window, exclude_claim=ctx.claim_id)
            if sims:
                desc = "; ".join(f"{s['claim_id']} (incident {s['incident_date']})" for s in sims)
                out.append(Finding("SIMILAR_CLAIM", "Similar claim for the same vehicle", STATUS_CANDIDATE, "medium", self.name,
                                   f"Vehicle {reg.value} has other claim(s) with an incident within {config.SIMILAR_DAYS} days: {desc}. "
                                   "The documents are not identical; this is a similarity prompt only.",
                                   "Check whether this is the same incident reported twice or a separate event.",
                                   [evidence(ctx.extractions, "claim_form", "vehicle_reg_no"),
                                    evidence(ctx.extractions, "claim_form", "incident_date")]))
            else:
                out.append(Finding("SIMILAR_CLAIM", "Similar claim for the same vehicle", STATUS_MATCH, "info", self.name,
                                   "No other claim for this vehicle near the incident date.", ""))
        return out
