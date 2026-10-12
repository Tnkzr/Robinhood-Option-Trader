"""Engine fill rules, and the no-lookahead property for every strategy."""
from __future__ import annotations

import random
import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from _synth import ET, daily_history, flat_session, random_dataset
from src.research.bars import Bar, to_sessions
from src.research.costs import CostModel
from src.research.engine import index_context, prepare, simulate
from src.research.strategies import FAMILIES, EntryIntent, Strategy

D = date(2026, 6, 15)
COSTS = CostModel()        # XYZ is a single stock: 1bp half spread + 1bp slippage


class Once(Strategy):
    family = "once"

    def __init__(self, at_i: int, intent: EntryIntent) -> None:
        super().__init__()
        self.at_i, self.intent = at_i, intent

    def on_bar(self, c):
        return self.intent if c.i == self.at_i else None


def run(bars: list[Bar], strat: Strategy) -> list:
    sessions, _ = to_sessions(bars, "XYZ", "5minute")
    sd = prepare("XYZ", sessions, daily_history(D, n=20, rng=2.0))
    return simulate(sd, strat, COSTS)


def with_bar(bars, i, o, h, l, c):
    bars = list(bars)
    bars[i] = Bar(bars[i].ts, o, h, l, c, bars[i].volume)
    return bars


LONG = EntryIntent(1, stop=99.0, target_r=2.0)


def test_entry_fills_at_next_open_and_target_needs_trade_through():
    bars = with_bar(flat_session(D), 8, 100, 102.2, 99.9, 102)
    [t] = run(bars, Once(5, LONG))
    assert t.entry == pytest.approx(100 * 1.0002)
    assert t.entry_ts == bars[6].ts and t.signal_ts == bars[5].ts
    assert t.exit_reason == "target" and t.exit == pytest.approx(t.target)
    assert t.r == pytest.approx(2.0)
    assert t.gross_r > t.r


def test_touching_target_is_not_a_fill():
    t_px = 100.02 + 2 * 1.02
    bars = with_bar(flat_session(D), 8, 100, t_px, 99.9, 100)
    [t] = run(bars, Once(5, LONG))
    assert t.exit_reason == "eod"
    assert t.exit_ts.astimezone(ET).time() == time(15, 45)


def test_stop_assumed_first_when_bar_spans_both():
    bars = with_bar(flat_session(D), 8, 100, 103, 98.5, 100)
    [t] = run(bars, Once(5, LONG))
    assert t.exit_reason == "stop"
    assert t.exit == pytest.approx(99.0 * (1 - 4e-4))
    assert t.r < -1.0


def test_gap_through_stop_fills_at_worse_open():
    bars = with_bar(flat_session(D), 8, 98.0, 98.1, 97.9, 98.0)
    [t] = run(bars, Once(5, LONG))
    assert t.exit_reason == "gap_stop"
    assert t.raw_exit == 98.0


def test_entry_skipped_when_open_gaps_through_stop():
    bars = with_bar(flat_session(D), 6, 98.9, 99.0, 98.8, 98.9)
    assert run(bars, Once(5, LONG)) == []


def test_no_entries_in_final_45_minutes():
    assert run(flat_session(D), Once(68, LONG)) == []
    assert len(run(flat_session(D), Once(67, LONG))) == 1


def test_short_side_is_symmetric():
    bars = with_bar(flat_session(D), 8, 100, 100.1, 98.9, 99)
    [t] = run(bars, Once(5, EntryIntent(-1, stop=101.0, target_r=1.0)))
    assert t.exit_reason == "target" and t.r == pytest.approx(1.0)


def test_one_signal_per_session_by_default():
    class Always(Once):
        def on_bar(self, c):
            return self.intent

    assert len(run(flat_session(D), Always(0, LONG))) == 1


# ------------------------------ no lookahead --------------------------------

