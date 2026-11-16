"""Orchestrator: coordinates the workflow, shared state and routing.

Pipeline:  intake -> extraction (per document, parallel) -> verification agents (parallel)
           -> confidence & evidence -> PENDING_REVIEW -> HUMAN DECISION (the only way to approve/reject).
There is deliberately no code path that sets APPROVED or REJECTED without a named human reviewer.
"""
from __future__ import annotations
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from .agents.confidence import ConfidenceEvidenceAgent
from .agents.duplicate_verification import DuplicateVerificationAgent
from .agents.field_verification import FieldVerificationAgent
from .agents.policy_verification import PolicyVerificationAgent
from .db import Database, sha256_bytes
from .extraction import extract
from .normalize import iso, parse_amount, parse_date
from .quality import check_image_quality
from .schemas import (DOC_LABELS, REQUIRED_DOCS, STATUS_MATCH, STATUS_MISSING, ClaimContext, Finding, STATUS_REVIEW)

DECISIONS = {"APPROVE": "APPROVED", "QUERY": "QUERY_RAISED", "REJECT": "REJECTED"}
CUSTOMER_STATUS = {
    "SUBMITTED": "Received. Your documents have been submitted.",
    "PROCESSING": "Your documents are being checked.",
    "PENDING_REVIEW": "Under review by our claims team.",
    "QUERY_RAISED": "We need some more information from you.",
    "APPROVED": "Your claim has been approved.",
    "REJECTED": "Your claim was not approved. Please contact us if you have questions.",
}


class ValidationError(ValueError):
    pass


