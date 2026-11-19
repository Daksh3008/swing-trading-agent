"""Paper-trade tracker: replay sessions one day at a time.

``update_day(D)`` mirrors the backtest engine's day loop exactly:

1. exits on already-open positions (ratchet from prior-day high-water/ATR,
   then test D's bar; limit-locked days cannot exit);
2. suggested trades whose signal date is the previous session fill at D's
   open, subject to slots, cash and the no-same-day-re-entry rule;
3. every remaining open position is marked at D's close, and portfolio equity
   is written.

Because the state lives in SQLite and each day is a pure function of the
previous state, replaying three skipped days in order produces the same state
as three daily runs. That property is tested, not assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime

import pandas as pd

from ..backtest.costs import CostModel
from ..core.config import Settings
from ..data.store import ParquetBarStore
from .db import Database
from .rules import (
    PositionView,
    atr_series,
    exit_signal,
    initial_stop,
    initial_target,
    pnl,
    shares_for,
    update_trailing_stop,
)
from .settings import LiveSettings, load_live_settings

log = logging.getLogger(__name__)

CASH_KEY = "cash"
CURSOR_KEY = "tracker.last_day"


@dataclass
class DayOutcome:
    day: Date
    entries: int = 0
    exits: int = 0
    expired: int = 0
    open_positions: int = 0
    cash: float = 0.0
    nav: float = 0.0
    skipped: bool = False
    reason: str | None = None
    exits_detail: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(), "entries": self.entries,
            "exits": self.exits, "expired": self.expired,
            "open_positions": self.open_positions,
            "cash": round(self.cash, 2), "nav": round(self.nav, 2),
            "skipped": self.skipped, "reason": self.reason,
            "exits_detail": self.exits_detail,
        }


class Tracker:
    def __init__(self, settings: Settings, db: Database,
                 live: LiveSettings | None = None,
                 costs: CostModel | None = None,
                 histories: dict[str, pd.DataFrame] | None = None,
                 store: ParquetBarStore | None = None):
        self.settings = settings
        self.db = db
        self.live = live or load_live_settings(settings, db)
        self.costs = costs or CostModel()
        self.store = store or ParquetBarStore(settings.data_dir)
        self._histories = histories or {}
        self._atr: dict[str, pd.Series] = {}
        self._owns_histories = histories is None

    # ----------------------------------------------------------------- public

    def update_day(self, day: Date | str) -> DayOutcome:
        day = _as_date(day)
        trusted = self.db.trusted_day()
        if trusted and day.isoformat() > trusted:
            log.info("tracker %s: skipped, the cache is only covered through %s",
                     day, trusted)
            return DayOutcome(day=day, skipped=True,
                              reason=f"cache covered through {trusted}")
        last = self.db.get_state(CURSOR_KEY)
        if last and day.isoformat() <= last:
            return DayOutcome(day=day, skipped=True, reason="already replayed")

        params = self.live.rules()
        prev = self.db.previous_day(day)
        cash = float(self.db.get_state(
            CASH_KEY, self.live.position_size * self.live.max_positions))
        outcome = DayOutcome(day=day)
        exited_today: set[str] = set()
        exits: list[dict] = []

        # 1. exits -------------------------------------------------------
        for row in self.db.open_trades():
            symbol = row["symbol"]
            entry_date = str(row["entry_date"] or "")[:10]
            if entry_date and entry_date >= day.isoformat():
                # Filled today (only possible after a crash mid-replay). A
                # position is never judged on its entry session.
                continue
            days_held = int(row["days_held"] or 0)
            bar = self._bar(symbol, day)
            if bar is None:
                # suspended or no print: the position freezes but still ages
                self.db.update_trade(row["id"], days_held=days_held + 1)
                continue
            days_held += 1
            entry = float(row["entry_price"])
            high_water = float(row["high_water"] or entry)
            stop_live = float(row["stop_live"] or row["stop_initial"])
            atr_prev = self._atr_at(symbol, day)

            stop_live = update_trailing_stop(
                entry_price=entry, high_water=high_water,
                stop_live=stop_live, atr_prev=atr_prev, params=params)
            view = PositionView(
                entry_price=entry, stop_initial=float(row["stop_initial"]),
                stop_live=stop_live, target=float(row["target"]),
                high_water=high_water, bars_held=days_held)
            signal = exit_signal(view, bar, params, atr_prev=atr_prev)

            if signal is None:
                high_water = max(high_water, float(bar["high"]))
                self.db.update_trade(row["id"], stop_live=stop_live,
                                     high_water=high_water, days_held=days_held)
                continue

            price, reason = signal
            shares = int(row["shares"])
            value = price * shares
            cost = self.costs.sell_cost(value)
            trade_pnl = pnl(entry, price, shares,
                            float(row["entry_cost"] or 0.0), cost)
            self.db.update_trade(
                row["id"], status="closed", exit_date=day, exit_price=price,
                exit_cost=cost, exit_reason=reason.value, days_held=days_held,
                stop_live=stop_live, high_water=high_water, pnl=trade_pnl)
            cash += value - cost
            exited_today.add(symbol)
            outcome.exits += 1
            exits.append({
                "symbol": symbol, "reason": reason.value, "price": price,
                "pnl": trade_pnl,
                "return_pct": (trade_pnl / (entry * shares)) if entry and shares else 0.0,
                "days_held": days_held,
            })

        # 2. suggestions -------------------------------------------------
        fillable = []
        for row in self.db.suggested_trades():
            signal_date = row["signal_date"]
            if signal_date is None or (prev is not None and signal_date < prev):
                self.db.update_trade(row["id"], status="expired")
                outcome.expired += 1
            elif signal_date == prev:
                fillable.append(row)

        open_count = len(self.db.open_trades())
        if cash < params.position_size * 0.98:
            fillable = []          # engine-level "no cash" gate for the day
        for row in fillable:
            symbol = row["symbol"]
            if (open_count >= params.max_positions or symbol in exited_today
                    or self.db.has_open_trade(symbol)):
                self.db.update_trade(row["id"], status="expired")
                outcome.expired += 1
                continue
            bar = self._bar(symbol, day)
            open_px = float(bar["open"]) if bar is not None else float("nan")
            shares = shares_for(params.position_size, open_px)
            if shares <= 0:
                self.db.update_trade(row["id"], status="expired")
                outcome.expired += 1
                continue
            value = shares * open_px
            cost = self.costs.buy_cost(value)
            if value + cost > cash:
                self.db.update_trade(row["id"], status="expired")
                outcome.expired += 1
                continue
            stop = initial_stop(open_px, params.stop_pct)
            self.db.update_trade(
                row["id"], status="open", entry_date=day, entry_price=open_px,
                shares=shares, stop_initial=stop, stop_live=stop,
                target=initial_target(open_px, params.target_pct),
                high_water=open_px, entry_cost=cost, days_held=0, pnl=0.0)
            cash -= value + cost
            open_count += 1
            outcome.entries += 1

        # 3. marks + equity ----------------------------------------------
        # A symbol with no bar is valued at its last recorded close, never at
        # cost. Writing a mark for a session that did not print would hide the
        # staleness, so the mark date stays honest and the UI flags it.
        prior = self.db.latest_marks()
        positions_value = 0.0
        open_rows = self.db.open_trades()
        for row in open_rows:
            symbol = row["symbol"]
            shares = int(row["shares"] or 0)
            bar = self._bar(symbol, day)
            if bar is None:
                mark = prior.get(row["id"])
                fallback = (float(mark["close"]) if mark and mark["close"] is not None
                            else float(row["entry_price"] or 0.0))
                positions_value += fallback * shares
                continue
            close = float(bar["close"])
            positions_value += close * shares
            self.db.upsert_mark(
                row["id"], day, close, float(row["stop_live"] or 0.0),
                float(row["high_water"] or 0.0),
                (close - float(row["entry_price"])) * shares
                - float(row["entry_cost"] or 0.0))

        nav = cash + positions_value
        self.db.upsert_equity(day, cash, positions_value, nav, len(open_rows))
        self.db.upsert_days([day])
        self.db.set_state(CASH_KEY, cash)
        self.db.set_state(CURSOR_KEY, day.isoformat())

        outcome.open_positions = len(open_rows)
        outcome.cash = cash
        outcome.nav = nav
        outcome.exits_detail = exits
        self._check_health(day)
        if exits:
            self._notify_exits(day, exits)
        log.info("tracker %s: %d exits, %d entries, %d expired, %d open, nav %.2f",
                 day, outcome.exits, outcome.entries, outcome.expired,
                 outcome.open_positions, outcome.nav)
        return outcome

    def catch_up(self, until: Date | str | None = None) -> list[DayOutcome]:
        """Replay every unprocessed session the cache can actually support."""
        self.reconcile()
        until = _as_date(until) if until is not None else None
        trusted = self.db.trusted_day()
        if trusted:
            cap = _as_date(trusted)
            until = cap if until is None or until > cap else until
        cursor = self.db.get_state(CURSOR_KEY)
        start = cursor or self.db.first_signal_day()
        if start is None and until is None:
            return []
        days = self.db.trading_days(start=start, end=until)
        out = []
        for day in days:
            result = self.update_day(day)
            if not result.skipped:
                out.append(result)
        return out

    def pending_days(self, until: Date | str | None = None) -> list[str]:
        self.reconcile()
        until = _as_date(until) if until is not None else None
        trusted = self.db.trusted_day()
        if trusted:
            cap = _as_date(trusted)
            until = cap if until is None or until > cap else until
        cursor = self.db.get_state(CURSOR_KEY)
        start = cursor or self.db.first_signal_day()
        if start is None:
            return []
        return [d for d in self.db.trading_days(start=start, end=until)
                if not cursor or d > cursor]

    def reconcile(self) -> dict:
        """Undo a cursor or an entry advanced past the session the cache supports.

        The old tracker processed a session that almost no symbol traded, marked
        positions at cost and moved the cursor, so the real session could never
        be replayed. This rolls that back: entries after the trusted session are
        returned to the queue and their cash refunded, equity and marks after it
        are dropped, ``days_held`` is recomputed from the calendar, and the
        cursor returns to the trusted session.
        """
        trusted = self.db.trusted_day()
        if not trusted:
            return {"changed": False, "cursor": None, "trusted": None}
        rolled = self._rollback_entries_after(trusted)
        cursor = self.db.get_state(CURSOR_KEY)
        changed = rolled > 0
        equity_rows = mark_rows = 0
        repaired = 0
        if cursor and cursor > trusted:
            with self.db.connect() as conn:
                equity_rows = conn.execute(
                    "DELETE FROM equity WHERE day > ?", (trusted,)).rowcount
                mark_rows = conn.execute(
                    "DELETE FROM marks WHERE date > ?", (trusted,)).rowcount
            sessions = self.db.trading_days(end=trusted)
            for row in self.db.open_trades():
                entry = str(row["entry_date"] or "")[:10]
                if not entry:
                    continue
                held = sum(1 for day in sessions if day > entry)
                if held != int(row["days_held"] or 0):
                    self.db.update_trade(row["id"], days_held=held)
                    repaired += 1
            self.db.set_state(CURSOR_KEY, trusted)
            changed = True
        if not changed:
            return {"changed": False, "cursor": cursor, "trusted": trusted}
        log.warning("tracker reconciled: cursor %s -> %s, rolled back %d entry/entries, "
                    "dropped %d equity and %d mark rows, fixed %d day counters",
                    cursor, trusted, rolled, equity_rows, mark_rows, repaired)
        return {"changed": True, "cursor": trusted, "trusted": trusted,
                "entries_rolled_back": rolled, "equity_rows": equity_rows,
                "mark_rows": mark_rows, "days_fixed": repaired}

    def _rollback_entries_after(self, trusted: str) -> int:
        """Return positions opened after the trusted session to the queue."""
        rolled = 0
        cash = float(self.db.get_state(
            CASH_KEY, self.live.position_size * self.live.max_positions))
        for row in self.db.open_trades():
            entry = str(row["entry_date"] or "")[:10]
            if not entry or entry <= trusted:
                continue
            shares = int(row["shares"] or 0)
            cash += (float(row["entry_price"] or 0.0) * shares
                     + float(row["entry_cost"] or 0.0))
            self.db.update_trade(
                row["id"], status="suggested", entry_date=None, entry_price=None,
                shares=None, stop_initial=None, stop_live=None, target=None,
                high_water=None, entry_cost=0.0, days_held=0, pnl=None)
            rolled += 1
        if rolled:
            self.db.set_state(CASH_KEY, cash)
            log.warning("tracker: rolled back %d position(s) entered after %s",
                        rolled, trusted)
        return rolled

    # -------------------------------------------------------------- internals

    def _check_health(self, day: Date) -> None:
        """Best-effort alert when positions have gone unmarked."""
        try:
            from .ops import check_mark_health

            check_mark_health(self.settings, self.db, day)
        except Exception:  # noqa: BLE001 — never fail a replay over a toast
            log.debug("mark health check failed", exc_info=True)

    def _notify_exits(self, day: Date, exits: list[dict]) -> None:
        """Desktop toast + alert-log line when the rules close a position.

        Fires only for the newest session, so replaying a multi-day gap does
        not spray one notification per historical exit.
        """
        try:
            latest = self.db.last_day()
            if latest and str(day) != str(latest):
                return
            from .ops import alert

            if len(exits) == 1:
                e = exits[0]
                title = f"STA exit: {str(e['symbol']).replace('.BO', '')}"
                body = (f"{_reason_label(e['reason'])} at ₹{e['price']:,.2f} — "
                        f"{e['return_pct']:+.1%} net, after "
                        f"{e['days_held']} sessions.")
            else:
                title = f"STA: {len(exits)} positions closed"
                body = "; ".join(
                    f"{str(e['symbol']).replace('.BO', '')} "
                    f"{_reason_label(e['reason'])} {e['return_pct']:+.1%}"
                    for e in exits[:5])
            alert(self.settings, title, body)
        except Exception:  # noqa: BLE001 — never fail a replay over a toast
            log.debug("exit notification failed", exc_info=True)

    def _hist(self, symbol: str) -> pd.DataFrame | None:
        hist = self._histories.get(symbol)
        if hist is None and self._owns_histories:
            hist = self.store.load_symbol(symbol)
            if hist is not None:
                self._histories[symbol] = hist
        return hist

    def _bar(self, symbol: str, day: Date):
        hist = self._hist(symbol)
        if hist is None:
            return None
        ts = pd.Timestamp(day)
        if ts not in hist.index:
            return None
        return hist.loc[ts]

    def _atr_at(self, symbol: str, day: Date) -> float:
        series = self._atr.get(symbol)
        if series is None:
            hist = self._hist(symbol)
            if hist is None:
                return float("nan")
            series = atr_series(hist, self.live.atr_window)
            self._atr[symbol] = series
        try:
            return float(series.loc[pd.Timestamp(day)])
        except KeyError:
            return float("nan")


def _as_date(value: Date | str | None) -> Date:
    if isinstance(value, str):
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    if value is None:
        raise ValueError("a day is required")
    return value


_REASON_LABELS = {
    "stop_initial": "Initial stop",
    "stop_trailing": "Trailing stop",
    "stop_profit_lock": "Profit lock",
    "stop_failed_breakout": "Failed breakout",
    "stop_atr_collapsed": "Volatility collapse",
    "target": "Target reached",
    "time": "Time stop",
    "trend": "Trend exit",
}


def _reason_label(reason: str) -> str:
    return _REASON_LABELS.get(str(reason), str(reason).replace("_", " ").title())
