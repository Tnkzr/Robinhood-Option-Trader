"""Bar ingestion, validation and storage.

The broker's historicals tool has two traps this module exists to defuse:

* Outside its intraday retention window (~March 2026 onward, as of Oct 2026) it
  does not error — it returns synthetic bars flagged `interpolated` at a constant
  price with zero volume. Backtesting on those is backtesting on fiction.
* Prices are split-adjusted as of fetch time, so bars fetched on either side of
  a split disagree. Merging them silently would corrupt every return.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
INTRADAY_MINUTES = {"minute": 1, "5minute": 5, "10minute": 10, "30minute": 30, "hour": 60}
DAILY = "day"
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime        # UTC, left edge of the bar
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def et(self) -> datetime:
        return self.ts.astimezone(ET)


class AdjustmentMismatch(RuntimeError):
    """New bars disagree with stored bars on overlapping timestamps."""


@dataclass
class QualityReport:
    symbol: str
    interval: str
    received: int = 0
    interpolated: int = 0
    synthetic: int = 0
    invalid: int = 0
    duplicates: int = 0
    outside_rth: int = 0
    kept: int = 0

    def line(self) -> str:
        return (f"{self.symbol} {self.interval}: received {self.received}, kept {self.kept} "
                f"(dropped interpolated {self.interpolated}, synthetic {self.synthetic}, "
                f"invalid {self.invalid}, duplicate {self.duplicates}, "
                f"outside RTH {self.outside_rth})")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def clean(raw: list[dict], symbol: str, interval: str) -> tuple[list[Bar], QualityReport]:
    """Turn raw tool bars into validated Bars. Never invents or repairs a bar."""
    rep = QualityReport(symbol, interval, received=len(raw))
    step = INTRADAY_MINUTES.get(interval)
    seen: set[datetime] = set()
    out: list[Bar] = []
    for r in raw:
        if r.get("interpolated"):
            rep.interpolated += 1
            continue
        try:
            b = Bar(_ts(r["begins_at"]), float(r["open_price"]), float(r["high_price"]),
                    float(r["low_price"]), float(r["close_price"]), int(r.get("volume") or 0))
        except (KeyError, TypeError, ValueError):
            rep.invalid += 1
            continue
        if min(b.open, b.high, b.low, b.close) <= 0 or b.volume < 0 \
                or b.high + 1e-9 < max(b.open, b.close, b.low) \
                or b.low - 1e-9 > min(b.open, b.close, b.high):
            rep.invalid += 1
            continue
        if b.volume == 0 and b.open == b.high == b.low == b.close:
            rep.synthetic += 1
            continue
        if step is not None:
            t = b.et.time()
            end = (b.et + timedelta(minutes=step)).time()
            if t < RTH_OPEN or t >= RTH_CLOSE or (end > RTH_CLOSE and end != time(0)):
                rep.outside_rth += 1
                continue
        if b.ts in seen:
            rep.duplicates += 1
            continue
        seen.add(b.ts)
        out.append(b)
    out.sort(key=lambda b: b.ts)
    rep.kept = len(out)
    return out, rep


def parse_tool_result(payload: dict) -> list[tuple[str, str, list[dict]]]:
    """(symbol, interval, raw_bars) for each result in a get_equity_historicals payload."""
    results = payload.get("data", payload).get("results", [])
    return [(r["symbol"].upper(), r["interval"], r.get("bars", [])) for r in results]


def session_date(bar: Bar, interval: str) -> date:
    # Daily bars are labelled 00:00Z on the session date; converting to ET would
    # shift them to the previous day.
    return bar.ts.date() if interval not in INTRADAY_MINUTES else bar.et.date()


@dataclass
class Session:
    symbol: str
    date: date
    bars: list[Bar]
    close_et: datetime          # scheduled close implied by a complete session

    @property
    def early_close(self) -> bool:
        return self.close_et.time() == EARLY_CLOSE


def to_sessions(bars: list[Bar], symbol: str, interval: str,
                min_completeness: float = 0.97) -> tuple[list[Session], list[str]]:
    """Group intraday bars into sessions, excluding ones with missing data.

    A session is accepted when it starts at the open, ends at 16:00 (or exactly
    13:00 — a scheduled early close), and has at least `min_completeness` of its
    bars. A session that ends at any other time is treated as truncated data.
    """
    step = INTRADAY_MINUTES[interval]
    by_day: dict[date, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.et.date(), []).append(b)
    sessions, excluded = [], []
    for d in sorted(by_day):
        day = by_day[d]
        first, last = day[0].et, day[-1].et + timedelta(minutes=step)
        problems = []
        if first.time() != RTH_OPEN:
            problems.append(f"starts {first:%H:%M}")
        if last.time() not in (RTH_CLOSE, EARLY_CLOSE):
            problems.append(f"ends {last:%H:%M}")
        expected = int((last - datetime.combine(d, RTH_OPEN, ET)).total_seconds() // 60 // step)
        if expected <= 0 or len(day) / expected < min_completeness:
            problems.append(f"{len(day)}/{max(expected, 0)} bars")
        if problems:
            excluded.append(f"{symbol} {d}: {', '.join(problems)}")
            continue
        sessions.append(Session(symbol, d, day, last))
    return sessions, excluded


class BarStore:
    """Gzipped CSV per symbol and interval. Deterministic bytes, so hashes are stable."""

    HEADER = ["ts_utc", "open", "high", "low", "close", "volume"]

    def __init__(self, root: str | Path = "data/bars") -> None:
        self.root = Path(root)

    def path(self, symbol: str, interval: str) -> Path:
        return self.root / interval / f"{symbol.upper()}.csv.gz"

    def symbols(self, interval: str) -> list[str]:
        d = self.root / interval
        return sorted(p.name[:-7] for p in d.glob("*.csv.gz")) if d.exists() else []

    def load(self, symbol: str, interval: str) -> list[Bar]:
        p = self.path(symbol, interval)
        if not p.exists():
            return []
        with gzip.open(p, "rt", newline="") as fh:
            rows = list(csv.reader(fh))
        return [Bar(_ts(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]))
                for r in rows[1:]]

    def _write(self, symbol: str, interval: str, bars: list[Bar]) -> None:
        p = self.path(symbol, interval)
        p.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(self.HEADER)
        for b in bars:
            w.writerow([b.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), f"{b.open:.6f}", f"{b.high:.6f}",
                        f"{b.low:.6f}", f"{b.close:.6f}", b.volume])
        with open(p, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(buf.getvalue().encode())

    def merge(self, symbol: str, interval: str, new: list[Bar], replace: bool = False,
              tolerance: float = 0.002) -> tuple[int, int]:
        """Merge new bars, refusing if overlapping bars disagree (e.g. a split).

        Returns (added, total). With replace=True the stored series is discarded.
        """
        old = [] if replace else self.load(symbol, interval)
        by_ts = {b.ts: b for b in old}
        overlap = [(by_ts[b.ts].close, b.close) for b in new if b.ts in by_ts]
        if overlap:
            ratios = [n / o for o, n in overlap]
            bad = sum(abs(r - 1) > tolerance for r in ratios)
            if bad > max(1, 0.01 * len(ratios)):
                raise AdjustmentMismatch(
                    f"{symbol} {interval}: {bad}/{len(ratios)} overlapping bars differ "
                    f"(median ratio {median(ratios):.4f}) — likely a split or dividend "
                    f"re-adjustment; refetch the full history and merge with replace=True")
        added = 0
        for b in new:
            if b.ts not in by_ts:
                added += 1
            by_ts[b.ts] = b
        merged = sorted(by_ts.values(), key=lambda b: b.ts)
        self._write(symbol, interval, merged)
        return added, len(merged)

    def fingerprint(self, symbols: list[str], interval: str) -> str:
        h = hashlib.sha256()
        for s in sorted(symbols):
            p = self.path(s, interval)
            h.update(s.encode())
            h.update(p.read_bytes() if p.exists() else b"")
        return h.hexdigest()[:16]


def ingest_file(path: str | Path, store: BarStore, replace: bool = False) -> list[str]:
    """Ingest a saved get_equity_historicals result. Returns one report line per symbol."""
    payload = json.loads(Path(path).read_text())
    lines = []
    for symbol, interval, raw in parse_tool_result(payload):
        bars, rep = clean(raw, symbol, interval)
        if not bars:
            lines.append(rep.line() + " — nothing stored")
            continue
        added, total = store.merge(symbol, interval, bars, replace=replace)
        lines.append(f"{rep.line()}; +{added} new, {total} stored "
                     f"[{bars[0].ts:%Y-%m-%d} → {bars[-1].ts:%Y-%m-%d}]")
    return lines