class Orchestrator:
    def __init__(self, db: Database, policy_service, store_dir=None, use_llm=None):
        self.db = db
        self.policy_service = policy_service
        self.store_dir = Path(store_dir or config.FILE_STORE)
        self.use_llm = use_llm
        self.field_agent = FieldVerificationAgent()
        self.policy_agent = PolicyVerificationAgent()
        self.dup_agent = DuplicateVerificationAgent()
        self.conf_agent = ConfidenceEvidenceAgent()

    # ------------------------------------------------------------------ intake
    def _validate(self, files):
        for doc_type, (name, data) in files.items():
            if doc_type not in DOC_LABELS:
                raise ValidationError(f"Unknown document type: {doc_type}")
            if Path(name).suffix.lower() not in config.ALLOWED_EXT:
                raise ValidationError(f"{name}: unsupported file type (allowed: {', '.join(config.ALLOWED_EXT)})")
            if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
                raise ValidationError(f"{name}: file larger than {config.MAX_UPLOAD_MB} MB")
            if not data:
                raise ValidationError(f"{name}: file is empty")
        if not files:
            raise ValidationError("At least one document is required")

    def _store(self, claim_id, files, actor):
        folder = self.store_dir / claim_id
        folder.mkdir(parents=True, exist_ok=True)
        for doc_type, (name, data) in files.items():
            n = len(list(folder.glob(f"{doc_type}_v*"))) + 1
            path = folder / f"{doc_type}_v{n}{Path(name).suffix.lower()}"
            path.write_bytes(data)
            sha = sha256_bytes(data)
            self.db.add_document(claim_id, doc_type, name, path, sha)
            self.db.audit(claim_id, actor, "customer" if actor.startswith("customer") else "system", "DOCUMENT_UPLOADED",
                          {"doc_type": doc_type, "filename": name, "sha256": sha, "version": n})

    def submit_claim(self, files: dict, claimant_ref: str = "", actor: str = "customer", process: bool = True) -> str:
        """files: {doc_type: (filename, bytes)}"""
        self._validate(files)
        claim_id = self.db.new_claim(claimant_ref)
        self.db.audit(claim_id, actor, "customer", "CLAIM_SUBMITTED", {"consent": True})
        self._store(claim_id, files, actor)
        if process:
            self.process(claim_id)
        return claim_id

    def resubmit(self, claim_id: str, files: dict, actor: str = "customer"):
        """Customer supplies replacement/missing documents after a query; the claim is re-verified."""
        claim = self.db.get_claim(claim_id)
        if not claim or claim["status"] not in ("QUERY_RAISED", "PENDING_REVIEW"):
            raise ValidationError("Documents can only be added while the claim is under review or a query is open.")
        self._validate(files)
        self._store(claim_id, files, actor)
        self.process(claim_id)

    # ------------------------------------------------------------------ processing
    def process(self, claim_id: str):
        t0 = time.perf_counter()
        self.db.update_claim(claim_id, status="PROCESSING")
        docs = self.db.get_documents(claim_id)

        def run_extract(item):
            doc_type, d = item
            try:
                return doc_type, extract(doc_type, d["path"], self.use_llm), None
            except Exception as e:  # never silently drop a document
                return doc_type, None, f"{type(e).__name__}: {e}"

        extractions, errors = {}, {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for doc_type, ex, err in pool.map(run_extract, docs.items()):
                if ex is not None:
                    extractions[doc_type] = ex
                else:
                    errors[doc_type] = err
        self.db.save_extractions(claim_id, extractions)
        cf = extractions.get("claim_form")
        reg = cf.fields["vehicle_reg_no"].value if cf and "vehicle_reg_no" in cf.fields else None
        inc = parse_date(cf.fields["incident_date"].value) if cf and "incident_date" in cf.fields else None
        amt = parse_amount(cf.fields["claimed_amount"].value) if cf and "claimed_amount" in cf.fields else None
        self.db.update_claim(claim_id, vehicle_reg=reg, incident_date=iso(inc), claimed_amount=amt)
        self.db.audit(claim_id, "orchestrator", "system", "EXTRACTION_COMPLETE",
                      {"documents": list(extractions), "failed": errors})

        ctx = ClaimContext(claim_id, docs, extractions, self.db, self.policy_service)
        agents = [self.field_agent, self.policy_agent, self.dup_agent]

        def run_agent(agent):
            try:
                return agent.name, agent.run(ctx), None
            except Exception as e:
                return agent.name, [Finding(f"AGENT_ERROR:{agent.name}", f"{agent.name} could not complete", STATUS_REVIEW,
                                            "medium", agent.name, f"This check failed to run ({type(e).__name__}).",
                                            "Review this claim manually.")], str(e)

        findings = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for name, fs, err in pool.map(run_agent, agents):
                findings.extend(fs)
                self.db.audit(claim_id, name, "system", "AGENT_COMPLETE",
                              {"findings": len(fs), "flags": sum(1 for f in fs if f.status != STATUS_MATCH), "error": err})
        for doc_type, d in docs.items():  # photo quality (cheap, no OCR); recorded for the officer and the customer
            q = check_image_quality(d["path"])
            if not q.ok:
                findings.append(Finding(f"IMAGE_QUALITY:{doc_type}", f"Photo quality: {DOC_LABELS[doc_type]}", STATUS_REVIEW, "low",
                                        "Image quality", f"The uploaded image looks {q.summary}, so details may be read incorrectly.",
                                        f"Ask the claimant to retake it: {q.advice}."))
        for doc_type, err in errors.items():
            findings.append(Finding(f"EXTRACTION_ERROR:{doc_type}", f"{DOC_LABELS[doc_type]} could not be read", STATUS_REVIEW,
                                    "high", "Document intelligence", f"The file could not be processed ({err}).",
                                    "Ask the claimant for a clearer or different file."))
        findings, summary = self.conf_agent.run(ctx, findings)
        ms = (time.perf_counter() - t0) * 1000
        self.db.save_findings(claim_id, findings)
        self.db.update_claim(claim_id, status="PENDING_REVIEW", lane=summary["lane"], score=summary["score"],
                             processing_ms=round(ms, 1), summary_json=json.dumps(summary))
        self.db.audit(claim_id, "orchestrator", "system", "VERIFICATION_COMPLETE",
                      {"lane": summary["lane"], "score": summary["score"], "ms": round(ms, 1)})
        return summary

    # ------------------------------------------------------------------ human decision
    def decide(self, claim_id: str, decision: str, reviewer: dict, reason: str = "", customer_message: str = ""):
        """The ONLY way a claim becomes approved / rejected / queried. Requires an authenticated employee."""
        if not reviewer or reviewer.get("role") != "employee":
            raise PermissionError("Only an authorised insurance employee can record a decision.")
        decision = decision.upper()
        if decision not in DECISIONS:
            raise ValidationError("Decision must be APPROVE, QUERY or REJECT.")
        claim = self.db.get_claim(claim_id)
        if not claim:
            raise ValidationError("Unknown claim.")
        if claim["status"] not in ("PENDING_REVIEW", "QUERY_RAISED"):
            raise ValidationError(f"Claim is {claim['status']}; no decision can be recorded now.")
        if decision in ("QUERY", "REJECT") and not reason.strip():
            raise ValidationError("A reason is required to query or reject a claim.")
        if decision == "QUERY" and not (customer_message or reason).strip():
            raise ValidationError("A message for the customer is required when raising a query.")
        msg = customer_message.strip() or (reason.strip() if decision == "QUERY" else "")
        self.db.add_decision(claim_id, decision, reviewer["username"], reason.strip(), msg)
        self.db.update_claim(claim_id, status=DECISIONS[decision])
        self.db.audit(claim_id, reviewer["username"], "employee", "HUMAN_DECISION",
                      {"decision": decision, "reason": reason.strip()})
        return DECISIONS[decision]

    # ------------------------------------------------------------------ customer view
    def customer_view(self, claim_id: str):
        """What the claimant may see: status and requests only. No scores, no fraud/duplicate flags."""
        claim = self.db.get_claim(claim_id)
        if not claim:
            return None
        requests = []
        findings = self.db.get_findings(claim_id)
        for f in findings:
            if f.status == STATUS_MISSING and f.check_id.startswith("DOC_MISSING_"):
                requests.append(f"Please upload your {f.title.replace(' provided', '').lower()}.")
            elif f.check_id.startswith(("DOC_TYPE_", "EXTRACTION_ERROR:")):
                requests.append(f"Please upload a clearer or correct copy of: {f.title.replace(' looks correct', '').replace(' could not be read', '')}.")
        low, poor = {}, set()
        for f in findings:
            if f.check_id.startswith("IMAGE_QUALITY:"):
                doc = f.check_id.split(":")[1]
                poor.add(doc)
                requests.append(f"Please retake or rescan your {DOC_LABELS[doc].lower()}: "
                                f"{f.action.replace('Ask the claimant to retake it: ', '').rstrip('.')}.")
            elif f.check_id.startswith("LOW_CONFIDENCE:"):
                low[f.check_id.split(":")[1].split(".")[0]] = True
        for doc in low:
            if doc in poor:
                continue
            requests.append(f"Some details in your {DOC_LABELS[doc].lower()} were hard to read. A clearer photo or scan would help.")
        decisions = self.db.get_decisions(claim_id)
        message = decisions[-1]["customer_message"] if decisions and decisions[-1]["decision"] in ("QUERY", "REJECT") else ""
        return {"claim_id": claim_id, "status": claim["status"], "status_text": CUSTOMER_STATUS.get(claim["status"], claim["status"]),
                "requests": list(dict.fromkeys(requests)), "message_from_team": message, "submitted_at": claim["created_at"]}
