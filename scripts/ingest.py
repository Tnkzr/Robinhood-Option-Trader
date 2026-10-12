"""Ingest saved get_equity_historicals results into the bar store.

    python -m scripts.ingest <saved-result.json> [...] [--store data/bars] [--replace]

The broker data reaches this repo only through Claude's tool calls; large
results are saved to disk by the harness and ingested from there.
"""
from __future__ import annotations

import argparse
import sys

from src.research.bars import AdjustmentMismatch, BarStore, ingest_file


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--store", default="data/bars")
    ap.add_argument("--replace", action="store_true",
                    help="discard stored bars for these symbols first (after a split)")
    a = ap.parse_args(argv)
    store, failed = BarStore(a.store), 0
    for f in a.files:
        try:
            for line in ingest_file(f, store, replace=a.replace):
                print(line)
        except AdjustmentMismatch as e:
            failed += 1
            print(f"REFUSED {f}: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
