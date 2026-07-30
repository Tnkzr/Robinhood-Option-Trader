"""Contract selection.

Encodes the lesson from the 21-trade review: the narrative layer was fine, the
contract-selection layer did not exist. Every candidate is scored on liquidity,
reachability against ATR, and modelled premium at the targets — never on price
alone, never "cheapest", never "furthest OTM".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config.settings import (Direction, ExpirationPolicy, LiquidityGates,
                             ReachabilityBands)


@dataclass(frozen=True)
class Contract:
    symbol: str
    option_id: str
    direction: Direction
    strike: float
    dte: int
    bid: float
    ask: float
    volume: int
    open_interest: int
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float

    @property
    def mark(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mark if self.mark > 0 else float("inf")

    @property
    def cost(self) -> float:
        return self.ask * 100.0

    @property
    def breakeven(self) -> float:
        return (self.strike + self.mark if self.direction is Direction.CALL
                else self.strike - self.mark)


@dataclass(frozen=True)
class Rejection:
    contract: Contract
    reason: str


def liquidity_gate(c: Contract, g: LiquidityGates) -> str | None:
    if c.bid <= 0 or c.ask <= 0:
        return "no two-sided market"
    if c.spread_pct > g.max_spread_pct_of_mark:
        return f"spread {c.spread_pct:.1%} > {g.max_spread_pct_of_mark:.0%} of mark"
    if c.open_interest < g.min_open_interest:
        return f"open interest {c.open_interest} < {g.min_open_interest}"
    if c.volume < g.min_contract_volume:
        return f"volume {c.volume} < {g.min_contract_volume}"
    return None


def expiration_gate(c: Contract, p: ExpirationPolicy) -> str | None:
    if c.dte == 0 and not p.allow_0dte:
        return "0DTE not enabled in configuration"
    if c.dte in p.forbidden_dte:
        return f"{c.dte}DTE is in the forbidden 3-9 band (0-for-3 in the record)"
    return None


def reachability(spot: float, strike: float, atr: float) -> float:
    """|strike - spot| / ATR(14). See PLAYBOOK.md section 3."""
    if atr <= 0:
        return float("inf")
    return abs(strike - spot) / atr


def reachability_gate(r: float, b: ReachabilityBands, has_hard_catalyst: bool) -> str | None:
    if r > b.catalyst_max:
        return f"reachability {r:.2f}x ATR exceeds {b.catalyst_max}x — lottery"
    if r > b.day_trade_max and not has_hard_catalyst:
        return f"reachability {r:.2f}x ATR needs a hard catalyst"
    if r < b.day_trade_min:
        return f"reachability {r:.2f}x ATR — too close, little convexity"
    return None


def project_premium(c: Contract, spot: float, target: float,
                    minutes_held: float, iv_change: float = 0.0) -> float:
    """Estimate premium at a target using a local Taylor expansion.

    delta/gamma for the move, theta for elapsed time, vega for IV change.
    Deliberately simple and transparent — it is a ranking aid, not a pricer.
    """
    move = target - spot
    if c.direction is Direction.PUT:
        pass  # delta is already signed for puts
    est = (c.mark
           + c.delta * move
           + 0.5 * c.gamma * move * move
           + c.theta * (minutes_held / 390.0)
           + c.vega * iv_change * 100.0)
    return max(0.0, est)


def minutes_to_target(spot: float, target: float, atr: float) -> float:
    """Rough time budget: an ATR-sized move takes about one full session."""
    if atr <= 0:
        return float("inf")
    frac = abs(target - spot) / atr
    return 390.0 * frac


@dataclass
class ScoredContract:
    contract: Contract
    reachability: float
    score: float
    est_at_target: float
    est_at_extended: float
    return_at_target: float
    return_at_extended: float
    minutes_needed: float
    rationale: str


def score_contract(c: Contract, spot: float, atr: float, target: float,
                   extended: float, bands: ReachabilityBands) -> ScoredContract:
    r = reachability(spot, c.strike, atr)
    mins = minutes_to_target(spot, target, atr)

    est_t = project_premium(c, spot, target, minutes_held=mins)
    est_x = project_premium(c, spot, extended, minutes_held=mins * 1.5)
    ret_t = (est_t - c.ask) / c.ask if c.ask > 0 else 0.0
    ret_x = (est_x - c.ask) / c.ask if c.ask > 0 else 0.0

    # Components, each 0-1.
    liq = max(0.0, 1.0 - c.spread_pct / 0.10)
    depth = min(1.0, math.log10(max(c.open_interest, 1)) / 4.0)
    responsiveness = min(1.0, abs(c.delta) / 0.50)
    convexity = min(1.0, c.gamma * spot / 8.0)
    payoff = min(1.0, max(0.0, ret_t / 1.0))
    band_fit = 1.0 if bands.day_trade_min <= r <= bands.day_trade_max else 0.4

    score = 100.0 * (
        0.22 * liq
        + 0.13 * depth
        + 0.20 * responsiveness
        + 0.15 * convexity
        + 0.20 * payoff
        + 0.10 * band_fit
    )
    return ScoredContract(
        contract=c, reachability=r, score=round(score, 1),
        est_at_target=round(est_t, 2), est_at_extended=round(est_x, 2),
        return_at_target=round(ret_t, 3), return_at_extended=round(ret_x, 3),
        minutes_needed=round(mins, 1),
        rationale=(f"{r:.2f}x ATR, spread {c.spread_pct:.1%}, "
                   f"delta {c.delta:.2f}, OI {c.open_interest}"),
    )


def select(candidates: list[Contract], spot: float, atr: float, target: float,
           extended: float, gates: LiquidityGates, bands: ReachabilityBands,
           exp_policy: ExpirationPolicy, budget: float,
           has_hard_catalyst: bool = False
           ) -> tuple[ScoredContract | None, list[Rejection]]:
    """Return the best contract plus an audit trail of every rejection."""
    rejects: list[Rejection] = []
    scored: list[ScoredContract] = []

    for c in candidates:
        for gate in (liquidity_gate(c, gates), expiration_gate(c, exp_policy)):
            if gate:
                rejects.append(Rejection(c, gate))
                break
        else:
            r = reachability(spot, c.strike, atr)
            why = reachability_gate(r, bands, has_hard_catalyst)
            if why:
                rejects.append(Rejection(c, why))
            elif c.cost > budget:
                rejects.append(Rejection(c, f"cost ${c.cost:.0f} exceeds budget ${budget:.0f}"))
            else:
                scored.append(score_contract(c, spot, atr, target, extended, bands))

    if not scored:
        return None, rejects
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[0], rejects
