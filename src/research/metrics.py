"""Trade statistics in R (initial-risk) units, net of modelled costs.

Confidence intervals use a day-block bootstrap: trades on the same session are
correlated (one market, one regime), so resampling individual trades would
overstate certainty.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable, Sequence

from src.research.engine import Trade


@dataclass(frozen=True)
class Summary:
    n: int
    sessions: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    profit_factor: float
    total_r: float
    max_drawdown_r: float
    std_r: float
    ci_low: float | None = None      # two-sided 95% day-block bootstrap of expectancy
    ci_high: float | None = None

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


PF_CAP = 99.0


def _core(rs: Sequence[float]) -> tuple:
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_loss = -sum(losses)
    pf = PF_CAP if gross_loss == 0 else min(PF_CAP, sum(wins) / gross_loss)
    mean = sum(rs) / n
    std = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    eq = peak = mdd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return (len(wins) / n, sum(wins) / len(wins) if wins else 0.0,
            sum(losses) / len(losses) if losses else 0.0, mean, pf, sum(rs), mdd, std)


def bootstrap_means(trades: Sequence[Trade], n_boot: int = 2000, seed: int = 7) -> list[float]:
    by_day: dict[date, list[float]] = {}
    for t in trades:
        by_day.setdefault(t.session, []).append(t.r)
    days = list(by_day.values())
    if not days:
        return []
    rng = random.Random(seed)
    out = []
    for _ in range(n_boot):
        tot = cnt = 0
        for _ in range(len(days)):
            d = days[rng.randrange(len(days))]
            tot += sum(d)
            cnt += len(d)
        out.append(tot / cnt)
    out.sort()
    return out


def percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    i = min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))
    return sorted_vals[i]


def summarize(trades: Sequence[Trade], bootstrap: bool = True) -> Summary | None:
    if not trades:
        return None
    ts = sorted(trades, key=lambda t: t.entry_ts)
    rs = [t.r for t in ts]
    wr, aw, al, mean, pf, tot, mdd, std = _core(rs)
    lo = hi = None
    if bootstrap and len(ts) >= 10:
        bs = bootstrap_means(ts)
        lo, hi = percentile(bs, 0.025), percentile(bs, 0.975)
    return Summary(len(ts), len({t.session for t in ts}), wr, aw, al, mean, pf, tot, mdd,
                   std, lo, hi)


def breakdown(trades: Sequence[Trade], key: Callable[[Trade], str]) -> dict[str, Summary]:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: summarize(v, bootstrap=False) for k, v in sorted(groups.items())}


def time_bucket(t: Trade) -> str:
    m = t.entry_minute
    return ("09:30-10:30" if m < 60 else "10:30-11:30" if m < 120
            else "11:30-14:00" if m < 270 else "14:00-close")
