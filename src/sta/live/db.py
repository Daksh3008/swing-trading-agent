"""SQLite persistence for the live app (stdlib ``sqlite3`` only).

One file (``data/app.db``) holds every piece of mutable state:

``signals``     one row per (signal_date, symbol) — the daily suggestion
``trades``      suggested -> open -> closed paper positions
``marks``       daily mark-to-market per trade (close, stop, high-water)
``equity``      end-of-day portfolio value, for the performance chart
``days``        the trading calendar observed by the sync
``sync_state``  per-symbol freshness / quality verdict
``sync_log``    one row per sync run
``app_state``   small key/value store (cash, settings overrides, cursors)

Connections are short-lived and WAL-enabled, so the web request thread and the
background job thread can write without stepping on each other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date  TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    exchange     TEXT NOT NULL DEFAULT 'BSE',
    name         TEXT NOT NULL DEFAULT '',
    score        REAL NOT NULL DEFAULT 0,
    close        REAL NOT NULL,
    cap_class    TEXT NOT NULL DEFAULT '',
    entry_plan   REAL NOT NULL,
    stop_initial REAL NOT NULL,
    qty          INTEGER NOT NULL DEFAULT 0,
    risk         REAL NOT NULL DEFAULT 0,
    dist_high    REAL,
    action       TEXT NOT NULL DEFAULT 'new',   -- new | track | skip
    created_at   TEXT NOT NULL,
    UNIQUE (signal_date, symbol)
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER REFERENCES signals(id),
    symbol       TEXT NOT NULL,
    exchange     TEXT NOT NULL DEFAULT 'BSE',
    status       TEXT NOT NULL,                 -- suggested|open|closed|expired|cancelled
    signal_date  TEXT,
    entry_date   TEXT,
    entry_price  REAL,
    shares       INTEGER,
    stop_initial REAL,
    stop_live    REAL,
    target       REAL,
    high_water   REAL,
    entry_cost   REAL NOT NULL DEFAULT 0,
    exit_date    TEXT,
    exit_price   REAL,
    exit_cost    REAL NOT NULL DEFAULT 0,
    exit_reason  TEXT,
    days_held    INTEGER NOT NULL DEFAULT 0,
    pnl          REAL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    trade_id       INTEGER NOT NULL REFERENCES trades(id),
    date           TEXT NOT NULL,
    close          REAL,
    stop_live      REAL,
    high_water     REAL,
    unrealized_pnl REAL,
    PRIMARY KEY (trade_id, date)
);

CREATE TABLE IF NOT EXISTS equity (
    day            TEXT PRIMARY KEY,
    cash           REAL NOT NULL,
    positions_value REAL NOT NULL,
    nav            REAL NOT NULL,
    open_positions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sync_state (
    symbol     TEXT PRIMARY KEY,
    last_date  TEXT,
    bars       INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'ok',      -- ok | stale | suspicious | failed
    error      TEXT,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    duration_s  REAL,
    symbols     INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    refreshed   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    suspicious  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    note        TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_date  ON signals (signal_date);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
CREATE INDEX IF NOT EXISTS idx_marks_trade   ON marks (trade_id);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(value: Date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]
    return value.isoformat()


class Database:
    """Thin, explicit data-access layer. No ORM, no hidden transactions."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._memory_conn: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------ plumbing

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            yield self._memory_conn
            self._memory_conn.commit()
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> Database:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        return self

    # -------------------------------------------------------------- signals

    def insert_signal(self, row: dict[str, Any]) -> int | None:
        """Idempotent per (signal_date, symbol): returns None if it existed."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO signals
                   (signal_date, symbol, exchange, name, score, close, cap_class,
                    entry_plan, stop_initial, qty, risk, dist_high, action, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_iso(row["signal_date"]), row["symbol"], row.get("exchange", "BSE"),
                 row.get("name", ""), float(row.get("score", 0.0)),
                 float(row["close"]), row.get("cap_class", ""),
                 float(row.get("entry_plan", row["close"])),
                 float(row["stop_initial"]), int(row.get("qty", 0)),
                 float(row.get("risk", 0.0)), row.get("dist_high"),
                 row.get("action", "new"), _now()))
            return int(cur.lastrowid) if cur.rowcount else None

    def signals_for(self, day: Date | str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM signals WHERE signal_date = ? ORDER BY score DESC",
                (_iso(day),)).fetchall()

    def signals_between(self, start: Date | str, end: Date | str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM signals WHERE signal_date BETWEEN ? AND ?
                   ORDER BY signal_date DESC, score DESC""",
                (_iso(start), _iso(end))).fetchall()

    def latest_signal_day(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(signal_date) AS d FROM signals").fetchone()
        return row["d"] if row else None

    def first_signal_day(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MIN(signal_date) AS d FROM signals").fetchone()
        return row["d"] if row else None

    def set_signal_action(self, signal_id: int, action: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE signals SET action = ? WHERE id = ?", (action, signal_id))

    def signal(self, signal_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()

    def tracked_signals(self, day: Date | str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM signals WHERE signal_date = ? AND action = 'track'
                   ORDER BY score DESC""", (_iso(day),)).fetchall()

    # --------------------------------------------------------------- trades

    def insert_trade(self, row: dict[str, Any]) -> int:
        now = _now()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (signal_id, symbol, exchange, status, signal_date, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (row.get("signal_id"), row["symbol"], row.get("exchange", "BSE"),
                 row.get("status", "suggested"), _iso(row.get("signal_date")), now, now))
            return int(cur.lastrowid)

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE trades SET {cols} WHERE id = ?",
                         (*fields.values(), trade_id))

    def trade(self, trade_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()

    def trades(self, *statuses: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if not statuses:
                return conn.execute(
                    "SELECT * FROM trades ORDER BY id").fetchall()
            marks = ",".join("?" * len(statuses))
            return conn.execute(
                f"SELECT * FROM trades WHERE status IN ({marks}) ORDER BY id",
                statuses).fetchall()

    def open_trades(self) -> list[sqlite3.Row]:
        return self.trades("open")

    def closed_trades(self) -> list[sqlite3.Row]:
        return self.trades("closed")

    def suggested_trades(self) -> list[sqlite3.Row]:
        """Queued fills, best-ranked first (the scanner's order wins over the
        order in which you happened to click Track)."""
        with self.connect() as conn:
            return conn.execute(
                """SELECT t.* FROM trades t
                   LEFT JOIN signals s ON s.id = t.signal_id
                   WHERE t.status = 'suggested'
                   ORDER BY COALESCE(s.score, 0) DESC, t.id""").fetchall()

    def open_symbols(self) -> set[str]:
        return {row["symbol"] for row in self.open_trades()}

    def has_open_trade(self, symbol: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE symbol = ? AND status = 'open' LIMIT 1",
                (symbol,)).fetchone()
        return row is not None

    def trade_for_signal(self, signal_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM trades WHERE signal_id = ?
                   ORDER BY id DESC LIMIT 1""", (signal_id,)).fetchone()

    # ---------------------------------------------------------------- marks

    def upsert_mark(self, trade_id: int, day: Date | str, close: float,
                    stop_live: float, high_water: float,
                    unrealized_pnl: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO marks (trade_id, date, close, stop_live, high_water,
                                      unrealized_pnl)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(trade_id, date) DO UPDATE SET
                     close = excluded.close, stop_live = excluded.stop_live,
                     high_water = excluded.high_water,
                     unrealized_pnl = excluded.unrealized_pnl""",
                (trade_id, _iso(day), close, stop_live, high_water, unrealized_pnl))

    def latest_marks(self) -> dict[int, sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT m.* FROM marks m
                   JOIN (SELECT trade_id, MAX(date) AS d FROM marks GROUP BY trade_id) x
                     ON x.trade_id = m.trade_id AND x.d = m.date""").fetchall()
        return {row["trade_id"]: row for row in rows}

    def marks_for(self, trade_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM marks WHERE trade_id = ? ORDER BY date",
                (trade_id,)).fetchall()

    # --------------------------------------------------------------- equity

    def upsert_equity(self, day: Date | str, cash: float, positions_value: float,
                      nav: float, open_positions: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO equity (day, cash, positions_value, nav, open_positions)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(day) DO UPDATE SET
                     cash = excluded.cash, positions_value = excluded.positions_value,
                     nav = excluded.nav, open_positions = excluded.open_positions""",
                (_iso(day), cash, positions_value, nav, open_positions))

    def equity_series(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM equity ORDER BY day").fetchall()

    def equity_on(self, day: Date | str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM equity WHERE day = ?", (_iso(day),)).fetchone()

    # ----------------------------------------------------------------- days

    def upsert_days(self, days: Iterable[Date | str]) -> None:
        rows = [(_iso(d),) for d in days if d is not None]
        if not rows:
            return
        with self.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO days (day) VALUES (?)", rows)

    def trading_days(self, start: Date | str | None = None,
                     end: Date | str | None = None) -> list[str]:
        sql = "SELECT day FROM days"
        params: list[Any] = []
        clauses = []
        if start is not None:
            clauses.append("day >= ?")
            params.append(_iso(start))
        if end is not None:
            clauses.append("day <= ?")
            params.append(_iso(end))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self.connect() as conn:
            return [r["day"] for r in conn.execute(sql + " ORDER BY day", params)]

    def previous_day(self, day: Date | str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(day) AS d FROM days WHERE day < ?",
                               (_iso(day),)).fetchone()
        return row["d"] if row else None

    def next_day(self, day: Date | str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MIN(day) AS d FROM days WHERE day > ?",
                               (_iso(day),)).fetchone()
        return row["d"] if row else None

    def last_day(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(day) AS d FROM days").fetchone()
        return row["d"] if row else None

    # ------------------------------------------------------------ sync state

    def set_sync_state(self, symbol: str, last_date: Date | str | None,
                       bars: int = 0, status: str = "ok",
                       error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO sync_state (symbol, last_date, bars, status, error, checked_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                     last_date = excluded.last_date, bars = excluded.bars,
                     status = excluded.status, error = excluded.error,
                     checked_at = excluded.checked_at""",
                (symbol, _iso(last_date), bars, status, error, _now()))

    def sync_state(self, symbol: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sync_state WHERE symbol = ?", (symbol,)).fetchone()

    def sync_states(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sync_state ORDER BY symbol").fetchall()

    def flagged_symbols(self) -> set[str]:
        """Symbols that must never emit signals (suspicious history)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol FROM sync_state WHERE status IN ('suspicious','failed')"
            ).fetchall()
        return {row["symbol"] for row in rows}

    def start_sync_log(self) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sync_log (started_at, status) VALUES (?, 'running')",
                (_now(),))
            return int(cur.lastrowid)

    def finish_sync_log(self, log_id: int, **fields: Any) -> None:
        fields.setdefault("finished_at", _now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE sync_log SET {cols} WHERE id = ?",
                         (*fields.values(), log_id))

    def last_sync_log(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()

    def last_finished_sync_log(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM sync_log WHERE finished_at IS NOT NULL
                   ORDER BY id DESC LIMIT 1""").fetchone()

    def reap_sync_logs(self) -> int:
        """Mark runs orphaned by a crash/kill as interrupted."""
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE sync_log SET status = 'interrupted', finished_at = ?
                   WHERE status = 'running'""", (_now(),))
        return cur.rowcount

    # ------------------------------------------------------------ app state

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO app_state (key, value) VALUES (?,?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, "" if value is None else str(value)))

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_state(key)
        if raw is None or raw == "":
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_state(key, json.dumps(value))
