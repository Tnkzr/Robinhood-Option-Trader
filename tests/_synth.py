"""Synthetic market data for research tests. Never used for research itself."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.research.bars import Bar
from src.research.engine import Trade

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def open_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(UTC)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def flat_session(d: date, price: float = 100.0, n: int = 78, vol: int = 1000) -> list[Bar]:
    t0 = open_utc(d)
    return [Bar(t0 + timedelta(minutes=5 * i), price, price + 0.05, price - 0.05, price, vol)
            for i in range(n)]


def daily_history(end_before: date, n: int = 30, price: float = 100.0,
                  rng: float = 2.0) -> list[Bar]:
    days = [d for d in weekdays(end_before - timedelta(days=int(n * 1.6) + 5), n * 2)
            if d < end_before][-n:]
    return [Bar(datetime(d.year, d.month, d.day, tzinfo=UTC), price, price + rng / 2,
                price - rng / 2, price, 1_000_000) for d in days]


def random_dataset(symbols: list[str], start: date, sessions: int, warmup_days: int = 80,
                   seed: int = 11) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    """Random-walk intraday bars with trending days and volume bursts, plus daily
    bars (warm-up history + aggregates of the intraday sessions)."""
    r = random.Random(seed)
    intraday: dict[str, list[Bar]] = {}
    daily: dict[str, list[Bar]] = {}
    all_days = weekdays(start - timedelta(days=int(warmup_days * 1.5) + 3),
                        warmup_days + sessions + 60)
    warm = [d for d in all_days if d < start][-warmup_days:]
    live = [d for d in all_days if d >= start][:sessions]
    for s in symbols:
        p = r.uniform(50, 300)
        dl, il = [], []
        for d in warm:
            o = p
            p *= 1 + r.gauss(0, 0.012)
            hi, lo = max(o, p) * (1 + abs(r.gauss(0, 0.006))), min(o, p) * (1 - abs(r.gauss(0, 0.006)))
            dl.append(Bar(datetime(d.year, d.month, d.day, tzinfo=UTC), o, hi, lo, p, 10**6))
        for d in live:
            drift = r.choice([-1, 0, 0, 1]) * 0.0008
            t0, bars = open_utc(d), []
            for i in range(78):
                o = p
                p = max(1.0, p * (1 + drift + r.gauss(0, 0.0015)))
                hi = max(o, p) * (1 + abs(r.gauss(0, 0.0005)))
                lo = min(o, p) * (1 - abs(r.gauss(0, 0.0005)))
                v = int(r.lognormvariate(9, 0.5) * (4 if r.random() < 0.08 else 1))
                bars.append(Bar(t0 + timedelta(minutes=5 * i), o, hi, lo, p, v))
            il += bars
            dl.append(Bar(datetime(d.year, d.month, d.day, tzinfo=UTC), bars[0].open,
                          max(b.high for b in bars), min(b.low for b in bars), bars[-1].close,
                          sum(b.volume for b in bars)))
        intraday[s], daily[s] = il, dl
    return intraday, daily


def mk_trade(r: float, session: date, *, symbol: str = "T", minute: int = 30,
             hold_min: int = 30, direction: int = 1, entry: float = 100.0,
             stop: float = 99.0, regime: str | None = "up/normal",
             reason: str = "target", params: str = "k") -> Trade:
    t = open_utc(session) + timedelta(minutes=minute)
    risk = abs(entry - stop)
    exit_px = entry + direction * r * risk
    return Trade(symbol, "fam", params, direction, session, t - timedelta(minutes=5), t,
                 t + timedelta(minutes=hold_min), entry, exit_px, entry, exit_px, stop,
                 entry + direction * 2 * risk, reason, r, r, max(r, 0.0), min(r, 0.0),
                 regime, minute)
