"""Anchored walk-forward on a development period, with a sealed holdout.

The final `holdout_sessions` are excluded from everything here. They are
evaluated once, later, through src/research/ledger.py — and only for a strategy
that has already passed on walk-forward out-of-sample data.

Because strategies are causal and do not learn, each parameter set's trades can
be simulated once over the development period and then partitioned by fold;
the only thing "trained" on each fold is the choice of parameter set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.research.engine import Trade


@dataclass(frozen=True)
class WFConfig:
    holdout_sessions: int = 30
    min_train_sessions: int = 40
    test_sessions: int = 20
    min_train_trades: int = 30


@dataclass(frozen=True)
class Fold:
    train: tuple[date, ...]
    test: tuple[date, ...]


def split(calendar: list[date], cfg: WFConfig) -> tuple[list[date], list[date], list[Fold]]:
    cal = sorted(set(calendar))
    if len(cal) <= cfg.holdout_sessions + cfg.min_train_sessions:
        return cal, [], []
    dev, holdout = cal[:-cfg.holdout_sessions], cal[-cfg.holdout_sessions:]
    folds, start = [], cfg.min_train_sessions
    while start < len(dev):
        folds.append(Fold(tuple(dev[:start]), tuple(dev[start:start + cfg.test_sessions])))
        start += cfg.test_sessions
    return dev, holdout, folds


def select(trades_by_key: dict[str, list[Trade]], dates: set[date],
           min_trades: int) -> str | None:
    """Highest expectancy among parameter sets with enough trades in `dates`."""
    best, best_e = None, float("-inf")
    for key in sorted(trades_by_key):
        rs = [t.r for t in trades_by_key[key] if t.session in dates]
        if len(rs) < min_trades:
            continue
        e = sum(rs) / len(rs)
        if e > best_e:
            best, best_e = key, e
    return best


@dataclass
class WFResult:
    folds: list[tuple[Fold, str | None, int]] = field(default_factory=list)
    oos_trades: list[Trade] = field(default_factory=list)
    final_key: str | None = None   # chosen on the whole development period

    @property
    def oos_sessions(self) -> int:
        return sum(len(f.test) for f, _, _ in self.folds)


def walk_forward(trades_by_key: dict[str, list[Trade]], dev: list[date],
                 folds: list[Fold], cfg: WFConfig) -> WFResult:
    dev_set = set(dev)
    for key, ts in trades_by_key.items():
        if any(t.session not in dev_set for t in ts):
            raise ValueError(f"{key}: trades outside the development period — "
                             "the holdout must not be simulated during walk-forward")
    res = WFResult()
    for f in folds:
        key = select(trades_by_key, set(f.train), cfg.min_train_trades)
        test = set(f.test)
        oos = [t for t in trades_by_key.get(key, []) if t.session in test] if key else []
        res.folds.append((f, key, len(oos)))
        res.oos_trades.extend(oos)
    res.final_key = select(trades_by_key, dev_set, cfg.min_train_trades)
    return res
