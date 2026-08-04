"""Tests for continuation-vs-exhaustion reading.

The anchor case is 2026-08-04 (QQQ), reconstructed from the real tape. The
agent sold a 725 call at 0.78 against a hand-picked target of QQQ 717. QQQ
closed at 723.68. These tests assert the arithmetic would have said so.
"""
from __future__ import annotations

import sys
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from config.settings import Direction
from src.monitoring.exhaustion import (Bar, ExitAction, MoveContext, Phase,
                                       classify_phase, decide,
                                       measured_targets, progress_recent,
                                       projected_premium, rejection_pressure,
                                       structure_intact, theta_cost,
                                       trend_day_score, volume_trend)
from src.strategies.playbook import Session

# ---------------------------------------------------------------------------
# The real 2026-08-04 QQQ tape: 1-minute bars, 9:30-9:36 ET.
# Opening range (9:30-9:34) = 707.59 - 711.80.  ATR(14) = 15.36.
# QQQ closed the session at 723.68.
# ---------------------------------------------------------------------------
QQQ_BARS = [
    Bar(high=709.85, low=707.59, close=709.85, volume=325209),
    Bar(high=710.11, low=709.64, close=709.97, volume=108789),
    Bar(high=710.72, low=710.14, close=710.69, volume=101391),
    Bar(high=711.48, low=710.50, close=711.45, volume=143382),
    Bar(high=711.80, low=711.07, close=711.75, volume=165263),
    Bar(high=712.47, low=711.76, close=712.38, volume=110356),
    Bar(high=712.52, low=711.93, close=711.98, volume=82688),
]


def qqq_ctx(**over) -> MoveContext:
    kw = dict(
        direction=Direction.CALL, bars=list(QQQ_BARS), vwap=710.60, atr=15.361,
        opening_range_high=711.80, opening_range_low=707.59,
        session=Session.OPENING_DRIVE, minutes_to_liquidation=360.0,
        entry_premium=0.52, current_premium=0.78, delta=0.30, gamma=0.030,
        theta_per_day=-1.22, strike=725.0, index_confirms=True,
    )
    kw.update(over)
    return MoveContext(**kw)


# ------------------------- the regression that matters ----------------------

def test_measured_targets_would_have_projected_the_actual_close():
    """Both projections land near 723.68 — the agent's guess was 717."""
    tg = measured_targets(qqq_ctx())
    assert tg.orb_extension_3x == pytest.approx(724.43, abs=0.05)
    assert tg.atr_projection == pytest.approx(722.95, abs=0.05)
    # The two agree inside a third of an ATR, so consensus is their midpoint.
    assert tg.consensus == pytest.approx(723.69, abs=0.35)
    assert abs(tg.consensus - 723.68) < 1.0, "must land within a point of the close"


def test_target_is_far_above_the_discretionary_guess():
    tg = measured_targets(qqq_ctx())
    assert tg.consensus > 722.0, "717 was the error this module exists to prevent"


def test_early_in_the_move_most_of_it_is_still_ahead():
    tg = measured_targets(qqq_ctx())
    assert tg.captured_pct < 0.35
    assert not tg.move_is_mostly_done


def test_the_actual_exit_would_have_been_hold():
    """At 0.78 with structure intact, the model holds rather than sells."""
    d = decide(qqq_ctx())
    assert d.action is ExitAction.HOLD
    assert d.phase.continues
    assert d.worth_holding


def test_holding_edge_is_large_at_the_point_of_the_real_exit():
    d = decide(qqq_ctx())
    assert d.expected_gain > d.holding_cost * 3, "the trade-off was not close"


# ------------------------------ trend day -----------------------------------

def test_trend_day_recognised_from_the_open():
    """Open is the low, VWAP held, structure clean -> high trend-day score."""
    assert trend_day_score(qqq_ctx()) >= 85.0


def test_trend_day_score_drops_when_the_open_is_not_the_extreme():
    bars = [Bar(712.0, 705.0, 706.0, 300000)] + QQQ_BARS[1:]
    ctx = qqq_ctx(bars=bars)
    assert trend_day_score(ctx) < trend_day_score(qqq_ctx())


def test_trend_day_raises_probability_of_continuation():
    strong = decide(qqq_ctx())
    weak = decide(qqq_ctx(index_confirms=False,
                          bars=[Bar(712.0, 705.0, 706.0, 300000)] + QQQ_BARS[1:]))
    assert strong.trend_day > weak.trend_day


