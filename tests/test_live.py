"""Live scanner parity with the backtest, exits, journal and kill switch."""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from _synth import ET, random_dataset
from config.settings import RiskLimits
from src.execution.journal import LiveJournal
from src.execution.live import exit_for, scan, spread_ok
from src.research.bars import BarStore, to_sessions
from src.research.costs import CostModel
from src.research.edge import EdgeVerdict, Status
from src.research.engine import index_context, prepare, simulate
from src.research.regime import RegimeIndex
from src.research.strategies import FAMILIES, Strategy

SYMS = ["SPY", "AAA", "BBB"]
COSTS = CostModel()
BIG = RiskLimits(max_positions=99, max_consecutive_losses=99, max_daily_loss_pct=1.0)


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    root = tmp_path_factory.mktemp("bars")
    intraday, daily = random_dataset(SYMS, date(2026, 4, 1), sessions=30)
    st = BarStore(root)
    for s in SYMS:
        st.merge(s, "5minute", intraday[s])
        st.merge(s, "day", daily[s])
    days = sorted({b.et.date() for b in intraday["SPY"]})
    return st, days


def verdict(family: str, key: str, end: date) -> EdgeVerdict:
    return EdgeVerdict(strategy=family, params=key, status=Status.VALIDATED,
                       oos={"n": 200, "win_rate": 0.5, "expectancy_r": 0.1, "std_r": 1.0,
                            "profit_factor": 1.3},
                       oos_lower_bound=0.01, stressed_expectancy_r=0.05,
                       holdout={"n": 40, "expectancy_r": 0.1, "win_rate": 0.5,
                                "profit_factor": 1.2},
                       by_regime={}, reasons=("test",), run_id="t", data_start="2026-01-01",
                       data_end=str(end), families_tested=1)


class Recording(Strategy):
    """Wraps a strategy and records every signal the backtest generates."""

    def __init__(self, inner: Strategy) -> None:
        self.inner, self.p, self.family, self.seen = inner, inner.p, inner.family, set()

    @property
    def key(self):
        return self.inner.key

    def on_bar(self, c):
        it = self.inner.on_bar(c)
        if it is not None:
            self.seen.add((c.symbol, c.bar.ts))
        return it


@pytest.mark.parametrize("family", ["orb5_close", "late_momentum", "vwap_pullback_long"])
def test_live_scan_reproduces_backtest_signals(market, family, tmp_path):
    st, days = market
    strat = FAMILIES[family].grid()[0]
    test_days = days[-8:]

    data = {s: prepare(s, to_sessions(st.load(s, "5minute"), s, "5minute")[0],
                       st.load(s, "day")) for s in SYMS}
    idx, reg = index_context(data["SPY"]), RegimeIndex(st.load("SPY", "day"))
    rec = Recording(strat)
    for s in SYMS:
        simulate(data[s], rec, COSTS, index_ctx=idx, regime=reg, dates=set(test_days))

    live = set()
    for d in test_days:
        vs = {family: verdict(family, strat.key, d)}
        t = datetime.combine(d, time(9, 35), ET)
        while t.time() <= time(15, 10):
            r = scan(st, vs, LiveJournal(tmp_path / f"{d}.jsonl"),
                     now=t.astimezone(timezone.utc) + timedelta(seconds=20),
                     equity=1e6, symbols=SYMS, limits=BIG, costs=COSTS, kill_switch=False)
            now = t.astimezone(timezone.utc) + timedelta(seconds=20)
            for sig in [s for s, _ in r.entries] + [s for s, _ in r.rejected]:
                live.add((sig.symbol, sig.signal_ts))
                lag = now - sig.signal_ts           # signal_ts is the bar's left edge
                assert timedelta(minutes=5) < lag < timedelta(minutes=10), \
                    f"{sig.symbol} signal on {sig.signal_ts} seen at {now} — wrong bar"
            t += timedelta(minutes=5)
    assert rec.seen, "test needs at least one signal to be meaningful"
    assert live == rec.seen


def test_no_eligible_strategy_and_no_quota_means_no_entries(market, tmp_path):
    from config.settings import QuotaPolicy
    st, days = market
    now = datetime.combine(days[-1], time(10, 0), ET).astimezone(timezone.utc)
    r = scan(st, {}, LiveJournal(tmp_path / "j.jsonl"), now=now, equity=1e4,
             symbols=SYMS, limits=RiskLimits(), costs=COSTS, kill_switch=False,
             policy=QuotaPolicy(min_trades_per_day=0))
    assert r.entries == [] and any("no eligible strategy" in w for w in r.warnings)


