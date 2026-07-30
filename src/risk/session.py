"""Session clock and the mandatory end-of-day liquidation state machine.

This module is deliberately pure and side-effect free so it can be unit tested
exhaustively. It is the one component that cannot be disabled: there is no code
path that returns a phase permitting an overnight hold.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from config.settings import SessionTimes

ET = ZoneInfo("America/New_York")


class Phase(str, Enum):
    PREMARKET = "premarket"
    OPEN = "open"                  # normal two-way trading
    NO_NEW_ENTRIES = "no_new"      # manage existing only
    LIQUIDATE = "liquidate"        # close everything, escalating urgency
    HARD_DEADLINE = "hard_deadline"  # past final cutoff, must already be flat
    CLOSED = "closed"


@dataclass(frozen=True)
class SessionState:
    phase: Phase
    now_et: datetime
    minutes_to_entry_cutoff: float
    minutes_to_exit_window: float
    minutes_to_final_deadline: float

    @property
    def may_open(self) -> bool:
        return self.phase is Phase.OPEN

    @property
    def must_liquidate(self) -> bool:
        return self.phase in (Phase.LIQUIDATE, Phase.HARD_DEADLINE)

    @property
    def urgency(self) -> float:
        """0.0 → patient limit at mid. 1.0 → cross the spread immediately."""
        if self.phase is Phase.HARD_DEADLINE:
            return 1.0
        if self.phase is not Phase.LIQUIDATE:
            return 0.0
        total = max(self._liquidation_window_minutes, 1e-9)
        elapsed = total - self.minutes_to_final_deadline
        return min(1.0, max(0.0, elapsed / total))

    _liquidation_window_minutes: float = 10.0


class SessionClock:
    def __init__(self, times: SessionTimes | None = None) -> None:
        self.t = times or SessionTimes()
        self._window = self._delta_min(self.t.exit_window_start, self.t.final_deadline)

    @staticmethod
    def _delta_min(a: time, b: time) -> float:
        base = datetime(2000, 1, 1)
        return (datetime.combine(base, b) - datetime.combine(base, a)).total_seconds() / 60

    def state(self, now: datetime | None = None) -> SessionState:
        now_et = (now or datetime.now(ET)).astimezone(ET)
        cur = now_et.time()

        if cur < self.t.market_open:
            phase = Phase.PREMARKET
        elif cur >= self.t.market_close:
            phase = Phase.CLOSED
        elif cur >= self.t.final_deadline:
            phase = Phase.HARD_DEADLINE
        elif cur >= self.t.exit_window_start:
            phase = Phase.LIQUIDATE
        elif cur >= self.t.entry_cutoff:
            phase = Phase.NO_NEW_ENTRIES
        else:
            phase = Phase.OPEN

        return SessionState(
            phase=phase,
            now_et=now_et,
            minutes_to_entry_cutoff=self._delta_min(cur, self.t.entry_cutoff),
            minutes_to_exit_window=self._delta_min(cur, self.t.exit_window_start),
            minutes_to_final_deadline=self._delta_min(cur, self.t.final_deadline),
            _liquidation_window_minutes=self._window,
        )

    def can_reach_target(self, minutes_needed: float, now: datetime | None = None) -> bool:
        """Reject a setup whose projected move cannot complete before liquidation.

        Guards the spec rule: never open a position that cannot reasonably be
        exited before the mandatory window.
        """
        st = self.state(now)
        if not st.may_open:
            return False
        return minutes_needed <= st.minutes_to_exit_window

    def limit_aggression(self, now: datetime | None = None) -> float:
        """Fraction of the bid/ask spread to give up when closing. Rises to 1.0."""
        return self.state(now).urgency
