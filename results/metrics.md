# Measured results (synthetic dataset)

40 synthetic claims, seed 21; about a third rendered as degraded scans (rotation, blur, noise, JPEG). Planted errors have ground-truth labels.

| Metric | Result |
|---|---|
| Field extraction accuracy (all / clean / degraded) | 98.2% / 99.1% / 96.4% |
| Planted-mismatch recall (flagged for attention) | 100.0% |
| Mismatch precision, hard flags only | 100.0% |
| Mismatch precision, including review prompts | 75.0% |
| Clean claims with any hard flag / any flag incl. review | 0.0% / 30.0% |
| Identical-file duplicate precision / recall | 100.0% / 100.0% |
| Similar-claim precision / recall | 100.0% / 100.0% |
| Review lane correct | 92.5% |
| Flags with explanation and suggested action | 100.0% |
| Flags with structured field evidence (doc, field, value) | 91.9% |
| Processing time per claim (median / p95) | 3155 ms / 3703 ms |
| Avg low-confidence review prompts per claim | 0.35 |
| Claims auto-approved or auto-rejected | 0 |

Caveat: synthetic, cleanly rendered documents with a fixed layout. Real scans, varied layouts and regional languages will perform worse. Treat these as a pipeline sanity check, not production accuracy.