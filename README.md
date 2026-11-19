# Swing Trading Agent (STA)

Research system for momentum swing trading on Indian mid-cap, small-cap, and
micro-cap equities (NSE + BSE), plus a local paper-trading desk that turns the
validated strategy into a daily workflow.

**Status: research + paper trading only — no broker integration, no order
execution.** The app binds to `127.0.0.1` and assumes one trusted user.

Position: ₹15,000 per slot, 5 concurrent slots (₹75,000 book — the validated
backtests used 4), 10% initial stop, 3×ATR(22) chandelier trailing stop, winners
run.

## Quick start

```bash
pip install -e ".[dev]"

python -m sta universe                     # market-cap scan -> data/universe/membership.parquet
python -m sta download                     # max-history parquet per universe symbol

python -m sta sync                         # incremental daily refresh (minutes)
python -m sta scan --commit                # today's suggestions -> data/app.db
python -m sta track                        # fill/exit/replay paper trades
python -m sta.web                          # http://127.0.0.1:8000
```

`python -m sta.web` is the same as `python -m sta web`.

## Daily workflow

1. Open <http://127.0.0.1:8000>.
2. Check the freshness strip. Amber means the cache is behind — press **Sync now**
   (or fix the scheduler, see below).
3. On **Today**, review the suggestions: score, close, quantity for one slot,
   initial stop, rupee risk, distance to the 52-week high.
4. Press **Track** on the ones you would take, **Skip** on the rest. With
   auto-track on, every suggestion is queued automatically.
5. After the next session's data lands, **Tracker** shows fills, live stops,
   high-water marks and unrealised P&L; **Performance** shows the equity curve,
   yearly P&L and why each trade ended.
6. Record the real fill price in your broker; the app tracks paper prices only.

The tracker replays sessions in order, so a laptop that was off for three days
produces the same state as three daily runs.

## Scheduling (Windows Task Scheduler)

Register once, from the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
```

That creates:

- **STA Daily** - `scripts\run_daily.ps1` at 18:30: `sync --catch-up` →
  `scan --commit` → `track --catch-up` → `backup`, logging to
  `data/logs/daily_YYYYMMDD.log`.
- **STA Catch-up** - `scripts\run_catchup.ps1` at logon: the same sequence,
  skipping work that is already done. An ONLOGON trigger needs an elevated
  PowerShell on current Windows builds; the script reports it if it cannot
  register.
- **STA Web** - `scripts\run_web.ps1` at logon, installed as a launcher in the
  current user's Startup folder (`scripts\install_startup.ps1`, no elevation
  needed). The supervisor restarts the app with a bounded backoff and refuses
  to start a second copy on the same port.

The app is then always at <http://127.0.0.1:8000>. Stop it with
`Get-NetTCPConnection -LocalPort 8000 | Stop-Process`; the supervisor will bring
it back, so remove the launcher first if you want it down:
`scripts\install_startup.ps1 -Remove`.

Equivalent manual commands:

```powershell
schtasks /Create /SC DAILY /ST 18:30 /TN "STA Daily" /TR "powershell -ExecutionPolicy Bypass -File <repo>\scripts\run_daily.ps1"
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
```

Remove everything with `scripts\register_tasks.ps1 -Remove`. The app never
registers tasks itself; it also runs a catch-up sync on startup when the data is
genuinely stale (disable with `STA_AUTOSYNC=0`, or write `1` to the
`app.autosync_disabled` key in `data/app.db`).

`data/.sync.lock` is an exclusive lock so the scheduler, the CLI and the app can
never sync at the same time. A killed run can leave it behind; **Data** offers a
**Clear stale lock** button once it is older than 15 minutes.

## Pages

| Page | What it answers |
|---|---|
| `/` Today | What should I do with the latest session? Freshness, NAV, suggestions, Track/Skip, Run scan |
| `/tracker` | What is open, how is it doing, and how did past trades end? Filters + CSV export |
| `/performance` | Equity curve, yearly P&L, win rate, profit factor, expectancy, exit-reason mix |
| `/data` | Last sync, coverage, stale/suspicious/failed symbols, lock state, Sync now |
| `/settings` | Position size, slots, suggestions per session, auto-track |

The tracker only replays sessions the cache can support (the coverage day, else
the newest synced bar). A position without a bar for a session is valued at its
last recorded close, never at cost, and the row shows the mark date. If an older
build ever moved the cursor past a usable session, `Tracker.reconcile()` drops
the unsupported equity and mark rows, rolls the cursor back, and recomputes
`days_held`; it runs automatically at the start of every replay.

Long jobs (sync, scan, replay) run in a background thread; the banner polls
`/partials/job` and reloads the page when the job finishes.

## Layout

```
src/sta/core/       domain models, interfaces (Protocols), config, market calendar - no I/O
src/sta/data/       yfinance fetcher, universe builder, incremental sync, cross-listing
                    gap repair, store shim
