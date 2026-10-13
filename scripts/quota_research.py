"""Measure the daily-minimum system on walk-forward out-of-sample sessions.

    python -m scripts.quota_research

Natural trades: each signal family's walk-forward OOS trades (honest — params
chosen on earlier data). Fills: the fixed quota-fill rule on the same sessions.
Saves a `quota_fill` verdict (its measured record, shown on every live fill) and
writes research/reports/<run>-quota.md. Appended to the ledger.
"""
from __future__ import annotations

import statistics as st
from collections import Counter
from dataclasses import asdict

from config.settings import QuotaPolicy, RiskLimits
from config.universe import UNIVERSE
from src.execution.quota import FILL_FAMILY, signal_families
from src.research.bars import BarStore
from src.research.costs import CostModel
from src.research.edge import EdgeCriteria, EdgeVerdict, Status, VerdictStore
from src.research.ledger import Ledger, new_run_id
from src.research.metrics import breakdown, summarize
from src.research.pipeline import load_dataset, simulate_grid, write_report
from src.research.portfolio import run_portfolio
from src.research.strategies import FAMILIES
from src.research.walkforward import WFConfig, split, walk_forward


def main() -> int:
    policy, limits, costs = QuotaPolicy(), RiskLimits(), CostModel.calibrated()
    ds = load_dataset(BarStore(), UNIVERSE)
    verdicts = VerdictStore().load()
    fams = signal_families(verdicts, policy)
    dev, holdout, folds = split(ds.calendar, WFConfig())
    oos_days = sorted({d for f in folds for d in f.test})
    natural = []
    for fam, _tier in fams:
        grid = FAMILIES[fam].grid()
        wf = walk_forward(simulate_grid(ds, grid, costs, set(dev)), dev, folds, WFConfig())
        natural += wf.oos_trades
    fill_strat = FAMILIES[FILL_FAMILY]()
    fills = simulate_grid(ds, [fill_strat], costs, set(oos_days))[fill_strat.key]
    pf = run_portfolio(natural, limits, 50.0, costs, fills=fills,
                       min_trades_per_day=policy.min_trades_per_day)
    system = [f.trade for f in pf.accepted]
    natural = [t for t in system if t.strategy != FILL_FAMILY]
    taken_fills = [t for t in system if t.strategy == FILL_FAMILY]

    per = Counter(t.session for t in system)
    counts = [per.get(d, 0) for d in oos_days]
    nat, fil, allm = summarize(natural), summarize(taken_fills), summarize(system)
    stressed_fills = simulate_grid(ds, [fill_strat], costs.stressed(1.5), set(oos_days))
    sf = summarize([t for t in stressed_fills[fill_strat.key]
                    if (t.session, t.symbol) in {(x.session, x.symbol) for x in taken_fills}],
                   bootstrap=False)

    run_id = new_run_id()
    v = EdgeVerdict(strategy=FILL_FAMILY, params=fill_strat.key, status=Status.NO_EDGE,
                    oos=fil.as_dict() if fil else None,
                    oos_lower_bound=fil.ci_low if fil else None,
                    stressed_expectancy_r=sf.expectancy_r if sf else None,
                    holdout=None,
                    by_regime={k: s.as_dict() for k, s in
                               breakdown(taken_fills, lambda t: t.regime or "unknown").items()},
                    reasons=("quota fill — measured, not an edge claim",), run_id=run_id,
                    data_start=str(oos_days[0]), data_end=str(dev[-1]),
                    families_tested=1, criteria=asdict(EdgeCriteria()))
    VerdictStore().save(v)

    def row(name, s):
        if not s:
            return f"| {name} | 0 | | | | |"
        ci = f"[{s.ci_low:+.3f}, {s.ci_high:+.3f}]" if s.ci_low is not None else "n/a"
        return (f"| {name} | {s.n} | {s.win_rate:.1%} | {s.expectancy_r:+.3f} | {ci} | "
                f"{s.profit_factor:.2f} |")

    lines = [f"# Daily-minimum system — run {run_id}", "",
             f"Policy: ≥{policy.min_trades_per_day} trades/day, fills at minute "
             f"{policy.checkpoint_min} (13:30 ET). Signal families: "
             f"{', '.join(f'{f} ({t})' for f, t in fams) or 'none'}. "
             f"{len(oos_days)} walk-forward out-of-sample sessions "
             f"({oos_days[0]} → {oos_days[-1]}); holdout untouched.", "",
             "All rows are trades the portfolio replay accepted under the live limits.", "",
             "| Slice | Trades | Win% | Expectancy R | 95% CI | PF |", "|---|---|---|---|---|---|",
             row("Natural signals", nat), row("Quota fills", fil), row("Whole system", allm), "",
             f"Trades per day: median {st.median(counts)}, min {min(counts)}, "
             f"days at or above {policy.min_trades_per_day}: "
             f"{sum(c >= policy.min_trades_per_day for c in counts)}/{len(counts)}.",
             f"Fill expectancy at 1.5× costs: {sf.expectancy_r:+.3f}R." if sf else "",
             "",
             f"Portfolio replay at $50, live limits ({limits.max_position_pct:.0%} per trade, "
             f"{limits.max_positions} positions, {limits.max_daily_loss_pct:.0%} daily stop): "
             f"{len(pf.accepted)} filled, "
             f"return {pf.total_return:+.2%} (${pf.final_equity - 50:+.2f}), "
             f"max drawdown {pf.max_drawdown_pct:.2%}.",
             ]
    text = "\n".join(lines) + "\n"
    write_report(text, run_id + "-quota")
    Ledger().append({"kind": "quota_system", "run_id": run_id, "data": ds.fingerprint,
                     "policy": asdict(policy), "families": fams,
                     "natural": nat.as_dict() if nat else None,
                     "fills": fil.as_dict() if fil else None,
                     "system": allm.as_dict() if allm else None,
                     "per_day_median": st.median(counts),
                     "portfolio_50": {"return": pf.total_return, "mdd": pf.max_drawdown_pct}})
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
