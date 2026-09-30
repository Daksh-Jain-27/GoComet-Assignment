"""Populate the store with ~weeks of history so NL queries like 'how many were flagged
this week?' have something real to answer. Timestamps are backdated over 28 days.
Runs the REAL pipeline on each document (offline mode by default = free)."""
import _path  # noqa: F401
import argparse
import random
from datetime import datetime, timedelta, timezone

from nova.config import ROOT
from nova.graph import run_document
from nova.storage import record_feedback

ap = argparse.ArgumentParser()
ap.add_argument("--mode", default="offline", choices=["offline", "llm"])
ap.add_argument("--days", type=int, default=28)
a = ap.parse_args()

rng = random.Random(1)
files = sorted((ROOT / "data" / "seed").glob("*.pdf"))
if not files:
    raise SystemExit("No seed docs. Run: python scripts/generate_samples.py --seed-set 30")
now = datetime.now(timezone.utc)
for f in files:
    ts = now - timedelta(days=rng.uniform(0, a.days), hours=rng.uniform(0, 8))
    s = run_document(str(f), "acme_electronics", mode=a.mode, force=True,
                     created_at=ts.strftime("%Y-%m-%d %H:%M:%S"))
    # simulate CG reviewing most older items (online-metric signal)
    if ts < now - timedelta(days=2) and rng.random() < 0.8:
        record_feedback(s["run_id"], "accept_decision" if rng.random() < 0.9 else "override_decision")
    print(f"{f.name}: {s['decision']['action']:13} {ts:%Y-%m-%d}")
