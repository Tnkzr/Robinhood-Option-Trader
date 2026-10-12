"""Metrics, walk-forward, edge verdicts, ledger, degradation and proposals."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from _synth import mk_trade, weekdays
from config.settings import RiskLimits
from src.execution.proposal import ProposalRejected, TradeProposal, build_proposal
from src.research.degradation import check
from src.research.edge import (EdgeCriteria, EdgeVerdict, Status, VerdictStore,
                               adjusted_alpha, judge_holdout, judge_walk_forward)
from src.research.ledger import HoldoutReuse, Ledger
from src.research.metrics import summarize
from src.research.walkforward import Fold, WFConfig, WFResult, split, walk_forward
from src.risk.limits import RiskState

DAYS = weekdays(date(2026, 3, 2), 160)


def trades_with(rs: list[float], days: list[date], **kw):
    return [mk_trade(r, days[i % len(days)], minute=5 * (i // len(days)), **kw)
            for i, r in enumerate(rs)]


# --------------------------------- metrics ----------------------------------

def test_summary_arithmetic():
    s = summarize(trades_with([2, -1, 2, -1, -1], DAYS[:5]))
    assert s.n == 5 and s.win_rate == pytest.approx(0.4)
    assert s.expectancy_r == pytest.approx(0.2)
    assert s.profit_factor == pytest.approx(4 / 3)
    assert s.max_drawdown_r == pytest.approx(2.0)
    assert s.avg_win_r == 2 and s.avg_loss_r == -1


def test_bootstrap_ci_brackets_the_mean_and_is_reproducible():
    ts = trades_with([2, -1] * 60, DAYS[:60])
    a, b = summarize(ts), summarize(ts)
    assert a.ci_low < a.expectancy_r < a.ci_high
    assert (a.ci_low, a.ci_high) == (b.ci_low, b.ci_high)


# ------------------------------- walk-forward -------------------------------

def test_split_seals_the_holdout():
    dev, hold, folds = split(DAYS[:150], WFConfig())
    assert len(hold) == 30 and hold == DAYS[120:150] and dev == DAYS[:120]
    assert all(set(f.test).isdisjoint(hold) and set(f.train).isdisjoint(hold) for f in folds)
    assert all(max(f.train) < min(f.test) for f in folds)
    assert [len(f.test) for f in folds] == [20, 20, 20, 20]


def test_walk_forward_refuses_holdout_trades():
    dev, hold, folds = split(DAYS[:150], WFConfig())
    with pytest.raises(ValueError, match="holdout"):
        walk_forward({"k": trades_with([1.0], [hold[0]])}, dev, folds, WFConfig())


def test_selection_uses_train_only_and_reports_test_only():
    dev, _, folds = split(DAYS[:150], WFConfig())
    early, late = set(dev[:40]), set(dev[40:])
    good_early = trades_with([1.0] * 80, [d for d in dev if d in early]) + \
        trades_with([-1.0] * 80, [d for d in dev if d in late])
    steady = trades_with([0.2] * 120, dev)
    for t in good_early:
        object.__setattr__(t, "params", "good_early")
    res = walk_forward({"good_early": good_early, "steady": steady}, dev, folds, WFConfig())
    first_fold = res.folds[0]
    assert first_fold[1] == "good_early"           # chosen on train data alone
    assert all(t.session in set(first_fold[0].test) for t in res.oos_trades[:first_fold[2]])
    assert summarize(res.oos_trades[:first_fold[2]]).expectancy_r < 0   # honest OOS loss


# ---------------------------------- edge ------------------------------------

def wf_of(trades) -> WFResult:
    f = Fold(tuple(DAYS[:40]), tuple(sorted({t.session for t in trades})))
    return WFResult(folds=[(f, "k", len(trades))], oos_trades=trades, final_key="k")


def judge(trades, families=1, stressed=None):
    return judge_walk_forward("fam", wf_of(trades), stressed if stressed is not None else trades,
                              families, EdgeCriteria(), "run", "2026-03-02", "2026-08-31")


def test_clear_edge_passes_walk_forward_but_waits_for_holdout():
    v = judge(trades_with([2.0, -1.0] * 100, DAYS[:80]))
    assert v.status is Status.PENDING_HOLDOUT and not v.tradeable
    assert v.oos_lower_bound > 0


def test_coin_flip_has_no_edge():
    v = judge(trades_with([1.0, -1.0] * 100, DAYS[:80]))
    assert v.status is Status.NO_EDGE


def test_small_samples_are_insufficient_not_validated():
    v = judge(trades_with([2.0, -1.0] * 20, DAYS[:40]))
    assert v.status is Status.INSUFFICIENT_DATA


def test_edge_must_survive_stressed_costs():
    ts = trades_with([2.0, -1.0] * 100, DAYS[:80])
    v = judge(ts, stressed=trades_with([0.9, -1.0] * 100, DAYS[:80]))
    assert v.status is Status.NO_EDGE and any("costs" in r for r in v.reasons)


def test_more_families_tested_means_a_stricter_bound():
    assert adjusted_alpha(0.05, 10) < adjusted_alpha(0.05, 1) == pytest.approx(0.05)
    ts = trades_with([1.12, -1.0] * 100, DAYS[:80])
    assert judge(ts, families=1).oos_lower_bound > judge(ts, families=50).oos_lower_bound


def test_holdout_confirms_or_contradicts():
    v = judge(trades_with([2.0, -1.0] * 100, DAYS[:80]))
    ok = judge_holdout(v, trades_with([2.0, -1.0] * 15, DAYS[120:150]), EdgeCriteria())
    bad = judge_holdout(v, trades_with([-1.0] * 30, DAYS[120:150]), EdgeCriteria())
    assert ok.status is Status.VALIDATED and ok.tradeable
    assert bad.status is Status.FAILED_HOLDOUT
    with pytest.raises(ValueError):
        judge_holdout(judge(trades_with([1.0, -1.0] * 100, DAYS[:80])), [], EdgeCriteria())


def test_verdicts_roundtrip(tmp_path):
    vs = VerdictStore(tmp_path / "v.json")
    v = judge(trades_with([2.0, -1.0] * 100, DAYS[:80]))
    vs.save(v)
    assert vs.load()["fam"] == v


# --------------------------------- ledger -----------------------------------

def test_holdout_can_only_be_used_once(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append({"kind": "walk_forward", "family": "orb"})
    lg.append({"kind": "holdout", "family": "orb", "holdout_start": "2026-09-01",
               "holdout_end": "2026-10-09", "run_id": "r1"})
    with pytest.raises(HoldoutReuse):
        lg.assert_holdout_unused("orb", date(2026, 10, 1), date(2026, 11, 13))
    lg.assert_holdout_unused("orb", date(2026, 10, 12), date(2026, 11, 20))
    lg.assert_holdout_unused("momentum", date(2026, 9, 1), date(2026, 10, 9))
    assert lg.families_tested(including=["momentum"]) == 2


# ------------------------------- degradation --------------------------------

def test_degradation_flags_sustained_shortfall_only():
    assert check(0.15, 1.2, [0.15] * 5).status == "insufficient"
    assert check(0.15, 1.2, [1.0, -0.7] * 20).status == "ok"
    bad = check(0.15, 1.2, [-1.0] * 25)
    assert bad.status == "degraded" and bad.halt


# -------------------------------- proposals ---------------------------------

def validated(**over) -> EdgeVerdict:
    d = dict(strategy="orb", params="orb(x)", status=Status.VALIDATED,
             oos={"n": 150, "win_rate": 0.42, "expectancy_r": 0.12, "profit_factor": 1.3},
             oos_lower_bound=0.02, stressed_expectancy_r=0.05,
             holdout={"n": 30, "expectancy_r": 0.10, "win_rate": 0.4, "profit_factor": 1.2},
             by_regime={"up/normal": {"n": 50, "expectancy_r": 0.2},
                        "down/high": {"n": 25, "expectancy_r": -0.1}},
             reasons=("ok",), run_id="r", data_start="2026-03-02", data_end="2026-09-01",
             families_tested=4)
    d.update(over)
    return EdgeVerdict(**d)


AT = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)


def propose(verdict=None, **over):
    kw = dict(ticker="XYZ", strategy="orb", params="orb(x)", direction=1,
              entry_trigger="5m close above opening-range high", limit_price=100.0,
              stop_price=99.0, target_price=102.0, regime="up/normal",
              reasons_to_reject=["index below VWAP would void it"],
              verdicts={"orb": verdict or validated()},
              state=RiskState(session=AT.date(), start_equity=25_000),
              limits=RiskLimits(), at=AT, kill_switch=False)
    kw.update(over)
    return build_proposal(**kw)


def test_valid_proposal_carries_researched_statistics():
    p = propose()
    assert p.shares == 125 and p.risk_dollars == 125.0
    assert p.sample_size == 150 and p.oos_win_rate == 0.42
    assert p.expectancy_dollars == pytest.approx(15.0) and p.risk_reward == 2.0
    out = p.render()
    for field in ("Historical sample", "OOS win rate", "Expectancy after costs",
                  "Reasons to reject", "Regime", "Holdout"):
        assert field in out


@pytest.mark.parametrize("over,fragment", [
    ({"verdict": validated(status=Status.PENDING_HOLDOUT)}, "pending_holdout"),
    ({"verdict": validated(status=Status.NO_EDGE)}, "no_edge"),
    ({"regime": "down/high"}, "lost money in regime"),
    ({"at": datetime(2026, 12, 1, tzinfo=timezone.utc)}, "re-run research"),
    ({"target_price": 100.5}, "reward:risk"),
    ({"direction": -1, "stop_price": 101.0, "target_price": 98.0}, "short selling"),
    ({"kill_switch": True}, "kill switch"),
    ({"strategy": "momentum"}, "no research verdict"),
])
def test_proposals_without_a_validated_edge_are_refused(over, fragment):
    with pytest.raises(ProposalRejected) as e:
        propose(**over)
    assert any(fragment in r for r in e.value.reasons), e.value.reasons


def test_proposal_object_cannot_be_built_around_the_gate():
    with pytest.raises(ProposalRejected):
        TradeProposal("XYZ", "orb", "orb(x)", 1, "x", 100.0, 99.0, 102.0, 10, 10.0, None,
                      ("r",), validated(status=Status.NO_EDGE))
    with pytest.raises(ProposalRejected):
        TradeProposal("XYZ", "orb", "orb(other)", 1, "x", 100.0, 99.0, 102.0, 10, 10.0, None,
                      ("r",), validated())
    with pytest.raises(ProposalRejected):
        TradeProposal("XYZ", "orb", "orb(x)", 1, "x", 100.0, 99.0, 102.0, 10, 10.0, None,
                      (), validated())