# ------------------- pause vs distribution: the core read -------------------

def test_pause_is_not_a_top():
    """Stalled progress on drying volume is a rest — the model must hold."""
    resting = QQQ_BARS + [
        Bar(712.55, 712.10, 712.30, 40000),
        Bar(712.60, 712.15, 712.35, 31000),
        Bar(712.58, 712.20, 712.40, 25000),
    ]
    phase, why = classify_phase(qqq_ctx(bars=resting))
    assert phase is Phase.PAUSE
    assert phase.continues
    assert decide(qqq_ctx(bars=resting)).action is ExitAction.HOLD


def test_distribution_is_a_top():
    """Same lack of progress, but heavy volume and rejection wicks."""
    sold_into = QQQ_BARS + [
        Bar(716.00, 712.20, 712.40, 420000),
        Bar(716.20, 712.10, 712.35, 460000),
        Bar(716.10, 712.00, 712.30, 510000),
    ]
    phase, why = classify_phase(qqq_ctx(bars=sold_into))
    assert phase is Phase.DISTRIBUTION
    assert not phase.continues
    assert decide(qqq_ctx(bars=sold_into)).action is ExitAction.TRIM_THIRD


def test_pause_and_distribution_differ_only_by_volume_and_wicks():
    """Guard the exact discrimination this module was built for."""
    quiet = QQQ_BARS + [Bar(712.55, 712.10, 712.30, 30000)] * 3
    heavy = QQQ_BARS + [Bar(716.00, 712.10, 712.30, 480000)] * 3
    assert classify_phase(qqq_ctx(bars=quiet))[0] is Phase.PAUSE
    assert classify_phase(qqq_ctx(bars=heavy))[0] is Phase.DISTRIBUTION


def test_climax_exits_into_strength():
    """Parabolic means >=2 ATR from VWAP on a volume spike, not merely 'fast'."""
    parabolic = QQQ_BARS + [
        Bar(732.0, 715.0, 731.5, 900000),
        Bar(746.0, 731.0, 745.0, 1400000),
    ]
    ctx = qqq_ctx(bars=parabolic, vwap=712.0)
    assert (745.0 - 712.0) / 15.361 >= 2.0, "fixture must actually be parabolic"
    phase, _ = classify_phase(ctx)
    assert phase is Phase.CLIMAX
    assert decide(ctx).action is ExitAction.TRIM_HALF


def test_fast_but_not_extended_is_impulse_not_climax():
    """Guard the boundary: speed alone is not exhaustion."""
    fast = QQQ_BARS + [
        Bar(730.0, 715.0, 729.5, 900000),
        Bar(742.0, 729.0, 741.5, 1400000),
    ]
    ctx = qqq_ctx(bars=fast, vwap=712.0)
    assert (741.5 - 712.0) / 15.361 < 2.0
    assert classify_phase(ctx)[0] is Phase.IMPULSE


def test_reversal_when_vwap_lost_and_structure_broken():
    broken = QQQ_BARS + [
        Bar(712.0, 707.0, 707.5, 400000),
        Bar(708.0, 704.0, 704.5, 500000),
        Bar(705.0, 701.0, 701.5, 600000),
    ]
    ctx = qqq_ctx(bars=broken, vwap=710.0)
    assert classify_phase(ctx)[0] is Phase.REVERSAL
    assert decide(ctx).action is ExitAction.EXIT


def test_contradicted_catalyst_is_immediate_reversal():
    ctx = qqq_ctx(catalyst_contradicted=True)
    assert classify_phase(ctx)[0] is Phase.REVERSAL
    assert decide(ctx).action is ExitAction.EXIT


# ------------------------ selling too late ----------------------------------

def test_move_mostly_captured_triggers_a_trim():
    """The other half of the mandate: do not ride a finished move to zero."""
    late = QQQ_BARS + [Bar(723.9, 722.8, 723.2, 60000)] * 3
    ctx = qqq_ctx(bars=late, current_premium=4.20)
    tg = measured_targets(ctx)
    assert tg.move_is_mostly_done
    assert decide(ctx).action in (ExitAction.TRIM_HALF, ExitAction.TRIM_THIRD)


