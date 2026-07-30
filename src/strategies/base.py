"""Strategy interface and the ten intraday setups.

Every strategy returns the same structured Signal so the ranker can compare
them on equal terms. A strategy never places an order; it only describes a
setup, its trigger, and what would invalidate it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from config.settings import Direction
from src.scanners.scoring import Catalyst, TapeEvidence


@dataclass
class MarketContext:
    ticker: str
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
    sector_change_pct: float
    index_change_pct: float
    gap_pct: float = 0.0
    breakout_held_bars: int = 0
    reclaimed_level: bool = False
    lost_level: bool = False


@dataclass
class Signal:
    setup: str
    ticker: str
    direction: Direction
    confidence: float                # 0-100
    entry_trigger: str
    invalidation: float              # underlying price that kills the thesis
    initial_target: float
    extended_target: float
    supporting: list[str] = field(default_factory=list)
    opposing: list[str] = field(default_factory=list)
    contract_hint: str = "near-the-money, tight spread, high OI"

    @property
    def risk_reward(self) -> float:
        risk = abs(self.invalidation)
        return abs(self.initial_target) / risk if risk else 0.0


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def evaluate(self, ctx: MarketContext, cat: Catalyst,
                 tape: TapeEvidence) -> Signal | None:
        ...

    @staticmethod
    def _targets(ctx: MarketContext, direction: Direction) -> tuple[float, float, float]:
        """Targets and invalidation expressed in ATR units off the spot."""
        a = ctx.atr
        if direction is Direction.CALL:
            return ctx.spot + 0.6 * a, ctx.spot + 1.2 * a, ctx.spot - 0.4 * a
        return ctx.spot - 0.6 * a, ctx.spot - 1.2 * a, ctx.spot + 0.4 * a


class BreakingNewsUnderreaction(Strategy):
    name = "breaking_news_underreaction"

    def evaluate(self, ctx, cat, tape):
        if cat.remaining_repricing < 55 or tape.relative_volume < 1.3:
            return None
        if cat.financial_impact < 50:
            return None
        direction = Direction.CALL if cat.surprise > 0 else Direction.PUT
        aligned = tape.above_vwap if direction is Direction.CALL else tape.below_vwap
        if not aligned:
            return None
        t1, t2, inv = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=min(95.0, 45 + cat.remaining_repricing * 0.5),
            entry_trigger="continuation through the post-news consolidation high/low",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=[f"{cat.remaining_repricing:.0f} repricing left",
                        f"rvol {tape.relative_volume:.1f}",
                        f"impact {cat.financial_impact:.0f}"],
            opposing=["market may have already discounted the headline"],
        )


class NewsMomentumContinuation(Strategy):
    name = "news_momentum_continuation"

    def evaluate(self, ctx, cat, tape):
        if tape.premium_just_spiked:
            return None  # never chase the initial premium spike
        if tape.relative_volume < 1.5:
            return None
        direction = Direction.CALL if tape.above_vwap and tape.higher_highs else (
            Direction.PUT if tape.below_vwap and tape.lower_lows else None)
        if direction is None:
            return None
        t1, t2, inv = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=70.0 if tape.sector_confirms else 58.0,
            entry_trigger="break of consolidation in the direction of the news move",
            invalidation=ctx.vwap, initial_target=t1, extended_target=t2,
            supporting=["controlled consolidation", f"rvol {tape.relative_volume:.1f}"],
            opposing=["late in the move; premium already expanded"],
        )


class GapAndGo(Strategy):
    name = "gap_and_go"

    def evaluate(self, ctx, cat, tape):
        if abs(ctx.gap_pct) < 2.0 or tape.relative_volume < 2.0:
            return None
        if ctx.gap_pct > 0 and ctx.spot > ctx.premarket_high and ctx.breakout_held_bars >= 2:
            direction = Direction.CALL
            inv = ctx.premarket_high
        elif ctx.gap_pct < 0 and ctx.spot < ctx.premarket_low and ctx.breakout_held_bars >= 2:
            direction = Direction.PUT
            inv = ctx.premarket_low
        else:
            return None
        t1, t2, _ = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=72.0,
            entry_trigger=f"hold {'above' if direction is Direction.CALL else 'below'} premarket level",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=[f"gap {ctx.gap_pct:+.1f}%", f"rvol {tape.relative_volume:.1f}"],
            opposing=["gap fills are common without a genuine catalyst"],
        )


class OpeningRangeBreakout(Strategy):
    name = "opening_range_breakout"

    def evaluate(self, ctx, cat, tape):
        if ctx.breakout_held_bars < 2 or tape.relative_volume < 1.4:
            return None
        if ctx.spot > ctx.opening_range_high and ctx.index_change_pct >= 0:
            direction, inv = Direction.CALL, ctx.opening_range_high
        elif ctx.spot < ctx.opening_range_low and ctx.index_change_pct <= 0:
            direction, inv = Direction.PUT, ctx.opening_range_low
        else:
            return None
        t1, t2, _ = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=66.0, entry_trigger="opening range break with volume expansion",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=["index confirms", f"held {ctx.breakout_held_bars} bars"],
            opposing=["opening range breaks fail often in chop"],
        )


class VwapContinuation(Strategy):
    name = "vwap_continuation"

    def evaluate(self, ctx, cat, tape):
        if tape.extended_from_vwap_atr > 1.5:
            return None  # too stretched to enter
        if tape.above_vwap and tape.higher_highs and cat.surprise > 0:
            direction = Direction.CALL
        elif tape.below_vwap and tape.lower_lows and cat.surprise < 0:
            direction = Direction.PUT
        else:
            return None
        t1, t2, _ = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=68.0, entry_trigger="pullback holds VWAP, then volume returns",
            invalidation=ctx.vwap, initial_target=t1, extended_target=t2,
            supporting=["VWAP structure intact"],
            opposing=["VWAP loss invalidates immediately"],
        )


class FailedBreakoutReversal(Strategy):
    name = "failed_breakout_reversal"

    def evaluate(self, ctx, cat, tape):
        if not (ctx.lost_level or ctx.reclaimed_level):
            return None
        if ctx.lost_level and tape.below_vwap:
            direction, inv = Direction.PUT, ctx.opening_range_high
        elif ctx.reclaimed_level and tape.above_vwap:
            direction, inv = Direction.CALL, ctx.opening_range_low
        else:
            return None
        t1, t2, _ = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=74.0, entry_trigger="failed move plus VWAP flip — trapped participants",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=["trapped traders provide fuel"],
            opposing=["reversals are lower probability than continuations"],
        )


class RelativeStrengthWeakness(Strategy):
    name = "relative_strength_weakness"

    def evaluate(self, ctx, cat, tape):
        rel = ctx.sector_change_pct - ctx.index_change_pct
        if abs(rel) < 1.0:
            return None
        direction = Direction.CALL if rel > 0 else Direction.PUT
        if direction is Direction.CALL and not tape.above_vwap:
            return None
        if direction is Direction.PUT and not tape.below_vwap:
            return None
        t1, t2, inv = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=64.0, entry_trigger="divergence persists through an index pullback/rally",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=[f"relative move {rel:+.1f}% vs index"],
            opposing=["relative strength can mean-revert quickly"],
        )


class SectorSympathy(Strategy):
    name = "sector_sympathy"

    def evaluate(self, ctx, cat, tape):
        if not cat.tickers or ctx.ticker in cat.tickers:
            return None  # this is the lagging peer, not the primary name
        if cat.remaining_repricing < 50:
            return None
        direction = Direction.CALL if cat.surprise > 0 else Direction.PUT
        t1, t2, inv = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=60.0, entry_trigger="peer has repriced, this name has not",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=[f"primary movers {', '.join(cat.tickers)} already moved"],
            opposing=["sympathy moves are weaker and slower than the primary"],
        )


class VolatilityExpansion(Strategy):
    name = "volatility_expansion"

    def evaluate(self, ctx, cat, tape):
        if cat.vol_expansion < 55 or tape.relative_volume < 1.6:
            return None
        direction = Direction.CALL if tape.above_vwap else Direction.PUT
        t1, t2, inv = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=63.0, entry_trigger="price leaves consolidation on expanding range",
            invalidation=inv, initial_target=t1, extended_target=t2,
            supporting=[f"vol expansion {cat.vol_expansion:.0f}"],
            opposing=["avoid if IV already at an extreme"],
        )


class IntradayIndexMomentum(Strategy):
    name = "intraday_index_momentum"
    ELIGIBLE = {"SPY", "QQQ", "IWM"}

    def evaluate(self, ctx, cat, tape):
        if ctx.ticker not in self.ELIGIBLE:
            return None
        if not (tape.index_confirms and tape.sector_confirms):
            return None
        if abs(ctx.index_change_pct) < 0.4:
            return None
        direction = Direction.CALL if ctx.index_change_pct > 0 else Direction.PUT
        aligned = tape.above_vwap if direction is Direction.CALL else tape.below_vwap
        if not aligned:
            return None
        t1, t2, inv = self._targets(ctx, direction)
        return Signal(
            setup=self.name, ticker=ctx.ticker, direction=direction,
            confidence=69.0, entry_trigger="index breaks intraday level with breadth",
            invalidation=ctx.vwap, initial_target=t1, extended_target=t2,
            supporting=["breadth and VWAP aligned"],
            opposing=["macro releases can reverse index momentum instantly"],
        )


ALL_STRATEGIES: list[Strategy] = [
    BreakingNewsUnderreaction(), NewsMomentumContinuation(), GapAndGo(),
    OpeningRangeBreakout(), VwapContinuation(), FailedBreakoutReversal(),
    RelativeStrengthWeakness(), SectorSympathy(), VolatilityExpansion(),
    IntradayIndexMomentum(),
]


def evaluate_all(ctx: MarketContext, cat: Catalyst, tape: TapeEvidence) -> list[Signal]:
    out = []
    for s in ALL_STRATEGIES:
        try:
            sig = s.evaluate(ctx, cat, tape)
        except Exception:  # a broken strategy must never halt the scan
            continue
        if sig:
            out.append(sig)
    return sorted(out, key=lambda s: s.confidence, reverse=True)
