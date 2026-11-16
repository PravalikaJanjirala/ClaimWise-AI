# ClaimWise AI

**Intelligent Multi-Agent Motor Insurance Claims Verification System** (Flarre AI Innovation Challenge 2026)

*Every insurance claim deserves a quick, accurate and transparent verification process.*

ClaimWise AI is a **human-in-the-loop** verification layer for motor insurance claims. It reads the claim form, RC, driving licence and
policy (OCR + structured extraction), cross-checks them, validates the policy against carrier records, looks for duplicate
submissions, and prepares an **evidence report**. An authorised employee makes every decision. The system never approves or rejects a claim.

## What is built (prototype scope)

| Capability | Status |
|---|---|
| Document upload, validation, consent, SHA-256 hashing | Built |
| OCR (Tesseract; PDF text layer) and structured extraction with per-field confidence | Built |
| **Photo quality check at upload** (blur, dark, faded, low resolution) with retake tips, also recorded for the officer | Built |
| Format-aware OCR repair (O/0, I/1, VIN rules, registration/licence structure) | Built |
| Optional LLM fill for unreadable fields (validated, confidence-capped, off by default) | Built, **untested here** (needs API key + network) |
| Field verification: cross-document matching (rules + fuzzy names) | Built |
| Policy verification against a mock carrier record (period, status, IDV, licence validity, estimate vs claim) | Built |
| Duplicate verification: identical files (hash) and similar claims (vehicle + date), reported separately | Built |
| Confidence score and suggested review lane (advisory) | Built |
| Employee dashboard, evidence beside source documents, PDF/Markdown report | Built |
| Human decision (approve / query / reject) with required reasons, audit trail (append-only) | Built |
| Customer portal with status tracking and document requests (no scores or flags shown) | Built |
| Synthetic data generator with planted mismatches + labelled evaluation | Built |
| Damage assessment (computer vision), advanced fraud analytics, Guidewire/Duck Creek integration, other insurance lines | **Future scope, not built** |

## Quick start

```bash
# 1. system dependency: Tesseract OCR engine
sudo apt install tesseract-ocr          # macOS: brew install tesseract

# 2. python deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. generate synthetic demo claims + mock carrier records
python scripts/generate_synthetic.py

# 4. run the app
streamlit run app.py
```

Employee demo sign-in: `officer1` or `officer2`, password `demo-pass` (override with `CLAIMWISE_DEMO_PASSWORD`). Prototype only.

### Demo script (3 minutes)

1. **Customer portal → "Demo: submit a prepared sample claim"** → submit `S001_clean`. Status shows "Under review"; no scores are visible to the customer.
2. Submit `S003_reg_mismatch` (a planted registration-number mismatch).
3. **Employee dashboard** (sign in) → the mismatch claim is in the queue; open it. See the flagged item with **which document, what value, what was expected, why, and the suggested action**, next to the original document image.
4. Submit `S011_duplicate_file` (after S001) to see the identical-file duplicate prompt, and `S010_missing_doc` to see the missing-document request on the customer side.
5. Record a **Query** with a message, then check the customer's status page; then **Approve** a clean claim. Open **Audit log** to see every step.

### Optional extras

```bash
python mock_api/server.py &                       # mock carrier REST API on :8001
export CLAIMWISE_POLICY_API_URL=http://127.0.0.1:8001

export CLAIMWISE_USE_LLM=1 ANTHROPIC_API_KEY=...  # optional LLM fill for unreadable fields
```

## Tests and evaluation

```bash
python -m unittest discover -s tests -v    # 30 tests (end-to-end tests need Tesseract; skipped otherwise)
python scripts/evaluate.py                 # 40 labelled synthetic claims -> results/
python scripts/evaluate.py --seed 21       # different dataset
```

### Measured results (synthetic data)

Measured on 40 synthetic claims per run (about a third rendered as degraded scans: rotation, blur, noise, JPEG), planted errors with ground-truth labels.
Seed 7 was used while developing (so it is optimistic); **seed 21 is a held-out run that was not tuned against.**