def test_theta_overwhelms_a_small_remaining_move():
    """When decay costs more than the move left, reduce — regardless of P&L."""
    near_target = QQQ_BARS + [Bar(723.5, 722.9, 723.1, 50000)] * 3
    ctx = qqq_ctx(bars=near_target, current_premium=4.00,
                  theta_per_day=-3.50, minutes_to_liquidation=45.0)
    d = decide(ctx)
    assert d.holding_cost > 0
    assert d.action is not ExitAction.HOLD


def test_theta_cost_scales_with_horizon():
    ctx = qqq_ctx()
    assert theta_cost(ctx, 390.0) == pytest.approx(1.22, abs=0.01)
    assert theta_cost(ctx, 195.0) == pytest.approx(0.61, abs=0.01)
    assert theta_cost(ctx, 0.0) == 0.0


def test_liquidation_window_overrides_everything():
    d = decide(qqq_ctx(), must_liquidate=True)
    assert d.action is ExitAction.EXIT_MANDATORY


# ------------------------------ primitives ----------------------------------

def test_structure_intact_tolerates_one_break_not_two():
    good = [Bar(10, 5, 9, 1), Bar(11, 6, 10, 1), Bar(12, 7, 11, 1), Bar(13, 6.5, 12, 1)]
    bad = [Bar(10, 8, 9, 1), Bar(11, 7, 10, 1), Bar(12, 6, 11, 1), Bar(13, 5, 12, 1)]
    ctx = qqq_ctx()
    assert structure_intact(qqq_ctx(bars=good))
    assert not structure_intact(qqq_ctx(bars=bad))


def test_volume_trend_detects_drying_up_and_spiking():
    quiet = QQQ_BARS + [Bar(712.5, 712.1, 712.3, 20000)] * 3
    loud = QQQ_BARS + [Bar(712.5, 712.1, 712.3, 900000)] * 3
    assert volume_trend(qqq_ctx(bars=quiet)) < 0.5
    assert volume_trend(qqq_ctx(bars=loud)) > 3.0


def test_rejection_pressure_reads_upper_wicks_for_calls():
    wicky = [Bar(720.0, 710.0, 711.0, 100)] * 3
    clean = [Bar(720.0, 710.0, 719.8, 100)] * 3
    assert rejection_pressure(qqq_ctx(bars=wicky)) > 0.8
    assert rejection_pressure(qqq_ctx(bars=clean)) < 0.1


def test_progress_measured_in_atr_units():
    assert progress_recent(qqq_ctx()) > 0
    assert progress_recent(qqq_ctx(bars=list(reversed(QQQ_BARS)))) < 0


def test_projected_premium_uses_delta_and_gamma():
    ctx = qqq_ctx()
    p = projected_premium(ctx, 723.68)
    move = 723.68 - ctx.spot
    assert p == pytest.approx(0.78 + 0.30 * move + 0.5 * 0.030 * move ** 2, abs=0.01)
    assert p > 4.0, "the contract really was worth several dollars at the close"


def test_projected_premium_never_negative():
    assert projected_premium(qqq_ctx(), 600.0) == 0.0


def test_projected_premium_never_rises_on_an_adverse_move():
    """The gamma term is symmetric; taken literally a call would gain as the
    underlying collapses. Both bounds must hold at every adverse distance."""
    ctx = qqq_ctx()
    for target in (711.0, 705.0, 690.0, 650.0, 600.0, 500.0):
        p = projected_premium(ctx, target)
        assert 0.0 <= p <= ctx.current_premium, f"broken at {target}"


def test_projected_premium_decays_monotonically_downward():
    ctx = qqq_ctx()
    prices = [712.0, 708.0, 704.0, 700.0, 696.0]
    vals = [projected_premium(ctx, p) for p in prices]
    assert vals == sorted(vals, reverse=True)


# ------------------------------- puts ---------------------------------------

def test_put_direction_mirrors_the_logic():
    falling = [
        Bar(712.0, 707.0, 707.5, 300000),
        Bar(708.0, 703.0, 703.5, 250000),
        Bar(704.0, 699.0, 699.5, 260000),
        Bar(700.0, 695.0, 695.5, 280000),
    ]
    ctx = qqq_ctx(direction=Direction.PUT, bars=falling, vwap=706.0,
                  opening_range_high=712.0, opening_range_low=707.0, strike=690.0)
    tg = measured_targets(ctx)
    assert tg.orb_extension_3x < ctx.spot, "puts project downward"
    assert ctx.favorable, "price below VWAP is favourable for a put"
    assert structure_intact(ctx)
