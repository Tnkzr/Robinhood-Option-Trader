"""Tests for daily-participation selection and full-balance deployment."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from config.settings import Direction
from src.news.catalyst import CatalystType, DirectionalThesis
from src.scanners.universe import (Allocation, Candidate, Selection, Tier,
                                   aggression_for, allocate,
                                   best_contract_cost_for_balance,
                                   classify_tier, select, target_delta)
from src.strategies.playbook import Setup, Tape

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)


def th(ticker="XYZ", ctype=CatalystType.EARNINGS_BEAT_RAISE,
       direction=Direction.CALL, moved=2.0) -> DirectionalThesis:
    return DirectionalThesis(
        ticker=ticker, direction=direction, catalyst_type=ctype,
        headline=f"{ticker} catalyst", source="Reuters",
        source_url="https://reuters.com/x", published_at=NOW - timedelta(minutes=20),
        mechanism="Q3 revenue guided 20% above consensus, resetting FY estimates upward",
        why_not_priced="Landed 12 minutes ago and only 2% of the 6% typical move has occurred",
        invalidation_price=98.0, expected_move_pct=6.0, corroborating_sources=2,
        verified_at=NOW - timedelta(minutes=15), tape_confirms=True,
        already_moved_pct=moved)


def setup(rr=3.0, conf=80.0, ticker="XYZ") -> Setup:
    return Setup("s", ticker, Direction.CALL, "trigger", entry_level=100.0,
                 invalidation=98.0, target_1=100.0 + 2.0 * rr, target_2=112.0,
                 confidence=conf, evidence=[], against=[])


def tape() -> Tape:
    return Tape(spot=101.0, vwap=100.0, atr=3.0, opening_range_high=100.5,
                opening_range_low=98.0, premarket_high=100.8, premarket_low=97.0,
                prev_day_high=99.0, prev_day_low=95.0, high_of_day=101.5,
                low_of_day=98.5, relative_volume=2.5)


def cand(tier=Tier.HARD_CATALYST, rr=3.0, conf=80.0, ticker="XYZ",
         liquid=True, ctype=CatalystType.EARNINGS_BEAT_RAISE) -> Candidate:
    return Candidate(th(ticker, ctype), setup(rr, conf, ticker), tape(), tier,
                     liquidity_ok=liquid)


# ----------------------- a trade is produced every day ----------------------

def test_always_returns_a_trade_when_anything_is_viable():
    sel = select([cand()], dtime(9, 45))
    assert sel is not None and sel.candidate.thesis.ticker == "XYZ"


def test_low_conviction_still_trades_rather_than_sitting_out():
    """The mandate: participate daily. Weak conviction changes size, not action."""
    sel = select([cand(tier=Tier.INDEX_MOMENTUM, conf=30.0, rr=2.0)], dtime(12, 30))
    assert sel is not None
    assert sel.is_low_conviction
    assert sel.aggression <= 0.45, "thin day must not take a lottery strike"


def test_escalates_when_top_tier_empty():
    sel = select([cand(tier=Tier.RELATIVE_STRENGTH)], dtime(9, 45))
    assert sel is not None
    assert sel.tier is Tier.RELATIVE_STRENGTH
    assert Tier.HARD_CATALYST in sel.escalated_through
    assert Tier.SECTOR_MOVE in sel.escalated_through


def test_prefers_higher_tier_even_when_lower_scores_well():
    hard = cand(tier=Tier.HARD_CATALYST, conf=60.0, ticker="HARD")
    idx = cand(tier=Tier.INDEX_MOMENTUM, conf=99.0, ticker="IDX")
    sel = select([idx, hard], dtime(9, 45))
    assert sel.candidate.thesis.ticker == "HARD"


def test_picks_best_within_a_tier():
    a = cand(conf=60.0, ticker="A")
    b = cand(conf=95.0, ticker="B")
    assert select([a, b], dtime(9, 45)).candidate.thesis.ticker == "B"


# ------------------------- rules still bind --------------------------------

def test_illiquid_candidate_rejected_with_reason():
    sel = select([cand(liquid=False)], dtime(9, 45))
    assert sel is None
    # and when there is an alternative, the illiquid one is skipped
    sel2 = select([cand(liquid=False, ticker="BAD"), cand(ticker="GOOD")], dtime(9, 45))
    assert sel2.candidate.thesis.ticker == "GOOD"
    assert any("BAD" in r for r in sel2.rejected)


def test_poor_reward_risk_still_rejected():
    sel = select([cand(rr=0.5)], dtime(9, 45))
    assert sel is None


def test_contradictory_direction_rejected():
    bad = Candidate(th(direction=Direction.PUT), setup(), tape(), Tier.HARD_CATALYST)
    sel = select([bad], dtime(9, 45))
    assert sel is None


def test_no_new_trades_in_closing_window():
    assert select([cand()], dtime(15, 30)) is None


# --------------------- conviction controls aggression -----------------------

@pytest.mark.parametrize("score,expected", [
    (95.0, 1.00), (70.0, 0.70), (55.0, 0.45), (40.0, 0.25), (10.0, 0.10),
])
def test_aggression_ladder(score, expected):
    assert aggression_for(score) == expected


def test_low_conviction_targets_higher_delta():
    """Thin day -> closer to the money -> higher delta -> better odds."""
    lo_band = target_delta(aggression_for(20.0))
    hi_band = target_delta(aggression_for(95.0))
    assert max(lo_band) > max(hi_band)


def test_midday_scores_below_open_for_identical_setup():
    c = cand()
    assert c.score(dtime(12, 30)) < c.score(dtime(9, 45))


# --------------------------- full-balance use -------------------------------

def test_deploys_maximum_whole_contracts():
    a = allocate(251.0, 1.12)
    assert a.contracts == 224
    assert a.total_cost == pytest.approx(250.88)
    assert a.unused < 1.12


def test_flags_single_contract_as_unable_to_scale():
    a = allocate(115.0, 112.0)
    assert a.contracts == 1
    assert not a.can_scale_out
    assert "scale out" in a.warning


def test_two_contracts_enables_scaling():
    a = allocate(251.0, 110.0)
    assert a.contracts == 2 and a.can_scale_out


def test_unaffordable_contract_reported_not_crashed():
    a = allocate(22.0, 112.0)
    assert a.contracts == 0 and "cannot afford" in a.warning


def test_flags_meaningful_idle_capital():
    a = allocate(219.0, 110.0)
    assert a.contracts == 1
    assert "idle" in a.warning


def test_best_cost_maximises_deployment():
    # $251 balance: $1.12 deploys 250.88; $2.00 deploys 250.00
    assert best_contract_cost_for_balance(251.0, [1.12, 2.00, 300.0]) == 1.12


def test_best_cost_breaks_ties_toward_scaling():
    """Equal deployment -> prefer the option giving more contracts."""
    chosen = best_contract_cost_for_balance(100.0, [50.0, 25.0])
    assert chosen == 25.0


def test_best_cost_ignores_unaffordable():
    assert best_contract_cost_for_balance(50.0, [80.0, 120.0]) is None


# ------------------------------ tier mapping --------------------------------

@pytest.mark.parametrize("ctype,tier", [
    (CatalystType.EARNINGS_BEAT_RAISE, Tier.HARD_CATALYST),
    (CatalystType.FDA_REJECTION, Tier.HARD_CATALYST),
    (CatalystType.MA_TARGET, Tier.HARD_CATALYST),
    (CatalystType.SECTOR_SYMPATHY, Tier.SECTOR_MOVE),
    (CatalystType.COMMODITY_SHOCK, Tier.SECTOR_MOVE),
    (CatalystType.ANALYST_UPGRADE, Tier.RELATIVE_STRENGTH),
])
def test_tier_classification(ctype, tier):
    assert classify_tier(ctype) is tier


def test_tier_ceilings_are_ordered():
    assert (Tier.HARD_CATALYST.conviction_ceiling
            > Tier.SECTOR_MOVE.conviction_ceiling
            > Tier.RELATIVE_STRENGTH.conviction_ceiling
            > Tier.INDEX_MOMENTUM.conviction_ceiling)
