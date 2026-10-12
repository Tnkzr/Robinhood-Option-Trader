"""Bar ingestion, validation, storage and causal indicators."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from _synth import daily_history, flat_session, open_utc
from src.research.bars import (AdjustmentMismatch, Bar, BarStore, clean, ingest_file,
                               to_sessions)
from src.research.indicators import (DailyContextIndex, ema, relative_volume, rsi,
                                     session_vwap, sma)
from src.research.strategies import Prefix

D = date(2026, 6, 15)


def raw(ts: datetime, o=100.0, h=100.5, l=99.5, c=100.2, v=1000, **extra) -> dict:
    return {"begins_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "open_price": str(o),
            "high_price": str(h), "low_price": str(l), "close_price": str(c),
            "volume": v, **extra}


def test_clean_drops_fabricated_and_bad_bars():
    t = open_utc(D)
    rows = [
        raw(t),
        raw(t + timedelta(minutes=5), interpolated=True),               # broker gap-fill
        raw(t + timedelta(minutes=10), o=777.31, h=777.31, l=777.31, c=777.31, v=0),
        raw(t + timedelta(minutes=15), h=99.0),                         # high < open
        raw(t),                                                         # duplicate
        raw(t - timedelta(minutes=30)),                                 # premarket
        raw(t + timedelta(minutes=390)),                                # 16:00 bar
        raw(t + timedelta(minutes=20)),
    ]
    bars, rep = clean(rows, "XYZ", "5minute")
    assert [b.ts for b in bars] == [t, t + timedelta(minutes=20)]
    assert (rep.interpolated, rep.synthetic, rep.invalid, rep.duplicates,
            rep.outside_rth) == (1, 1, 1, 1, 2)


def test_daily_bars_keep_their_utc_date():
    bars, _ = clean([raw(datetime(2026, 6, 15, tzinfo=timezone.utc))], "XYZ", "day")
    assert bars[0].ts.date() == D


def test_sessions_accept_full_and_early_close_reject_truncated():
    full = flat_session(D)
    early = flat_session(D + timedelta(days=1), n=42)          # 09:30 → 13:00
    cut = flat_session(D + timedelta(days=2), n=54)            # ends 14:00
    holey = flat_session(D + timedelta(days=3))
    del holey[10:20]
    sessions, excluded = to_sessions(full + early + cut + holey, "XYZ", "5minute")
    assert [s.date for s in sessions] == [D, D + timedelta(days=1)]
    assert sessions[1].early_close and not sessions[0].early_close
    assert len(excluded) == 2


def test_store_roundtrip_is_deterministic(tmp_path):
    st = BarStore(tmp_path)
    bars = flat_session(D)
    assert st.merge("XYZ", "5minute", bars) == (78, 78)
    assert st.load("XYZ", "5minute") == bars
    fp = st.fingerprint(["XYZ"], "5minute")
    assert st.merge("XYZ", "5minute", bars) == (0, 78)
    assert st.fingerprint(["XYZ"], "5minute") == fp


def test_store_refuses_split_mismatch(tmp_path):
    st = BarStore(tmp_path)
    st.merge("XYZ", "5minute", flat_session(D, 100.0))
    with pytest.raises(AdjustmentMismatch):
        st.merge("XYZ", "5minute", flat_session(D, 50.0))
    assert st.merge("XYZ", "5minute", flat_session(D, 50.0), replace=True) == (78, 78)
    assert st.load("XYZ", "5minute")[0].close == 50.0


def test_ingest_tool_result_file(tmp_path):
    t = open_utc(D)
    payload = {"data": {"results": [{"symbol": "xyz", "interval": "5minute",
                                     "bars": [raw(t), raw(t, interpolated=True)]}]},
               "guide": "..."}
    f = tmp_path / "result.txt"
    f.write_text(json.dumps(payload))
    lines = ingest_file(f, BarStore(tmp_path / "store"))
    assert "kept 1" in lines[0] and "interpolated 1" in lines[0]
    assert len(BarStore(tmp_path / "store").load("XYZ", "5minute")) == 1


def test_basic_indicators():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ema([1.0] * 5 + [2.0], 3)
    assert e[:2] == [None, None] and e[2] == 1.0 and 1.0 < e[5] < 2.0
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0
    vw = session_vwap([Bar(open_utc(D), 10, 11, 9, 10, 100), Bar(open_utc(D), 20, 21, 19, 20, 300)])
    assert vw == [10.0, 17.5]


def test_relative_volume_uses_only_prior_sessions():
    sessions, _ = to_sessions(
        sum((flat_session(D + timedelta(days=k), vol=1000 * (k + 1)) for k in range(12)), []),
        "XYZ", "5minute")
    rv = relative_volume(sessions, lookback=20, min_history=10)
    assert all(v is None for row in rv[:10] for v in row)
    # session 10 has vol 11000/bar; prior ten average 5500/bar → rvol 2.0
    assert rv[10][0] == pytest.approx(2.0)


def test_daily_context_never_includes_the_session_itself():
    hist = daily_history(D, n=20, rng=2.0)
    today = Bar(datetime(2026, 6, 15, tzinfo=timezone.utc), 100, 150, 50, 100, 1)
    idx = DailyContextIndex(hist + [today])
    ctx = idx.get(D)
    assert ctx.atr == pytest.approx(2.0)
    assert ctx.prev_high == hist[-1].high


def test_prefix_view_blocks_future_bars():
    bars = flat_session(D, n=10)
    p = Prefix(bars, 3)
    assert len(p) == 3 and p[-1] is bars[2] and len(p[:10]) == 3
    with pytest.raises(IndexError):
        p[3]