def fill_verdict(end: date) -> EdgeVerdict:
    from src.research.strategies import QuotaFill
    return EdgeVerdict(strategy="quota_fill", params=QuotaFill().key, status=Status.NO_EDGE,
                       oos={"n": 293, "win_rate": 0.33, "expectancy_r": -0.27, "std_r": 1.0,
                            "profit_factor": 0.36},
                       oos_lower_bound=-0.36, stressed_expectancy_r=-0.31, holdout=None,
                       by_regime={}, reasons=("measured",), run_id="t",
                       data_start="2026-01-01", data_end=str(end), families_tested=1)


def test_quota_fills_only_at_checkpoint_up_to_the_minimum(market, tmp_path):
    from config.settings import QuotaPolicy
    st, days = market
    d = days[-1]
    vs = {"quota_fill": fill_verdict(d)}
    j = LiveJournal(tmp_path / "q.jsonl")
    before = datetime.combine(d, time(13, 25, 30), ET).astimezone(timezone.utc)
    assert scan(st, vs, j, now=before, equity=1e6, symbols=SYMS, limits=BIG, costs=COSTS,
                kill_switch=False).entries == []
    at = datetime.combine(d, time(13, 30, 30), ET).astimezone(timezone.utc)
    r = scan(st, vs, j, now=at, equity=1e6, symbols=SYMS, limits=BIG, costs=COSTS,
             kill_switch=False, policy=QuotaPolicy(min_trades_per_day=2))
    assert len(r.entries) == 2 and all(p.tier == "quota_fill" for _, p in r.entries)
    scores = [s.score for s, _ in r.entries]
    assert scores == sorted(scores, reverse=True)
    assert "NOT an edge" in r.entries[0][1].render()


def test_quota_counts_todays_journal_entries(market, tmp_path):
    from config.settings import QuotaPolicy
    st, days = market
    d = days[-1]
    j = LiveJournal(tmp_path / "c.jsonl")
    j.record_entry(session=d, symbol="AAA", strategy="orb_long", params="k", direction=1,
                   signal_ts="x", entry_ts=datetime.combine(d, time(10), ET).isoformat(),
                   fill=1.0, shares=1.0, stop=0.5, target=2.0, order_id="o")
    at = datetime.combine(d, time(13, 30, 30), ET).astimezone(timezone.utc)
    r = scan(st, {"quota_fill": fill_verdict(d)}, j, now=at, equity=1e6, symbols=SYMS,
             limits=BIG, costs=COSTS, kill_switch=False, policy=QuotaPolicy(min_trades_per_day=2))
    assert [s.symbol for s, _ in r.entries] != [] and len(r.entries) == 1
    assert r.entries[0][0].symbol != "AAA"            # never re-enter a symbol traded today


def test_kill_switch_rejects_every_signal(market, tmp_path):
    st, days = market
    strat = FAMILIES["orb5_close"].grid()[0]
    hits = []
    for d in days[-5:]:
        now = datetime.combine(d, time(9, 35, 20), ET).astimezone(timezone.utc)
        r = scan(st, {"orb5_close": verdict("orb5_close", strat.key, d)},
                 LiveJournal(tmp_path / "k.jsonl"), now=now, equity=1e6, symbols=SYMS,
                 limits=BIG, costs=COSTS, kill_switch=True)
        assert r.entries == []
        hits += [why for _, why in r.rejected]
    assert hits and all(any("kill switch" in w for w in why) for why in hits)


def test_future_bars_in_the_store_are_ignored(market, tmp_path):
    """The store may hold a partial in-progress bar; scans must not use it."""
    st, days = market
    d = days[-1]
    now = datetime.combine(d, time(10, 2, 30), ET).astimezone(timezone.utc)
    from src.execution.live import load_live
    data, _, _, _ = load_live(st, SYMS, d, now)
    last = data["SPY"].sessions[-1].bars[-1]
    assert last.et.time() == time(9, 55)        # the 10:00 bar is still forming