| Metric | Dev (seed 7) | Held-out (seed 21) |
|---|---|---|
| Field extraction accuracy (all / clean / degraded scans) | 96.3% / 96.4% / 96.1% | 98.2% / 99.1% / 96.4% |
| Planted-mismatch recall (flagged for attention) | 100% | 100% |
| Mismatch precision, hard flags only | 100% | 100% |
| Mismatch precision incl. review prompts | 62.5% | 75.0% |
| Clean claims with any hard flag / any flag incl. review | 0% / 30% | 0% / 30% |
| Identical-file duplicate precision / recall | 100% / 100% | 100% / 100% |
| Similar-claim precision / recall | 100% / 100% | 100% / 100% |
| Suggested lane matches expected | 92.5% | 92.5% |
| Processing time per claim (median) | ~2.5 s | ~2.4 to 3.2 s (varies by machine and load) |
| Claims automatically approved or rejected | 0 | 0 |

**Read these honestly.** The documents are synthetic, cleanly rendered, in one fixed layout, in English, and the planted errors are
the same kinds the rules look for. Real scans, varied layouts, handwriting and regional languages will be harder. The numbers
show that the pipeline works as designed; they do not establish production accuracy, which needs real, consented carrier data.
The "75% precision including review prompts" figure reflects deliberate caution: low-confidence reads are surfaced to a human
rather than silently trusted.

## Project structure

```
app.py                       Streamlit app (customer portal, employee dashboard, audit log)
claimwise/
  orchestrator.py            workflow, agents in parallel, human decision gate, customer view
  ocr.py / extraction.py     OCR + structured extraction (+ optional LLM fill)
  quality.py                 photo quality check (blur, brightness, contrast, resolution)
  normalize.py               dates, amounts, names, OCR-confusable logic, format-aware repair
  agents/                    field / policy / duplicate verification, confidence & evidence
  policy_service.py          mock carrier lookup (in-process or REST)
  db.py                      SQLite, audit triggers, demo users
  report.py                  Markdown + PDF evidence report
  synthetic.py               synthetic documents with planted mismatches + labels
mock_api/server.py           mock carrier REST API (stdlib only)
scripts/                     generate_synthetic.py, evaluate.py
tests/                       unittest suite
data/                        samples/ (12 synthetic demo claims), carrier_policies.json
results/                     measured metrics
docs/ARCHITECTURE.md         diagram, design decisions, mapping to the proposal
```

## Image quality check

Bad photos are a common cause of claim delays: the insurer discovers a blurry licence days later and has to ask again.
ClaimWise checks each image **the moment the customer selects it** (Customer portal), and says what is wrong and how to fix it
("This photo looks blurry. Tip: hold the phone steady, tap to focus, and retake"). The customer can retake it or tick
"Submit anyway". The same check runs in the pipeline, so the officer sees an `IMAGE_QUALITY` item and the customer's status page
repeats the retake request if the claim is queried.

How it works (`claimwise/quality.py`, no OCR, runs in milliseconds):

| Check | Method | Threshold |
|---|---|---|
| Blur | variance of the Laplacian on a 1000 px-wide copy | below 100 |
| Too dark | mean grey level | below 90 |
| Washed out / glare | very bright and almost no ink | mean above 250 |
| Faded / low contrast | paper level minus ink level | gap below 70 |
| Low resolution | longest side | below 900 px |

Calibration (synthetic data): sharp scans score about 1400 to 2000 for sharpness; noisy but readable scans about 280 to 400; blurred
images below 45. On the 157 readable synthetic documents of the held-out dataset, **0 were wrongly flagged**; deliberately blurred, dark, faded
and tiny images are all caught (unit tests). **Thresholds must be re-tuned on real phone photos** (shadows, glare, curved pages and
camera noise behave differently), and noise can inflate the sharpness score, so very noisy blurred photos may slip through.
PDFs are skipped because digital PDFs have a text layer.

## Responsible-AI and privacy notes

- All data is synthetic; every generated page carries a visible "SYNTHETIC SAMPLE" footer.
- Flags are prompts for review, not accusations. Duplicate and similar-claim signals are never described as fraud.
- Customers never see internal scores or flags; employees see evidence, not only a score.
- Every system step and human decision is in an append-only audit log.
- Not production-ready: files are stored unencrypted on local disk, login is demo-grade, there is no rate limiting, and the
  Streamlit UI was smoke-tested with a stubbed library, not in a real browser. Treat deployment security as future work.

## Known limitations

- Tesseract is weaker than commercial document-AI on poor scans; low-confidence fields are routed to humans by design.
- Layout is label-based (`Label: value`). Unseen real-world layouts need template learning or a layout-aware model.
- The optional LLM path is implemented but was not run in the build environment (no network).
- Similar-claim detection uses vehicle + incident date only.
