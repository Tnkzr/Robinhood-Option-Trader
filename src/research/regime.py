"""Market regime from the index's daily bars before the session — never the session itself."""
from __future__ import annotations

from bisect import bisect_left
from datetime import date
from typing import Sequence

from src.research.bars import Bar
from src.research.indicators import sma, wilder_atr


class RegimeIndex:
    def __init__(self, index_daily: Sequence[Bar], vol_window: int = 250) -> None:
        self.dates = [b.ts.date() for b in index_daily]
        closes = [b.close for b in index_daily]
        self.closes = closes
        self.sma20 = sma(closes, 20)
        self.sma50 = sma(closes, 50)
        atr = wilder_atr(index_daily, 14)
        self.atr_pct = [a / c if a is not None else None for a, c in zip(atr, closes)]
        self.vol_window = vol_window

    def label(self, d: date) -> str | None:
        k = bisect_left(self.dates, d) - 1
        if k < 55 or self.sma50[k] is None or self.sma20[k - 5] is None:
            return None
        c, s20, s20_prev, s50 = self.closes[k], self.sma20[k], self.sma20[k - 5], self.sma50[k]
        if c > s50 and s20 > s20_prev:
            trend = "up"
        elif c < s50 and s20 < s20_prev:
            trend = "down"
        else:
            trend = "range"
        hist = [v for v in self.atr_pct[max(0, k - self.vol_window):k + 1] if v is not None]
        cur = self.atr_pct[k]
        rank = sum(v <= cur for v in hist) / len(hist) if hist and cur is not None else 0.5
        vol = "high" if rank >= 0.7 else "low" if rank <= 0.3 else "normal"
        return f"{trend}/{vol}"
