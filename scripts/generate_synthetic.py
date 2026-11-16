"""Generate the demo sample claims (data/samples) and the mock carrier records (data/carrier_policies.json).

    python scripts/generate_synthetic.py            # 12 curated demo cases
    python scripts/generate_synthetic.py --eval 40  # also write a labelled evaluation set to data/eval_set
"""
import argparse
import json
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from claimwise import config  # noqa: E402
from claimwise.synthetic import CYCLE, build_case, build_dataset, write_cases  # noqa: E402


def demo_cases(seed=5):
    rng = random.Random(seed)
    cases = [build_case(rng, 1, "clean", "clean"), build_case(rng, 2, "clean", "degraded")]
    for i, m in enumerate(CYCLE):
        base = cases[0] if m == "duplicate_file" else cases[1] if m == "similar_claim" else None
        cases.append(build_case(rng, 3 + i, m, "clean" if i % 2 == 0 else "degraded", base))
    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", type=int, default=0, help="also write N labelled evaluation cases to data/eval_set")
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    samples = config.SAMPLES_DIR
    if samples.exists():
        shutil.rmtree(samples)
    cases = demo_cases(a.seed)
    carrier = write_cases(cases, samples)
    if a.eval:
        ev = config.DATA_DIR / "eval_set"
        if ev.exists():
            shutil.rmtree(ev)
        ecases = build_dataset(a.eval, 7)
        for c in ecases:
            carrier.update(c.carrier)
        write_cases(ecases, ev)
    config.CARRIER_DATA.parent.mkdir(parents=True, exist_ok=True)
    config.CARRIER_DATA.write_text(json.dumps(carrier, indent=1))
    print(f"Wrote {len(cases)} demo cases to {samples} and {len(carrier)} carrier records to {config.CARRIER_DATA}")
    for c in cases:
        print(f"  {c.case_id}_{c.mutation:<20} expected flags: {sorted(c.expected_flags) or '-'}")


if __name__ == "__main__":
    main()
