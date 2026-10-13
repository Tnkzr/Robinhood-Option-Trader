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
    score: float = 0.0           # cross-sectional rank key (quota fills)


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


def long_only(base: type[Strategy]) -> type[Strategy]:
    """The same rules, taking only the long side — the agentic account cannot
    short shares, so only long-only results describe tradeable behaviour."""
    class _LongOnly(base):  # type: ignore[valid-type, misc]
        family = base.family + "_long"

        def on_bar(self, c: BarContext) -> EntryIntent | None:
            intent = super().on_bar(c)
            return intent if intent is not None and intent.direction > 0 else None

    _LongOnly.__name__ = _LongOnly.__qualname__ = base.__name__ + "Long"
    return _LongOnly


class OpeningRangeToClose(Strategy):
    """5-minute opening range, held to the close (Zarattini & Aziz, 2023).

    If the first 5-minute bar is bullish on elevated relative volume ("stock in
    play"), buy at the second bar's open with the stop at the first bar's low
    (or 10% of ATR); no profit target — exit at the stop or the session close.
    Long side only.
    """

    family = "orb5_close"
    DEFAULTS = {"rvol_min": 1.0, "stop": "bar_low"}
    GRID = {"rvol_min": [1.0, 2.0], "stop": ["bar_low", "atr10"]}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        if c.i != 0 or c.rvol is None or c.rvol < self.p["rvol_min"]:
            return None
        b = c.bar
        if b.close <= b.open:
            return None
        stop = b.low if self.p["stop"] == "bar_low" else b.close - 0.10 * c.atr
        return EntryIntent(1, stop, target_r=50.0, note="ORB5 to close")


class LateDayMomentum(Strategy):
    """Market intraday momentum (Gao, Han, Li & Zhou, JFE 2018).

    The day's early return predicts its final stretch. At 15:00 ET, buy when the
    first-half-hour (or open-to-now) return is positive by at least min_ret_atr
    of daily ATR; exit at the 15:45 flatten or the stop. Long side only.
    """

    family = "late_momentum"
    DEFAULTS = {"signal": "first30", "min_ret_atr": 0.0, "stop_atr": 0.25, "check_min": 330}
    GRID = {"signal": ["first30", "day"], "min_ret_atr": [0.0, 0.25],
            "stop_atr": [0.25, 0.5]}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        if c.minutes_since_open != self.p["check_min"] or c.i < 6:
            return None
        o = c.bars[0].open
        ref = c.bars[5].close if self.p["signal"] == "first30" else c.bar.close
        if ref - o <= self.p["min_ret_atr"] * c.atr:
            return None
        return EntryIntent(1, c.bar.close - self.p["stop_atr"] * c.atr, target_r=50.0,
                           note=f"late momentum ({self.p['signal']})")


class QuotaFill(Strategy):
    """Fills the owner's daily trade minimum — not an edge claim.

    At the checkpoint bar every symbol is a candidate, ranked by the day's move
    from the open in ATR units (strongest first); stop 0.25 ATR below; no target,
    exit at the stop or the 15:45 flatten. Long only.

    History: v1 (2026-10-13) admitted only symbols above VWAP with EMA9 > EMA20;
    on weak days that left too few candidates to reach the minimum (53/90 days),
    so v2 admits every symbol. Changed for coverage of the owner's requirement,
    not for returns; both versions' results are in the ledger.
    """

    family = "quota_fill"
    DEFAULTS = {"checkpoint_min": 240, "stop_atr": 0.25}

    def on_bar(self, c: BarContext) -> EntryIntent | None:
        if c.minutes_since_open != self.p["checkpoint_min"]:
            return None
        b = c.bar
        return EntryIntent(1, b.close - self.p["stop_atr"] * c.atr, target_r=50.0,
                           note="quota fill (relative strength)",
                           score=(b.close - c.bars[0].open) / c.atr)


BOTH_SIDES = (OpeningRangeBreakout, VwapPullback, MomentumContinuation, VwapMeanReversion)

FAMILIES: dict[str, type[Strategy]] = {
    cls.family: cls for cls in
    (*BOTH_SIDES, *(long_only(c) for c in BOTH_SIDES), OpeningRangeToClose, LateDayMomentum,
     QuotaFill)
}

# Families the account can actually trade (no short selling).
LONG_ONLY_FAMILIES = [f for f, cls in FAMILIES.items()
                      if f.endswith("_long") or cls in (OpeningRangeToClose, LateDayMomentum)]
