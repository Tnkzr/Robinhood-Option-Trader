"""Deterministic risk controls and their enforcement in the portfolio replay."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from _synth import mk_trade
from config.settings import RiskLimits
from src.research.costs import CostModel
from src.research.portfolio import run_portfolio
from src.risk.limits import (OpenPosition, RiskState, check_new_entry, kill_switch_engaged,
                             option_contracts_for_risk, position_size)

L = RiskLimits()
D = date(2026, 6, 15)
AT = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)
NO_COST = CostModel(sec_fee_rate=0.0, taf_per_share=0.0)


def test_size_risks_half_a_percent():
    s = position_size(10_000, 100.0, 99.0, L)
    assert s.shares == 50 and s.risk_dollars == 50.0 and s.notional == 5000.0


def test_size_capped_by_leverage():
    s = position_size(10_000, 100.0, 99.9, L)        # risk says 500 sh = $50k notional
    assert s.shares == 100 and s.notional == 10_000.0


def test_size_refuses_rather_than_rounding_up():
    s = position_size(1_000, 100.0, 90.0, L)          # $5 budget, $10/share risk
    assert s.shares == 0 and "below one share" in s.reason


def test_option_risk_is_the_premium():
    assert option_contracts_for_risk(25_000, 52.0, L).shares == 2
    assert option_contracts_for_risk(1_000, 52.0, L).shares == 0


def state(**kw) -> RiskState:
    s = RiskState(session=D, start_equity=10_000)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def entry(st, **kw):
    args = dict(symbol="XYZ", direction=1, entry=100.0, stop=99.0, risk_dollars=50.0,
                notional=5000.0, limits=L, at=AT)
    args.update(kw)
    return check_new_entry(st, **args)


@pytest.mark.parametrize("st_kw,entry_kw,fragment", [
    ({}, {"kill_switch": True}, "kill switch"),
    ({"realized_pnl": -200.0}, {}, "daily loss limit"),
    ({"consecutive_losses": 3}, {}, "consecutive losses"),
    ({}, {"direction": -1, "stop": 101.0}, "short selling not enabled"),
    ({}, {"stop": 100.5}, "long stop must be below"),
    ({}, {"risk_dollars": 60.0}, "exceeds"),
    ({}, {"notional": 11_000.0}, "leverage"),
])
def test_entry_blocked(st_kw, entry_kw, fragment):
    r = entry(state(**st_kw), **entry_kw)
    assert not r and any(fragment in f for f in r.failures), r.failures


def test_no_adding_to_an_open_position():
    st = state()
    st.open(OpenPosition("XYZ", 1, 10, 100.0, 99.0))
    r = entry(st)
    assert not r and any("averaging down" in f for f in r.failures)
    with pytest.raises(ValueError):
        st.open(OpenPosition("XYZ", 1, 10, 99.0, 98.0))


def test_cooldown_after_a_loss_on_the_same_symbol():
    st = state()
    st.close("XYZ", -50.0, AT)
    assert not entry(st, at=AT + timedelta(minutes=5), risk_dollars=45.0)
    assert entry(st, at=AT + timedelta(minutes=16), risk_dollars=45.0)
    assert entry(st, symbol="ABC", at=AT + timedelta(minutes=5), risk_dollars=45.0)


def test_risk_budget_shrinks_with_equity():
    st = state(realized_pnl=-50.0)
    assert not entry(st, risk_dollars=50.0)
    assert entry(st, risk_dollars=49.75)


def test_session_roll_resets_daily_counters_and_compounds():
    st = state(realized_pnl=-150.0, consecutive_losses=3)
    st.roll_session(D + timedelta(days=1))
    assert st.start_equity == 9_850.0 and st.consecutive_losses == 0
    assert entry(st, risk_dollars=49.25)


def test_kill_switch_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADER_KILL_SWITCH", raising=False)
    p = tmp_path / "KILL_SWITCH"
    assert not kill_switch_engaged(str(p))
    p.write_text("halt")
    assert kill_switch_engaged(str(p))


# ------------------------------ portfolio replay -----------------------------

def test_three_consecutive_losses_halt_the_session():
    ts = [mk_trade(-1.0, D, symbol=f"S{i}", minute=10 * i, hold_min=5) for i in range(5)]
    ts.append(mk_trade(1.0, D + timedelta(days=1), symbol="NEXT"))
    res = run_portfolio(ts, L, 10_000, NO_COST)
    assert [f.trade.symbol for f in res.accepted] == ["S0", "S1", "S2", "NEXT"]
    assert all("consecutive" in r[0] for _, r in res.rejected)


def test_daily_loss_limit_halts_new_entries():
    lim = RiskLimits(max_consecutive_losses=99)
    ts = [mk_trade(-1.0, D, symbol=f"S{i}", minute=10 * i, hold_min=5) for i in range(8)]
    res = run_portfolio(ts, lim, 10_000, NO_COST)
    # sizing follows equity: after -$197 (-1.97%) one more fits, then -2.46% halts
    assert [f.pnl for f in res.accepted] == [-50.0, -49.0, -49.0, -49.0, -49.0]
    assert len(res.rejected) == 3
    assert all("daily loss limit" in r[0] for _, r in res.rejected)


def test_only_realized_losses_count_at_decision_time():
    ts = [mk_trade(-1.0, D, symbol="A", minute=0, hold_min=5),
          mk_trade(-1.0, D, symbol="B", minute=10, hold_min=5),
          mk_trade(-1.0, D, symbol="C", minute=20, hold_min=120),   # loses, but still open...
          mk_trade(-1.0, D, symbol="D", minute=40, hold_min=5),     # ...when D enters
          mk_trade(1.0, D, symbol="E", minute=60, hold_min=5)]      # A, B, D realized
    res = run_portfolio(ts, L, 10_000, NO_COST)
    assert [f.trade.symbol for f in res.accepted] == ["A", "B", "C", "D"]
    assert res.rejected[0][0].symbol == "E"


def test_replay_pnl_and_drawdown():
    ts = [mk_trade(2.0, D), mk_trade(-1.0, D + timedelta(days=1))]
    res = run_portfolio(ts, L, 10_000, NO_COST)
    assert [f.shares for f in res.accepted] == [50, 50]    # $50.50 budget → 50 sh
    assert [f.pnl for f in res.accepted] == [100.0, -50.0]
    assert res.final_equity == pytest.approx(10_050.0)
    assert res.max_drawdown_pct == pytest.approx(50.0 / 10_100)
