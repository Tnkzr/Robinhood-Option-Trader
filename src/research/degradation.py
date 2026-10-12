"""Detect when live/paper results drift below the validated baseline.

This only flags. It never re-tunes a strategy — a few recent losses are not
evidence to rewrite rules, and automatic re-fitting is how overfitting enters
production. A DEGRADED flag halts new entries for that strategy until a human
reviews it and a fresh walk-forward is run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class DegradationReport:
    status: str                 # insufficient | ok | warning | degraded
    n: int
    live_expectancy_r: float | None
    cusum: float
    reasons: tuple[str, ...]

    @property
    def halt(self) -> bool:
        return self.status == "degraded"


def check(baseline_mean_r: float, baseline_std_r: float, live_r: Sequence[float], *,
          min_trades: int = 10, window: int = 30, k: float = 0.5,
          h: float = 4.0) -> DegradationReport:
    n = len(live_r)
    if n < min_trades:
        return DegradationReport("insufficient", n, None, 0.0,
                                 (f"{n} live trades; need {min_trades} to judge",))
    sd = max(baseline_std_r, 1e-6)
    s = 0.0
    for r in live_r:   # one-sided lower CUSUM in standard-deviation units
        s = max(0.0, s + (baseline_mean_r - r) / sd - k)
    mean = sum(live_r) / n
    reasons = []
    status = "ok"
    if s > h:
        status = "degraded"
        reasons.append(f"CUSUM {s:.2f} > {h} — sustained shortfall vs baseline "
                       f"{baseline_mean_r:+.3f}R")
    recent = list(live_r)[-window:]
    rm = sum(recent) / len(recent)
    upper = rm + 1.96 * sd / math.sqrt(len(recent))
    if len(recent) >= min_trades and upper < 0:
        status = "degraded"
        reasons.append(f"last {len(recent)} trades: {rm:+.3f}R, 95% upper bound "
                       f"{upper:+.3f}R < 0")
    if status == "ok" and mean < baseline_mean_r - 2 * sd / math.sqrt(n):
        status = "warning"
        reasons.append(f"live {mean:+.3f}R is >2 SE below baseline {baseline_mean_r:+.3f}R")
    return DegradationReport(status, n, mean, s, tuple(reasons) or ("within expectations",))
