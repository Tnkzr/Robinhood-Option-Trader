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
                  costs: CostModel, fills: list[Trade] | None = None,
                  min_trades_per_day: int = 0) -> PortfolioResult:
    """Chronological replay. `fills` are quota-fill candidates: at each day's
    checkpoint the shortfall below `min_trades_per_day` (counted from entries
    actually accepted) is filled strongest-first, the same way the live scanner
    counts journal entries."""
    res = PortfolioResult(starting_equity)
    by_day: dict = {}
    for f in fills or []:
        by_day.setdefault(f.session, []).append(f)
    events = [(t.entry_ts, 0, t.symbol, t) for t in trades]
    events += [(min(x.entry_ts for x in fs), 1, "", fs) for fs in by_day.values()]
    if not events:
        return res
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    first = events[0][3]
    state = RiskState(session=(first.session if isinstance(first, Trade) else first[0].session),
                      start_equity=starting_equity)
    live: list[tuple] = []   # heap of (exit_ts, seq, FilledTrade)
    traded_today: set[str] = set()
    entries_today = 0
    seq = 0

    def realize_until(ts) -> None:
        while live and (ts is None or live[0][0] <= ts):
            exit_ts, _, ft = heapq.heappop(live)
            state.close(ft.trade.symbol, ft.pnl, exit_ts)

    def attempt(t: Trade) -> bool:
        nonlocal seq, entries_today
        size = position_size(state.equity, t.entry, t.stop, limits, state.gross_exposure)
        if size.shares == 0:
            res.rejected.append((t, (size.reason or "size zero",)))
            return False
        chk = check_new_entry(state, symbol=t.symbol, direction=t.direction, entry=t.entry,
                              stop=t.stop, risk_dollars=size.risk_dollars,
                              notional=size.notional, limits=limits, at=t.entry_ts)
        if not chk:
            res.rejected.append((t, chk.failures))
            return False
        fees = (costs.fees(size.shares, t.entry, t.direction)
                + costs.fees(size.shares, t.exit, -t.direction))
        pnl = size.shares * (t.exit - t.entry) * t.direction - fees
        ft = FilledTrade(t, size.shares, round(pnl, 2), round(fees, 4))
        state.open(OpenPosition(t.symbol, t.direction, size.shares, t.entry, t.stop))
        seq += 1
        heapq.heappush(live, (t.exit_ts, seq, ft))
        res.accepted.append(ft)
        traded_today.add(t.symbol)
        entries_today += 1
        return True

    for ts, kind, _, item in events:
        day = item.session if kind == 0 else item[0].session
        if day != state.session:
            realize_until(None)
            res.session_equity.append((state.session, round(state.equity, 2)))
            state.roll_session(day)
            traded_today.clear()
            entries_today = 0
        realize_until(ts)
        if kind == 0:
            attempt(item)
            continue
        for c in sorted(item, key=lambda x: (-x.score, x.symbol)):
            if entries_today >= min_trades_per_day:
                break
            if c.symbol in traded_today or c.symbol in state.positions:
                continue
            attempt(c)

    realize_until(None)
    res.session_equity.append((state.session, round(state.equity, 2)))
    return res
