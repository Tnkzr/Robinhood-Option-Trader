"""Pre-trade gates, duplicate-order protection, and fill confirmation.

A submitted order is never treated as a position. Only a brokerage-confirmed
fill creates one.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from config.settings import Mode, RiskLimits, Settings
from src.risk.session import SessionState


class OrderState(str, Enum):
    UNSUBMITTED = "unsubmitted"
    SUBMITTED = "submitted"     # sent, NOT a position
    CONFIRMED = "confirmed"     # broker acknowledged, still not filled
    FILLED = "filled"           # only this creates a position
    PARTIAL = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL = {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED}
OPEN_STATES = {OrderState.SUBMITTED, OrderState.CONFIRMED, OrderState.PARTIAL}


@dataclass
class Quote:
    bid: float
    ask: float
    as_of: datetime

    def is_stale(self, now: datetime | None = None, max_age_s: float = 5.0) -> bool:
        now = now or datetime.now(timezone.utc)
        return (now - self.as_of).total_seconds() > max_age_s

    @property
    def mark(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


def order_fingerprint(account: str, option_id: str, side: str, qty: int) -> str:
    raw = f"{account}|{option_id}|{side}|{qty}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class DuplicateGuard:
    """Blocks a second order for the same contract while one is still working."""

    def __init__(self, cooldown_s: float = 30.0) -> None:
        self._open: dict[str, datetime] = {}
        self.cooldown = timedelta(seconds=cooldown_s)

    def check(self, fp: str, now: datetime | None = None) -> str | None:
        now = now or datetime.now(timezone.utc)
        prior = self._open.get(fp)
        if prior and now - prior < self.cooldown:
            age = (now - prior).total_seconds()
            return f"duplicate order blocked: identical order sent {age:.0f}s ago"
        return None

    def record(self, fp: str, now: datetime | None = None) -> None:
        self._open[fp] = now or datetime.now(timezone.utc)

    def release(self, fp: str) -> None:
        self._open.pop(fp, None)


def preflight(
    *,
    settings: Settings,
    session: SessionState,
    stock_quote: Quote,
    option_quote: Quote,
    is_halted: bool,
    trigger_still_valid: bool,
    catalyst_still_valid: bool,
    minutes_needed: float,
    cost: float,
    account_value: float,
    open_positions: int,
    daily_pnl_pct: float,
    dup: str | None,
) -> GateResult:
    """Every gate from the spec's order-execution checklist, in one place."""
    f: list[str] = []

    if session.phase.value in ("premarket", "closed"):
        f.append(f"market not open (phase={session.phase.value})")
    if not session.may_open:
        f.append(f"entry window closed (phase={session.phase.value})")
    if is_halted:
        f.append("underlying is halted")
    if stock_quote.is_stale():
        f.append("stale stock quote")
    if option_quote.is_stale():
        f.append("stale option quote")
    if option_quote.bid <= 0 or option_quote.ask <= 0:
        f.append("option has no two-sided market")
    if not trigger_still_valid:
        f.append("entry trigger no longer present")
    if not catalyst_still_valid:
        f.append("catalyst invalidated or contradicted")
    if minutes_needed > session.minutes_to_exit_window:
        f.append(f"target needs {minutes_needed:.0f}m but only "
                 f"{session.minutes_to_exit_window:.0f}m before liquidation")
    if dup:
        f.append(dup)

    r: RiskLimits = settings.risk
    if open_positions >= r.max_positions:
        f.append(f"at position limit ({open_positions}/{r.max_positions})")
    if account_value > 0 and cost / account_value > r.max_pct_of_account_per_trade:
        f.append(f"size {cost/account_value:.0%} exceeds "
                 f"{r.max_pct_of_account_per_trade:.0%} cap")
    if daily_pnl_pct <= -r.max_daily_loss_pct:
        f.append(f"daily loss limit hit ({daily_pnl_pct:.0%})")

    return GateResult(passed=not f, failures=f)


def entry_limit_price(q: Quote, max_spread_cross: float = 0.5) -> float:
    """Limit orders only. Never pay through the offer."""
    cross = min(max(max_spread_cross, 0.0), 1.0)
    return round(q.bid + (q.ask - q.bid) * cross, 2)


def exit_limit_price(q: Quote, urgency: float) -> float:
    """Closing price that walks from mid toward the bid as the deadline nears."""
    urgency = min(max(urgency, 0.0), 1.0)
    return round(q.mark - (q.mark - q.bid) * urgency, 2)


def may_execute_live(settings: Settings, capabilities: set[str]) -> tuple[bool, str]:
    """Refuse live execution unless every required capability is present."""
    required = {"get_accounts", "get_portfolio", "get_option_positions",
                "get_option_quotes", "place_option_order", "get_option_orders",
                "cancel_option_order"}
    if settings.mode is not Mode.LIVE:
        return False, f"mode is {settings.mode.value}; no live orders"
    missing = required - capabilities
    if missing:
        return False, f"missing brokerage capabilities: {sorted(missing)} — fall back to advisory"
    if not settings.account_number:
        return False, "no account configured"
    return True, "live execution permitted"
