"""Is this move finished, or is it resting? — continuation vs exhaustion.

Built after 2026-08-04. A QQQ 725 call was bought at 0.52 and sold at 0.78
(+50%) inside four minutes. QQQ closed at 723.68 and that contract was worth
roughly $4-6 at the bell — an 8-11x. The exit was not the error. The error was
that the target it was measured against (QQQ 717) came from intuition rather
than arithmetic, and the tape had given two independent projections of ~723
before 9:40am:

    opening range 707.59-711.80 = 4.21 wide; 3x extension -> 724.43
    day low 707.59 + 1.0 ATR(15.36)          -> 722.95
    actual close                              -> 723.68

`hold_score` scores a snapshot: is the structure intact right now? That is
necessary and not sufficient. It cannot say how much move is *left*, so every
exit becomes a guess dressed as discipline.

This module answers the two questions that actually decide an exit:

    1. Where should this move end?      -> measured_targets()
    2. Is it ending now, or resting?    -> classify_phase()

and then combines them economically. The reason to sell a convex position is
never "it went up". It is that the expected remaining gain no longer exceeds
the cost of holding — theta burn plus the giveback risked if it turns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from config.settings import Direction
from src.strategies.playbook import Session


class Phase(str, Enum):
    """What the tape is doing right now, in the direction of the trade.

    The distinction that matters most is PAUSE vs DISTRIBUTION. Both look like
    "it stopped going up". One is a rest before the next leg; the other is the
    top. Volume separates them: a pause dries up, a top trades heavy.
    """
    IMPULSE = "impulse"              # driving, volume expanding -> hold
    PAUSE = "pause"                  # resting, volume drying up -> hold
    DISTRIBUTION = "distribution"    # heavy volume, no progress -> trim
    CLIMAX = "climax"                # parabolic + volume spike -> exit into strength
    REVERSAL = "reversal"            # structure broken, VWAP lost -> exit

    @property
    def continues(self) -> bool:
        return self in (Phase.IMPULSE, Phase.PAUSE)


@dataclass(frozen=True)
class Bar:
    high: float
    low: float
    close: float
    volume: float

    @property
    def range(self) -> float:
        return max(0.0, self.high - self.low)

    def upper_wick(self) -> float:
        return max(0.0, self.high - self.close)

    def lower_wick(self) -> float:
        return max(0.0, self.close - self.low)


@dataclass
class MoveContext:
    """Everything needed to judge whether the move has more left in it."""
    direction: Direction
    bars: list[Bar]                  # session bars, oldest first, uniform interval
    vwap: float
    atr: float                       # ATR(14) on the daily, in underlying points
    opening_range_high: float
    opening_range_low: float
    session: Session
    minutes_to_liquidation: float

    # option side
    entry_premium: float
    current_premium: float
    delta: float = 0.30
    gamma: float = 0.02
    theta_per_day: float = 0.0       # negative number, premium lost per full day
    strike: float = 0.0

    # thesis side
    catalyst_contradicted: bool = False
    index_confirms: bool = True

    # ---------------------------------------------------------------- basics

    @property
    def spot(self) -> float:
        return self.bars[-1].close if self.bars else 0.0

    @property
    def day_high(self) -> float:
        return max(b.high for b in self.bars)

    @property
    def day_low(self) -> float:
        return min(b.low for b in self.bars)

    @property
    def opening_range(self) -> float:
        return max(0.0, self.opening_range_high - self.opening_range_low)

    @property
    def pnl_pct(self) -> float:
        if self.entry_premium <= 0:
            return 0.0
        return (self.current_premium - self.entry_premium) / self.entry_premium

    @property
    def favorable(self) -> bool:
        """True when price is on the right side of VWAP for the direction."""
        return (self.spot > self.vwap if self.direction is Direction.CALL
                else self.spot < self.vwap)

    def _signed(self, a: float, b: float) -> float:
        """a - b for calls, b - a for puts: positive always means 'in favour'."""
        return (a - b) if self.direction is Direction.CALL else (b - a)


# --------------------------------------------------------------------------
# 1. Where should this move end?
# --------------------------------------------------------------------------

@dataclass
class Targets:
    """Measured-move projections, cheapest-to-compute first.

    These are arithmetic, not opinion. On 2026-08-04 both orb_extension_3x and
    atr_projection landed within a point of the actual close while a discretionary
    guess was 7 points low.
    """
    orb_extension_1x: float
    orb_extension_2x: float
    orb_extension_3x: float
    atr_projection: float            # day's extreme +/- 1.0 ATR
    consensus: float                 # the level to actually manage against
    remaining_pct: float             # how much of the projected move is left
    captured_pct: float              # how much is already done

    @property
    def move_is_mostly_done(self) -> bool:
        return self.captured_pct >= 0.80


def measured_targets(ctx: MoveContext) -> Targets:
    """Project where the move ends from the tape's own geometry.

    Opening-range extensions are the workhorse for trend days: an ORB that runs
    1x usually runs 2x, and a genuine trend day reaches 3x. The ATR projection
    is the independent cross-check — when the two agree, the number is real.
    """
    orb = ctx.opening_range
    base = ctx.opening_range_high if ctx.direction is Direction.CALL else ctx.opening_range_low
    sign = 1.0 if ctx.direction is Direction.CALL else -1.0

    e1 = base + sign * orb
    e2 = base + sign * 2.0 * orb
    e3 = base + sign * 3.0 * orb

    anchor = ctx.day_low if ctx.direction is Direction.CALL else ctx.day_high
    atr_proj = anchor + sign * ctx.atr

    # Manage against the pair that agree. If the 3x extension and the ATR
    # projection are within a third of an ATR, that is a high-confidence level.
    if ctx.atr > 0 and abs(e3 - atr_proj) <= ctx.atr / 3.0:
        consensus = (e3 + atr_proj) / 2.0
    else:
        consensus = min(e3, atr_proj) if ctx.direction is Direction.CALL else max(e3, atr_proj)

    total = abs(consensus - (ctx.day_low if ctx.direction is Direction.CALL else ctx.day_high))
    done = abs(ctx.spot - (ctx.day_low if ctx.direction is Direction.CALL else ctx.day_high))
    captured = min(1.0, done / total) if total > 0 else 1.0

    return Targets(e1, e2, e3, atr_proj, consensus,
                   remaining_pct=max(0.0, 1.0 - captured), captured_pct=captured)


# --------------------------------------------------------------------------
# 2. Is it ending now, or resting?
# --------------------------------------------------------------------------

def _recent(bars: list[Bar], n: int) -> list[Bar]:
    return bars[-n:] if len(bars) >= n else bars[:]


def structure_intact(ctx: MoveContext, lookback: int = 4) -> bool:
    """Higher lows for calls, lower highs for puts, over the recent bars.

    One violated bar does not break a trend; the sequence does.
    """
    bars = _recent(ctx.bars, lookback)
    if len(bars) < 3:
        return True
    if ctx.direction is Direction.CALL:
        pts = [b.low for b in bars]
        breaks = sum(1 for a, b in zip(pts, pts[1:]) if b < a)
    else:
        pts = [b.high for b in bars]
        breaks = sum(1 for a, b in zip(pts, pts[1:]) if b > a)
    return breaks <= 1


def volume_trend(ctx: MoveContext, lookback: int = 3) -> float:
    """Recent volume relative to the session's own average. 1.0 == typical."""
    if len(ctx.bars) < lookback + 1:
        return 1.0
    recent = _recent(ctx.bars, lookback)
    prior = ctx.bars[:-lookback]
    avg_prior = sum(b.volume for b in prior) / len(prior) if prior else 0.0
    avg_recent = sum(b.volume for b in recent) / len(recent)
    return (avg_recent / avg_prior) if avg_prior > 0 else 1.0