def test_exits_stop_target_and_eod(tmp_path):
    from src.research.bars import Bar, Session
    from src.research.engine import SymbolData
    from src.execution.journal import LiveEntry
    d = date(2026, 6, 15)
    t0 = datetime.combine(d, time(9, 30), ET).astimezone(timezone.utc)
    e = LiveEntry("x", str(d), "XYZ", "f", "k", 1, t0.isoformat(),
                  (t0 + timedelta(minutes=10)).isoformat(), 100.0, 1.0, 99.0, 102.0, "o")

    def sd_with(bars):
        s = Session("XYZ", d, bars, datetime.combine(d, time(16), ET))
        return SymbolData("XYZ", "5minute", [s], None, [], [], [], [], [], [], [], [])

    flat = [Bar(t0 + timedelta(minutes=5 * i), 100, 100.5, 99.5, 100, 1) for i in range(4)]
    now = t0 + timedelta(minutes=20)
    assert exit_for(e, sd_with(flat), now) is None
    dip = flat[:3] + [Bar(flat[3].ts, 100, 100.2, 98.9, 99, 1)]
    assert exit_for(e, sd_with(dip), now).reason == "stop"
    pop = flat[:3] + [Bar(flat[3].ts, 100, 102.02, 99.8, 102, 1)]
    assert exit_for(e, sd_with(pop), now).reason == "target"
    touch = flat[:3] + [Bar(flat[3].ts, 100, 102.0, 99.8, 101.9, 1)]
    assert exit_for(e, sd_with(touch), now) is None          # touching is not a fill
    late = datetime.combine(d, time(15, 45), ET).astimezone(timezone.utc)
    assert exit_for(e, sd_with(flat), late).reason == "eod"


def test_journal_rebuilds_risk_state(tmp_path):
    j = LiveJournal(tmp_path / "j.jsonl")
    d = date(2026, 10, 14)
    a = j.record_entry(session=d, symbol="AAA", strategy="f", params="k", direction=1,
                       signal_ts="2026-10-14T13:35:00+00:00", entry_ts="2026-10-14T13:40:00+00:00",
                       fill=100.0, shares=1.0, stop=99.0, target=102.0, order_id="o1")
    with pytest.raises(ValueError):
        j.record_entry(session=d, symbol="AAA", strategy="f", params="k", direction=1,
                       signal_ts="x", entry_ts="y", fill=1, shares=1, stop=0.5, target=2,
                       order_id="o2")
    x = j.record_exit(a.id, exit_ts="2026-10-14T14:00:00+00:00", fill=98.9, reason="stop",
                      order_id="o3")
    assert x.pnl == -1.1 and x.r == pytest.approx(-1.1)
    st = j.risk_state(d, equity_now=998.9)
    assert st.start_equity == pytest.approx(1000.0) and st.consecutive_losses == 1
    assert st.positions == {} and j.closed_r("f") == [pytest.approx(-1.1)]


def test_spread_check():
    c = CostModel.calibrated()
    assert spread_ok("SPY", 777.82, 777.83, c)[0]
    assert not spread_ok("SPY", 777.0, 778.0, c)[0]
    assert not spread_ok("SPY", 0, 778.0, c)[0]
    assert c.half_spread_for("COIN") > c.half_spread_for("AAPL")


# --------------------------------- calendar ---------------------------------

def cal(y, m, d, hh, mm):
    from scripts.live import calendar
    return calendar(datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(timezone.utc))


def test_calendar_phases_and_cycle_times():
    pre = cal(2026, 10, 14, 9, 25)                       # Wednesday, 8:25 CT
    assert pre["trading_day"] and pre["phase"] == "preopen"
    assert pre["next_cycle_utc"] == "2026-10-14T13:36+00:00"    # 9:36 ET, first bar closed
    mid = cal(2026, 10, 14, 11, 2)
    assert mid["phase"] == "open" and mid["next_cycle_utc"] == "2026-10-14T15:06+00:00"
    assert cal(2026, 10, 14, 15, 20)["phase"] == "manage_only"
    late = cal(2026, 10, 14, 15, 52)
    assert late["phase"] == "flatten" and late["next_cycle_utc"] is None
    assert pre["eod_safety_utc"] == "2026-10-14T19:44+00:00"


def test_calendar_holidays_early_closes_and_dst():
    assert not cal(2026, 11, 26, 9, 0)["trading_day"]           # Thanksgiving
    assert not cal(2026, 10, 17, 9, 0)["trading_day"]           # Saturday
    half = cal(2026, 11, 27, 9, 25)
    assert half["close_et"] == "13:00" and half["flatten_at_et"] == "12:45"
    est = cal(2026, 11, 2, 9, 25)                               # after DST ends
    assert est["next_cycle_utc"] == "2026-11-02T14:36+00:00"
    assert cal(2026, 10, 16, 9, 0)["last_session_of_week"]       # Friday
    assert not cal(2026, 11, 25, 9, 0)["last_session_of_week"]   # Fri 11/27 still trades
    assert cal(2026, 7, 2, 9, 0)["last_session_of_week"]         # Fri 7/3 is a holiday
