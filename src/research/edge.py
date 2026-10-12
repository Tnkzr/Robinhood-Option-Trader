"""Deterministic judgement of whether a strategy has a validated edge.

A strategy reaches VALIDATED only by:
1. walk-forward out-of-sample results that clear every criterion below, with the
   confidence bound tightened for the number of strategy families ever tested
   (Šidák), then
2. a single, previously unseen holdout evaluation that does not contradict them.

Nothing here can be overridden by a confidence estimate from a person or a model.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

from src.research.engine import Trade
from src.research.metrics import bootstrap_means, breakdown, percentile, summarize
from src.research.walkforward import WFResult


class Status(str, Enum):
    VALIDATED = "validated"
    PENDING_HOLDOUT = "pending_holdout"
    NO_EDGE = "no_edge"
    INSUFFICIENT_DATA = "insufficient_data"
    FAILED_HOLDOUT = "failed_holdout"


@dataclass(frozen=True)
class EdgeCriteria:
    min_oos_trades: int = 100
    min_oos_sessions: int = 40
    min_profit_factor: float = 1.15
    family_alpha: float = 0.05        # one-sided, before multiple-testing adjustment
    max_drawdown_r: float = 20.0
    stress_cost_mult: float = 1.5     # expectancy must survive costs x this
    min_holdout_trades: int = 20


@dataclass(frozen=True)
class EdgeVerdict:
    strategy: str
    params: str | None
    status: Status
    oos: dict | None
    oos_lower_bound: float | None
    stressed_expectancy_r: float | None
    holdout: dict | None
    by_regime: dict[str, dict]
    reasons: tuple[str, ...]
    run_id: str
    data_start: str
    data_end: str
    families_tested: int
    criteria: dict = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.status is Status.VALIDATED

    def to_json(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["reasons"] = list(self.reasons)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "EdgeVerdict":
        d = dict(d)
        d["status"] = Status(d["status"])
        d["reasons"] = tuple(d.get("reasons", ()))
        return cls(**d)


def adjusted_alpha(alpha: float, families: int) -> float:
    return 1 - (1 - alpha) ** (1 / max(1, families))


def judge_walk_forward(family: str, wf: WFResult, stressed_oos: Sequence[Trade],
                       families_tested: int, crit: EdgeCriteria, run_id: str,
                       data_start: str, data_end: str) -> EdgeVerdict:
    oos = wf.oos_trades
    s = summarize(oos)
    base = dict(strategy=family, params=wf.final_key, holdout=None, run_id=run_id,
                data_start=data_start, data_end=data_end, families_tested=families_tested,
                criteria=asdict(crit))
    if s is None or s.n < crit.min_oos_trades or s.sessions < crit.min_oos_sessions \
            or wf.final_key is None:
        n = s.n if s else 0
        sessions = s.sessions if s else 0
        return EdgeVerdict(status=Status.INSUFFICIENT_DATA, oos=s.as_dict() if s else None,
                           oos_lower_bound=None, stressed_expectancy_r=None, by_regime={},
                           reasons=(f"{n} OOS trades over {sessions} sessions; need "
                                    f"{crit.min_oos_trades} over {crit.min_oos_sessions}",),
                           **base)
    a = adjusted_alpha(crit.family_alpha, families_tested)
    lb = percentile(bootstrap_means(oos), a)
    stressed = summarize(stressed_oos, bootstrap=False)
    se = stressed.expectancy_r if stressed else float("-inf")
    fails = []
    if s.expectancy_r <= 0:
        fails.append(f"OOS expectancy {s.expectancy_r:+.3f}R is not positive")
    if lb <= 0:
        fails.append(f"bootstrap lower bound {lb:+.3f}R ≤ 0 at α={a:.4f} "
                     f"({families_tested} families tested)")
    if s.profit_factor < crit.min_profit_factor:
        fails.append(f"profit factor {s.profit_factor:.2f} < {crit.min_profit_factor}")
    if s.max_drawdown_r > crit.max_drawdown_r:
        fails.append(f"max drawdown {s.max_drawdown_r:.1f}R > {crit.max_drawdown_r}R")
    if se <= 0:
        fails.append(f"expectancy {se:+.3f}R at {crit.stress_cost_mult}x costs is not positive")
    regimes = {k: v.as_dict() for k, v in breakdown(oos, lambda t: t.regime or "unknown").items()}
    return EdgeVerdict(status=Status.NO_EDGE if fails else Status.PENDING_HOLDOUT,
                       oos=s.as_dict(), oos_lower_bound=lb, stressed_expectancy_r=se,
                       by_regime=regimes,
                       reasons=tuple(fails) or ("passed walk-forward; holdout sealed",),
                       **base)


def judge_holdout(prior: EdgeVerdict, holdout: Sequence[Trade],
                  crit: EdgeCriteria) -> EdgeVerdict:
    if prior.status is not Status.PENDING_HOLDOUT:
        raise ValueError(f"holdout is only evaluated after walk-forward passes "
                         f"(status is {prior.status.value})")
    s = summarize(holdout)
    d = prior.to_json()
    d["holdout"] = s.as_dict() if s else None
    if s is None or s.n < crit.min_holdout_trades:
        d["status"] = Status.INSUFFICIENT_DATA.value
        d["reasons"] = [f"holdout has {s.n if s else 0} trades; need {crit.min_holdout_trades}"]
    elif s.expectancy_r > 0 and s.profit_factor >= 1.0:
        d["status"] = Status.VALIDATED.value
        d["reasons"] = [f"holdout confirms: {s.expectancy_r:+.3f}R over {s.n} trades"]
    else:
        d["status"] = Status.FAILED_HOLDOUT.value
        d["reasons"] = [f"holdout contradicts: {s.expectancy_r:+.3f}R, "
                        f"PF {s.profit_factor:.2f} over {s.n} trades"]
    return EdgeVerdict.from_json(d)


class VerdictStore:
    def __init__(self, path: str | Path = "research/verdicts.json") -> None:
        self.path = Path(path)

    def load(self) -> dict[str, EdgeVerdict]:
        if not self.path.exists():
            return {}
        return {k: EdgeVerdict.from_json(v) for k, v in json.loads(self.path.read_text()).items()}

    def save(self, v: EdgeVerdict) -> None:
        cur = {k: x.to_json() for k, x in self.load().items()}
        cur[v.strategy] = v.to_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n")
