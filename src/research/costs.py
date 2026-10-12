"""Transaction cost model. Every simulated fill passes through here.

The defaults are assumptions, not measurements — Robinhood exposes no historical
quote data. They are deliberately conservative relative to typical NBBO spreads
for the liquid universe, and every report is re-run at a stressed multiple.
Replace them with measured spreads from paper-trading fills once available.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

TICK = 0.01
INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA"})


@dataclass(frozen=True)
class CostModel:
    half_spread_bps: float = 1.0          # single stocks
    index_half_spread_bps: float = 0.3    # SPY/QQQ/IWM/DIA
    slippage_bps: float = 1.0             # per side, on top of the half spread
    stop_extra_bps: float = 2.0           # stops fill into momentum
    commission_per_share: float = 0.0
    sec_fee_rate: float = 0.0000278       # on sell notional
    taf_per_share: float = 0.000166
    taf_cap: float = 8.30
    overrides_bps: dict[str, float] = field(default_factory=dict)

    def stressed(self, mult: float) -> "CostModel":
        return replace(self, half_spread_bps=self.half_spread_bps * mult,
                       index_half_spread_bps=self.index_half_spread_bps * mult,
                       slippage_bps=self.slippage_bps * mult,
                       stop_extra_bps=self.stop_extra_bps * mult,
                       overrides_bps={k: v * mult for k, v in self.overrides_bps.items()})

    def _bps(self, symbol: str, stop: bool) -> float:
        hs = self.overrides_bps.get(symbol,
                                    self.index_half_spread_bps if symbol in INDEX_ETFS
                                    else self.half_spread_bps)
        return hs + self.slippage_bps + (self.stop_extra_bps if stop else 0.0)

    def market_fill(self, symbol: str, price: float, side: int, stop: bool = False) -> float:
        """side=+1 buying, -1 selling. Always adverse to the trader."""
        return price * (1 + side * self._bps(symbol, stop) / 1e4)

    @staticmethod
    def limit_fills(limit: float, bar_low: float, bar_high: float, side: int) -> bool:
        """A resting limit fills only if price trades THROUGH it by a tick.

        Touching the price is not enough — the queue ahead may absorb it.
        """
        return bar_low <= limit - TICK if side > 0 else bar_high >= limit + TICK

    def fees(self, shares: int, price: float, side: int) -> float:
        f = self.commission_per_share * shares
        if side < 0:
            f += shares * price * self.sec_fee_rate
            f += min(shares * self.taf_per_share, self.taf_cap)
        return f
