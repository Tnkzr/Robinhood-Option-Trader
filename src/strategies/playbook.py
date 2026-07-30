"""Professional intraday setups, each gated on a verified DirectionalThesis.

Design rule: a setup may not fire without a thesis. Technical patterns are
*triggers* for a fundamental reason to express itself — never reasons on their
own. That inversion is what produced the 2026-07-30 error.

Risk rule borrowed from the traders who survive: size from the stop, not from
the account. Reward-to-risk below 2:1 is declined regardless of conviction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum

from config.settings import Direction
from src.news.catalyst import CatalystType, DirectionalThesis


class Session(str, Enum):
    """Time-of-day regimes. Edge is not uniform across the session."""
    OPENING_DRIVE = "opening_drive"      # 9:30-10:30 ET — highest edge
    MORNING_TREND = "morning_trend"      # 10:30-11:30 ET — continuation
    MIDDAY_CHOP = "midday_chop"          # 11:30-14:00 ET — where accounts die
    AFTERNOON_TREND = "afternoon_trend"  # 14:00-15:15 ET — second-best window
    CLOSING = "closing"                  # 15:15+ ET — manage only

    @property
    def quality(self) -> float:
        return {Session.OPENING_DRIVE: 1.00, Session.MORNING_TREND: 0.85,
                Session.MIDDAY_CHOP: 0.35, Session.AFTERNOON_TREND: 0.80,
                Session.CLOSING: 0.0}[self]

    @classmethod
    def of(cls, t: time) -> "Session":
        if t < time(10, 30):
            return cls.OPENING_DRIVE
        if t < time(11, 30):
            return cls.MORNING_TREND
        if t < time(14, 0):
            return cls.MIDDAY_CHOP
        if t < time(15, 15):
            return cls.AFTERNOON_TREND
        return cls.CLOSING


@dataclass
class Tape:
    """Live intraday state. Populated from market data, never from narrative."""
    spot: float
    vwap: float
    atr: float
    opening_range_high: float
    opening_range_low: float
    premarket_high: float
    premarket_low: float
    prev_day_high: float
    prev_day_low: float
    high_of_day: float
    low_of_day: float
    relative_volume: float
    bars_held_beyond_level: int = 0
    volume_expanding: bool = False
    higher_lows: int = 0            # consecutive
    lower_highs: int = 0
    reclaimed_vwap: bool = False
    rejected_vwap: bool = False
    consecutive_up_days: int = 0    # for first-red-day
    consecutive_down_days: int = 0
    sector_rel_strength_pct: float = 0.0   # ticker vs its sector, today
    index_rel_strength_pct: float = 0.0    # ticker vs SPY, today

    @property
    def above_vwap(self) -> bool:
        return self.spot > self.vwap

    @property
    def extension_atr(self) -> float:
        """How stretched from VWAP, in ATR units. >2.0 = do not initiate."""
        return abs(self.spot - self.vwap) / self.atr if self.atr > 0 else 0.0


@dataclass
class Setup:
    name: str
    ticker: str
    direction: Direction
    entry_trigger: str
    entry_level: float
    invalidation: float
    target_1: float
    target_2: float
    confidence: float
    evidence: list[str]
    against: list[str]

    @property
    def risk(self) -> float:
        return abs(self.entry_level - self.invalidation)

    @property
    def reward_risk(self) -> float:
        return abs(self.target_1 - self.entry_level) / self.risk if self.risk else 0.0

    def acceptable(self, min_rr: float = 2.0) -> tuple[bool, str]:
        if self.risk <= 0:
            return False, "no defined risk — invalidation equals entry"
        if self.reward_risk < min_rr:
            return False, f"reward:risk {self.reward_risk:.1f} below {min_rr:.1f}:1"
        return True, "ok"


# --------------------------------------------------------------------------
# The setups. Each takes a *validated* thesis; none infers direction on its own.
# --------------------------------------------------------------------------

def gap_and_go(th: DirectionalThesis, t: Tape, gap_pct: float) -> Setup | None:
    """The highest-expectancy momentum trade: news gap + volume + level break.

    Requires a real catalyst gap (not drift), heavy relative volume, and the
    premarket level to break AND hold. Holding is what separates this from
    buying the open and being trapped.
    """
    if abs(gap_pct) < 2.0 or t.relative_volume < 2.0:
        return None
    if t.bars_held_beyond_level < 2:
        return None

    if th.direction is Direction.CALL:
        if not (gap_pct > 0 and t.spot > t.premarket_high):
            return None
        entry, inval = t.premarket_high, min(t.premarket_high - 0.3 * t.atr, t.vwap)
        t1, t2 = t.spot + 0.8 * t.atr, t.spot + 1.6 * t.atr
    else:
        if not (gap_pct < 0 and t.spot < t.premarket_low):
            return None
        entry, inval = t.premarket_low, max(t.premarket_low + 0.3 * t.atr, t.vwap)
        t1, t2 = t.spot - 0.8 * t.atr, t.spot - 1.6 * t.atr

    return Setup(
        "gap_and_go", th.ticker, th.direction,
        f"held {t.bars_held_beyond_level} bars beyond premarket level on {t.relative_volume:.1f}x volume",
        entry, inval, t1, t2,
        confidence=min(95.0, 55 + 25 * th.catalyst_type.profile.reliability
                       + 5 * min(t.relative_volume, 4)),
        evidence=[f"gap {gap_pct:+.1f}% on {th.catalyst_type.value}",
                  f"rvol {t.relative_volume:.1f}x", th.mechanism],
        against=["gaps fill when the catalyst is weaker than the move"],
    )


def opening_range_breakout(th: DirectionalThesis, t: Tape) -> Setup | None:
    """ORB — break of the first 15-30 min range, with the index agreeing."""
    if t.relative_volume < 1.5 or not t.volume_expanding:
        return None
    if th.direction is Direction.CALL:
        if t.spot <= t.opening_range_high or t.index_rel_strength_pct < 0:
            return None
        entry, inval = t.opening_range_high, t.opening_range_low
        t1, t2 = entry + (entry - t.opening_range_low), entry + 2 * (entry - t.opening_range_low)
    else:
        if t.spot >= t.opening_range_low or t.index_rel_strength_pct > 0:
            return None
        entry, inval = t.opening_range_low, t.opening_range_high
        t1, t2 = entry - (t.opening_range_high - entry), entry - 2 * (t.opening_range_high - entry)

    return Setup(
        "opening_range_breakout", th.ticker, th.direction,
        "opening range broken with volume expansion and index agreement",
        entry, inval, t1, t2,
        confidence=70.0, evidence=["ORB with volume", th.mechanism],
        against=["ORB fails often in low-volatility regimes"],
    )


def vwap_reclaim(th: DirectionalThesis, t: Tape) -> Setup | None:
    """VWAP is the institutional benchmark. Reclaims and rejections are where
    size enters. Requires the reclaim/rejection to be fresh, not extended."""
    if t.extension_atr > 1.5:
        return None  # too stretched — the entry is gone
    if th.direction is Direction.CALL:
        if not (t.reclaimed_vwap and t.higher_lows >= 2):
            return None
        entry, inval = t.vwap, t.vwap - 0.4 * t.atr
    else:
        if not (t.rejected_vwap and t.lower_highs >= 2):
            return None
        entry, inval = t.vwap, t.vwap + 0.4 * t.atr

    sign = 1 if th.direction is Direction.CALL else -1
    return Setup(
        "vwap_reclaim", th.ticker, th.direction,
        "VWAP reclaimed/rejected with structure confirming",
        entry, inval, entry + sign * 0.8 * t.atr, entry + sign * 1.6 * t.atr,
        confidence=68.0,
        evidence=[f"{t.higher_lows or t.lower_highs} consecutive structure points",
                  th.mechanism],
        against=["VWAP loss invalidates immediately"],
    )


def relative_strength_leader(th: DirectionalThesis, t: Tape) -> Setup | None:
    """Trade the leader, not the laggard.

    A name outperforming both its sector and the index on a catalyst day is
    where institutional demand actually is. The single most reliable filter in
    momentum trading — and the one most often skipped in favour of the cheaper
    sympathy name.
    """
    if th.direction is Direction.CALL:
        if t.sector_rel_strength_pct < 1.0 or t.index_rel_strength_pct < 1.0:
            return None
        if not t.above_vwap:
            return None
        entry, inval = t.spot, t.vwap
        t1, t2 = t.spot + 0.7 * t.atr, t.spot + 1.5 * t.atr
    else:
        if t.sector_rel_strength_pct > -1.0 or t.index_rel_strength_pct > -1.0:
            return None
        if t.above_vwap:
            return None
        entry, inval = t.spot, t.vwap
        t1, t2 = t.spot - 0.7 * t.atr, t.spot - 1.5 * t.atr

    return Setup(
        "relative_strength_leader", th.ticker, th.direction,
        "outperforming sector and index while holding VWAP",
        entry, inval, t1, t2,
        confidence=76.0,
        evidence=[f"{t.sector_rel_strength_pct:+.1f}% vs sector",
                  f"{t.index_rel_strength_pct:+.1f}% vs index", th.mechanism],
        against=["relative strength mean-reverts when the catalyst is fully priced"],
    )


def failed_breakout_trap(th: DirectionalThesis, t: Tape) -> Setup | None:
    """Trapped participants are the cleanest fuel available.

    A breakout that fails back through its level forces late longs to exit into
    your direction. Requires the level to be genuinely lost, not just tested.
    """
    if th.direction is Direction.PUT:
        if not (t.high_of_day > t.opening_range_high and t.spot < t.opening_range_high
                and not t.above_vwap):
            return None
        entry, inval = t.opening_range_high, t.high_of_day
        t1, t2 = entry - 1.0 * t.atr, entry - 1.8 * t.atr
    else:
        if not (t.low_of_day < t.opening_range_low and t.spot > t.opening_range_low
                and t.above_vwap):
            return None
        entry, inval = t.opening_range_low, t.low_of_day
        t1, t2 = entry + 1.0 * t.atr, entry + 1.8 * t.atr

    return Setup(
        "failed_breakout_trap", th.ticker, th.direction,
        "breakout failed and level reclaimed against the crowd",
        entry, inval, t1, t2,
        confidence=74.0,
        evidence=["trapped participants must exit into our direction", th.mechanism],
        against=["reversals are lower base-rate than continuations"],
    )


def first_red_day(th: DirectionalThesis, t: Tape) -> Setup | None:
    """Extended multi-day runner posts its first down day — momentum unwinds.

    High expectancy on parabolic names, but only when the move is genuinely
    extended and the catalyst has exhausted.
    """
    if th.direction is not Direction.PUT:
        return None
    if t.consecutive_up_days < 3 or t.extension_atr < 1.5:
        return None
    if t.above_vwap or t.lower_highs < 2:
        return None
    entry, inval = t.vwap, t.high_of_day
    return Setup(
        "first_red_day", th.ticker, Direction.PUT,
        "first red day after an extended run, below VWAP with lower highs",
        entry, inval, entry - 1.0 * t.atr, entry - 2.0 * t.atr,
        confidence=72.0,
        evidence=[f"{t.consecutive_up_days} up days", f"{t.extension_atr:.1f}x ATR extended",
                  th.mechanism],
        against=["parabolic names can squeeze violently before breaking"],
    )


def sympathy_laggard(th: DirectionalThesis, t: Tape, peer_move_pct: float,
                     this_move_pct: float) -> Setup | None:
    """The un-repriced peer. Only valid while the gap is still open."""
    if th.catalyst_type is not CatalystType.SECTOR_SYMPATHY:
        return None
    if abs(peer_move_pct) < 5.0:
        return None
    captured = abs(this_move_pct) / abs(peer_move_pct)
    if captured > 0.60:
        return None  # already caught up; edge gone
    sign = 1 if th.direction is Direction.CALL else -1
    if (sign > 0 and not t.above_vwap) or (sign < 0 and t.above_vwap):
        return None
    entry = t.spot
    return Setup(
        "sympathy_laggard", th.ticker, th.direction,
        f"peer moved {peer_move_pct:+.1f}%, this name only {this_move_pct:+.1f}%",
        entry, t.vwap, entry + sign * 0.8 * t.atr, entry + sign * 1.5 * t.atr,
        confidence=62.0,
        evidence=[f"only {captured:.0%} of the peer move captured", th.mechanism],
        against=["sympathy moves are weaker and fade faster than the primary"],
    )


ALL_SETUPS = (gap_and_go, opening_range_breakout, vwap_reclaim,
              relative_strength_leader, failed_breakout_trap, first_red_day)


def evaluate(th: DirectionalThesis, t: Tape, now: time,
             gap_pct: float = 0.0, min_rr: float = 2.0) -> list[Setup]:
    """Run every setup against a validated thesis. Returns only tradeable ones."""
    sess = Session.of(now)
    if sess is Session.CLOSING:
        return []

    out: list[Setup] = []
    for fn in ALL_SETUPS:
        try:
            s = fn(th, t, gap_pct) if fn is gap_and_go else fn(th, t)
        except Exception:
            continue
        if s is None:
            continue
        ok, _ = s.acceptable(min_rr)
        if not ok:
            continue
        s.confidence *= sess.quality
        if sess is Session.MIDDAY_CHOP:
            s.against.append("midday session — lowest-expectancy window")
        out.append(s)

    return sorted(out, key=lambda s: s.confidence, reverse=True)
