"""Bar-by-bar intraday simulator.

Fill rules (all conservative):
* A signal on bar i's close fills at bar i+1's OPEN, through the cost model.
  If that open has already gapped through the stop or past the target, the
  entry is skipped — a live trader would cancel it.
* Stops fill at the stop price plus stop slippage; a gap through the stop fills
  at the (worse) open.
* Targets are resting limits: they fill only when price trades through by a tick.
* If a bar touches both stop and target, the stop is assumed to fill first.
* Everything is flat at close-15m (15:45 normally), at that bar's open.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.research.bars import INTRADAY_MINUTES, Bar, Session
from src.research.costs import CostModel
from src.research.indicators import (DailyContextIndex, ema, relative_volume, rsi,
                                     session_vwap)
from src.research.regime import RegimeIndex
from src.research.strategies import BarContext, EntryIntent, Prefix, Strategy


@dataclass(frozen=True)
class Trade:
    symbol: str
    strategy: str
    params: str
    direction: int
    session: date
    signal_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry: float            # cost-inclusive fill
    exit: float
    raw_entry: float        # pre-cost reference price
    raw_exit: float
    stop: float
    target: float
    exit_reason: str        # stop | gap_stop | target | eod
    r: float                # net of spread and slippage, in initial-risk units
    gross_r: float
    mfe_r: float
    mae_r: float
    regime: str | None
    entry_minute: int       # minutes after the open
    score: float = 0.0

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)


@dataclass
class SymbolData:
    symbol: str
    interval: str
    sessions: list[Session]
    daily: DailyContextIndex
    vwap: list[list[float]]
    rvol: list[list[float | None]]
    ema9: list[list[float | None]]
    ema20: list[list[float | None]]
    ema50: list[list[float | None]]
    rsi: list[list[float | None]]
    hod: list[list[float]]
    lod: list[list[float]]

    @property
    def dates(self) -> list[date]:
        return [s.date for s in self.sessions]


def _split(flat: list, sessions: list[Session]) -> list[list]:
    out, k = [], 0
    for s in sessions:
        out.append(flat[k:k + len(s.bars)])
        k += len(s.bars)
    return out


def prepare(symbol: str, sessions: list[Session], daily_bars: list[Bar],
            interval: str = "5minute") -> SymbolData:
    closes = [b.close for s in sessions for b in s.bars]
    hod, lod = [], []
    for s in sessions:
        h, l, hs, ls = float("-inf"), float("inf"), [], []
        for b in s.bars:
            h, l = max(h, b.high), min(l, b.low)
            hs.append(h)
            ls.append(l)
        hod.append(hs)
        lod.append(ls)
    return SymbolData(
        symbol=symbol, interval=interval, sessions=sessions,
        daily=DailyContextIndex(daily_bars),
        vwap=[session_vwap(s.bars) for s in sessions],
        rvol=relative_volume(sessions),
        ema9=_split(ema(closes, 9), sessions), ema20=_split(ema(closes, 20), sessions),
        ema50=_split(ema(closes, 50), sessions), rsi=_split(rsi(closes, 14), sessions),
        hod=hod, lod=lod)


def index_context(idx: SymbolData) -> dict[datetime, bool]:
    """bar timestamp -> index closed above its session VWAP on that bar."""
    return {b.ts: b.close > v for s, vw in zip(idx.sessions, idx.vwap)
            for b, v in zip(s.bars, vw)}


@dataclass
class _Pos:
    direction: int
    entry: float
    raw_entry: float
    stop: float
    target: float
    risk: float
    entry_i: int
    entry_ts: datetime
    signal_ts: datetime
    best: float
    worst: float
    score: float = 0.0


@dataclass
class SessionRun:
    """One session processed through bar `upto` (inclusive)."""
    trades: list[Trade]
    pending: tuple[EntryIntent, datetime] | None   # signal awaiting the next open
    position: _Pos | None                          # still open after `upto`
    regime: str | None
    signals: int


def _trade(sym: str, strat: Strategy, p: _Pos, raw: float, ts: datetime, reason: str,
           s: Session, reg: str | None, fill: float) -> Trade:
    raw_risk = (p.raw_entry - p.stop) * p.direction
    return Trade(
        symbol=sym, strategy=strat.family, params=strat.key, direction=p.direction,
        session=s.date, signal_ts=p.signal_ts, entry_ts=p.entry_ts, exit_ts=ts,
        entry=p.entry, exit=fill, raw_entry=p.raw_entry, raw_exit=raw, stop=p.stop,
        target=p.target, exit_reason=reason,
        r=(fill - p.entry) * p.direction / p.risk,
        gross_r=(raw - p.raw_entry) * p.direction / raw_risk if raw_risk > 0 else 0.0,
        mfe_r=(p.best - p.entry) * p.direction / p.risk,
        mae_r=(p.worst - p.entry) * p.direction / p.risk,
        regime=reg, entry_minute=int((p.entry_ts - s.bars[0].ts).total_seconds() // 60),
        score=p.score)


def run_session(sd: SymbolData, k: int, strat: Strategy, costs: CostModel, *,
                index_ctx: dict[datetime, bool] | None = None,
                regime: RegimeIndex | None = None, max_trades_per_day: int = 1,
                min_risk_atr: float = 0.05, upto: int | None = None) -> SessionRun | None:
    """The single source of truth for intraday behaviour — used by the backtest
    (whole sessions) and by the live scanner (a session in progress)."""
    s, sym = sd.sessions[k], sd.symbol
    dctx = sd.daily.get(s.date)
    if dctx is None or dctx.atr <= 0:
        return None
    step = timedelta(minutes=INTRADAY_MINUTES[sd.interval])
    reg = regime.label(s.date) if regime else None
    entry_cut = s.close_et - timedelta(minutes=45)
    exit_at = s.close_et - timedelta(minutes=15)
    bars = s.bars if upto is None else s.bars[:upto + 1]
    trades: list[Trade] = []
    pending, pos, signals = None, None, 0

    for i, b in enumerate(bars):
        t0 = b.et
        if pos is not None and t0 >= exit_at:
            fill = costs.market_fill(sym, b.open, -pos.direction)
            trades.append(_trade(sym, strat, pos, b.open, b.ts, "eod", s, reg, fill))
            pos = None

        if pos is None and pending is not None:
            intent, sig_ts = pending
            pending = None
            if t0 < entry_cut:
                pos = _open(intent, b, i, sig_ts, sym, costs, dctx.atr * min_risk_atr)

        if pos is not None:
            d = pos.direction
            gap = i > pos.entry_i and (b.open - pos.stop) * d <= 0
            hit_stop = (b.low <= pos.stop) if d > 0 else (b.high >= pos.stop)
            if gap or hit_stop:
                raw = b.open if gap else pos.stop
                pos.worst = raw if (raw - pos.worst) * d < 0 else pos.worst
                fill = costs.market_fill(sym, raw, -d, stop=True)
                trades.append(_trade(sym, strat, pos, raw, b.ts,
                                     "gap_stop" if gap else "stop", s, reg, fill))
                pos = None
            elif costs.limit_fills(pos.target, b.low, b.high, -d):
                pos.best = pos.target if (pos.target - pos.best) * d > 0 else pos.best
                trades.append(_trade(sym, strat, pos, pos.target, b.ts, "target", s, reg,
                                     pos.target))
                pos = None
            else:
                fav, adv = (b.high, b.low) if d > 0 else (b.low, b.high)
                if (fav - pos.best) * d > 0:
                    pos.best = fav
                if (adv - pos.worst) * d < 0:
                    pos.worst = adv

        if (pos is None and pending is None and signals < max_trades_per_day
                and t0 + step < entry_cut):
            ctx = BarContext(
                symbol=sym, date=s.date, i=i, bars=Prefix(bars, i + 1),
                vwap=sd.vwap[k][i], ema9=sd.ema9[k][i], ema20=sd.ema20[k][i],
                ema50=sd.ema50[k][i], rsi=sd.rsi[k][i], rvol=sd.rvol[k][i],
                atr=dctx.atr, prev_close=dctx.prev_close,
                hod=sd.hod[k][i], lod=sd.lod[k][i],
                prev_hod=sd.hod[k][i - 1] if i else float("-inf"),
                prev_lod=sd.lod[k][i - 1] if i else float("inf"),
                index_above_vwap=index_ctx.get(b.ts) if index_ctx is not None else None,
                minutes_since_open=int((t0 + step - bars[0].et).total_seconds() // 60))
            intent = strat.on_bar(ctx)
            if intent is not None:
                pending = (intent, b.ts)
                signals += 1
    return SessionRun(trades, pending, pos, reg, signals)


def simulate(sd: SymbolData, strat: Strategy, costs: CostModel, *,
             index_ctx: dict[datetime, bool] | None = None,
             regime: RegimeIndex | None = None, dates: set[date] | None = None,
             max_trades_per_day: int = 1, min_risk_atr: float = 0.05) -> list[Trade]:
    trades: list[Trade] = []
    for k, s in enumerate(sd.sessions):
        if dates is not None and s.date not in dates:
            continue
        run = run_session(sd, k, strat, costs, index_ctx=index_ctx, regime=regime,
                          max_trades_per_day=max_trades_per_day, min_risk_atr=min_risk_atr)
        if run is None:
            continue
        trades += run.trades
        if run.position is not None:      # session data ended before the exit bar
            last = s.bars[-1]
            fill = costs.market_fill(sd.symbol, last.close, -run.position.direction)
            trades.append(_trade(sd.symbol, strat, run.position, last.close, last.ts,
                                 "eod", s, run.regime, fill))
    return trades


def _open(intent: EntryIntent, b: Bar, i: int, sig_ts: datetime, sym: str,
          costs: CostModel, min_risk: float) -> _Pos | None:
    d, raw = intent.direction, b.open
    if (raw - intent.stop) * d <= 0:
        return None                                  # opened through the stop
    fill = costs.market_fill(sym, raw, d)
    risk = (fill - intent.stop) * d
    if risk < min_risk or risk <= 0:
        return None
    target = (intent.target_price if intent.target_price is not None
              else fill + d * (intent.target_r or 0.0) * risk)
    if (target - raw) * d <= 0:
        return None                                  # opened past the target
    return _Pos(d, fill, raw, intent.stop, target, risk, i, b.ts, sig_ts, raw, raw,
                intent.score)
