"""Catalyst scoring, call-vs-put resolution, and opportunity ranking."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from config.settings import Direction, ScoreThresholds


class CatalystClass(str, Enum):
    CONFIRMED_POLICY = "confirmed_policy"
    EXECUTIVE_ACTION = "executive_action"
    FORMAL_PROPOSAL = "formal_proposal"
    NEGOTIATING_STATEMENT = "negotiating_statement"
    POLITICAL_OPINION = "political_opinion"
    UNVERIFIED_REPORT = "unverified_report"
    REPEATED_STATEMENT = "repeated_statement"
    EARNINGS = "earnings"
    ANALYST_ACTION = "analyst_action"
    REGULATORY = "regulatory"
    MACRO = "macro"

    @property
    def weight(self) -> float:
        """Confirmed actions outweigh commentary (spec: political news engine)."""
        return {
            CatalystClass.CONFIRMED_POLICY: 1.00,
            CatalystClass.EXECUTIVE_ACTION: 1.00,
            CatalystClass.REGULATORY: 0.95,
            CatalystClass.EARNINGS: 0.95,
            CatalystClass.MACRO: 0.85,
            CatalystClass.FORMAL_PROPOSAL: 0.70,
            CatalystClass.ANALYST_ACTION: 0.60,
            CatalystClass.NEGOTIATING_STATEMENT: 0.45,
            CatalystClass.POLITICAL_OPINION: 0.25,
            CatalystClass.REPEATED_STATEMENT: 0.10,
            CatalystClass.UNVERIFIED_REPORT: 0.00,   # never tradeable alone
        }[self]


class Bias(str, Enum):
    STRONG_BULL = "strong_bullish"
    MOD_BULL = "moderate_bullish"
    NEUTRAL = "neutral"
    MOD_BEAR = "moderate_bearish"
    STRONG_BEAR = "strong_bearish"
    UNCLEAR = "unclear"


@dataclass
class Catalyst:
    headline: str
    source: str
    source_url: str
    published_at: datetime
    received_at: datetime
    verified_at: datetime | None
    tickers: tuple[str, ...]
    klass: CatalystClass
    corroborating_sources: int = 0
    reliability: float = 0.0       # 0-100
    surprise: float = 0.0          # 0-100
    financial_impact: float = 0.0  # 0-100
    remaining_repricing: float = 0.0
    directional_clarity: float = 0.0
    vol_expansion: float = 0.0
    large_move_potential: float = 0.0

    def freshness(self, now: datetime | None = None) -> float:
        """Decays to zero over 120 minutes. Stale news is not a catalyst."""
        now = now or datetime.now(timezone.utc)
        age_min = (now - self.published_at).total_seconds() / 60
        if age_min < 0:
            return 0.0
        return max(0.0, 100.0 * (1.0 - age_min / 120.0))

    def is_tradeable(self, th: ScoreThresholds, now: datetime | None = None) -> tuple[bool, str]:
        if self.klass is CatalystClass.UNVERIFIED_REPORT:
            return False, "unverified report — never tradeable on its own"
        if self.verified_at is None:
            return False, "not verified against original source"
        if self.reliability < th.min_reliability:
            return False, f"reliability {self.reliability:.0f} < {th.min_reliability:.0f}"
        if self.freshness(now) < th.min_freshness:
            return False, f"freshness {self.freshness(now):.0f} < {th.min_freshness:.0f}"
        if self.corroborating_sources < 1 and self.klass.weight < 0.9:
            return False, "single uncorroborated source for a low-weight class"
        return True, "ok"

    def bias(self) -> Bias:
        if self.directional_clarity < 40:
            return Bias.UNCLEAR
        strength = self.klass.weight * (self.financial_impact / 100.0)
        if self.surprise >= 0:
            return Bias.STRONG_BULL if strength > 0.6 else Bias.MOD_BULL
        return Bias.STRONG_BEAR if strength > 0.6 else Bias.MOD_BEAR


@dataclass
class TapeEvidence:
    """Price/volume confirmation. Populated from market data, not from news."""
    above_vwap: bool = False
    below_vwap: bool = False
    reclaimed_vwap: bool = False
    rejected_vwap: bool = False
    higher_highs: bool = False
    lower_lows: bool = False
    relative_volume: float = 1.0
    sector_confirms: bool = False
    index_confirms: bool = False
    extended_from_vwap_atr: float = 0.0   # how stretched, in ATR units
    premium_just_spiked: bool = False
    spread_widening: bool = False
    volume_declining: bool = False


@dataclass
class DirectionalScore:
    bull: float
    bear: float
    chosen: Direction | None
    edge: float
    reason: str


def score_direction(cat: Catalyst, tape: TapeEvidence, th: ScoreThresholds) -> DirectionalScore:
    """Separate bullish and bearish scores; pick a side only on a clear edge."""
    base = cat.klass.weight * (abs(cat.surprise) / 100.0) * (cat.financial_impact / 100.0) * 100.0

    bull = bear = 0.0
    if cat.surprise > 0:
        bull += base
    elif cat.surprise < 0:
        bear += base

    bull += 12 * tape.above_vwap + 10 * tape.reclaimed_vwap + 10 * tape.higher_highs
    bull += 8 * tape.sector_confirms + 6 * tape.index_confirms
    bear += 12 * tape.below_vwap + 10 * tape.rejected_vwap + 10 * tape.lower_lows
    bear += 8 * tape.sector_confirms + 6 * tape.index_confirms

    volume_kicker = min(15.0, max(0.0, (tape.relative_volume - 1.0) * 10.0))
    if bull > bear:
        bull += volume_kicker
    elif bear > bull:
        bear += volume_kicker

    bull = min(100.0, bull)
    bear = min(100.0, bear)
    edge = abs(bull - bear)

    if cat.bias() is Bias.UNCLEAR:
        return DirectionalScore(bull, bear, None, edge, "unclear catalyst — no trade")
    if edge < th.min_directional_edge:
        return DirectionalScore(bull, bear, None, edge,
                                f"indecisive: edge {edge:.0f} < {th.min_directional_edge:.0f}")
    chosen = Direction.CALL if bull > bear else Direction.PUT
    return DirectionalScore(bull, bear, chosen, edge, f"{chosen.value} on edge {edge:.0f}")


@dataclass
class Opportunity:
    ticker: str
    catalyst: Catalyst
    tape: TapeEvidence
    direction: DirectionalScore
    contract_score: float = 0.0
    minutes_needed: float = 30.0
    notes: list[str] = field(default_factory=list)

    def opportunity_score(self, now: datetime | None = None) -> float:
        """0-100. Blends catalyst, tape, contract quality and time remaining."""
        c = self.catalyst
        catalyst_part = (
            0.25 * c.reliability
            + 0.20 * c.freshness(now)
            + 0.20 * c.remaining_repricing
            + 0.20 * c.financial_impact
            + 0.15 * c.large_move_potential
        )
        tape_part = min(100.0, (
            25 * (self.tape.sector_confirms + self.tape.index_confirms)
            + min(30.0, max(0.0, (self.tape.relative_volume - 1.0) * 20.0))
            + 20 * (self.tape.above_vwap or self.tape.below_vwap)
        ))
        return round(
            0.45 * catalyst_part
            + 0.25 * tape_part
            + 0.30 * self.contract_score,
            1,
        )


def rank(opps: list[Opportunity], th: ScoreThresholds,
         now: datetime | None = None) -> list[Opportunity]:
    """Rank the full eligible universe. No preferred ticker, ever."""
    eligible = []
    for o in opps:
        ok, why = o.catalyst.is_tradeable(th, now)
        if not ok:
            o.notes.append(f"rejected: {why}")
            continue
        if o.direction.chosen is None:
            o.notes.append(f"rejected: {o.direction.reason}")
            continue
        if o.opportunity_score(now) < th.min_opportunity_score:
            o.notes.append(f"rejected: opportunity {o.opportunity_score(now):.0f}"
                           f" < {th.min_opportunity_score:.0f}")
            continue
        eligible.append(o)
    return sorted(eligible, key=lambda o: o.opportunity_score(now), reverse=True)
