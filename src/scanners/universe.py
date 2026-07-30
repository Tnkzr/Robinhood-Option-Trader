"""Always-produce-a-trade selection with tiered escalation.

Operating mandate (account owner, 2026-07-30): trade every session the market is
open, deploy the full balance, and still obey the rules.

The reconciliation is that "no candidate found" is almost never a fact about the
market — it is a fact about how hard the scan looked. On 2026-07-30 the agent
reported no catalyst while CIFR +27%, IREN +27% and RIOT +23% were moving on a
verified sector catalyst it had not scanned for. So this module removes "no
trade" as an output and replaces it with an escalating search that must return
the best available candidate.

What conviction changes is *strike aggression*, not participation:
  high conviction -> further OTM, more convexity, bigger multiple
  low conviction  -> closer to the money, higher probability, smaller multiple

That keeps daily participation without pretending a weak setup is a strong one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from enum import IntEnum

from config.settings import Direction
from src.news.catalyst import CatalystType, DirectionalThesis
from src.strategies.playbook import Session, Setup, Tape


class Tier(IntEnum):
    """Search tiers, best first. Escalate only when the tier above is empty."""
    HARD_CATALYST = 1      # earnings, FDA, M&A, guidance — company-specific
    SECTOR_MOVE = 2        # a verified sector-wide repricing
    RELATIVE_STRENGTH = 3  # clear leader/laggard vs sector and index
    INDEX_MOMENTUM = 4     # liquid ETF trend with breadth confirmation

    @property
    def conviction_ceiling(self) -> float:
        return {Tier.HARD_CATALYST: 100.0, Tier.SECTOR_MOVE: 80.0,
                Tier.RELATIVE_STRENGTH: 65.0, Tier.INDEX_MOMENTUM: 50.0}[self]


HARD_CATALYSTS = {
    CatalystType.EARNINGS_BEAT_RAISE, CatalystType.EARNINGS_MISS_CUT,
    CatalystType.GUIDANCE_RAISE, CatalystType.GUIDANCE_CUT,
    CatalystType.FDA_APPROVAL, CatalystType.FDA_REJECTION,
    CatalystType.MA_TARGET, CatalystType.REGULATORY_ACTION,
    CatalystType.OFFERING_DILUTION, CatalystType.INDEX_INCLUSION,
}
SECTOR_CATALYSTS = {
    CatalystType.SECTOR_SYMPATHY, CatalystType.COMMODITY_SHOCK,
    CatalystType.POLICY_TARIFF, CatalystType.GEOPOLITICAL,
}


@dataclass
class Candidate:
    thesis: DirectionalThesis
    setup: Setup
    tape: Tape
    tier: Tier
    contract_cost: float = 0.0
    liquidity_ok: bool = True
    notes: list[str] = field(default_factory=list)

    def score(self, now: time, session_quality: float | None = None) -> float:
        """Blend thesis confidence, setup confidence, R:R and time of day."""
        q = session_quality if session_quality is not None else Session.of(now).quality
        rr = min(self.setup.reward_risk / 3.0, 1.0)
        raw = (0.40 * self.thesis.confidence()
               + 0.35 * self.setup.confidence
               + 0.25 * rr * 100.0)
        return round(min(raw * q, self.tier.conviction_ceiling), 1)


@dataclass
class Selection:
    """The trade for the session. Never None while the market is open."""
    candidate: Candidate
    score: float
    tier: Tier
    aggression: float          # 0.0 = near the money, 1.0 = far OTM lottery
    rejected: list[str] = field(default_factory=list)
    escalated_through: list[Tier] = field(default_factory=list)

    @property
    def is_low_conviction(self) -> bool:
        return self.score < 50.0


def aggression_for(score: float) -> float:
    """Map conviction onto strike aggression.

    Deliberately conservative at the bottom: a weak day still gets a trade, but
    it gets a near-the-money contract with a realistic chance, not a lottery
    ticket. Big swings are earned by evidence, not mandated by a schedule.
    """
    if score >= 80:
        return 1.00     # far OTM, maximum convexity
    if score >= 65:
        return 0.70
    if score >= 50:
        return 0.45
    if score >= 35:
        return 0.25
    return 0.10         # near the money — protect the balance on a thin day


def target_delta(aggression: float) -> tuple[float, float]:
    """Delta band implied by the aggression level. Higher delta = safer."""
    lo = 0.10 + 0.00 * aggression
    hi = 0.55 - 0.35 * aggression
    return (max(0.05, hi - 0.15), hi) if aggression < 0.5 else (lo, lo + 0.15)


def select(candidates: list[Candidate], now: time,
           min_rr: float = 2.0) -> Selection | None:
    """Return the single best trade, escalating tiers until something is found.

    Returns None only when the market is closed or every candidate fails a hard
    liquidity/risk gate — never merely because conviction is low.
    """
    if Session.of(now) is Session.CLOSING:
        return None

    rejected: list[str] = []
    escalated: list[Tier] = []

    viable = []
    for c in candidates:
        if not c.liquidity_ok:
            rejected.append(f"{c.thesis.ticker}: failed liquidity gate")
            continue
        ok, why = c.setup.acceptable(min_rr)
        if not ok:
            rejected.append(f"{c.thesis.ticker}: {why}")
            continue
        if not c.thesis.direction_agrees_with_catalyst():
            rejected.append(f"{c.thesis.ticker}: direction contradicts catalyst")
            continue
        viable.append(c)

    if not viable:
        return None

    for tier in sorted(Tier):
        pool = [c for c in viable if c.tier is tier]
        if not pool:
            escalated.append(tier)
            continue
        best = max(pool, key=lambda c: c.score(now))
        s = best.score(now)
        return Selection(best, s, tier, aggression_for(s), rejected, escalated)

    return None


def classify_tier(t: CatalystType) -> Tier:
    if t in HARD_CATALYSTS:
        return Tier.HARD_CATALYST
    if t in SECTOR_CATALYSTS:
        return Tier.SECTOR_MOVE
    return Tier.RELATIVE_STRENGTH


# --------------------------------------------------------------------------
# Full-balance deployment
# --------------------------------------------------------------------------

@dataclass
class Allocation:
    contracts: int
    per_contract_cost: float
    total_cost: float
    unused: float
    can_scale_out: bool
    warning: str | None = None


def allocate(balance: float, per_contract_cost: float,
             prefer_scaling: bool = True) -> Allocation:
    """Deploy as much of the balance as whole contracts allow.

    Prefers >=2 contracts when affordable so a winner can be scaled — taking
    half off and letting the rest run is impossible at one contract, which has
    constrained every trade in this account's history.
    """
    if per_contract_cost <= 0:
        return Allocation(0, 0.0, 0.0, balance, False, "invalid contract cost")

    n = int(balance // per_contract_cost)
    if n <= 0:
        return Allocation(0, per_contract_cost, 0.0, balance, False,
                          f"cannot afford one contract at ${per_contract_cost:.2f}")

    total = n * per_contract_cost
    unused = balance - total
    warn = None
    if n == 1:
        warn = ("single contract — no ability to scale out of a winner; "
                "exit is all-or-nothing")
    if unused > per_contract_cost * 0.9:
        warn = f"${unused:.2f} idle — nearly enough for another contract"

    return Allocation(n, per_contract_cost, round(total, 2), round(unused, 2),
                      can_scale_out=n >= 2, warning=warn)


def best_contract_cost_for_balance(balance: float, candidates_costs: list[float],
                                   prefer_scaling: bool = True) -> float | None:
    """Pick the per-contract cost that deploys the most capital.

    Ties break toward the option allowing >=2 contracts, so scaling out stays
    available.
    """
    best, best_deployed, best_n = None, -1.0, 0
    for c in candidates_costs:
        if c <= 0 or c > balance:
            continue
        n = int(balance // c)
        deployed = n * c
        better = deployed > best_deployed + 1e-9
        tie = abs(deployed - best_deployed) <= 1e-9
        if better or (tie and prefer_scaling and n > best_n):
            best, best_deployed, best_n = c, deployed, n
    return best
