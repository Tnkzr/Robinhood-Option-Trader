"""Live scanning: today's bars replayed through the backtest's own session loop.

`scan()` answers, for the bar that just closed: which validated strategies fire
an entry, and which open positions must exit. It decides nothing the backtest
did not decide the same way — `run_session` is shared code, not a re-implementation.

Deliberate differences from the backtest, all conservative:
* A signal is acted on only in the cycle right after its bar closes. If a cycle
  is missed the signal is skipped, never chased.
* Exits are checked on closed 5-minute bars, so a stop can fill later (worse)
  than the backtest's intrabar assumption. Live slippage is journalled and fed
  to the degradation monitor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from config.settings import RiskLimits
from src.execution.journal import LiveEntry, LiveJournal
from src.execution.proposal import ProposalRejected, TradeProposal, build_proposal
from src.research.bars import (ET, INTRADAY_MINUTES, BarStore, live_session,
                               scheduled_close, to_sessions)
from src.research.costs import TICK, CostModel
from src.research.degradation import check as degradation_check
from src.research.edge import EdgeVerdict
from src.research.engine import SymbolData, index_context, prepare, run_session
from src.research.regime import RegimeIndex
from src.research.strategies import FAMILIES, Strategy
from src.risk.limits import OpenPosition, RiskState

INTERVAL = "5minute"
STEP = timedelta(minutes=INTRADAY_MINUTES[INTERVAL])


@dataclass(frozen=True)
class EntrySignal:
    family: str
    params: str
    symbol: str
    direction: int
    signal_ts: datetime
    ref_price: float           # close of the signal bar
    stop: float
    target_r: float | None
    target_price: float | None
    note: str
    regime: str | None

    def target_for(self, fill: float) -> float:
        """Same rule as the engine: R-multiple targets are measured from the fill."""
        if self.target_price is not None:
            return self.target_price
        return fill + self.direction * (self.target_r or 0.0) * abs(fill - self.stop)


@dataclass(frozen=True)
class ExitSignal:
    entry: LiveEntry
    reason: str                # stop | target | eod
    bar_ts: datetime | None


@dataclass
class ScanResult:
    now: datetime
    session: date
    validated: list[str]
    entries: list[tuple[EntrySignal, TradeProposal]] = field(default_factory=list)
    rejected: list[tuple[EntrySignal, tuple[str, ...]]] = field(default_factory=list)
    exits: list[ExitSignal] = field(default_factory=list)
    halted: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def strategy_for(family: str, key: str) -> Strategy:
    for s in FAMILIES[family].grid():
        if s.key == key:
            return s
    raise KeyError(f"{key} is not in the {family} grid — verdict and code disagree")


def load_live(store: BarStore, symbols: list[str], day: date, now: datetime,
              index: str = "SPY") -> tuple[dict[str, SymbolData], dict, RegimeIndex, list[str]]:
    warn, data = [], {}
    for sym in sorted(set(symbols) | {index}):
        bars, daily = store.load(sym, INTERVAL), store.load(sym, "day")
        hist, _ = to_sessions([b for b in bars if b.et.date() < day], sym, INTERVAL)
        live = live_session(bars, sym, INTERVAL, day, now)
        if live is None:
            warn.append(f"{sym}: no complete bar sequence for {day} up to {now:%H:%M}Z — skipped")
            continue
        if not hist or not daily or daily[-1].ts.date() < hist[-1].date:
            warn.append(f"{sym}: daily bars end before the last intraday session — "
                        f"fetch daily bars; skipped")
            continue
        data[sym] = prepare(sym, hist + [live], daily, INTERVAL)
    idx = index_context(data[index]) if index in data else {}
    if index not in data:
        warn.append(f"index {index} unavailable — strategies needing it will not fire")
    return data, idx, RegimeIndex(store.load(index, "day")), warn


def exit_for(entry: LiveEntry, sd: SymbolData | None, now: datetime) -> ExitSignal | None:
    close_et = scheduled_close(now.astimezone(ET).date())
    if now.astimezone(ET) >= close_et - timedelta(minutes=15):
        return ExitSignal(entry, "eod", None)
    if sd is None:
        return None
    since = datetime.fromisoformat(entry.entry_ts.replace("Z", "+00:00"))
    d = entry.direction
    for b in sd.sessions[-1].bars:
        if b.ts + STEP <= since:
            continue
        if (b.low <= entry.stop) if d > 0 else (b.high >= entry.stop):
            return ExitSignal(entry, "stop", b.ts)
        if (b.high >= entry.target + TICK) if d > 0 else (b.low <= entry.target - TICK):
            return ExitSignal(entry, "target", b.ts)
    return None


def scan(store: BarStore, verdicts: dict[str, EdgeVerdict], journal: LiveJournal, *,
         now: datetime, equity: float, symbols: list[str], limits: RiskLimits,
         costs: CostModel, kill_switch: bool) -> ScanResult:
    day = now.astimezone(ET).date()
    validated = sorted(f for f, v in verdicts.items() if v.tradeable)
    res = ScanResult(now, day, validated)
    data, idx, regime, res.warnings = load_live(store, symbols, day, now)

    for e in journal.open_positions():
        x = exit_for(e, data.get(e.symbol), now)
        if x:
            res.exits.append(x)

    if not validated:
        res.warnings.append("no strategy has a validated edge — no entries")
        return res
    if now.astimezone(ET) >= scheduled_close(day) - timedelta(minutes=45):
        res.warnings.append("entry window closed")
        return res

    state: RiskState = journal.risk_state(day, equity)
    for fam in validated:
        v = verdicts[fam]
        live_r = journal.closed_r(fam)
        rep = degradation_check(v.oos["expectancy_r"], v.oos["std_r"], live_r)
        if rep.halt:
            res.halted[fam] = "; ".join(rep.reasons)
            continue
        strat = strategy_for(fam, v.params)
        for sym, sd in sorted(data.items()):
            run = run_session(sd, len(sd.sessions) - 1, strat, costs, index_ctx=idx,
                              regime=regime)
            if run is None or run.pending is None:
                continue
            intent, sig_ts = run.pending
            last = sd.sessions[-1].bars[-1]
            if sig_ts != last.ts:
                continue                       # stale: its entry bar already opened
            sig = EntrySignal(fam, v.params, sym, intent.direction, sig_ts, last.close,
                              intent.stop, intent.target_r, intent.target_price,
                              intent.note, run.regime)
            try:
                p = build_proposal(
                    ticker=sym, strategy=fam, params=v.params, direction=intent.direction,
                    entry_trigger=f"{intent.note} on the {sig_ts.astimezone(ET):%H:%M} ET bar",
                    limit_price=last.close, stop_price=intent.stop,
                    target_price=sig.target_for(last.close), regime=run.regime,
                    reasons_to_reject=[
                        "spread wider than 2x the cost model at order time",
                        "ask already at/through the stop or target (gap) — cancel",
                        "halt, or fresh verified news contradicting the direction"],
                    verdicts=verdicts, state=state, limits=limits, at=now,
                    kill_switch=kill_switch)
            except ProposalRejected as e:
                res.rejected.append((sig, e.reasons))
                continue
            res.entries.append((sig, p))
            state.open(OpenPosition(sym, p.direction, p.shares, p.limit_price, p.stop_price))
    return res


def spread_ok(symbol: str, bid: float, ask: float, costs: CostModel,
              tolerance: float = 2.0) -> tuple[bool, str]:
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, "no valid two-sided quote"
    half_bps = (ask - bid) / 2 / ((ask + bid) / 2) * 1e4
    modelled = costs.half_spread_for(symbol)
    ok = half_bps <= tolerance * modelled
    return ok, (f"half spread {half_bps:.2f}bp vs modelled {modelled:.2f}bp "
                f"({'ok' if ok else f'> {tolerance}x — skip'})")
