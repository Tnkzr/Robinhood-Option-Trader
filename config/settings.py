"""Typed configuration. Secrets come from the environment, never source."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import time
from enum import Enum


class Mode(str, Enum):
    DRY_RUN = "dry_run"      # evaluate only, never call an order tool
    PAPER = "paper"          # simulate fills, persist to journal
    LIVE = "live"            # real orders, requires capability verification


class Direction(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class SessionTimes:
    """All times are US/Eastern. Overnight holding is not representable."""
    market_open: time = time(9, 30)
    market_close: time = time(16, 0)
    entry_cutoff: time = time(15, 15)        # no new positions after
    exit_window_start: time = time(15, 45)   # begin closing everything
    final_deadline: time = time(15, 55)      # must be flat by

    def __post_init__(self) -> None:
        if not (self.market_open < self.entry_cutoff
                < self.exit_window_start < self.final_deadline
                < self.market_close):
            raise ValueError("session times must be strictly ordered within the session")


@dataclass(frozen=True)
class LiquidityGates:
    """Hard gates. Derived from the 21-trade review — see PLAYBOOK.md."""
    max_spread_pct_of_mark: float = 0.10
    min_open_interest: int = 500
    min_contract_volume: int = 100
    min_underlying_dollar_volume: float = 5_000_000.0


@dataclass(frozen=True)
class ScoreThresholds:
    min_opportunity_score: float = 65.0
    min_directional_edge: float = 20.0   # |bull - bear| required to pick a side
    min_reliability: float = 70.0
    min_freshness: float = 60.0
    exit_hold_score: float = 40.0        # below this, close the position


@dataclass(frozen=True)
class ReachabilityBands:
    """|strike - spot| / ATR(14). See PLAYBOOK.md section 3."""
    day_trade_min: float = 0.25
    day_trade_max: float = 1.00
    catalyst_max: float = 1.50           # above this only with a hard catalyst


@dataclass(frozen=True)
class RiskLimits:
    max_positions: int = 3
    max_pct_of_account_per_trade: float = 0.35
    max_daily_loss_pct: float = 0.25     # halt trading for the day
    size_assumes_total_loss: bool = True


@dataclass(frozen=True)
class ExpirationPolicy:
    allow_0dte: bool = False             # opt-in only
    forbidden_dte: tuple[int, ...] = (3, 4, 5, 6, 7, 8, 9)  # 0-for-3 in the record
    preferred_dte: tuple[int, ...] = (0, 1, 2)


@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.DRY_RUN
    session: SessionTimes = field(default_factory=SessionTimes)
    liquidity: LiquidityGates = field(default_factory=LiquidityGates)
    scores: ScoreThresholds = field(default_factory=ScoreThresholds)
    reachability: ReachabilityBands = field(default_factory=ReachabilityBands)
    risk: RiskLimits = field(default_factory=RiskLimits)
    expiration: ExpirationPolicy = field(default_factory=ExpirationPolicy)
    account_number: str | None = None
    journal_path: str = "journal/trades.jsonl"

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            mode=Mode(os.getenv("TRADER_MODE", "dry_run")),
            account_number=os.getenv("RH_ACCOUNT_NUMBER"),
            journal_path=os.getenv("TRADER_JOURNAL_PATH", "journal/trades.jsonl"),
        )
        if os.getenv("TRADER_ALLOW_0DTE", "").lower() in ("1", "true", "yes"):
            s = replace(s, expiration=replace(s.expiration, allow_0dte=True))
        return s

    def requires_live_verification(self) -> bool:
        return self.mode is Mode.LIVE
