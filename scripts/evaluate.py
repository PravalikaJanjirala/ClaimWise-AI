"""Measure ClaimWise on a synthetic labelled dataset. Writes results/metrics.json, results/metrics.md, results/per_case.csv.

    python scripts/evaluate.py            # 40 cases, seed 7
    python scripts/evaluate.py --n 60 --seed 3

IMPORTANT: results are on synthetic, rendered documents with planted errors. They show the pipeline works
as designed; they do NOT establish production accuracy on real scans (needs real, consented carrier data).
"""
import argparse
import csv
import json
import statistics as stats
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claimwise.db import Database  # noqa: E402
from claimwise.normalize import name_similarity, norm_alnum, parse_amount, parse_date  # noqa: E402
from claimwise.orchestrator import Orchestrator  # noqa: E402
from claimwise.policy_service import PolicyService  # noqa: E402
from claimwise.schemas import FIELD_SPECS, STATUS_MATCH, STATUS_REVIEW  # noqa: E402
from claimwise.synthetic import build_dataset  # noqa: E402

CHECK_PREFIX_IGNORED = ("LOW_CONFIDENCE", "AGENT_ERROR", "DOC_TYPE_", "EXTRACTION_ERROR", "IMAGE_QUALITY")


def same(kind, got, truth):
    if kind in ("reg", "dl", "chassis", "policy_no"):
        return norm_alnum(got) == norm_alnum(str(truth))
    if kind == "date":
        return parse_date(got) == parse_date(str(truth))
    if kind == "amount":
        a = parse_amount(got)
        return a is not None and abs(a - float(truth)) < 0.5
    if kind == "name":
        return name_similarity(got, str(truth)) >= 0.999
    return " ".join(str(got).upper().split()) == " ".join(str(truth).upper().split())


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    cases = build_dataset(a.n, a.seed)
    carrier = {}
    for c in cases:
        carrier.update(c.carrier)
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "eval.db")
    orch = Orchestrator(db, PolicyService(data=carrier), store_dir=tmp / "files", use_llm=False)

    rows, field_stats = [], {"clean": [0, 0], "degraded": [0, 0]}
    tp = fp_strict = fp_all = exp_total = 0
    fp_strict_n = flagged_strict_total = flagged_all_total = 0
    dup = {"DUP_FILE": [0, 0, 0], "SIMILAR_CLAIM": [0, 0, 0]}  # tp, fp, fn
    lane_ok = expl_ok = expl_total = ev_ok = ev_total = 0
    review_prompts, times = [], []
    clean_fp_strict = clean_fp_all = n_clean = 0
    auto_decisions = 0

    for c in cases:
        cid = orch.submit_claim(c.docs, claimant_ref=c.case_id)
        claim = db.get_claim(cid)
        fs = db.get_findings(cid)
        ext = db.get_extractions(cid)
        times.append(claim["processing_ms"])
        if claim["status"] in ("APPROVED", "REJECTED"):
            auto_decisions += 1
        # extraction accuracy (strict, normalised) per quality tier
        if c.quality in field_stats:
            for doc_type, truth in c.truth.items():
                ex = ext.get(doc_type)
                for fname, _, kind in FIELD_SPECS[doc_type]:
                    field_stats[c.quality][1] += 1
                    fv = ex.fields.get(fname) if ex else None
                    if fv and same(kind, fv.value, truth[fname]):
                        field_stats[c.quality][0] += 1
        flagged = {f.check_id for f in fs if f.status != STATUS_MATCH and not f.check_id.startswith(CHECK_PREFIX_IGNORED)}
        strict = {f.check_id for f in fs if f.status not in (STATUS_MATCH, STATUS_REVIEW) and not f.check_id.startswith(CHECK_PREFIX_IGNORED)}
        exp = c.expected_flags
        tp += len(exp & flagged)
        exp_total += len(exp)
        fp_all += len(flagged - exp)
        flagged_all_total += len(flagged)
        fp_strict += len(strict - exp)
        flagged_strict_total += len(strict)
        for k in dup:
            dup[k][0] += int(k in exp and k in strict)
            dup[k][1] += int(k not in exp and k in strict)
            dup[k][2] += int(k in exp and k not in strict)
        if c.mutation == "clean":
            n_clean += 1
            clean_fp_all += int(bool(flagged))
            clean_fp_strict += int(bool(strict))
        lane_ok += int(claim["lane"] == c.expected_lane)
        for f in fs:
            if f.status != STATUS_MATCH:
                expl_total += 1
                expl_ok += int(bool(f.message) and bool(f.action))
                ev_total += 1
                ev_ok += int(bool(f.evidence))
        review_prompts.append(sum(1 for f in fs if f.check_id.startswith("LOW_CONFIDENCE")))
        rows.append({"case": c.case_id, "mutation": c.mutation, "quality": c.quality, "expected": " ".join(sorted(exp)),
                     "flagged": " ".join(sorted(flagged)), "strict": " ".join(sorted(strict)), "lane": claim["lane"],
                     "expected_lane": c.expected_lane, "score": claim["score"], "ms": claim["processing_ms"]})

    f_clean, f_deg = field_stats["clean"], field_stats["degraded"]
    allf = [f_clean[0] + f_deg[0], f_clean[1] + f_deg[1]]
    prec = lambda t, f: pct(t, t + f)
    m = {
        "cases": len(cases), "seed": a.seed,
        "extraction_field_accuracy_pct": {"all": pct(*allf), "clean_scans": pct(*f_clean), "degraded_scans": pct(*f_deg)},
        "mismatch_recall_pct": pct(tp, exp_total),
        "mismatch_precision_strict_pct": pct(flagged_strict_total - fp_strict, flagged_strict_total),
        "mismatch_precision_incl_review_prompts_pct": pct(flagged_all_total - fp_all, flagged_all_total),
        "clean_claims_with_any_flag_strict_pct": pct(clean_fp_strict, n_clean),
        "clean_claims_with_any_flag_incl_review_pct": pct(clean_fp_all, n_clean),
        "duplicate_file": {"precision_pct": prec(dup["DUP_FILE"][0], dup["DUP_FILE"][1]), "recall_pct": pct(dup["DUP_FILE"][0], dup["DUP_FILE"][0] + dup["DUP_FILE"][2])},
        "similar_claim": {"precision_pct": prec(dup["SIMILAR_CLAIM"][0], dup["SIMILAR_CLAIM"][1]), "recall_pct": pct(dup["SIMILAR_CLAIM"][0], dup["SIMILAR_CLAIM"][0] + dup["SIMILAR_CLAIM"][2])},
        "routing_lane_correct_pct": pct(lane_ok, len(cases)),
        "flags_with_explanation_and_action_pct": pct(expl_ok, expl_total),
        "flags_with_structured_field_evidence_pct": pct(ev_ok, ev_total),
        "processing_time_ms": {"median": round(stats.median(times)), "p95": round(sorted(times)[int(0.95 * (len(times) - 1))]), "mean": round(stats.mean(times))},
        "avg_low_confidence_prompts_per_claim": round(stats.mean(review_prompts), 2),
        "claims_auto_approved_or_rejected": auto_decisions,
    }
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(m, indent=2))
    with open(out / "per_case.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    md = ["# Measured results (synthetic dataset)", "", f"{len(cases)} synthetic claims, seed {a.seed}; about a third rendered as degraded scans "
          "(rotation, blur, noise, JPEG). Planted errors have ground-truth labels.", "",
          "| Metric | Result |", "|---|---|",
          f"| Field extraction accuracy (all / clean / degraded) | {m['extraction_field_accuracy_pct']['all']}% / {m['extraction_field_accuracy_pct']['clean_scans']}% / {m['extraction_field_accuracy_pct']['degraded_scans']}% |",
          f"| Planted-mismatch recall (flagged for attention) | {m['mismatch_recall_pct']}% |",
          f"| Mismatch precision, hard flags only | {m['mismatch_precision_strict_pct']}% |",
          f"| Mismatch precision, including review prompts | {m['mismatch_precision_incl_review_prompts_pct']}% |",
          f"| Clean claims with any hard flag / any flag incl. review | {m['clean_claims_with_any_flag_strict_pct']}% / {m['clean_claims_with_any_flag_incl_review_pct']}% |",
          f"| Identical-file duplicate precision / recall | {m['duplicate_file']['precision_pct']}% / {m['duplicate_file']['recall_pct']}% |",
          f"| Similar-claim precision / recall | {m['similar_claim']['precision_pct']}% / {m['similar_claim']['recall_pct']}% |",
          f"| Review lane correct | {m['routing_lane_correct_pct']}% |",
          f"| Flags with explanation and suggested action | {m['flags_with_explanation_and_action_pct']}% |",
          f"| Flags with structured field evidence (doc, field, value) | {m['flags_with_structured_field_evidence_pct']}% |",
          f"| Processing time per claim (median / p95) | {m['processing_time_ms']['median']} ms / {m['processing_time_ms']['p95']} ms |",
          f"| Avg low-confidence review prompts per claim | {m['avg_low_confidence_prompts_per_claim']} |",
          f"| Claims auto-approved or auto-rejected | {m['claims_auto_approved_or_rejected']} |", "",
          "Caveat: synthetic, cleanly rendered documents with a fixed layout. Real scans, varied layouts and regional languages "
          "will perform worse. Treat these as a pipeline sanity check, not production accuracy."]
    (out / "metrics.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