def progress_recent(ctx: MoveContext, lookback: int = 3) -> float:
    """Favourable price progress over the recent bars, in ATR units."""
    bars = _recent(ctx.bars, lookback + 1)
    if len(bars) < 2 or ctx.atr <= 0:
        return 0.0
    return ctx._signed(bars[-1].close, bars[0].close) / ctx.atr


def extension_from_vwap(ctx: MoveContext) -> float:
    if ctx.atr <= 0:
        return 0.0
    return ctx._signed(ctx.spot, ctx.vwap) / ctx.atr


def rejection_pressure(ctx: MoveContext, lookback: int = 3) -> float:
    """Fraction of recent bar range spent being rejected at the extreme."""
    bars = _recent(ctx.bars, lookback)
    tot = sum(b.range for b in bars)
    if tot <= 0:
        return 0.0
    wick = sum((b.upper_wick() if ctx.direction is Direction.CALL else b.lower_wick())
               for b in bars)
    return wick / tot


def classify_phase(ctx: MoveContext) -> tuple[Phase, list[str]]:
    """The central read. Separates a rest from a top.

    A pause and a top both stop making progress. The difference is volume:
    buyers stepping away (pause, volume dries up) versus buyers being sold into
    (distribution, volume stays heavy while price stalls).
    """
    why: list[str] = []
    vol = volume_trend(ctx)
    prog = progress_recent(ctx)
    ext = extension_from_vwap(ctx)
    rej = rejection_pressure(ctx)
    intact = structure_intact(ctx)

    if ctx.catalyst_contradicted:
        return Phase.REVERSAL, ["catalyst contradicted"]

    if not ctx.favorable and not intact:
        return Phase.REVERSAL, [f"VWAP lost and structure broken (ext {ext:+.2f} ATR)"]

    # Climax: vertical, over-extended, on a volume spike.
    if ext >= 2.0 and vol >= 1.8 and prog >= 0.5:
        why.append(f"{ext:.1f} ATR from VWAP on {vol:.1f}x volume — parabolic")
        return Phase.CLIMAX, why

    # Distribution: heavy volume, no progress, being sold at the highs.
    if vol >= 1.2 and prog <= 0.05 and rej >= 0.45:
        why.append(f"volume {vol:.1f}x with {prog:+.2f} ATR progress and "
                   f"{rej:.0%} rejection — sold into")
        return Phase.DISTRIBUTION, why

    if not intact:
        why.append("structure broken")
        return Phase.DISTRIBUTION, why

    # Pause: progress stalled but volume dried up and structure holds.
    if prog <= 0.10 and vol < 1.0:
        why.append(f"volume {vol:.1f}x on a {prog:+.2f} ATR stall — resting, not topping")
        return Phase.PAUSE, why

    why.append(f"progress {prog:+.2f} ATR on {vol:.1f}x volume")
    return Phase.IMPULSE, why


