# Architecture

```
Customer portal ──► intake validation (type, size, consent) ──► file store + SHA-256 + audit event
                                                │
                                         ORCHESTRATOR (workflow state, routing)
                                                │
              ┌─────────────────────────────────┴───────────────────────────┐
   Document intelligence (per document, parallel)                          │
   OCR (Tesseract / PDF text layer) → label parsing → format repair →      │
   per-field confidence → optional LLM fill of unreadable fields           │
              └─────────────────────────────────┬───────────────────────────┘
                                                │ extractions
                 ┌──────────────────────────────┼─────────────────────────────┐   (run in parallel)
        Field verification            Policy verification           Duplicate verification
   cross-document matching     rules vs carrier record (mock API)   SHA-256 identical files
   (rules + fuzzy names)       period, status, IDV, licence, amount  similar claims (vehicle + date)
                 └──────────────────────────────┼─────────────────────────────┘
                                                │ findings
                                   Confidence & evidence
                         score + suggested review lane (ADVISORY)
                                                │
                              status = PENDING_REVIEW  (never APPROVED / REJECTED)
                                                │
            Employee dashboard: evidence beside source documents ──► HUMAN DECISION (approve / query / reject)
                                                │
                              Customer status + document requests (no scores, no flags)

 Shared data (SQLite): claims, documents, extractions, findings, decisions, users, append-only audit_log
```

## Design decisions

| Decision | Why |
|---|---|
| Deterministic rules for exact comparisons; AI only where documents are messy | Auditable and explainable; an LLM never decides a match |
| Carrier record is the source of truth for policy facts | A submitted policy copy can be outdated or altered |
| OCR-confusable differences (O/0, I/1, S/5, B/8) become REVIEW, not MISMATCH | Avoids falsely accusing a claimant over a reading error |
| Low-confidence mismatches are downgraded to REVIEW | Same reason; humans verify |
| A low-confidence value that agrees with an independent document is "corroborated" | Agreement across sources is stronger evidence than OCR confidence alone |
| Duplicate checks split into identical-file (hash) and similar-claim (vehicle + date) | Different mechanisms and reliability; never presented as proof of fraud |
| Lane and score are advisory | They order the queue; only `Orchestrator.decide` (authenticated employee) changes a claim's final status |
| Agent failure creates a REVIEW finding | A check is never silently skipped |
| Customer view hides scores and flags | Internal signals could mislead or be gamed |
| Append-only audit via SQLite triggers | Tamper resistance for decisions and system events |

## Review lanes (advisory)

- `MANUAL_REVIEW`: a critical failure (policy period, licence validity, IDV exceeded, unknown/inactive policy), a missing document, or an identical-file duplicate.
- `EXCEPTION_QUEUE`: any other non-matching finding (mismatch, review prompt, similar claim).
- `FAST_LANE`: every automated check agreed. Still requires a human decision.

## Mapping to the proposal

| Proposal component | Code |
|---|---|
| Orchestrator | `claimwise/orchestrator.py` |
| Document intelligence | `claimwise/ocr.py`, `claimwise/extraction.py` |
| Field verification | `claimwise/agents/field_verification.py` |
| Policy verification | `claimwise/agents/policy_verification.py`, `claimwise/policy_service.py`, `mock_api/server.py` |
| Duplicate verification | `claimwise/agents/duplicate_verification.py` |
| Confidence & evidence | `claimwise/agents/confidence.py`, `claimwise/report.py` |
| Intake UI, dashboard, review, audit, status | `app.py`, `claimwise/db.py` |

## Future scope (intentionally not built)

Damage assessment (computer vision), advanced fraud analytics, live Guidewire / Duck Creek adapters, other insurance lines,
encryption at rest and production identity management.