src/sta/scanner/    per-symbol momentum features + cross-sectional scanner
src/sta/backtest/   cost model, event engine, runner
src/sta/live/       rules (single source of truth), signals, tracker, SQLite, ops
src/sta/web/        FastAPI app, Jinja templates, vendored HTMX/Chart.js/fonts
scripts/            run_daily.ps1, run_catchup.ps1, register_tasks.ps1
config/             settings.toml (all behavior knobs)
```

### One source of truth for rules

`src/sta/live/rules.py` owns every stop and exit decision. The backtest engine
and the paper tracker both import it, so research and live paper trading cannot
drift. `tests/test_parity.py` replays identical histories through both and
compares every closed trade (symbol, dates, prices, reason, shares, P&L);
`tests/test_golden.py` does the same on the real cache over the last 12 months.

## Data notes

- Source: yfinance `Ticker.history(period="max", interval="1d",
  auto_adjust=False)` per ticker, cached as parquet in `data/daily/TICKER.parquet`.
- **Incremental sync** re-fetches the last 7 stored bars plus the missing tail,
  dedupes by date, and compares the overlapping closes. A >0.5% difference means
  Yahoo rewrote history (split/bonus) → that symbol is fully refreshed.
- **Batched fetch**: ~50 symbols per `yf.download` call, bisected on failure so
  one bad ticker cannot fail a chunk, with a per-ticker fallback.
- **NSE fallback**: ~300 BSE tickers have a broken daily feed on Yahoo. Their
  history came from the company's `.NS` listing; `data/universe/ns_fallbacks.csv`
  records the substitution and the sync keeps fetching the same source, so
  prices do not flip between exchanges.
- **Calendar guard**: the expected session comes from the exchange calendar
  (`src/sta/core/market_calendar.py`, weekends plus `config/holidays.txt` with
  the declared 2026 BSE/NSE holidays and the 16:00 IST cut-off). The sync still
  derives the session the universe actually reached, and symbols more than 3
  trading days behind are retried once, then listed as stale in
  `data/universe/sync_failures.csv` and on `/data`.
- **Source lag is not staleness**: when a successful sync already ran after the
  expected close and upstream still has no newer bar, the app reports
  `source_lag`, explains it, and does not start another automatic sync. The next
  attempt happens after the following session closes, or when you press
  **Sync now**. This is what stops a missing upstream bar from turning into a
  20 minute catch-up on every launch.
- **BSE session gaps**: Yahoo's `.BO` feed sometimes skips one session for most
  of the universe while the `.NS` listing has it. `src/sta/data/crosslist.py`
  fills just that date in batches of 50, and only after validating the two
  listings against each other (at least 3 common closes, median close ratio
  within 3%, median daily-return difference within 2%, and no implied >35%
  one-day move). Stored bars are never rewritten. Disable with
  `[download] gap_fill = false`.
- **Quality gate**: a >35% one-day move that the split factor
  (`Adj_Close / Close`) does not explain is flagged. Flags in the last 90 bars
  block that symbol from emitting signals. Stub responses never overwrite a
  multi-year history.
- **Atomic writes**: temp file + replace, so a killed sync cannot corrupt a
  symbol's history.
- Caveat: yfinance history has survivorship bias — delisted tickers are absent.

## Strategy (validated)

Default variant is **trailing**: the Phase-1 momentum scanner (52-week-high
proximity, top-15% 3-month RS, 2× volume surge, rising MA structure) with a
**3×ATR(22) chandelier trailing stop** in place of a fixed +30% target. The 10%
hard stop stays until the trailing stop ratchets above it.

Full universe (4,133 symbols), 2020-06 → 2026-10, net of costs:

| Variant | Sharpe | CAGR | Max DD |
|---|---|---|---|
| Baseline (+30% target) | 2.03 | 31.0% | -21.6% |
| Trailing 3xATR22 | **4.90** | **59.5%** | **-13.0%** |

Gate-flavor experiments (KAMA, RMA, RMA finetuned) were rejected: none survived
out-of-sample split validation against trailing-only. Conclusion: exits carry
the edge; MA flavor does not.

Exit reasons the tracker records: `stop_initial`, `stop_trailing`,
`stop_profit_lock`, `stop_failed_breakout`, `stop_atr_collapsed`, `time`,
`target`, `trend`.

Caveat: fills are idealized (stops at the stop price, targets at the target) and
history is survivorship-biased, so absolute Sharpe is optimistic. Relative
ordering has been stable across every slice tested.

## Backtest realism

- Signals on the day-D close → entries at the D+1 open.
- Stops fill at the stop price, or at the open when gapped through.
- Lower-circuit lock days cannot be exited (the position freezes).
- No same-day re-entry into a symbol that exited that session.
- Time stop at 40 sessions, only when the trade is not in profit.
- Costs: STT 0.1% both sides (delivery), statutory charges, ₹20/order, GST —
  see `backtest/costs.py`.

## Testing

```bash
python -m pytest tests -q                    # fast, offline, no network
$env:STA_GOLDEN = "1"; python -m pytest tests/test_golden.py -q   # real cache
```

The suite covers rule invariants (monotonic stops, profit floors, time-stop
suppression, same-day re-entry), engine/tracker parity, sync idempotency with a
mocked upstream, SQLite CRUD, and the web app via `TestClient`. The golden test
is opt-in because it reads real data.

## Configuration

`config/settings.toml` holds defaults for download (including `gap_fill`),
universe, `[live]` (position size, slots, auto-track, suggestions) and
`[schedule]` (time, timezone). Runtime changes made on `/settings` are stored in
`data/app.db` (`app_state`) and win over the file. `config/holidays.txt` is the
exchange holiday list; add a line when an exchange announces a change.

```bash
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant trailing
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant baseline
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant trend_exit
```

All commands accept `-v` for debug logging and `--config` for an alternate
`settings.toml` (flags may appear before or after the subcommand).