def perturb_after(bars: list[Bar], cut: datetime, seed: int) -> list[Bar]:
    r = random.Random(seed)
    out = []
    for b in bars:
        if b.ts < cut:
            out.append(b)
            continue
        k = r.uniform(0.9, 1.1)
        out.append(Bar(b.ts, b.open * k, b.high * k * 1.01, b.low * k * 0.99, b.close * k,
                       int(b.volume * r.uniform(0.2, 5))))
    return out


def signals_before(trades, cut):
    return sorted((t.symbol, t.params, t.signal_ts, t.direction, round(t.entry, 6))
                  for t in trades if t.entry_ts < cut)


SYMS = ["SPY", "AAA", "BBB"]


def build(intra, dly):
    data = {s: prepare(s, to_sessions(intra[s], s, "5minute")[0], dly[s]) for s in SYMS}
    return data, index_context(data["SPY"])


def future_changed(intraday, daily, cut: datetime):
    intra = {s: perturb_after(intraday[s], cut, 1) for s in SYMS}
    dly = {s: [b if b.ts.date() < cut.date() else replace(b, high=b.high * 1.5)
               for b in daily[s]] for s in SYMS}
    return intra, dly


class Recorder(Strategy):
    """Never trades; records every context the engine shows a strategy."""
    family = "recorder"

    def __init__(self) -> None:
        super().__init__()
        self.seen = []

    def on_bar(self, c):
        self.seen.append((c.symbol, c.bar.ts, len(c.bars), c.vwap, c.ema9, c.ema20, c.ema50,
                          c.rsi, c.rvol, c.atr, c.prev_close, c.hod, c.lod, c.prev_hod,
                          c.prev_lod, c.index_above_vwap, c.minutes_since_open))
        return None


def contexts(data, idx):
    rec = Recorder()
    for s in SYMS:
        simulate(data[s], rec, COSTS, index_ctx=idx)
    return rec.seen


@pytest.mark.parametrize("cut_at", [(25, time(9, 50)), (30, time(12, 0)), (35, time(14, 30))])
def test_contexts_cannot_see_the_future(cut_at):
    intraday, daily = random_dataset(SYMS, date(2026, 4, 1), sessions=40)
    day = sorted({b.et.date() for b in intraday["SPY"]})[cut_at[0]]
    cut = datetime.combine(day, cut_at[1], ET)
    base = [c for c in contexts(*build(intraday, daily)) if c[1] < cut]
    changed = [c for c in contexts(*build(*future_changed(intraday, daily, cut))) if c[1] < cut]
    assert len(base) > 1000 and base == changed


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_strategies_cannot_see_the_future(family):
    intraday, daily = random_dataset(SYMS, date(2026, 4, 1), sessions=40)
    days = sorted({b.et.date() for b in intraday["SPY"]})

    def all_trades(data, idx):
        return [t for strat in FAMILIES[family].grid() for s in SYMS
                for t in simulate(data[s], strat, COSTS, index_ctx=idx)]

    base = all_trades(*build(intraday, daily))
    for k, hhmm in ((20, time(10, 30)), (30, time(12, 0)), (36, time(14, 0))):
        cut = datetime.combine(days[k], hhmm, ET)
        changed = all_trades(*build(*future_changed(intraday, daily, cut)))
        assert signals_before(base, cut) == signals_before(changed, cut)
        assert [t for t in base if t.exit_ts < cut] == [t for t in changed if t.exit_ts < cut]


def test_lookahead_check_has_teeth():
    """The property test above must actually exercise trades."""
    syms = ["SPY", "AAA", "BBB"]
    intraday, daily = random_dataset(syms, date(2026, 4, 1), sessions=40)
    data = {s: prepare(s, to_sessions(intraday[s], s, "5minute")[0], daily[s]) for s in syms}
    idx = index_context(data["SPY"])
    for fam, cls in FAMILIES.items():
        n = sum(len(simulate(data[s], st, COSTS, index_ctx=idx)) for st in cls.grid() for s in syms)
        assert n > 0, f"{fam} produced no trades on synthetic data"
