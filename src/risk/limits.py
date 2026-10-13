"""Deterministic risk controls shared by the backtester, paper trading and live.

Nothing here consults a model. Every decision is a pure function of numbers, so
the same rule produces the same answer in research and in production.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from config.settings import RiskLimits


MIN_NOTIONAL = 1.0      # Robinhood's smallest dollar-based order


@dataclass(frozen=True)
class SizeDecision:
    shares: float
    risk_dollars: float
    notional: float
    reason: str | None = None   # set when shares == 0


def _floor(x: float, decimals: int) -> float:
    k = 10 ** decimals
    return math.floor(x * k + 1e-9) / k


def position_size(equity: float, entry: float, stop: float, limits: RiskLimits,
                  gross_exposure: float = 0.0) -> SizeDecision:
    """The largest size within every cap — risk at the stop, capital committed,
    and gross leverage. Always rounds down (to whole shares, or 6 decimals when
    fractional shares are enabled); never rounds up to make a trade fit.
    """
    per_share = abs(entry - stop)
    if equity <= 0:
        return SizeDecision(0, 0.0, 0.0, "no equity")
    if per_share <= 0 or entry <= 0:
        return SizeDecision(0, 0.0, 0.0, "stop must differ from entry")
    room = min(equity * limits.max_position_pct,
               equity * limits.max_gross_leverage - gross_exposure)
    raw = min(equity * limits.risk_per_trade_pct / per_share, max(room, 0.0) / entry)
    shares = _floor(raw, 6) if limits.fractional_shares else float(math.floor(raw))
    notional = shares * entry
    if shares <= 0 or notional < (MIN_NOTIONAL if limits.fractional_shares else entry):
        why = ("no capital room under position/leverage caps" if room < MIN_NOTIONAL
               else "risk budget below one share" if not limits.fractional_shares
               else "size below the minimum order")
        return SizeDecision(0, 0.0, 0.0, why)
    return SizeDecision(shares, round(shares * per_share, 2), round(notional, 2))


def option_contracts_for_risk(equity: float, premium_per_contract: float,
                              limits: RiskLimits) -> SizeDecision:
    """Long premium: the whole premium is both the capital and the loss at the stop."""
    if premium_per_contract <= 0:
        return SizeDecision(0, 0.0, 0.0, "invalid premium")
    budget = equity * min(limits.risk_per_trade_pct, limits.max_position_pct)
    n = math.floor(budget / premium_per_contract)
    if n < 1:
        return SizeDecision(0, 0.0, 0.0,
                            f"one contract (${premium_per_contract:.2f}) exceeds "
                            f"the per-trade budget ${budget:.2f}")
    cost = round(n * premium_per_contract, 2)
    return SizeDecision(n, cost, cost)


@dataclass
class OpenPosition:
    symbol: str
    direction: int          # +1 long, -1 short
    shares: float
    entry: float
    stop: float

    @property
    def notional(self) -> float:
        return self.shares * self.entry


@dataclass
class RiskState:
    """Session-scoped bookkeeping. Call roll_session() at each new trading day."""
    session: date
    start_equity: float
    realized_pnl: float = 0.0
    consecutive_losses: int = 0
    positions: dict[str, OpenPosition] = field(default_factory=dict)
    last_loss_at: dict[str, datetime] = field(default_factory=dict)

    @property
    def equity(self) -> float:
        return self.start_equity + self.realized_pnl

    @property
    def gross_exposure(self) -> float:
        return sum(p.notional for p in self.positions.values())

    def roll_session(self, session: date) -> None:
        if session == self.session:
            return
        self.start_equity = self.equity
        self.session = session
        self.realized_pnl = 0.0
        self.consecutive_losses = 0
        self.last_loss_at.clear()

    def open(self, pos: OpenPosition) -> None:
        if pos.symbol in self.positions:
            raise ValueError(f"already holding {pos.symbol}; adding is not permitted")
        self.positions[pos.symbol] = pos

    def close(self, symbol: str, pnl: float, at: datetime) -> None:
        self.positions.pop(symbol, None)
        self.realized_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_at[symbol] = at
        else:
            self.consecutive_losses = 0


@dataclass(frozen=True)
class RiskCheck:
    passed: bool
    failures: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.passed


def check_new_entry(state: RiskState, *, symbol: str, direction: int, entry: float,
                    stop: float, risk_dollars: float, notional: float,
                    limits: RiskLimits, at: datetime,
                    kill_switch: bool = False) -> RiskCheck:
    f: list[str] = []
    if kill_switch:
        f.append("kill switch engaged")
    if state.realized_pnl <= -limits.max_daily_loss_pct * state.start_equity:
        f.append(f"daily loss limit reached ({state.realized_pnl:+.2f} vs "
                 f"-{limits.max_daily_loss_pct:.1%} of {state.start_equity:.2f})")
    if (limits.max_consecutive_losses is not None
            and state.consecutive_losses >= limits.max_consecutive_losses):
        f.append(f"{state.consecutive_losses} consecutive losses — no new entries this session")
    if symbol in state.positions:
        f.append(f"already holding {symbol} — no adding, no averaging down")
    if len(state.positions) >= limits.max_positions:
        f.append(f"position limit {limits.max_positions} reached")
    if direction not in (1, -1):
        f.append("direction must be +1 or -1")
    elif direction < 0 and not limits.allow_short:
        f.append("short selling not enabled")
    if direction == 1 and not stop < entry:
        f.append("long stop must be below entry")
    if direction == -1 and not stop > entry:
        f.append("short stop must be above entry")
    budget = limits.risk_per_trade_pct * state.equity
    if risk_dollars > budget + 0.01:
        f.append(f"risk ${risk_dollars:.2f} exceeds {limits.risk_per_trade_pct:.2%} "
                 f"budget ${budget:.2f}")
    cap = limits.max_position_pct * state.equity
    if notional > cap + 0.01:
        f.append(f"position ${notional:.2f} exceeds {limits.max_position_pct:.0%} "
                 f"of equity (${cap:.2f})")
    if state.gross_exposure + notional > limits.max_gross_leverage * state.equity + 0.01:
        f.append("gross exposure would exceed leverage cap")
    last = state.last_loss_at.get(symbol)
    if last and at - last < timedelta(minutes=limits.cooldown_after_loss_min):
        f.append(f"{symbol} lost {(at - last).total_seconds() / 60:.0f}m ago — "
                 f"cooldown {limits.cooldown_after_loss_min:.0f}m")
    return RiskCheck(not f, tuple(f))


def kill_switch_engaged(path: str = "KILL_SWITCH") -> bool:
    """A file on disk or TRADER_KILL_SWITCH=1 halts all new orders."""
    return os.path.exists(path) or os.getenv("TRADER_KILL_SWITCH", "") == "1"
