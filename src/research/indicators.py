"""Causal indicators. Every output at index i depends only on inputs at <= i.

tests/test_research_engine.py checks this property directly by perturbing the
future and asserting the past does not change.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from src.research.bars import Bar, Session


def ema(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder RSI."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(values)):
        ch = values[i] - values[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def wilder_atr(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        pc = bars[i - 1].close if i else b.close
        trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    if len(trs) < period:
        return out
    a = sum(trs[:period]) / period
    out[period - 1] = a
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def session_vwap(bars: Sequence[Bar]) -> list[float]:
    out, pv, vol = [], 0.0, 0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume
        vol += b.volume
        out.append(pv / vol if vol else tp)
    return out


def relative_volume(sessions: Sequence[Session], lookback: int = 20,
                    min_history: int = 10) -> list[list[float | None]]:
    """Cumulative volume to bar i vs the average cumulative volume to bar i over
    the previous `lookback` sessions. Uses prior sessions only."""
    cums: list[list[int]] = []
    out: list[list[float | None]] = []
    for s in sessions:
        c, run = [], 0
        for b in s.bars:
            run += b.volume
            c.append(run)
        prior = cums[-lookback:]
        row: list[float | None] = []
        for i, v in enumerate(c):
            hist = [p[i] for p in prior if len(p) > i]
            row.append(v / (sum(hist) / len(hist)) if len(hist) >= min_history and sum(hist) else None)
        out.append(row)
        cums.append(c)
    return out


@dataclass(frozen=True)
class DailyContext:
    atr: float
    prev_close: float
    prev_high: float
    prev_low: float


class DailyContextIndex:
    """Context for session d built from daily bars strictly before d."""

    def __init__(self, daily: Sequence[Bar], atr_period: int = 14) -> None:
        self.dates = [b.ts.date() for b in daily]
        self.bars = list(daily)
        self.atr = wilder_atr(self.bars, atr_period)

    def get(self, d: date) -> DailyContext | None:
        k = bisect_left(self.dates, d) - 1
        if k < 0 or self.atr[k] is None:
            return None
        b = self.bars[k]
        return DailyContext(self.atr[k], b.close, b.high, b.low)