# --------------------------------------------------------------------------
# 3. Trend-day recognition — the reason targets get extended
# --------------------------------------------------------------------------

def trend_day_score(ctx: MoveContext) -> float:
    """0-100: how likely today closes near its extreme rather than mid-range.

    A trend day is the case where selling the first target is the expensive
    mistake. The tells are visible early: the open is the low, VWAP is never
    lost, and every pullback holds above the prior one.
    """
    if not ctx.bars:
        return 0.0
    score = 0.0

    open_is_extreme = (ctx.bars[0].low <= ctx.day_low + 1e-9
                       if ctx.direction is Direction.CALL
                       else ctx.bars[0].high >= ctx.day_high - 1e-9)
    if open_is_extreme:
        score += 30.0

    if ctx.favorable:
        score += 20.0
    if structure_intact(ctx, lookback=len(ctx.bars)):
        score += 25.0

    # Never traded back through the opening range.
    held = (ctx.day_low >= ctx.opening_range_low
            if ctx.direction is Direction.CALL
            else ctx.day_high <= ctx.opening_range_high)
    if held:
        score += 15.0

    if ctx.index_confirms:
        score += 10.0

    return min(100.0, score)


# --------------------------------------------------------------------------
# 4. The economic decision
# --------------------------------------------------------------------------

class ExitAction(str, Enum):
    HOLD = "hold"
    TRIM_THIRD = "trim_one_third"
    TRIM_HALF = "trim_one_half"
    EXIT = "exit"
    EXIT_MANDATORY = "exit_mandatory"


@dataclass
class ExitDecision:
    action: ExitAction
    phase: Phase
    targets: Targets
    trend_day: float
    expected_gain: float             # premium expected if the move completes
    holding_cost: float              # theta + reversal risk over the horizon
    edge: float                      # expected_gain - holding_cost
    reasons: list[str] = field(default_factory=list)

    @property
    def worth_holding(self) -> bool:
        return self.edge > 0


def theta_cost(ctx: MoveContext, horizon_minutes: float) -> float:
    """Premium lost to decay over the horizon. Accelerates near expiry."""
    if ctx.theta_per_day >= 0:
        return 0.0
    frac = max(0.0, horizon_minutes) / (6.5 * 60.0)   # of one trading day
    return abs(ctx.theta_per_day) * frac


