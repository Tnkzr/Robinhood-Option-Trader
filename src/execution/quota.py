"""The owner's daily trade minimum (QuotaPolicy), applied identically in the
backtest and live.

Tiers, in priority order:
1. validated   — passed walk-forward and holdout (CLAUDE.md §3).
2. provisional — failed validation, but positive out-of-sample expectancy after
                 costs over >= provisional_min_oos_trades trades. Best available,
                 not proven.
3. quota_fill  — at the checkpoint, tops the day up to the minimum. Its measured
                 record is carried on every fill; it is not an edge claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from config.settings import QuotaPolicy
from src.research.edge import EdgeVerdict, Status

TIERS = ("validated", "provisional", "quota_fill")
FILL_FAMILY = "quota_fill"


def tier_of(v: EdgeVerdict, policy: QuotaPolicy) -> str | None:
    if v.strategy == FILL_FAMILY:
        return None
    if v.status is Status.VALIDATED:
        return "validated"
    o = v.oos or {}
    if o.get("n", 0) >= policy.provisional_min_oos_trades and o.get("expectancy_r", 0) > 0:
        return "provisional"
    return None


def signal_families(verdicts: dict[str, EdgeVerdict], policy: QuotaPolicy) -> list[tuple[str, str]]:
    """(family, tier) for every family allowed to generate entries, best first."""
    out = [(f, tier_of(v, policy)) for f, v in verdicts.items()]
    out = [(f, t) for f, t in out if t]
    rank = {t: i for i, t in enumerate(TIERS)}
    return sorted(out, key=lambda ft: (rank[ft[1]], -verdicts[ft[0]].oos["expectancy_r"], ft[0]))


@dataclass(frozen=True)
class FillCandidate:
    symbol: str
    score: float


def select_fills(candidates: Iterable[FillCandidate], shortfall: int,
                 exclude: set[str]) -> list[FillCandidate]:
    """Strongest first, one per symbol, skipping symbols already held or traded today."""
    if shortfall <= 0:
        return []
    picked, seen = [], set(exclude)
    for c in sorted(candidates, key=lambda c: (-c.score, c.symbol)):
        if c.symbol in seen:
            continue
        picked.append(c)
        seen.add(c.symbol)
        if len(picked) == shortfall:
            break
    return picked
