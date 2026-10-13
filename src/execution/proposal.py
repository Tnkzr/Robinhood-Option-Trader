"""Trade proposals: the only object that can lead to an order.

Every statistic on a proposal (sample size, out-of-sample win rate, expectancy)
is read from a stored EdgeVerdict produced by the research pipeline. There is no
field for a hand-entered probability or confidence, so one cannot be invented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from config.settings import QuotaPolicy, RiskLimits
from src.execution.quota import FILL_FAMILY, tier_of
from src.research.edge import EdgeVerdict, Status
from src.risk.limits import MIN_NOTIONAL, RiskState, check_new_entry, position_size


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
    shares: float
    risk_dollars: float
    regime: str | None
    reasons_to_reject: tuple[str, ...]
    verdict: EdgeVerdict
    tier: str = "validated"

    def __post_init__(self) -> None:
        f = []
        expected = (FILL_FAMILY if self.tier == "quota_fill"
                    else tier_of(self.verdict, QuotaPolicy()))
        if self.tier not in ("validated", "provisional", "quota_fill"):
            f.append(f"unknown tier {self.tier}")
        elif self.tier == "quota_fill":
            if self.verdict.strategy != FILL_FAMILY:
                f.append("quota fills must carry the quota_fill verdict")
        elif expected != self.tier:
            f.append(f"{self.strategy} does not qualify as {self.tier} "
                     f"({self.verdict.status.value})")
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
        if self.shares <= 0 or self.shares * self.limit_price < MIN_NOTIONAL:
            f.append(f"order below the ${MIN_NOTIONAL:.2f} minimum")
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
            ("Tier", {"validated": "validated edge",
                      "provisional": "provisional — positive out-of-sample, NOT validated",
                      "quota_fill": "quota fill — daily minimum, measured record below, "
                                    "NOT an edge"}[self.tier]),
            ("Ticker", self.ticker), ("Strategy", f"{self.strategy} — {self.params}"),
            ("Direction", self.side), ("Entry trigger", self.entry_trigger),
            ("Limit", f"{self.limit_price:.2f}"), ("Stop", f"{self.stop_price:.2f}"),
            *((("Target", "none — exit at the stop or the 15:45 ET flatten"),
               ("Reward:risk", "open-ended (no target)")) if self.risk_reward >= 25 else
              (("Target", f"{self.target_price:.2f}"),
               ("Reward:risk", f"{self.risk_reward:.2f}"))),
            ("Size", f"{self.shares:g} sh (${self.shares * self.limit_price:.2f}), "
                     f"${self.risk_dollars:.2f} at risk"),
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
                   min_regime_trades: int = 20, max_verdict_age_days: int = 45,
                   tier: str = "validated", policy: QuotaPolicy | None = None) -> TradeProposal:
    v = verdicts.get(strategy)
    if v is None:
        raise ProposalRejected([f"no research verdict for {strategy}"])
    fails = []
    policy = policy or QuotaPolicy()
    if tier == "quota_fill":
        if strategy != FILL_FAMILY:
            fails.append("only the quota_fill rule may fill the daily minimum")
    elif tier_of(v, policy) != tier:
        fails.append(f"{strategy} is {v.status.value}, not {tier}: {'; '.join(v.reasons)}")
    age = (at.date() - date.fromisoformat(v.data_end)).days
    if age > max_verdict_age_days:
        fails.append(f"verdict data ends {v.data_end} ({age}d old) — re-run research")
    rg = v.by_regime.get(regime or "unknown")
    if tier != "quota_fill" and rg and rg["n"] >= min_regime_trades and rg["expectancy_r"] <= 0:
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
                         tuple(reasons_to_reject), v, tier)