def projected_premium(ctx: MoveContext, target_spot: float) -> float:
    """Taylor-expand the option to a target underlying price.

    The gamma term is quadratic and therefore symmetric, which is wrong for an
    adverse move: taken literally it says a call gains value as the underlying
    collapses. On the favourable side gamma genuinely accelerates the gain; on
    the adverse side it only *decelerates* the loss as delta shrinks, and the
    premium can neither rise nor fall through zero. Both bounds are enforced.
    """
    move = ctx._signed(target_spot, ctx.spot)
    if move >= 0:
        return ctx.current_premium + ctx.delta * move + 0.5 * ctx.gamma * move ** 2

    # Adverse: the expansion holds only while delta is still positive. Delta
    # reaches zero after |move| = delta/gamma; beyond that the contract is dead
    # and extrapolating further just re-inflates it through the gamma term.
    limit = (ctx.delta / ctx.gamma) if ctx.gamma > 0 else abs(move)
    eff = -min(abs(move), limit)
    raw = ctx.current_premium + ctx.delta * eff + 0.5 * ctx.gamma * eff ** 2
    return min(ctx.current_premium, max(0.0, raw))


def decide(ctx: MoveContext, must_liquidate: bool = False) -> ExitDecision:
    """Hold while the remaining move is worth more than the cost of waiting.

    This is the rule the 2026-08-04 exit lacked. It does not ask "am I up?" —
    it asks whether the projected move still pays for the theta and the
    giveback risk between here and there.
    """
    tg = measured_targets(ctx)
    phase, why = classify_phase(ctx)
    td = trend_day_score(ctx)

    if must_liquidate:
        return ExitDecision(ExitAction.EXIT_MANDATORY, phase, tg, td, 0.0, 0.0, 0.0,
                            ["session liquidation window — no discretion"])

    horizon = min(ctx.minutes_to_liquidation, 120.0)
    upside = projected_premium(ctx, tg.consensus) - ctx.current_premium
    p_continue = {
        Phase.IMPULSE: 0.70, Phase.PAUSE: 0.60, Phase.DISTRIBUTION: 0.30,
        Phase.CLIMAX: 0.25, Phase.REVERSAL: 0.05,
    }[phase]
    # A confirmed trend day materially raises the odds the target is reached.
    p_continue = min(0.90, p_continue + 0.20 * (td / 100.0))

    expected_gain = p_continue * upside
    giveback = (1.0 - p_continue) * max(0.0, ctx.current_premium - ctx.entry_premium) * 0.5
    holding_cost = theta_cost(ctx, horizon) + giveback
    edge = expected_gain - holding_cost

    why.append(f"target {tg.consensus:.2f} ({tg.captured_pct:.0%} captured), "
               f"trend-day {td:.0f}, P(continue) {p_continue:.0%}")
    why.append(f"expected +{expected_gain:.2f} vs cost {holding_cost:.2f} "
               f"-> edge {edge:+.2f}")

    if phase is Phase.REVERSAL:
        return ExitDecision(ExitAction.EXIT, phase, tg, td, expected_gain,
                            holding_cost, edge, ["thesis broken"] + why)

    if phase is Phase.CLIMAX:
        return ExitDecision(ExitAction.TRIM_HALF, phase, tg, td, expected_gain,
                            holding_cost, edge,
                            ["parabolic — sell into strength, keep a runner"] + why)

    if tg.move_is_mostly_done and phase is not Phase.IMPULSE:
        return ExitDecision(ExitAction.TRIM_HALF, phase, tg, td, expected_gain,
                            holding_cost, edge,
                            [f"{tg.captured_pct:.0%} of the measured move captured"] + why)

    if phase is Phase.DISTRIBUTION:
        return ExitDecision(ExitAction.TRIM_THIRD, phase, tg, td, expected_gain,
                            holding_cost, edge,
                            ["being sold into — reduce, do not dump"] + why)

    if edge <= 0:
        return ExitDecision(ExitAction.TRIM_HALF, phase, tg, td, expected_gain,
                            holding_cost, edge,
                            ["decay now costs more than the remaining move is worth"] + why)

    return ExitDecision(ExitAction.HOLD, phase, tg, td, expected_gain,
                        holding_cost, edge, why)
