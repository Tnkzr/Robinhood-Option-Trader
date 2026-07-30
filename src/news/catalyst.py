"""Directional thesis: the mandatory gate before any contract is priced.

Built in response to a specific failure on 2026-07-30. A SPY 744C was bought
because it fit a $22 budget, and a bull-flag story was reverse-engineered
afterward. When asked "what news is behind this," the honest answer was none —
and a scan run *after* the fact showed the day's real move was the AI-datacenter
complex (CIFR +27%, IREN +27%, RIOT +23%), never looked at.

The fix is structural, not advisory: a DirectionalThesis cannot be constructed
without naming a specific mechanism, and no contract may be priced without one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from config.settings import Direction


class CatalystType(str, Enum):
    """Taxonomy with empirical directional bias and typical single-day move.

    Bias is the direction the catalyst pushes *before* tape confirmation.
    Reliability is how often the initial direction persists through the session.
    """
    EARNINGS_BEAT_RAISE = "earnings_beat_and_raise"
    EARNINGS_MISS_CUT = "earnings_miss_and_cut"
    EARNINGS_MIXED = "earnings_mixed"
    GUIDANCE_RAISE = "guidance_raise"
    GUIDANCE_CUT = "guidance_cut"
    FDA_APPROVAL = "fda_approval"
    FDA_REJECTION = "fda_rejection_or_crl"
    CLINICAL_READOUT = "clinical_trial_readout"
    MA_TARGET = "acquisition_target"
    MA_ACQUIRER = "acquirer"
    ANALYST_UPGRADE = "analyst_upgrade_with_pt_raise"
    ANALYST_DOWNGRADE = "analyst_downgrade_with_pt_cut"
    OFFERING_DILUTION = "secondary_offering"
    INDEX_INCLUSION = "index_inclusion"
    GOVERNMENT_CONTRACT = "government_contract_award"
    REGULATORY_ACTION = "regulatory_or_doj_ftc_action"
    POLICY_TARIFF = "policy_or_tariff_change"
    MACRO_INFLATION = "inflation_print"
    MACRO_LABOR = "labor_print"
    FED_DECISION = "fed_decision_or_speak"
    SECTOR_SYMPATHY = "sector_sympathy"
    COMMODITY_SHOCK = "commodity_supply_shock"
    GEOPOLITICAL = "geopolitical_escalation"
    SHORT_SQUEEZE = "short_squeeze_dynamics"

    @property
    def profile(self) -> "CatalystProfile":
        return _PROFILES[self]


@dataclass(frozen=True)
class CatalystProfile:
    bias: Direction | None          # None = direction must come from the tape
    reliability: float              # 0-1: how often initial direction persists
    typical_move_pct: float         # typical single-day underlying move
    decays_fast: bool               # True = edge gone within ~60-90 minutes


_PROFILES: dict[CatalystType, CatalystProfile] = {
    CatalystType.EARNINGS_BEAT_RAISE:  CatalystProfile(Direction.CALL, 0.72, 6.0, False),
    CatalystType.EARNINGS_MISS_CUT:    CatalystProfile(Direction.PUT,  0.74, 7.0, False),
    CatalystType.EARNINGS_MIXED:       CatalystProfile(None,           0.45, 4.0, True),
    CatalystType.GUIDANCE_RAISE:       CatalystProfile(Direction.CALL, 0.70, 5.0, False),
    CatalystType.GUIDANCE_CUT:         CatalystProfile(Direction.PUT,  0.75, 6.5, False),
    CatalystType.FDA_APPROVAL:         CatalystProfile(Direction.CALL, 0.78, 12.0, True),
    CatalystType.FDA_REJECTION:        CatalystProfile(Direction.PUT,  0.82, 15.0, True),
    CatalystType.CLINICAL_READOUT:     CatalystProfile(None,           0.50, 20.0, True),
    CatalystType.MA_TARGET:            CatalystProfile(Direction.CALL, 0.90, 20.0, True),
    CatalystType.MA_ACQUIRER:          CatalystProfile(Direction.PUT,  0.60, 3.0, True),
    CatalystType.ANALYST_UPGRADE:      CatalystProfile(Direction.CALL, 0.55, 2.5, True),
    CatalystType.ANALYST_DOWNGRADE:    CatalystProfile(Direction.PUT,  0.58, 3.0, True),
    CatalystType.OFFERING_DILUTION:    CatalystProfile(Direction.PUT,  0.76, 5.0, True),
    CatalystType.INDEX_INCLUSION:      CatalystProfile(Direction.CALL, 0.68, 4.0, False),
    CatalystType.GOVERNMENT_CONTRACT:  CatalystProfile(Direction.CALL, 0.62, 4.0, True),
    CatalystType.REGULATORY_ACTION:    CatalystProfile(Direction.PUT,  0.70, 5.0, False),
    CatalystType.POLICY_TARIFF:        CatalystProfile(None,           0.60, 3.0, False),
    CatalystType.MACRO_INFLATION:      CatalystProfile(None,           0.65, 1.2, False),
    CatalystType.MACRO_LABOR:          CatalystProfile(None,           0.62, 1.0, False),
    CatalystType.FED_DECISION:         CatalystProfile(None,           0.55, 1.5, False),
    CatalystType.SECTOR_SYMPATHY:      CatalystProfile(None,           0.52, 3.0, True),
    CatalystType.COMMODITY_SHOCK:      CatalystProfile(None,           0.66, 4.0, False),
    CatalystType.GEOPOLITICAL:         CatalystProfile(None,           0.58, 2.0, True),
    CatalystType.SHORT_SQUEEZE:        CatalystProfile(Direction.CALL, 0.48, 15.0, True),
}


class ThesisError(ValueError):
    """Raised when a thesis is incomplete. Blocks the trade by construction."""


@dataclass
class DirectionalThesis:
    """The specific, falsifiable reason this underlying moves today.

    Every field is required. Vague entries are rejected at construction — the
    point is to make "it looked like it was going up" unrepresentable.
    """
    ticker: str
    direction: Direction
    catalyst_type: CatalystType

    # WHAT happened — the verifiable event
    headline: str
    source: str
    source_url: str
    published_at: datetime

    # WHY it moves THIS ticker — the transmission mechanism, in one sentence
    mechanism: str

    # WHY it is not already in the price — the actual edge
    why_not_priced: str

    # WHAT proves it wrong — a price level, not a feeling
    invalidation_price: float

    expected_move_pct: float
    corroborating_sources: int = 0
    verified_at: datetime | None = None
    tape_confirms: bool = False
    already_moved_pct: float = 0.0
    notes: list[str] = field(default_factory=list)

    _MIN_REASON_CHARS = 25
    _BANNED = ("looks good", "feels", "should go", "momentum", "chart looks",
               "seems", "probably", "gut", "trending")

    def __post_init__(self) -> None:
        for name, text in (("mechanism", self.mechanism),
                           ("why_not_priced", self.why_not_priced)):
            t = (text or "").strip()
            if len(t) < self._MIN_REASON_CHARS:
                raise ThesisError(
                    f"{name} must be a specific sentence (>={self._MIN_REASON_CHARS} chars), got {len(t)}")
            low = t.lower()
            if any(b in low for b in self._BANNED) and not any(c.isdigit() for c in t):
                raise ThesisError(
                    f"{name} is vague ('{t[:40]}...'). Name a concrete mechanism, not a vibe.")
        if not self.source_url.startswith("http"):
            raise ThesisError("source_url must be a real link to the original source")
        if self.invalidation_price <= 0:
            raise ThesisError("invalidation_price must be a real price level")
        if self.expected_move_pct <= 0:
            raise ThesisError("expected_move_pct must be positive")

    # ---------------------------------------------------------------- checks

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.published_at).total_seconds() / 60.0

    def remaining_move_pct(self) -> float:
        """How much of the expected move has not yet happened."""
        return max(0.0, self.expected_move_pct - abs(self.already_moved_pct))

    def priced_in_ratio(self) -> float:
        if self.expected_move_pct <= 0:
            return 1.0
        return min(1.0, abs(self.already_moved_pct) / self.expected_move_pct)

    def direction_agrees_with_catalyst(self) -> bool:
        bias = self.catalyst_type.profile.bias
        return bias is None or bias is self.direction

    def validate(self, now: datetime | None = None,
                 max_priced_in: float = 0.70) -> tuple[bool, list[str]]:
        """The gate. All of these must pass before a contract may be priced."""
        f: list[str] = []
        p = self.catalyst_type.profile

        if self.verified_at is None:
            f.append("catalyst not verified against the original source")
        if self.corroborating_sources < 1:
            f.append("no corroborating second source")
        if not self.direction_agrees_with_catalyst():
            f.append(f"{self.direction.value} contradicts {self.catalyst_type.value} "
                     f"bias ({p.bias.value if p.bias else 'none'})")
        if not self.tape_confirms:
            f.append("tape has not confirmed the direction (price/volume/VWAP)")

        age = self.age_minutes(now)
        limit = 90.0 if p.decays_fast else 360.0
        if age > limit:
            f.append(f"catalyst is {age:.0f}m old; edge decays after {limit:.0f}m "
                     f"for {self.catalyst_type.value}")

        if self.priced_in_ratio() > max_priced_in:
            f.append(f"{self.priced_in_ratio():.0%} of the expected move already "
                     f"happened (moved {self.already_moved_pct:+.1f}% of "
                     f"{self.expected_move_pct:.1f}% expected) — this is chasing")

        if p.reliability < 0.50:
            f.append(f"{self.catalyst_type.value} has {p.reliability:.0%} directional "
                     f"reliability — coin flip, direction must come from the tape")

        return (not f), f

    def confidence(self, now: datetime | None = None) -> float:
        """0-100. Only meaningful once validate() passes."""
        p = self.catalyst_type.profile
        age = self.age_minutes(now)
        limit = 90.0 if p.decays_fast else 360.0
        freshness = max(0.0, 1.0 - age / limit)
        unpriced = 1.0 - self.priced_in_ratio()
        corrob = min(1.0, self.corroborating_sources / 2.0)
        return round(100.0 * (
            0.35 * p.reliability
            + 0.25 * unpriced
            + 0.20 * freshness
            + 0.10 * corrob
            + 0.10 * (1.0 if self.tape_confirms else 0.0)
        ), 1)

    def summary(self) -> str:
        return (
            f"{self.ticker} {self.direction.value.upper()} — {self.catalyst_type.value}\n"
            f"  WHAT:  {self.headline} ({self.source})\n"
            f"  WHY:   {self.mechanism}\n"
            f"  EDGE:  {self.why_not_priced}\n"
            f"  WRONG BELOW/ABOVE: {self.invalidation_price}\n"
            f"  Expected {self.expected_move_pct:.1f}%, already moved "
            f"{self.already_moved_pct:+.1f}% ({self.priced_in_ratio():.0%} priced in)"
        )


def require_thesis(thesis: DirectionalThesis | None,
                   now: datetime | None = None) -> DirectionalThesis:
    """Hard gate. Call this before ANY option chain lookup.

    This exists because on 2026-07-30 a contract was selected first and the
    reason invented afterward. No thesis, no chain, no trade.
    """
    if thesis is None:
        raise ThesisError(
            "No directional thesis. A contract may not be priced before a "
            "specific, verified reason for the move exists. If nothing has a "
            "catalyst, the correct action is no trade — not finding something "
            "that fits the budget.")
    ok, failures = thesis.validate(now)
    if not ok:
        raise ThesisError("Thesis failed validation:\n  - " + "\n  - ".join(failures))
    return thesis
