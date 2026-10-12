"""Tests for the pieces that must never silently break."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from config.settings import (Direction, ExpirationPolicy, LiquidityGates,
                             Mode, ReachabilityBands, RiskLimits,
                             ScoreThresholds, SessionTimes, Settings)
from src.execution.guard import (DuplicateGuard, Quote, entry_limit_price,
                                 exit_limit_price, may_execute_live,
                                 order_fingerprint, preflight)
from src.monitoring.hold_score import Action, PositionTape, decide, hold_score
from src.options.selection import (Contract, liquidity_gate, expiration_gate,
                                   reachability, reachability_gate, select)
from src.risk.session import ET, Phase, SessionClock
from src.scanners.scoring import (Catalyst, CatalystClass, TapeEvidence,
                                  rank, score_direction, Opportunity)

TH = ScoreThresholds()


def at(h: int, m: int) -> datetime:
    return datetime(2026, 7, 30, h, m, tzinfo=ET)


# --------------------------- session clock ---------------------------------

@pytest.mark.parametrize("h,m,expected", [
    (8, 0, Phase.PREMARKET),
    (9, 30, Phase.OPEN),
    (14, 0, Phase.OPEN),
    (15, 14, Phase.OPEN),
    (15, 15, Phase.NO_NEW_ENTRIES),
    (15, 44, Phase.NO_NEW_ENTRIES),
    (15, 45, Phase.LIQUIDATE),
    (15, 54, Phase.LIQUIDATE),
    (15, 55, Phase.HARD_DEADLINE),
    (16, 0, Phase.CLOSED),
])
def test_phases(h, m, expected):
    assert SessionClock().state(at(h, m)).phase is expected


def test_no_entries_after_cutoff():
    c = SessionClock()
    assert c.state(at(15, 14)).may_open
    for h, m in [(15, 15), (15, 30), (15, 45), (15, 56)]:
        assert not c.state(at(h, m)).may_open


def test_urgency_escalates_to_full_cross():
    c = SessionClock()
    assert c.state(at(15, 45)).urgency == pytest.approx(0.0, abs=0.01)
    assert 0.4 < c.state(at(15, 50)).urgency < 0.6
    assert c.state(at(15, 55)).urgency == 1.0
    assert c.state(at(15, 58)).urgency == 1.0


def test_no_phase_ever_permits_overnight_hold():
    c = SessionClock()
    for minute in range(0, 24 * 60, 7):
        st = c.state(datetime(2026, 7, 30, minute // 60, minute % 60, tzinfo=ET))
        if st.phase in (Phase.LIQUIDATE, Phase.HARD_DEADLINE):
            assert st.must_liquidate
        assert not (st.may_open and st.must_liquidate)


def test_reject_target_that_cannot_complete():
    c = SessionClock()
    assert c.can_reach_target(30, at(14, 0))
    assert not c.can_reach_target(200, at(14, 0))   # needs past liquidation
    assert not c.can_reach_target(5, at(15, 20))    # already past entry cutoff


def test_session_times_must_be_ordered():
    with pytest.raises(ValueError):
        SessionTimes(entry_cutoff=__import__("datetime").time(15, 50),
                     exit_window_start=__import__("datetime").time(15, 45))


# --------------------------- contract selection -----------------------------

def mk(strike=100.0, bid=1.00, ask=1.05, oi=5000, vol=800, dte=1,
       delta=0.40, gamma=0.05, theta=-0.30, vega=0.05, iv=0.30):
    return Contract("TEST", "id", Direction.CALL, strike, dte, bid, ask, vol,
                    oi, delta, gamma, theta, vega, iv)


def test_liquidity_gate_blocks_wide_spread():
    assert liquidity_gate(mk(bid=1.00, ask=1.40), LiquidityGates()) is not None
    assert liquidity_gate(mk(bid=1.00, ask=1.05), LiquidityGates()) is None


def test_liquidity_gate_blocks_thin_oi_and_volume():
    assert "open interest" in liquidity_gate(mk(oi=31), LiquidityGates())
    assert "volume" in liquidity_gate(mk(vol=2), LiquidityGates())


def test_forbidden_dte_band_rejected():
    p = ExpirationPolicy()
    for d in (3, 5, 9):
        assert expiration_gate(mk(dte=d), p) is not None
    for d in (1, 2, 30):
        assert expiration_gate(mk(dte=d), p) is None


def test_0dte_requires_opt_in():
    assert expiration_gate(mk(dte=0), ExpirationPolicy()) is not None
    assert expiration_gate(mk(dte=0), ExpirationPolicy(allow_0dte=True)) is None


def test_reachability_matches_playbook_examples():
    # SPY 731P from 742.03 with ATR 7.57 -> 1.46x
    assert reachability(742.03, 731.0, 7.57) == pytest.approx(1.457, abs=0.01)
    # SPY 721P from 737.65 -> 2.2x
    assert reachability(737.65, 721.0, 7.57) == pytest.approx(2.199, abs=0.01)


def test_reachability_gate_bands():
    b = ReachabilityBands()
    assert reachability_gate(0.10, b, False) is not None   # too close
    assert reachability_gate(0.70, b, False) is None       # sweet spot
    assert reachability_gate(1.20, b, False) is not None   # needs catalyst
    assert reachability_gate(1.20, b, True) is None
    assert reachability_gate(2.20, b, True) is not None    # lottery, always out


def test_select_rejects_everything_over_budget_with_audit():
    # Liquid and reachable, but simply unaffordable — must fail on budget,
    # not on an earlier gate, and must say so in the audit trail.
    pricey = mk(strike=103.0, bid=4.95, ask=5.00)
    best, rejects = select([pricey], spot=100.0, atr=5.0, target=103.0,
                           extended=106.0, gates=LiquidityGates(),
                           bands=ReachabilityBands(), exp_policy=ExpirationPolicy(),
                           budget=100.0)
    assert best is None
    assert any("budget" in r.reason for r in rejects), [r.reason for r in rejects]


def test_gates_are_ordered_cheapest_check_first():
    """A wide-spread contract is rejected on spread, never reaching budget."""
    wide = mk(strike=103.0, bid=1.00, ask=5.00)
    _, rejects = select([wide], spot=100.0, atr=5.0, target=103.0, extended=106.0,
                        gates=LiquidityGates(), bands=ReachabilityBands(),
                        exp_policy=ExpirationPolicy(), budget=100.0)
    assert len(rejects) == 1 and "spread" in rejects[0].reason


def test_select_prefers_liquid_responsive_contract():
    good = mk(strike=103, bid=1.00, ask=1.03, oi=20000, vol=5000, delta=0.45, gamma=0.06)
    poor = mk(strike=103, bid=0.80, ask=1.20, oi=600, vol=150, delta=0.20, gamma=0.01)
    best, _ = select([poor, good], spot=100.0, atr=5.0, target=103.0, extended=106.0,
                     gates=LiquidityGates(), bands=ReachabilityBands(),
                     exp_policy=ExpirationPolicy(), budget=500.0)
    assert best is not None and best.contract.open_interest == 20000


# --------------------------- execution guard --------------------------------

def fresh(bid=1.0, ask=1.05):
    return Quote(bid, ask, datetime.now(timezone.utc))


def stale(bid=1.0, ask=1.05):
    return Quote(bid, ask, datetime.now(timezone.utc) - timedelta(seconds=60))


def base_preflight(**over):
    kw = dict(settings=Settings(), session=SessionClock().state(at(11, 0)),
              stock_quote=fresh(), option_quote=fresh(), is_halted=False,
              trigger_still_valid=True, catalyst_still_valid=True,
              minutes_needed=30.0, cost=5.0, account_value=1000.0,
              open_positions=0, daily_pnl_pct=0.0, consecutive_losses=0,
              kill_switch=False, dup=None)
    kw.update(over)
    return preflight(**kw)


def test_preflight_happy_path():
    assert base_preflight().passed


@pytest.mark.parametrize("over,fragment", [
    ({"is_halted": True}, "halted"),
    ({"stock_quote": stale()}, "stale stock"),
    ({"option_quote": stale()}, "stale option"),
    ({"trigger_still_valid": False}, "trigger"),
    ({"catalyst_still_valid": False}, "catalyst"),
    ({"minutes_needed": 500.0}, "before liquidation"),
    ({"session": SessionClock().state(at(15, 30))}, "entry window closed"),
    ({"open_positions": 9}, "position limit"),
    ({"cost": 900.0}, "exceeds"),
    ({"cost": 5.01}, "risk cap"),
    ({"daily_pnl_pct": -0.02}, "daily loss limit"),
    ({"consecutive_losses": 3}, "consecutive losses"),
    ({"kill_switch": True}, "kill switch"),
    ({"account_value": 0.0}, "no account equity"),
    ({"dup": "duplicate order blocked: x"}, "duplicate"),
])
def test_preflight_blocks(over, fragment):
    r = base_preflight(**over)
    assert not r.passed
    assert any(fragment in f for f in r.failures), r.failures


def test_duplicate_guard():
    g, fp = DuplicateGuard(), order_fingerprint("acct", "opt", "buy", 1)
    assert g.check(fp) is None
    g.record(fp)
    assert "duplicate" in g.check(fp)
    g.release(fp)
    assert g.check(fp) is None


def test_limit_prices_never_pay_through_offer():
    q = Quote(1.00, 1.10, datetime.now(timezone.utc))
    assert entry_limit_price(q, 0.0) == 1.00
    assert entry_limit_price(q, 0.5) == 1.05
    assert entry_limit_price(q, 1.0) == 1.10
    assert entry_limit_price(q, 5.0) == 1.10          # clamped


def test_exit_limit_walks_to_bid_with_urgency():
    q = Quote(1.00, 1.10, datetime.now(timezone.utc))
    assert exit_limit_price(q, 0.0) == 1.05
    assert exit_limit_price(q, 1.0) == 1.00


FULL_CAPS = {"get_accounts", "get_portfolio", "get_option_positions",
             "get_option_quotes", "place_option_order", "get_option_orders",
             "cancel_option_order"}


def test_live_execution_refused_without_capabilities():
    s = Settings(mode=Mode.LIVE, account_number="X", live_authorized=True)
    ok, why = may_execute_live(s, {"get_accounts"})
    assert not ok and "missing brokerage capabilities" in why
    assert may_execute_live(s, FULL_CAPS)[0]
    assert not may_execute_live(Settings(mode=Mode.DRY_RUN), FULL_CAPS)[0]
    assert not may_execute_live(Settings(mode=Mode.LIVE, live_authorized=True),
                                FULL_CAPS)[0]  # no account


def test_live_execution_requires_explicit_authorization():
    s = Settings(mode=Mode.LIVE, account_number="X")
    ok, why = may_execute_live(s, FULL_CAPS)
    assert not ok and "not authorized" in why


def test_authorization_comes_only_from_exact_phrase(monkeypatch):
    monkeypatch.setenv("TRADER_MODE", "live")
    monkeypatch.setenv("TRADER_LIVE_AUTHORIZATION", "yes")
    assert not Settings.from_env().live_authorized
    monkeypatch.setenv("TRADER_LIVE_AUTHORIZATION", "i-authorize-live-trading")
    assert Settings.from_env().live_authorized


# --------------------------- hold score -------------------------------------

def strong(**over):
    kw = dict(direction=Direction.CALL, entry_premium=1.0, current_premium=1.4,
              spot=101.0, vwap=100.0, atr=5.0, relative_volume=1.8,
              momentum_accelerating=True, structure_intact=True,
              sector_confirms=True, index_confirms=True, option_responding=True)
    kw.update(over)
    return PositionTape(**kw)


def test_winner_with_intact_structure_is_held_not_sold():
    """The $302 lesson: a gain plus a wobble must not force an exit."""
    d = decide(strong(), TH, must_liquidate=False)
    assert d.action is Action.HOLD


def test_counter_move_alone_does_not_exit():
    t = strong(momentum_accelerating=False, relative_volume=1.2)
    assert decide(t, TH, must_liquidate=False).action in (Action.HOLD, Action.TIGHTEN)


def test_vwap_loss_and_broken_structure_exits():
    t = strong(spot=99.0, structure_intact=False, option_responding=False,
               momentum_accelerating=False, relative_volume=0.5,
               sector_confirms=False, index_confirms=False, iv_collapsing=True)
    assert decide(t, TH, must_liquidate=False).action is Action.EXIT


def test_contradicted_catalyst_exits_immediately():
    d = decide(strong(catalyst_contradicted=True), TH, must_liquidate=False)
    assert d.action is Action.EXIT


def test_liquidation_overrides_a_perfect_setup():
    d = decide(strong(), TH, must_liquidate=True)
    assert d.action is Action.EXIT_MANDATORY


def test_big_winner_with_exhaustion_trims_rather_than_dumps():
    t = strong(current_premium=1.6, structure_intact=False,
               bars_without_consolidation=6, extended_from_vwap_atr=2.5)
    assert decide(t, TH, must_liquidate=False).action in (Action.TRIM, Action.EXIT)


def test_hold_score_bounds():
    assert 0.0 <= hold_score(strong())[0] <= 100.0


# --------------------------- catalyst / direction ----------------------------

def cat(**over):
    now = datetime.now(timezone.utc)
    kw = dict(headline="h", source="Reuters", source_url="u", published_at=now,
              received_at=now, verified_at=now, tickers=("X",),
              klass=CatalystClass.CONFIRMED_POLICY, corroborating_sources=2,
              reliability=90.0, surprise=70.0, financial_impact=80.0,
              remaining_repricing=70.0, directional_clarity=80.0,
              vol_expansion=60.0, large_move_potential=70.0)
    kw.update(over)
    return Catalyst(**kw)


def test_unverified_report_never_tradeable():
    ok, why = cat(klass=CatalystClass.UNVERIFIED_REPORT).is_tradeable(TH)
    assert not ok and "unverified" in why


def test_unverified_timestamp_blocks_trade():
    assert not cat(verified_at=None).is_tradeable(TH)[0]


def test_stale_news_is_not_a_catalyst():
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    assert cat(published_at=old).freshness() == 0.0
    assert not cat(published_at=old).is_tradeable(TH)[0]


def test_repeated_statement_weighted_near_zero():
    assert CatalystClass.REPEATED_STATEMENT.weight < 0.2
    assert CatalystClass.CONFIRMED_POLICY.weight == 1.0


def test_indecisive_setup_rejected():
    tape = TapeEvidence(above_vwap=True, below_vwap=True, relative_volume=1.0)
    d = score_direction(cat(surprise=5.0, financial_impact=10.0), tape, TH)
    assert d.chosen is None


def test_clear_bullish_setup_selects_calls():
    tape = TapeEvidence(above_vwap=True, higher_highs=True, reclaimed_vwap=True,
                        relative_volume=2.0, sector_confirms=True, index_confirms=True)
    assert score_direction(cat(), tape, TH).chosen is Direction.CALL


def test_clear_bearish_setup_selects_puts():
    tape = TapeEvidence(below_vwap=True, lower_lows=True, rejected_vwap=True,
                        relative_volume=2.0, sector_confirms=True, index_confirms=True)
    assert score_direction(cat(surprise=-70.0), tape, TH).chosen is Direction.PUT


def test_rank_filters_and_orders():
    tape = TapeEvidence(above_vwap=True, higher_highs=True, relative_volume=2.5,
                        sector_confirms=True, index_confirms=True)
    strong_o = Opportunity("A", cat(), tape, score_direction(cat(), tape, TH),
                           contract_score=85.0)
    weak_o = Opportunity("B", cat(klass=CatalystClass.UNVERIFIED_REPORT), tape,
                         score_direction(cat(), tape, TH), contract_score=10.0)
    out = rank([weak_o, strong_o], TH)
    assert [o.ticker for o in out] == ["A"]
