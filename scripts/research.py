"""Walk-forward research over the stored bars.

    python -m scripts.research --symbols SPY,QQQ,AAPL --families orb,momentum
    python -m scripts.research ... --holdout      # one-time holdout for families that pass

Every run is appended to research/ledger.jsonl; reports go to research/reports/.
The holdout is evaluated only for families that pass walk-forward, and never
twice on the same sessions.
"""
from __future__ import annotations

import argparse

from config.settings import RiskLimits
from src.research.bars import BarStore
from src.research.costs import CostModel
from src.research.edge import EdgeCriteria, VerdictStore
from src.research.ledger import HoldoutReuse, Ledger, new_run_id
from src.research.pipeline import (evaluate_holdout, load_dataset, render, research_family,
                                   write_report)
from src.research.strategies import FAMILIES
from src.research.walkforward import WFConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--families", default=",".join(FAMILIES))
    ap.add_argument("--store", default="data/bars")
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--equity", type=float, default=25_000.0)
    ap.add_argument("--cost-mult", type=float, default=1.0,
                    help="scale the base cost model (sensitivity runs)")
    ap.add_argument("--allow-short", action="store_true",
                    help="let the portfolio replay take short trades")
    a = ap.parse_args(argv)

    ds = load_dataset(BarStore(a.store), [s.strip().upper() for s in a.symbols.split(",")])
    costs = CostModel().stressed(a.cost_mult)
    crit, cfg = EdgeCriteria(), WFConfig()
    limits = RiskLimits(allow_short=a.allow_short)
    ledger, verdicts, run_id = Ledger(), VerdictStore(), new_run_id()

    out = [f"# Research run {run_id}", "",
           f"Calendar {ds.calendar[0]} → {ds.calendar[-1]} ({len(ds.calendar)} sessions), "
           f"symbols {', '.join(ds.symbols)}. Excluded {len(ds.excluded)} incomplete "
           f"symbol-sessions.", ""]
    families = [f.strip() for f in a.families.split(",")]
    for fam in families:
        fr = research_family(ds, fam, costs=costs, crit=crit, cfg=cfg, limits=limits,
                             ledger=ledger, run_id=run_id, run_families=families,
                             equity=a.equity)
        if a.holdout:
            try:
                fr.verdict = evaluate_holdout(ds, fr, costs=costs, crit=crit, ledger=ledger,
                                              run_id=run_id)
            except HoldoutReuse as e:
                out.append(f"> holdout refused: {e}")
        verdicts.save(fr.verdict)
        out.append(render(fr, ds))
    text = "\n".join(out)
    path = write_report(text, run_id)
    print(text)
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
