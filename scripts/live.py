"""Live trading cycle helper. Prints what to do; never places orders itself.

    python -m scripts.live scan --equity 1000 [--now 2026-10-14T14:05:00Z]
    python -m scripts.live quote-ok SYMBOL BID ASK
    python -m scripts.live record-entry SIGNAL_JSON --fill F --shares N --order-id ID --entry-ts TS
    python -m scripts.live record-exit ENTRY_ID --fill F --reason R --order-id ID --exit-ts TS
    python -m scripts.live status --equity 1000
    python -m scripts.live calendar [--now TS]     # trading day? phase? when is the next cycle?

Orders are placed by Claude through the broker tools, following CLAUDE.md §6.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from config.settings import RiskLimits, Settings
from config.universe import UNIVERSE
from src.execution.journal import LiveJournal
from src.execution.live import EntrySignal, scan, spread_ok
from datetime import timedelta

from src.research.bars import (ET, BarStore, is_trading_day, next_cycle_at,
                               scheduled_close)
from src.research.costs import CostModel
from src.research.edge import VerdictStore
from src.risk.limits import kill_switch_engaged


def _ts(s: str | None) -> datetime:
    return (datetime.fromisoformat(s.replace("Z", "+00:00")) if s
            else datetime.now(timezone.utc)).astimezone(timezone.utc)


def cmd_scan(a) -> int:
    settings = Settings.from_env()
    ks = kill_switch_engaged(settings.kill_switch_path)
    r = scan(BarStore(a.store), VerdictStore().load(), LiveJournal(), now=_ts(a.now),
             equity=a.equity, symbols=UNIVERSE, limits=RiskLimits(),
             costs=CostModel.calibrated(), kill_switch=ks)
    out = {
        "now": r.now.isoformat(), "session": str(r.session), "validated": r.validated,
        "signal_tiers": r.tiers, "entries_today_after_scan": r.entries_today, "quota": r.quota,
        "kill_switch": ks, "live_authorized": settings.live_authorized,
        "warnings": r.warnings, "halted": r.halted,
        "exits": [{"entry_id": x.entry.id, "symbol": x.entry.symbol, "side": "sell"
                   if x.entry.direction > 0 else "buy", "shares": x.entry.shares,
                   "reason": x.reason} for x in r.exits],
        "entries": [{"tier": p.tier,
                     "signal": {**sig.__dict__, "signal_ts": sig.signal_ts.isoformat()},
                     "order": {"symbol": p.ticker, "side": "buy" if p.direction > 0
                               else "sell", "type": "market", "quantity": f"{p.shares:.6f}",
                               "market_hours": "regular_hours"},
                     "proposal": p.render()} for sig, p in r.entries],
        "rejected": [{"symbol": s.symbol, "family": s.family, "reasons": list(why)}
                     for s, why in r.rejected],
    }
    print(json.dumps(out, indent=1, default=str))
    return 0


def cmd_quote_ok(a) -> int:
    ok, why = spread_ok(a.symbol.upper(), a.bid, a.ask, CostModel.calibrated())
    print(why)
    return 0 if ok else 1


def cmd_record_entry(a) -> int:
    sig = json.loads(a.signal_json)
    s = EntrySignal(**{**sig, "signal_ts": _ts(sig["signal_ts"])})
    e = LiveJournal().record_entry(
        session=_ts(a.entry_ts).date(), symbol=s.symbol, strategy=s.family, params=s.params,
        direction=s.direction, signal_ts=sig["signal_ts"], entry_ts=a.entry_ts,
        fill=a.fill, shares=a.shares, stop=s.stop, target=round(s.target_for(a.fill), 4),
        order_id=a.order_id)
    print(json.dumps(e.__dict__))
    return 0


def cmd_record_exit(a) -> int:
    x = LiveJournal().record_exit(a.entry_id, exit_ts=a.exit_ts, fill=a.fill,
                                  reason=a.reason, order_id=a.order_id)
    print(json.dumps(x.__dict__))
    return 0


def cmd_status(a) -> int:
    j = LiveJournal()
    d = _ts(a.now).date()
    st = j.risk_state(d, a.equity)
    from config.settings import QuotaPolicy
    from src.execution.quota import signal_families
    print(json.dumps({"trades_today": len([e for e in j.entries() if e.session == str(d)]),
                      "daily_minimum": QuotaPolicy().min_trades_per_day,
                      "signal_families": signal_families(VerdictStore().load(), QuotaPolicy()),
                      "start_equity": st.start_equity, "realized_today": st.realized_pnl,
                      "consecutive_losses": st.consecutive_losses,
                      "open": [e.__dict__ for e in j.open_positions()],
                      "validated": [f for f, v in VerdictStore().load().items() if v.tradeable]},
                     indent=1, default=str))
    return 0


def calendar(now: datetime) -> dict:
    d = now.astimezone(ET).date()
    out = {"now_et": now.astimezone(ET).isoformat(timespec="minutes"), "date": str(d),
           "trading_day": is_trading_day(d)}
    if not out["trading_day"]:
        return {**out, "phase": "closed"}
    close = scheduled_close(d)
    open_ = close.replace(hour=9, minute=30)
    cut, flat = close - timedelta(minutes=45), close - timedelta(minutes=15)
    et = now.astimezone(ET)
    phase = ("preopen" if et < open_ else "open" if et < cut else "manage_only" if et < flat
             else "flatten" if et < close else "closed")
    nxt = next_cycle_at(now)
    nd = d + timedelta(days=1)
    while not is_trading_day(nd):
        nd += timedelta(days=1)
    utc = lambda x: x.astimezone(timezone.utc).isoformat(timespec="minutes")
    return {**out, "phase": phase, "close_et": close.strftime("%H:%M"),
            "entry_cutoff_et": cut.strftime("%H:%M"), "flatten_at_et": flat.strftime("%H:%M"),
            "next_cycle_utc": utc(nxt) if nxt < close - timedelta(minutes=4) else None,
            "eod_safety_utc": utc(flat - timedelta(minutes=1)),
            "postclose_utc": utc(close + timedelta(minutes=15)),
            "last_session_of_week": nd.isocalendar()[1] != d.isocalendar()[1]}


def cmd_calendar(a) -> int:
    print(json.dumps(calendar(_ts(a.now)), indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--equity", type=float, required=True)
    s.add_argument("--now")
    s.add_argument("--store", default="data/bars")
    q = sub.add_parser("quote-ok")
    q.add_argument("symbol")
    q.add_argument("bid", type=float)
    q.add_argument("ask", type=float)
    e = sub.add_parser("record-entry")
    e.add_argument("signal_json")
    for k in ("--fill", "--shares"):
        e.add_argument(k, type=float, required=True)
    e.add_argument("--order-id", required=True)
    e.add_argument("--entry-ts", required=True)
    x = sub.add_parser("record-exit")
    x.add_argument("entry_id")
    x.add_argument("--fill", type=float, required=True)
    x.add_argument("--reason", required=True, choices=["stop", "target", "eod", "manual"])
    x.add_argument("--order-id", required=True)
    x.add_argument("--exit-ts", required=True)
    st = sub.add_parser("status")
    st.add_argument("--equity", type=float, required=True)
    st.add_argument("--now")
    c = sub.add_parser("calendar")
    c.add_argument("--now")
    a = ap.parse_args(argv)
    return {"calendar": cmd_calendar, "scan": cmd_scan, "quote-ok": cmd_quote_ok, "record-entry": cmd_record_entry,
            "record-exit": cmd_record_exit, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
