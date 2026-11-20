"""Entry point for the Cloud Run Job and local runs.

  python -m collectors all                 # indexing, gsc, bing, clarity, webflow, then rules
  python -m collectors gsc --backfill      # force the 16-month GSC backfill
  python -m collectors clarity --days 3    # recover up to 72 h of Clarity after a missed day
  python -m collectors import-legacy data/state.json

Exit code 1 if any collector failed outright or reported errors, so Cloud Run marks the execution failed.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from collectors import bing, clarity, gsc, indexing, webflow
from collectors.base import Run, log
from core import rules
from core.store import get_store

ORDER = ("webflow", "indexing", "gsc", "bing", "clarity")


def run_rules(store) -> dict:
    run = Run(store, "rules")
    res = rules.sync_actions(store, rules.evaluate_index_rules(store.query("url_inventory")))
    for k, v in res.items():
        run.count(k, v)
    return run.finish()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collectors")
    ap.add_argument("name", choices=("all", "rules", "import-legacy") + ORDER)
    ap.add_argument("path", nargs="?", default="data/state.json")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--days", type=int, default=1, choices=(1, 2, 3))
    a = ap.parse_args(argv)
    store = get_store()

    if a.name == "import-legacy":
        indexing.import_legacy(store, a.path)
        return 0

    fns = {
        "webflow": lambda: webflow.collect(store),
        "indexing": lambda: indexing.collect(store),
        "gsc": lambda: gsc.collect(store, backfill=a.backfill),
        "bing": lambda: bing.collect(store),
        "clarity": lambda: clarity.collect(store, days=a.days),
        "rules": lambda: run_rules(store),
    }
    names = list(ORDER) + ["rules"] if a.name == "all" else [a.name]
    results = {}
    for n in names:
        try:
            results[n] = fns[n]()
        except Exception as e:  # one collector failing must not stop the others
            traceback.print_exc()
            results[n] = {"status": "failed", "errors": [f"{type(e).__name__}: {e}"]}
    statuses = {n: r.get("status") for n, r in results.items()}
    log("summary: " + ", ".join(f"{n}={s}" for n, s in statuses.items()))
    # "skipped" (secret not created yet) is reported but does not fail the job.
    return 1 if any(s in ("failed", "partial") for s in statuses.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
