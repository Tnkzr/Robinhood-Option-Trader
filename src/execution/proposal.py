"""Trade proposals: the only object that can lead to an order.

Every statistic on a proposal (sample size, out-of-sample win rate, expectancy)
is read from a stored EdgeVerdict produced by the research pipeline. There is no
field for a hand-entered probability or confidence, so one cannot be invented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from config.settings import RiskLimits
from src.research.edge import EdgeVerdict, Status
from src.risk.limits import RiskState, check_new_entry, position_size


class ProposalRejected(ValueError):
    def __init__(self, reasons: list[str] | tuple[str, ...]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class TradeProposal:
    ticker: str
    strategy: str
    params: str
    direction: int                  # +1 long, -1 short
    entry_trigger: str
    limit_price: float
    stop_price: float
    target_price: float
    shares: int
    risk_dollars: float
    regime: str | None
    reasons_to_reject: tuple[str, ...]
    verdict: EdgeVerdict

    def __post_init__(self) -> None:
        f = []
        if self.verdict.status is not Status.VALIDATED:
            f.append(f"{self.strategy} has no validated edge ({self.verdict.status.value})")
        if self.verdict.strategy != self.strategy or self.verdict.params != self.params:
            f.append("signal parameters differ from the validated parameter set")
        d = self.direction
        if d not in (1, -1):
            f.append("direction must be +1 or -1")
        elif not ((self.stop_price - self.limit_price) * d < 0 < (self.target_price - self.limit_price) * d):
            f.append("stop and target must sit on opposite sides of the entry")
        if not self.entry_trigger.strip():
            f.append("entry trigger is required")
        if not self.reasons_to_reject:
            f.append("reasons to reject the setup must be stated")
        if self.shares < 1:
            f.append("size below one share")
        if f:
            raise ProposalRejected(f)

    @property
    def side(self) -> str:
        return "long" if self.direction > 0 else "short"

    @property
    def risk_reward(self) -> float:
        return abs(self.target_price - self.limit_price) / abs(self.limit_price - self.stop_price)

    @property
    def sample_size(self) -> int:
        return int(self.verdict.oos["n"])

    @property
    def oos_win_rate(self) -> float:
        return float(self.verdict.oos["win_rate"])

    @property
    def expectancy_r(self) -> float:
        return float(self.verdict.oos["expectancy_r"])

    @property
    def expectancy_dollars(self) -> float:
        return round(self.expectancy_r * self.risk_dollars, 2)

    def render(self) -> str:
        v = self.verdict
        rows = [
            ("Ticker", self.ticker), ("Strategy", f"{self.strategy} — {self.params}"),
            ("Direction", self.side), ("Entry trigger", self.entry_trigger),
            ("Limit", f"{self.limit_price:.2f}"), ("Stop", f"{self.stop_price:.2f}"),
            ("Target", f"{self.target_price:.2f}"), ("Reward:risk", f"{self.risk_reward:.2f}"),
            ("Size", f"{self.shares} sh, ${self.risk_dollars:.2f} at risk"),
            ("Historical sample", f"{self.sample_size} OOS trades "
                                  f"({v.data_start} → {v.data_end})"),
            ("OOS win rate", f"{self.oos_win_rate:.1%}"),
            ("Expectancy after costs", f"{self.expectancy_r:+.3f}R ≈ ${self.expectancy_dollars:+.2f}"),
            ("Holdout", f"{v.holdout['expectancy_r']:+.3f}R over {v.holdout['n']} trades"
                        if v.holdout else "n/a"),
            ("Regime", self.regime or "unknown"),
            ("Reasons to reject", "; ".join(self.reasons_to_reject)),
        ]
        return "\n".join(f"| {k} | {val} |" for k, val in [("Field", "Value"), ("---", "---"), *rows])


def build_proposal(*, ticker: str, strategy: str, params: str, direction: int,
                   entry_trigger: str, limit_price: float, stop_price: float,
                   target_price: float, regime: str | None, reasons_to_reject: list[str],
                   verdicts: dict[str, EdgeVerdict], state: RiskState, limits: RiskLimits,
                   at: datetime, kill_switch: bool, min_rr: float = 1.0,
                   min_regime_trades: int = 20, max_verdict_age_days: int = 45) -> TradeProposal:
    v = verdicts.get(strategy)
    if v is None:
        raise ProposalRejected([f"no research verdict for {strategy}"])
    fails = []
    if v.status is not Status.VALIDATED:
        fails.append(f"{strategy} is {v.status.value}: {'; '.join(v.reasons)}")
    age = (at.date() - date.fromisoformat(v.data_end)).days
    if age > max_verdict_age_days:
        fails.append(f"verdict data ends {v.data_end} ({age}d old) — re-run research")
    rg = v.by_regime.get(regime or "unknown")
    if rg and rg["n"] >= min_regime_trades and rg["expectancy_r"] <= 0:
        fails.append(f"{strategy} lost money in regime {regime} "
                     f"({rg['expectancy_r']:+.3f}R over {rg['n']} OOS trades)")
    risk = abs(limit_price - stop_price)
    if risk <= 0 or abs(target_price - limit_price) / risk < min_rr:
        fails.append(f"reward:risk below {min_rr}")
    size = position_size(state.equity, limit_price, stop_price, limits, state.gross_exposure)
    if size.shares == 0:
        fails.append(f"sizing: {size.reason}")
    else:
        chk = check_new_entry(state, symbol=ticker, direction=direction, entry=limit_price,
                              stop=stop_price, risk_dollars=size.risk_dollars,
                              notional=size.notional, limits=limits, at=at,
                              kill_switch=kill_switch)
        fails.extend(chk.failures)
    if fails:
        raise ProposalRejected(fails)
    return TradeProposal(ticker, strategy, params, direction, entry_trigger, limit_price,
                         stop_price, target_price, size.shares, size.risk_dollars, regime,
                         tuple(reasons_to_reject), v)
