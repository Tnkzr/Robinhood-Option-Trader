"""Live trade journal — the record the live risk state is rebuilt from.

Append-only JSONL. Only broker-confirmed fills are recorded: an order that was
submitted but not filled is not an entry.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from src.risk.limits import OpenPosition, RiskState


@dataclass(frozen=True)
class LiveEntry:
    id: str
    session: str
    symbol: str
    strategy: str
    params: str
    direction: int
    signal_ts: str
    entry_ts: str
    fill: float
    shares: float
    stop: float
    target: float
    order_id: str

    @property
    def risk_per_share(self) -> float:
        return abs(self.fill - self.stop)


@dataclass(frozen=True)
class LiveExit:
    entry_id: str
    exit_ts: str
    fill: float
    reason: str
    order_id: str
    pnl: float
    r: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LiveJournal:
    def __init__(self, path: str | Path = "journal/live_trades.jsonl") -> None:
        self.path = Path(path)

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps({"logged_at": _now(), **row}, sort_keys=True) + "\n")

    def entries(self) -> list[LiveEntry]:
        return [LiveEntry(**{k: r[k] for k in LiveEntry.__dataclass_fields__})
                for r in self._rows() if r["kind"] == "entry"]

    def exits(self) -> list[LiveExit]:
        return [LiveExit(**{k: r[k] for k in LiveExit.__dataclass_fields__})
                for r in self._rows() if r["kind"] == "exit"]

    def open_positions(self) -> list[LiveEntry]:
        closed = {x.entry_id for x in self.exits()}
        return [e for e in self.entries() if e.id not in closed]

    def record_entry(self, *, session: date, symbol: str, strategy: str, params: str,
                     direction: int, signal_ts: str, entry_ts: str, fill: float,
                     shares: float, stop: float, target: float, order_id: str) -> LiveEntry:
        if any(e.symbol == symbol for e in self.open_positions()):
            raise ValueError(f"{symbol} already open in the journal — no adding")
        e = LiveEntry(secrets.token_hex(4), str(session), symbol, strategy, params, direction,
                      signal_ts, entry_ts, fill, shares, stop, target, order_id)
        self._append({"kind": "entry", **asdict(e)})
        return e

    def record_exit(self, entry_id: str, *, exit_ts: str, fill: float, reason: str,
                    order_id: str) -> LiveExit:
        e = next((x for x in self.open_positions() if x.id == entry_id), None)
        if e is None:
            raise ValueError(f"no open journal entry {entry_id}")
        pnl = round((fill - e.fill) * e.direction * e.shares, 2)
        r = (fill - e.fill) * e.direction / e.risk_per_share if e.risk_per_share else 0.0
        x = LiveExit(entry_id, exit_ts, fill, reason, order_id, pnl, round(r, 4))
        self._append({"kind": "exit", **asdict(x), "symbol": e.symbol, "session": e.session})
        return x

    def risk_state(self, day: date, equity_now: float) -> RiskState:
        """Today's state: start-of-day equity, realized P&L, loss streak, open positions."""
        by_id = {e.id: e for e in self.entries()}
        today = [x for x in self.exits() if by_id[x.entry_id].session == str(day)]
        today.sort(key=lambda x: x.exit_ts)
        realized = sum(x.pnl for x in today)
        st = RiskState(session=day, start_equity=equity_now - realized)
        for x in today:
            st.close(by_id[x.entry_id].symbol, x.pnl,
                     datetime.fromisoformat(x.exit_ts.replace("Z", "+00:00")))
        for e in self.open_positions():
            st.open(OpenPosition(e.symbol, e.direction, e.shares, e.fill, e.stop))
        return st

    def closed_r(self, strategy: str) -> list[float]:
        by_id = {e.id: e for e in self.entries()}
        xs = sorted(self.exits(), key=lambda x: x.exit_ts)
        return [x.r for x in xs if by_id[x.entry_id].strategy == strategy]
