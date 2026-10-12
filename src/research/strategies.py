"""Deterministic intraday strategies for research.

A strategy sees one BarContext at the close of each bar and may return an
EntryIntent. It cannot see later bars (BarContext.bars is a bounded view) and it
never decides fills, sizing or exits — the engine does that identically for all.

Parameter grids are kept small on purpose: every extra combination is another
chance to fit noise, and walk-forward selection only partly corrects for that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import product
from typing import Any, Iterator, Sequence

from src.research.bars import Bar


class Prefix(Sequence[Bar]):
    """Read-only view of bars[:n]. Indexing past n raises — no peeking ahead."""

    __slots__ = ("_s", "_n")

    def __init__(self, s: Sequence[Bar], n: int) -> None:
        self._s, self._n = s, n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i):  # type: ignore[override]
        if isinstance(i, slice):
            return [self._s[j] for j in range(*i.indices(self._n))]
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError("bar not yet available")
        return self._s[i]

    def __iter__(self) -> Iterator[Bar]:
        for j in range(self._n):
            yield self._s[j]


@dataclass(frozen=True)
class BarContext:
    symbol: str
    date: date
    i: int                       # index of the bar that just closed
    bars: Prefix                 # bars[0..i]
    vwap: float
    ema9: float | None
    ema20: float | None
    ema50: float | None
    rsi: float | None
    rvol: float | None
    atr: float                   # daily ATR(14) from prior sessions
    prev_close: float
    hod: float                   # through bar i
    lod: float
    prev_hod: float              # through bar i-1
    prev_lod: float
    index_above_vwap: bool | None
    minutes_since_open: int      # at the close of bar i

    @property
    def bar(self) -> Bar:
        return self.bars[self.i]


@dataclass(frozen=True)
class EntryIntent:
    direction: int               # +1 long, -1 short
    stop: float
    target_r: float | None = None
    target_price: float | None = None
    note: str = ""


class Strategy:
    family = "base"
    DEFAULTS: dict[str, Any] = {}
    GRID: dict[str, list[Any]] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"{self.family}: unknown params {sorted(unknown)}")
        self.p = {**self.DEFAULTS, **params}

    @property
    def key(self) -> str:
        return self.family + "(" + ",".join(f"{k}={self.p[k]}" for k in sorted(self.p)) + ")"

    @classmethod
    def grid(cls) -> list["Strategy"]:
        names = list(cls.GRID)
        return [cls(**dict(zip(names, combo))) for combo in product(*cls.GRID.values())] \
            if names else [cls()]

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        raise NotImplementedError

    @staticmethod
    def _index_ok(c: BarContext, direction: int) -> bool:
        if c.index_above_vwap is None:
            return False
        return c.index_above_vwap if direction > 0 else not c.index_above_vwap


class OpeningRangeBreakout(Strategy):
    """First close outside the opening range, with volume and the index agreeing."""

    family = "orb"
    DEFAULTS = {"or_bars": 6, "rvol_min": 1.2, "target_r": 2.0, "stop": "range",
                "last_entry_min": 120}
    GRID = {"or_bars": [3, 6], "target_r": [1.5, 2.0], "stop": ["range", "mid"]}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        n = self.p["or_bars"]
        if c.i < n or c.minutes_since_open > self.p["last_entry_min"]:
            return None
        if c.rvol is None or c.rvol < self.p["rvol_min"]:
            return None
        orb = c.bars[:n]
        hi, lo = max(b.high for b in orb), min(b.low for b in orb)
        b, pb = c.bar, c.bars[c.i - 1]
        mid = (hi + lo) / 2
        if b.close > hi >= pb.close and self._index_ok(c, 1):
            return EntryIntent(1, lo if self.p["stop"] == "range" else mid,
                               target_r=self.p["target_r"], note="ORB long")
        if b.close < lo <= pb.close and self._index_ok(c, -1):
            return EntryIntent(-1, hi if self.p["stop"] == "range" else mid,
                               target_r=self.p["target_r"], note="ORB short")
        return None


class VwapPullback(Strategy):
    """In an established trend, a pullback to VWAP that holds and turns."""

    family = "vwap_pullback"
    DEFAULTS = {"min_minutes": 30, "touch_atr": 0.05, "stop_atr": 0.10, "target_r": 2.0,
                "rvol_min": 1.0}
    GRID = {"touch_atr": [0.05, 0.10], "stop_atr": [0.10, 0.20], "target_r": [1.5, 2.0]}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        if c.minutes_since_open < self.p["min_minutes"] or c.i < 1:
            return None
        if c.ema9 is None or c.ema20 is None or c.rvol is None or c.rvol < self.p["rvol_min"]:
            return None
        b, pb, v, a = c.bar, c.bars[c.i - 1], c.vwap, c.atr
        if (c.ema9 > c.ema20 and pb.close > v and b.close > v and b.close > b.open
                and b.low <= v + self.p["touch_atr"] * a and self._index_ok(c, 1)):
            return EntryIntent(1, min(b.low, v) - self.p["stop_atr"] * a,
                               target_r=self.p["target_r"], note="VWAP hold long")
        if (c.ema9 < c.ema20 and pb.close < v and b.close < v and b.close < b.open
                and b.high >= v - self.p["touch_atr"] * a and self._index_ok(c, -1)):
            return EntryIntent(-1, max(b.high, v) + self.p["stop_atr"] * a,
                               target_r=self.p["target_r"], note="VWAP reject short")
        return None


class MomentumContinuation(Strategy):
    """New high/low of day on volume, aligned EMAs, not yet stretched from VWAP."""

    family = "momentum"
    DEFAULTS = {"min_minutes": 30, "rvol_min": 1.5, "max_ext_atr": 0.5, "stop_atr": 0.25,
                "target_r": 2.0}
    GRID = {"rvol_min": [1.5, 2.0], "max_ext_atr": [0.3, 0.6], "target_r": [1.5, 2.0]}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        if c.minutes_since_open < self.p["min_minutes"] or c.i < 1:
            return None
        if None in (c.ema9, c.ema20, c.ema50) or c.rvol is None or c.rvol < self.p["rvol_min"]:
            return None
        b, v, a = c.bar, c.vwap, c.atr
        rng = b.high - b.low
        if rng <= 0:
            return None
        ext = self.p["max_ext_atr"] * a
        if (b.high > c.prev_hod and b.close - b.low >= 0.7 * rng
                and c.ema9 > c.ema20 > c.ema50 and 0 < b.close - v <= ext
                and self._index_ok(c, 1)):
            return EntryIntent(1, b.close - self.p["stop_atr"] * a,
                               target_r=self.p["target_r"], note="new HOD long")
        if (b.low < c.prev_lod and b.high - b.close >= 0.7 * rng
                and c.ema9 < c.ema20 < c.ema50 and 0 < v - b.close <= ext
                and self._index_ok(c, -1)):
            return EntryIntent(-1, b.close + self.p["stop_atr"] * a,
                               target_r=self.p["target_r"], note="new LOD short")
        return None


class VwapMeanReversion(Strategy):
    """Stretched from VWAP with exhausted RSI and a reversal bar — fade toward VWAP."""

    family = "mean_reversion"
    DEFAULTS = {"min_minutes": 30, "ext_atr": 0.6, "rsi_hi": 75, "stop_atr": 0.10,
                "min_rr": 1.0}
    GRID = {"ext_atr": [0.5, 0.8], "rsi_hi": [75, 80], "stop_atr": [0.10, 0.20]}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        if c.minutes_since_open < self.p["min_minutes"] or c.rsi is None:
            return None
        b, v, a = c.bar, c.vwap, c.atr
        ext = self.p["ext_atr"] * a
        if b.close - v >= ext and c.rsi >= self.p["rsi_hi"] and b.close < b.open:
            stop = c.hod + self.p["stop_atr"] * a
            if (b.close - v) / (stop - b.close) >= self.p["min_rr"]:
                return EntryIntent(-1, stop, target_price=v, note="fade to VWAP short")
        if v - b.close >= ext and c.rsi <= 100 - self.p["rsi_hi"] and b.close > b.open:
            stop = c.lod - self.p["stop_atr"] * a
            if (v - b.close) / (b.close - stop) >= self.p["min_rr"]:
                return EntryIntent(1, stop, target_price=v, note="fade to VWAP long")
        return None


FAMILIES: dict[str, type[Strategy]] = {
    cls.family: cls for cls in
    (OpeningRangeBreakout, VwapPullback, MomentumContinuation, VwapMeanReversion)
}
