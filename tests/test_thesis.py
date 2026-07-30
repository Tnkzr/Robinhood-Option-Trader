"""Tests for the catalyst gate and the professional setup playbook."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from config.settings import Direction
from src.news.catalyst import (CatalystType, DirectionalThesis, ThesisError,
                               require_thesis)
from src.strategies.playbook import (Session, Setup, Tape, evaluate,
                                     failed_breakout_trap, first_red_day,
                                     gap_and_go, relative_strength_leader,
                                     sympathy_laggard, vwap_reclaim)

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)


def mk_thesis(**over) -> DirectionalThesis:
    kw = dict(
        ticker="XYZ", direction=Direction.CALL,
        catalyst_type=CatalystType.EARNINGS_BEAT_RAISE,
        headline="XYZ beats and raises FY guidance",
        source="Reuters", source_url="https://reuters.com/xyz",
        published_at=NOW - timedelta(minutes=20),
        mechanism="Q3 revenue guided 20% above consensus, which resets FY estimates upward",
        why_not_priced="Guidance landed 12 minutes ago and only 2% of the 6% typical move has occurred",
        invalidation_price=98.0, expected_move_pct=6.0,
        corroborating_sources=2, verified_at=NOW - timedelta(minutes=15),
        tape_confirms=True, already_moved_pct=2.0,
    )
    kw.update(over)
    return DirectionalThesis(**kw)


# ------------------------- construction is the gate -------------------------

def test_vague_mechanism_rejected():
    with pytest.raises(ThesisError, match="vague|specific"):
        mk_thesis(mechanism="chart looks good and it has momentum")


def test_short_mechanism_rejected():
    with pytest.raises(ThesisError, match="specific sentence"):
        mk_thesis(mechanism="earnings")


def test_vague_why_not_priced_rejected():
    with pytest.raises(ThesisError, match="vague|specific"):
        mk_thesis(why_not_priced="it seems like it should go higher from here")


def test_vague_language_allowed_when_quantified():
    """'momentum' is fine if the sentence carries actual numbers."""
    t = mk_thesis(mechanism="momentum confirmed by 3.2x relative volume and a 20% guidance raise")
    assert t.ticker == "XYZ"


def test_fake_source_url_rejected():
    with pytest.raises(ThesisError, match="source_url"):
        mk_thesis(source_url="a friend told me")


def test_missing_invalidation_rejected():
    with pytest.raises(ThesisError, match="invalidation"):
        mk_thesis(invalidation_price=0.0)


# ------------------------------ the hard gate -------------------------------

def test_no_thesis_blocks_everything():
    """The 2026-07-30 failure: contract first, reason invented after."""
    with pytest.raises(ThesisError, match="No directional thesis"):
        require_thesis(None)


def test_unverified_catalyst_blocked():
    with pytest.raises(ThesisError, match="not verified"):
        require_thesis(mk_thesis(verified_at=None), NOW)


def test_single_source_blocked():
    with pytest.raises(ThesisError, match="corroborating"):
        require_thesis(mk_thesis(corroborating_sources=0), NOW)


def test_tape_must_confirm():
    with pytest.raises(ThesisError, match="tape has not confirmed"):
        require_thesis(mk_thesis(tape_confirms=False), NOW)


def test_direction_contradicting_catalyst_blocked():
    """Puts on a beat-and-raise is incoherent and must be refused."""
    with pytest.raises(ThesisError, match="contradicts"):
        require_thesis(mk_thesis(direction=Direction.PUT), NOW)


def test_chasing_a_priced_in_move_blocked():
    """The KO error: buying after the move already happened."""
    with pytest.raises(ThesisError, match="chasing"):
        require_thesis(mk_thesis(already_moved_pct=5.5, expected_move_pct=6.0), NOW)


def test_stale_fast_decay_catalyst_blocked():
    """Analyst upgrades decay inside 90 minutes."""
    with pytest.raises(ThesisError, match="old"):
        require_thesis(mk_thesis(
            catalyst_type=CatalystType.ANALYST_UPGRADE,
            published_at=NOW - timedelta(hours=3),
            verified_at=NOW - timedelta(hours=3)), NOW)


def test_slow_decay_catalyst_survives_longer():
    """Earnings hold their edge far longer than an upgrade."""
    t = mk_thesis(published_at=NOW - timedelta(hours=3),
                  verified_at=NOW - timedelta(hours=3))
    assert require_thesis(t, NOW) is t


def test_coin_flip_catalyst_blocked():
    with pytest.raises(ThesisError, match="reliability|coin flip"):
        require_thesis(mk_thesis(catalyst_type=CatalystType.SHORT_SQUEEZE), NOW)


def test_valid_thesis_passes_and_scores():
    t = mk_thesis()
    assert require_thesis(t, NOW) is t
    assert 50 <= t.confidence(NOW) <= 100


def test_priced_in_ratio_and_remaining():
    t = mk_thesis(already_moved_pct=3.0, expected_move_pct=6.0)
    assert t.priced_in_ratio() == pytest.approx(0.5)
    assert t.remaining_move_pct() == pytest.approx(3.0)


def test_summary_names_the_reason():
    s = mk_thesis().summary()
    assert "WHY:" in s and "EDGE:" in s and "20% above consensus" in s


# ------------------------------- setups -------------------------------------

def mk_tape(**over) -> Tape:
    kw = dict(spot=105.0, vwap=103.0, atr=3.0, opening_range_high=104.0,
              opening_range_low=101.0, premarket_high=104.5, premarket_low=100.0,
              prev_day_high=102.0, prev_day_low=98.0, high_of_day=105.5,
              low_of_day=100.5, relative_volume=3.0, bars_held_beyond_level=3,
              volume_expanding=True, higher_lows=3, reclaimed_vwap=True,
              sector_rel_strength_pct=2.5, index_rel_strength_pct=2.0)
    kw.update(over)
    return Tape(**kw)


def test_gap_and_go_requires_hold_not_just_break():
    th, t = mk_thesis(), mk_tape(bars_held_beyond_level=0)
    assert gap_and_go(th, t, gap_pct=4.0) is None
    assert gap_and_go(th, mk_tape(), gap_pct=4.0) is not None


def test_gap_and_go_requires_real_volume():
    assert gap_and_go(mk_thesis(), mk_tape(relative_volume=1.1), gap_pct=4.0) is None


def test_relative_strength_leader_needs_both_comparisons():
    th = mk_thesis()
    assert relative_strength_leader(th, mk_tape(sector_rel_strength_pct=0.2)) is None
    assert relative_strength_leader(th, mk_tape(index_rel_strength_pct=0.1)) is None
    assert relative_strength_leader(th, mk_tape()) is not None


def test_vwap_reclaim_rejected_when_overextended():
    """Do not initiate 2+ ATR from VWAP — the entry is gone."""
    assert vwap_reclaim(mk_thesis(), mk_tape(spot=112.0)) is None


def test_first_red_day_requires_extension_and_run():
    th = mk_thesis(direction=Direction.PUT,
                   catalyst_type=CatalystType.GUIDANCE_CUT,
                   mechanism="FY guidance cut 15% below consensus on weak enterprise demand",
                   why_not_priced="Cut disclosed 20 minutes ago, only 1% of a typical 6.5% move done",
                   invalidation_price=110.0)
    flat = mk_tape(consecutive_up_days=1, spot=99.0, lower_highs=3)
    assert first_red_day(th, flat) is None
    extended = mk_tape(consecutive_up_days=5, spot=97.0, vwap=103.0,
                       lower_highs=3, high_of_day=106.0)
    assert first_red_day(th, extended) is not None


def test_sympathy_laggard_closes_when_gap_captured():
    th = mk_thesis(catalyst_type=CatalystType.SECTOR_SYMPATHY,
                   mechanism="Peer IREN reaffirmed AI compute demand; this name has identical HPC exposure",
                   why_not_priced="Peer repriced 25% while this name has moved only 4% so far today")
    t = mk_tape()
    assert sympathy_laggard(th, t, peer_move_pct=25.0, this_move_pct=4.0) is not None
    # already caught up -> no edge left
    assert sympathy_laggard(th, t, peer_move_pct=25.0, this_move_pct=20.0) is None


def test_failed_breakout_trap_needs_level_actually_lost():
    th = mk_thesis(direction=Direction.PUT,
                   catalyst_type=CatalystType.GUIDANCE_CUT,
                   mechanism="FY guidance cut 15% below consensus on weak enterprise demand",
                   why_not_priced="Cut disclosed 20 minutes ago, only 1% of a typical 6.5% move done",
                   invalidation_price=110.0)
    still_above = mk_tape(spot=105.0, high_of_day=106.0)
    assert failed_breakout_trap(th, still_above) is None
    trapped = mk_tape(spot=102.5, vwap=103.5, high_of_day=106.0)
    assert failed_breakout_trap(th, trapped) is not None


# --------------------------- risk and timing --------------------------------

def test_reward_risk_below_two_to_one_declined():
    s = Setup("x", "XYZ", Direction.CALL, "t", entry_level=100.0,
              invalidation=98.0, target_1=101.0, target_2=105.0,
              confidence=80.0, evidence=[], against=[])
    ok, why = s.acceptable(min_rr=2.0)
    assert not ok and "reward:risk" in why


def test_reward_risk_accepted_at_three_to_one():
    s = Setup("x", "XYZ", Direction.CALL, "t", entry_level=100.0,
              invalidation=98.0, target_1=106.0, target_2=110.0,
              confidence=80.0, evidence=[], against=[])
    assert s.reward_risk == pytest.approx(3.0)
    assert s.acceptable()[0]


def test_zero_risk_setup_declined():
    s = Setup("x", "XYZ", Direction.CALL, "t", 100.0, 100.0, 110.0, 120.0,
              80.0, [], [])
    assert not s.acceptable()[0]


@pytest.mark.parametrize("t,expected", [
    (dtime(9, 45), Session.OPENING_DRIVE),
    (dtime(11, 0), Session.MORNING_TREND),
    (dtime(12, 30), Session.MIDDAY_CHOP),
    (dtime(14, 30), Session.AFTERNOON_TREND),
    (dtime(15, 30), Session.CLOSING),
])
def test_session_classification(t, expected):
    assert Session.of(t) is expected


def test_midday_is_discounted_not_banned():
    assert Session.MIDDAY_CHOP.quality < Session.OPENING_DRIVE.quality
    assert Session.MIDDAY_CHOP.quality > 0


def test_no_setups_fire_in_closing_window():
    assert evaluate(mk_thesis(), mk_tape(), dtime(15, 30), gap_pct=4.0) == []


def test_evaluate_returns_ranked_setups():
    out = evaluate(mk_thesis(), mk_tape(), dtime(9, 45), gap_pct=4.0)
    assert out, "expected setups to fire on a clean opening-drive tape"
    assert all(s.acceptable()[0] for s in out)
    assert out == sorted(out, key=lambda s: s.confidence, reverse=True)


def test_midday_lowers_confidence_versus_open():
    a = evaluate(mk_thesis(), mk_tape(), dtime(9, 45), gap_pct=4.0)
    b = evaluate(mk_thesis(), mk_tape(), dtime(12, 30), gap_pct=4.0)
    assert a and b
    assert b[0].confidence < a[0].confidence
