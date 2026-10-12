"""Replay simulated trades through the live risk controls, in time order.

Uses src/risk/limits.py — the same code paper and live trading use — so a rule
that would block a live order also blocks the backtested one. Only P&L realized
before an entry's timestamp is visible when deciding that entry.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import date

from config.settings import RiskLimits
from src.research.costs import CostModel
from src.research.engine import Trade
from src.risk.limits import OpenPosition, RiskState, check_new_entry, position_size


@dataclass(frozen=True)
class FilledTrade:
    trade: Trade
    shares: int
    pnl: float
    fees: float


@dataclass
class PortfolioResult:
    starting_equity: float
    accepted: list[FilledTrade] = field(default_factory=list)
    rejected: list[tuple[Trade, tuple[str, ...]]] = field(default_factory=list)
    session_equity: list[tuple[date, float]] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return self.session_equity[-1][1] if self.session_equity else self.starting_equity

    @property
    def total_return(self) -> float:
        return self.final_equity / self.starting_equity - 1

    @property
    def max_drawdown_pct(self) -> float:
        peak, mdd = self.starting_equity, 0.0
        for _, e in self.session_equity:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak)
        return mdd

    def rejection_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, reasons in self.rejected:
            key = reasons[0].split(" (")[0].split(" — ")[0]
            out[key] = out.get(key, 0) + 1
        return out


def run_portfolio(trades: list[Trade], limits: RiskLimits, starting_equity: float,
                  costs: CostModel) -> PortfolioResult:
    res = PortfolioResult(starting_equity)
    if not trades:
        return res
    ordered = sorted(trades, key=lambda t: (t.entry_ts, t.symbol, t.strategy))
    state = RiskState(session=ordered[0].session, start_equity=starting_equity)
    live: list[tuple] = []   # heap of (exit_ts, seq, FilledTrade)

    def realize_until(ts) -> None:
        while live and (ts is None or live[0][0] <= ts):
            exit_ts, _, ft = heapq.heappop(live)
            state.close(ft.trade.symbol, ft.pnl, exit_ts)

    for seq, t in enumerate(ordered):
        if t.session != state.session:
            realize_until(None)
            res.session_equity.append((state.session, round(state.equity, 2)))
            state.roll_session(t.session)
        realize_until(t.entry_ts)
        size = position_size(state.equity, t.entry, t.stop, limits, state.gross_exposure)
        if size.shares == 0:
            res.rejected.append((t, (size.reason or "size zero",)))
            continue
        chk = check_new_entry(state, symbol=t.symbol, direction=t.direction, entry=t.entry,
                              stop=t.stop, risk_dollars=size.risk_dollars,
                              notional=size.notional, limits=limits, at=t.entry_ts)
        if not chk:
            res.rejected.append((t, chk.failures))
            continue
        fees = (costs.fees(size.shares, t.entry, t.direction)
                + costs.fees(size.shares, t.exit, -t.direction))
        pnl = size.shares * (t.exit - t.entry) * t.direction - fees
        ft = FilledTrade(t, size.shares, round(pnl, 2), round(fees, 4))
        state.open(OpenPosition(t.symbol, t.direction, size.shares, t.entry, t.stop))
        heapq.heappush(live, (t.exit_ts, seq, ft))
        res.accepted.append(ft)

    realize_until(None)
    res.session_equity.append((state.session, round(state.equity, 2)))
    return res
