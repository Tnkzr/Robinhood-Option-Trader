"""Hold Score, big-winner management, parabolic exhaustion, news reversal.

This module encodes the most expensive lesson in the record: on 2026-07-29 a
position was closed at the two-thirds mark of its move on a 17-minute
counter-bounce, forfeiting roughly $302. Momentum weakening is scored, not
reacted to — and a counter-move alone can never force an exit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from config.settings import Direction, ScoreThresholds


class Action(str, Enum):
    HOLD = "hold"
    TRIM = "trim"                 # take partial, let the rest run
    TIGHTEN = "tighten_trail"
    EXIT = "exit"
    EXIT_MANDATORY = "exit_mandatory"


@dataclass
class PositionTape:
    direction: Direction
    entry_premium: float
    current_premium: float
    spot: float
    vwap: float
    atr: float
    relative_volume: float
    momentum_accelerating: bool = False
    structure_intact: bool = True         # higher highs / lower lows continuing
    sector_confirms: bool = True
    index_confirms: bool = True
    option_responding: bool = True        # premium tracks the underlying
    iv_collapsing: bool = False
    spread_widening: bool = False
    catalyst_valid: bool = True
    catalyst_contradicted: bool = False
    target_reached: bool = False
    bars_without_consolidation: int = 0
    extended_from_vwap_atr: float = 0.0
    rejection_wick: bool = False
    momentum_divergence: bool = False

    @property
    def pnl_pct(self) -> float:
        if self.entry_premium <= 0:
            return 0.0
        return (self.current_premium - self.entry_premium) / self.entry_premium

    @property
    def vwap_aligned(self) -> bool:
        return (self.spot > self.vwap if self.direction is Direction.CALL
                else self.spot < self.vwap)


@dataclass
class HoldDecision:
    score: float
    action: Action
    reasons: list[str] = field(default_factory=list)
    trail_level: float | None = None


def exhaustion_signals(t: PositionTape) -> list[str]:
    sig = []
    if t.bars_without_consolidation >= 5:
        sig.append("multiple large bars without consolidation")
    if t.extended_from_vwap_atr >= 2.0:
        sig.append(f"{t.extended_from_vwap_atr:.1f}x ATR from VWAP")
    if t.rejection_wick:
        sig.append("rejection wick at the extreme")
    if t.momentum_divergence:
        sig.append("momentum divergence")
    if t.spread_widening:
        sig.append("spread widening")
    return sig


def hold_score(t: PositionTape) -> tuple[float, list[str]]:
    score = 50.0
    why: list[str] = []

    def bump(cond: bool, pts: float, msg: str) -> None:
        nonlocal score
        if cond:
            score += pts
            why.append(f"{'+' if pts > 0 else ''}{pts:.0f} {msg}")

    bump(t.momentum_accelerating, +12, "momentum accelerating")
    bump(t.relative_volume >= 1.5, +8, f"rvol {t.relative_volume:.1f}")
    bump(t.vwap_aligned, +10, "VWAP aligned")
    bump(t.structure_intact, +10, "structure intact")
    bump(t.sector_confirms, +6, "sector confirms")
    bump(t.index_confirms, +6, "index confirms")
    bump(t.option_responding, +8, "premium responding")
    bump(t.catalyst_valid and not t.catalyst_contradicted, +8, "catalyst valid")
    bump(not t.target_reached, +5, "expected move not yet reached")

    bump(not t.vwap_aligned, -20, "VWAP lost")
    bump(not t.structure_intact, -15, "structure broken")
    bump(t.relative_volume < 0.8, -10, "volume declining")
    bump(not t.option_responding, -12, "premium not responding")
    bump(t.iv_collapsing, -10, "IV collapsing")
    bump(t.catalyst_contradicted, -40, "catalyst contradicted")

    ex = exhaustion_signals(t)
    if len(ex) >= 3:
        score -= 15
        why.append(f"-15 exhaustion: {'; '.join(ex)}")

    return max(0.0, min(100.0, score)), why


def trail_level(t: PositionTape, last_swing: float | None = None) -> float:
    """Trail on underlying structure, never on premium percentage."""
    if last_swing is not None:
        return last_swing
    return t.vwap


def decide(t: PositionTape, th: ScoreThresholds, must_liquidate: bool,
           last_swing: float | None = None) -> HoldDecision:
    if must_liquidate:
        return HoldDecision(0.0, Action.EXIT_MANDATORY,
                            ["session liquidation window — no discretion"])

    score, why = hold_score(t)
    ex = exhaustion_signals(t)

    if t.catalyst_contradicted:
        return HoldDecision(score, Action.EXIT,
                            ["catalyst contradicted — thesis dead"] + why)

    if score < th.exit_hold_score:
        return HoldDecision(score, Action.EXIT,
                            [f"hold score {score:.0f} < {th.exit_hold_score:.0f}"] + why)

    # Big-winner management: a gain alone is not a reason to exit.
    if t.pnl_pct >= 0.30 and (len(ex) >= 2 or not t.structure_intact):
        return HoldDecision(score, Action.TRIM,
                            [f"up {t.pnl_pct:.0%} with weakening structure"] + why,
                            trail_level(t, last_swing))

    if len(ex) >= 2:
        return HoldDecision(score, Action.TIGHTEN,
                            [f"exhaustion: {'; '.join(ex)}"] + why,
                            trail_level(t, last_swing))

    return HoldDecision(score, Action.HOLD, why, trail_level(t, last_swing))
