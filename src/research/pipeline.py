"""End-to-end research run: data → simulation → walk-forward → verdict → ledger."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from config.settings import RiskLimits
from src.research.bars import BarStore, to_sessions
from src.research.costs import CostModel
from src.research.edge import (EdgeCriteria, EdgeVerdict, Status, judge_holdout,
                               judge_walk_forward)
from src.research.engine import SymbolData, Trade, index_context, prepare, simulate
from src.research.ledger import Ledger
from src.research.metrics import breakdown, summarize, time_bucket
from src.research.portfolio import PortfolioResult, run_portfolio
from src.research.regime import RegimeIndex
from src.research.strategies import FAMILIES, Strategy
from src.research.walkforward import WFConfig, WFResult, split, walk_forward


@dataclass
class Dataset:
    symbols: list[str]
    interval: str
    data: dict[str, SymbolData]
    index_ctx: dict[datetime, bool]
    regime: RegimeIndex
    calendar: list[date]
    fingerprint: str
    excluded: list[str] = field(default_factory=list)


def load_dataset(store: BarStore, symbols: list[str], index_symbol: str = "SPY",
                 interval: str = "5minute") -> Dataset:
    syms = sorted(set(symbols) | {index_symbol})
    data, excluded = {}, []
    for s in syms:
        bars, daily = store.load(s, interval), store.load(s, "day")
        if not bars or not daily:
            raise FileNotFoundError(f"{s}: missing {interval if not bars else 'day'} bars "
                                    f"in {store.root} — ingest them first")
        sessions, ex = to_sessions(bars, s, interval)
        excluded += ex
        data[s] = prepare(s, sessions, daily, interval)
    fp = store.fingerprint(syms, interval) + "-" + store.fingerprint(syms, "day")
    return Dataset(syms, interval, data, index_context(data[index_symbol]),
                   RegimeIndex(store.load(index_symbol, "day")),
                   data[index_symbol].dates, fp, excluded)


def simulate_grid(ds: Dataset, strategies: list[Strategy], costs: CostModel,
                  dates: set[date]) -> dict[str, list[Trade]]:
    out: dict[str, list[Trade]] = {}
    for strat in strategies:
        ts: list[Trade] = []
        for s in ds.symbols:
            ts += simulate(ds.data[s], strat, costs, index_ctx=ds.index_ctx,
                           regime=ds.regime, dates=dates)
        out[strat.key] = ts
    return out


@dataclass
class FamilyResult:
    family: str
    verdict: EdgeVerdict
    wf: WFResult
    dev: list[date]
    holdout: list[date]
    portfolio: PortfolioResult | None
    grid_size: int
    limits: RiskLimits | None = None


def research_family(ds: Dataset, family: str, *, costs: CostModel, crit: EdgeCriteria,
                    cfg: WFConfig, limits: RiskLimits, ledger: Ledger, run_id: str,
                    run_families: list[str], equity: float = 25_000.0) -> FamilyResult:
    grid = FAMILIES[family].grid()
    dev, holdout, folds = split(ds.calendar, cfg)
    dev_set = set(dev)
    by_key = simulate_grid(ds, grid, costs, dev_set)
    stressed = simulate_grid(ds, grid, costs.stressed(crit.stress_cost_mult), dev_set)
    wf = walk_forward(by_key, dev, folds, cfg)
    stressed_oos = [t for f, key, _ in wf.folds if key
                    for t in stressed[key] if t.session in set(f.test)]
    fams = ledger.families_tested(including=run_families)
    verdict = judge_walk_forward(family, wf, stressed_oos, fams, crit, run_id,
                                 str(dev[0]) if dev else "", str(dev[-1]) if dev else "",
                                 data_through=str(ds.calendar[-1]))
    pf = run_portfolio(wf.oos_trades, limits, equity, costs) if wf.oos_trades else None
    ledger.append({
        "kind": "walk_forward", "run_id": run_id, "family": family, "data": ds.fingerprint,
        "symbols": ds.symbols, "dev": [str(dev[0]), str(dev[-1])] if dev else None,
        "holdout": [str(holdout[0]), str(holdout[-1])] if holdout else None,
        "grid_size": len(grid), "wf_config": asdict(cfg), "criteria": asdict(crit),
        "costs": asdict(costs),
        "folds": [{"test": [str(f.test[0]), str(f.test[-1])], "selected": k, "oos_trades": n}
                  for f, k, n in wf.folds],
        "oos": verdict.oos, "oos_lower_bound": verdict.oos_lower_bound,
        "stressed_expectancy_r": verdict.stressed_expectancy_r,
        "status": verdict.status.value, "reasons": list(verdict.reasons),
        "families_tested": fams})
    return FamilyResult(family, verdict, wf, dev, holdout, pf, len(grid), limits)


def evaluate_holdout(ds: Dataset, fr: FamilyResult, *, costs: CostModel, crit: EdgeCriteria,
                     ledger: Ledger, run_id: str) -> EdgeVerdict:
    v = fr.verdict
    if v.status is not Status.PENDING_HOLDOUT or not fr.holdout:
        return v
    ledger.assert_holdout_unused(fr.family, fr.holdout[0], fr.holdout[-1])
    strat = next(s for s in FAMILIES[fr.family].grid() if s.key == v.params)
    trades = simulate_grid(ds, [strat], costs, set(fr.holdout))[strat.key]
    out = judge_holdout(v, trades, crit)
    ledger.append({"kind": "holdout", "run_id": run_id, "family": fr.family,
                   "params": v.params, "data": ds.fingerprint,
                   "holdout_start": str(fr.holdout[0]), "holdout_end": str(fr.holdout[-1]),
                   "holdout": out.holdout, "status": out.status.value,
                   "reasons": list(out.reasons)})
    return out


def _row(name: str, s) -> str:
    if s is None:
        return f"| {name} | 0 | | | | | | |"
    return (f"| {name} | {s.n} | {s.win_rate:.1%} | {s.avg_win_r:+.2f} | {s.avg_loss_r:+.2f} | "
            f"{s.expectancy_r:+.3f} | {s.profit_factor:.2f} | {s.max_drawdown_r:.1f} |")


def render(fr: FamilyResult, ds: Dataset) -> str:
    v, wf = fr.verdict, fr.wf
    s = summarize(wf.oos_trades)
    head = "| Slice | Trades | Win% | Avg win R | Avg loss R | Expectancy R | PF | MaxDD R |\n" \
           "|---|---|---|---|---|---|---|---|"
    lines = [f"## {fr.family} — **{v.status.value.upper()}**", "",
             f"Run `{v.run_id}` · data `{ds.fingerprint}` · {len(ds.symbols)} symbols · "
             f"dev {fr.dev[0]} → {fr.dev[-1]} ({len(fr.dev)} sessions) · "
             f"holdout {'sealed' if v.holdout is None else 'evaluated'} "
             f"({len(fr.holdout)} sessions) · grid {fr.grid_size} · "
             f"families tested {v.families_tested}", ""]
    lines += [f"- {r}" for r in v.reasons] + [""]
    if s:
        ci = f"[{s.ci_low:+.3f}, {s.ci_high:+.3f}]" if s.ci_low is not None else "n/a"
        summary = (f"Walk-forward OOS: {s.n} trades over {s.sessions} sessions, "
                   f"expectancy {s.expectancy_r:+.3f}R (95% day-block CI {ci})")
        if v.oos_lower_bound is not None:
            summary += (f", adjusted lower bound {v.oos_lower_bound:+.3f}R, "
                        f"at {fr.verdict.criteria.get('stress_cost_mult')}x costs "
                        f"{v.stressed_expectancy_r:+.3f}R")
        lines += [summary + ".", "", head, _row("All OOS", s)]
        slices = (("", lambda t: "long" if t.direction > 0 else "short"),
                  ("", time_bucket),
                  ("regime ", lambda t: t.regime or "unknown"),
                  ("exit ", lambda t: t.exit_reason))
        for prefix, key in slices:
            for k, sub in breakdown(wf.oos_trades, key).items():
                lines.append(_row(prefix + k, sub))
        gross = sum(t.gross_r for t in wf.oos_trades) / len(wf.oos_trades)
        lines += ["", f"Cost drag: gross {gross:+.3f}R → net {s.expectancy_r:+.3f}R per trade."]
    lines += ["", "Folds (train → selected params → OOS trades):"]
    for f, k, n in wf.folds:
        lines.append(f"- test {f.test[0]}→{f.test[-1]}: {k or 'no qualifying params'} ({n})")
    if fr.portfolio:
        p = fr.portfolio
        rej = ", ".join(f"{k}: {n}" for k, n in sorted(p.rejection_counts().items())) or "none"
        L = fr.limits or RiskLimits()
        lines += ["", f"Portfolio replay on OOS ({L.max_position_pct:.0%} capital / "
                      f"{L.risk_per_trade_pct:.0%} risk cap per trade, "
                      f"{L.max_daily_loss_pct:.0%} daily stop, {L.max_consecutive_losses}-loss halt, "
                      f"{L.max_positions} positions, ${p.starting_equity:,.0f} start): "
                      f"{len(p.accepted)} filled, "
                      f"{len(p.rejected)} blocked ({rej}); return {p.total_return:+.2%}, "
                      f"max drawdown {p.max_drawdown_pct:.2%}."]
    if v.holdout:
        h = v.holdout
        lines += ["", f"Holdout: {h['n']} trades, win {h['win_rate']:.1%}, "
                      f"expectancy {h['expectancy_r']:+.3f}R, PF {h['profit_factor']:.2f}."]
    return "\n".join(lines) + "\n"


def write_report(text: str, run_id: str, root: str | Path = "research/reports") -> Path:
    p = Path(root) / f"{run_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p
