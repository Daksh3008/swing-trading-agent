# STA — Task List (data pipeline rebuild)

## Context
- Old bhavcopy pipeline deleted; new yfinance per-ticker parquet pipeline in place.
- Yahoo purged ~2,400 live BSE tickers from quoteSummary (metadata) but still
  serves their history on the chart endpoint (verified: 360ONE, 5PAISA, 7NR
  current to 2026-11-13). Current universe (1,996) is undercounted.
- Yahoo rate-limited as of 15:55 — cooldown observed before Phase A.

## Phase A — Complete the universe (mcap <= Rs 50,000 cr)
- [x] Test `v7/finance/quote` batch endpoint — RETIRED by Yahoo (401), unusable
- [x] Confirmed `fast_info.market_cap` (shares x last price) works for the
      "purged" tickers (360ONE Rs 50,436 cr, 5PAISA Rs 1,658 cr, 20MICRONS Rs 648 cr)
- [x] Switched market-cap path to fast_info-first; dead detection via
      `currentTradingPeriod`/`delisted`/404
- [x] Full rescan at 6+4 workers -> 4,331 ok, 85 dead, 304 transient
- [x] Universe rebuilt: 4,133 symbols (3,572 micro / 451 small / 110 mid)
- [x] Sanity checks pass (ABFRL/ASTRAZEN/5PAISA/20MICRONS in; 360ONE at
      Rs 50,436 cr correctly out; RELIANCE/TCS/HDFCBANK out)
- [ ] Optional: one more scan pass later to resolve the 304 transient stragglers
- [ ] Rebuild `membership.parquet`; verify ~4,200+ symbols; sanity-check
      (ABFRL/ASTRAZEN in, RELIANCE/TCS out; 360ONE/5PAISA in)
- [ ] Mark truly-dead tickers (`MIRCELECTR.BO` class) as `dead` in cache

## Phase B — Download max history (parquet)
- [x] `python -m sta download` for full universe — chart endpoint, resumable
- [x] 4,133 symbols downloaded; stub-guard (<30 rows) + retry passes working
- [x] Discovery: Yahoo's BSE daily feed is broken for ~300 symbols (returns
      only the latest bar or monthly aggregates). Added NSE-listing fallback
      to `fetch.py` (TICKER.BO -> TICKER.NS, same company), substitutions
      logged to `data/universe/ns_fallbacks.csv`.
- [x] Fixed stub-cache bug: download_ticker now re-fetches stub caches itself
- [x] Final repair pass for the ~300 BSE-broken symbols -> 303 fixed via NSE
- [x] Verified: 4,133 files, 16.99M rows, 1991 -> 2026-11-13; median 4,657
      rows/symbol; 7 stubs left (0.17%: CIGNITITEC, GSPL, HINDMOTORS,
      JBCHEPHARM, SANGHIIND, SNLFPP - will retry on next download run)
- [x] Spot-checks vs reference CSVs pass (ABFRL, ASTRAZEN, 5PAISA, 7NR,
      ZUARI, SUMMITSEC, ACC, 20MICRONS)
- [x] 303 NSE substitutions recorded in `data/universe/ns_fallbacks.csv`

## Phase C — Strategy experiments (walk-forward, net of costs)
- [x] Engine v2 (2026-11-14): richer exit reasons (`stop_initial`,
      `stop_trailing`, `stop_profit_lock`, `stop_failed_breakout`,
      `stop_atr_collapsed`, `time`, `target`, `trend`), winners-run time
      stop (only exits at 40d when NOT in profit), profit protection
      (>=10% max profit -> stop floored at +5%; >14% -> chandelier governs,
      monotonic), strict no-lookahead (stop from prior-day high-water and
      prior-day ATR; high-water updated after the exit check).
      Trade log now carries stop_at_exit / high_water / atr_at_exit.
- [ ] Rerun walk-forward with engine v2: data/backtest_v2_full (2005-2026)
      and data/backtest_v2_2015 (2015-2026) — running
- [ ] Export v2 CSVs to data/analysis/v2_full_history and v2_clean_2015
- [x] Add features: `slope_50`, `slope_150` (pct over 20d), `accel =
      slope_50 - slope_150` (computed, not gated) — tests pass
- [x] Variant A: KAMA trend gate (KAMA 10/30, same shape as SMA gate) —
      `--variant kama`
- [x] Variant B: chandelier trailing stop (3xATR(22), RMA/EWM-smoothed)
      replacing fixed +30% target — `--variant trailing` (optionally `_kama`)
- [x] Variant C: MA20 trend exit replacing time stop — `--variant trend_exit`
- [x] Adapted runner/scanner/store to yfinance schema (spans synthesized from
      history coverage; lowercase bars; derived value/prev_close)
- [x] Fixed two engine bugs found by validation: tz shift (18:30 index vs
      midnight lookups) and ATR/MA series looked up with the composite
      position key instead of the plain symbol (trailing stop + MA exit
      were silently disabled)
- [x] 400-symbol, 2020-2026 results (net of costs, after bug fixes):
      baseline: Sharpe 2.78 / CAGR 33.9% / DD -15.4% / exp +5.9%
      kama gate: 1.28 / 19.8% / -25.6% / +2.0%   (worse)
      trailing 3xATR22: 4.78 / 53.7% / -8.9% / +14.7%   (WINNER)
      trend exit MA20: 3.32 / 40.4% / -12.5% / +6.0%
      trailing+kama: 3.80 / 41.3% / -10.1% / +6.8%
      -> KAMA gate consistently hurts; ATR trailing stop is the real edge
- [ ] Run baseline + trailing on the full 4,133-symbol universe
      (memory risk: scanner builds all features in memory; chunk if OOM)
- [x] Implemented RMA (Wilder) gate: `rma()` + `rma_{n}`/`rma_{n}_up`
      columns for the full sweep (15..150), configurable scanner gate
      (`trend_mode="rma"`, `rma_fast`/`rma_slow`), CLI `--variant rma`
      and `--variant trailing_rma --rma-fast --rma-slow`; 3 new tests
- [x] Full-universe (4,133 symbols) confirmation, 2020-2026, net of costs:
      baseline Sharpe 2.03 / CAGR 31.0% / DD -21.6% / exp +4.5%
      trailing 3xATR22 Sharpe 4.90 / CAGR 59.5% / DD -13.0% / exp +15.9%
      -> trailing edge confirmed and widened vs the 400-symbol sample
- [ ] Stage 1: RMA gate alone — configs (25,75), (50,150), (20,60)
- [x] Stage 1: RMA gate alone (400 sym, 2020-26) — (20,60) Sharpe 2.64;
      (25,75) 2.77; (50,150) 2.88 vs SMA baseline 2.78; KAMA 1.28.
      Heavier smoothing wins; sweep upward from 50/150.
- [ ] Stage 2: RMA finetune grid fast 40/50/60 x slow 120/150/180 (running)
- [ ] Split-sample validate top-2 (2020-22 select -> 2023-26 check)
- [x] Stage 2: RMA finetune grid (400 sym, 2020-26) — longer fast RMA wins:
      60/150 Sharpe 3.25 (best), 60/120 3.11, 50/180 3.10; all above
      baseline 2.78. Top-2 = (60,150) and (60,120).
- [x] Stage 3 (400 sym, full period): trailing+RMA(60,120) Sharpe 5.13 /
      CAGR 53.6% / DD -8.4% vs trailing-only 4.78 / 53.7% / -8.9%;
      trailing+RMA(60,150) 4.35 (worse). Inconsistent winners -> caution.
- [x] Split-sample RMA standalone: (60,150) 3.80 -> 2.53; (60,120) 3.53 -> 2.56
- [x] Split-sample decision: trailing-only 2020-22 Sharpe 5.68 / 2023-26 6.06;
      trailing+RMA(60,120) 6.36 / 5.09. RMA wins in-sample, LOSES OOS ->
      REJECTED. Conclusion: exits carry the edge; gate flavor (SMA/KAMA/RMA)
      does not matter durably.
- [x] CLI default variant set to `trailing` (full-universe validated: Sharpe
      4.90, CAGR 59.5%, DD -13.0% vs baseline 2.03/31.0%/-21.6%)
- [x] Full-history walk-forward (2005-2026, 4,133 symbols, trailing): 1,966
      trades, PF 2.59, exp +8.4%, but 2005-2014 contaminated by bad Yahoo
      BSE bars (11,104 suspicious 1-day crashes >35%, 7,898 of them in
      2007-08; max DD -63.7% was a single-day artifact 2005-07-28/29)
- [ ] Clean-era walk-forward 2015-2026 (running) -> publish yearly table
- [ ] Candidate hardening: drop bars with |1d ret|>35% lacking a split
      factor before backtesting, or use Adj_Close split factors
- [ ] Full-universe confirmation of the overall winner
- [ ] Consider making trailing the CLI default after full-universe validation
- [ ] KAMA loop is slow (~1.5 min/120 symbols) — optimize if full-universe
      runs are too slow (numba or reduced gate usage)
- [ ] Note survivorship-bias caveat in every published result

---

# Phase D — Daily signals UI, trade tracker, and data scheduler

**Goal:** a local web app that shows today's trade suggestions from the validated
trailing engine, tracks every suggestion day by day (paper trading), and keeps
the price data current — daily after close, and automatically catching up when
the laptop is started after missed days.

## Locked decisions

- **Stack:** FastAPI + Jinja2 + HTMX + SQLite, served at `127.0.0.1:8000`,
  local-only (no auth). Reuses the existing scanner/engine; server-rendered
  pages; state in one SQLite file (`data/app.db`). No JS build step.
- **Single source of truth for rules:** extract stop/exit logic from
  `backtest/engine.py` into `src/sta/live/rules.py`; the engine and the live
  tracker both import it. A parity test gates the tracker.
- **Paper tracking only:** suggested entry price recorded; optional manual
  override for the actual fill. No broker integration in v2.
- **Scheduling:** Windows Task Scheduler (daily 18:30 IST) + "At logon"
  catch-up task; plus an in-app startup sync as belt-and-braces. File lock
  prevents concurrent runs.
- **Data updates:** incremental fetch with an overlap check; any mismatch
  (split/bonus retro-adjustment) triggers a full per-symbol refresh.

## D1 — Data sync engine (foundation)

- [ ] `src/sta/data/sync.py`: incremental update against a `sync_state` table.
      How: read each universe symbol's last stored date; fetch the overlap
      window (last 7 stored bars) + missing tail (`history(start=..., end=...)`);
      concat + dedupe by date; compare overlapping closes — if any differ
      >0.5%, do a full refresh (`period="max"`) for that symbol (Yahoo rewrites
      history after splits/bonuses).
- [ ] Batch path: `yf.download` in chunks of ~50 symbols (threaded) with
      per-ticker fallback, so a 4,133-symbol daily refresh runs ~10–20 min.
- [ ] Calendar guard: expected last trading day = max date across a sample of
      20 symbols; symbols lagging >3 trading days are stale → retry once, then
      log to `data/universe/sync_failures.csv`.
- [ ] Data-quality gate: flag bars with |1-day move| >35% and no
      split-factor change; flagged symbols never emit signals.
- [ ] CLI `python -m sta sync [--full] [--workers N]`; writes a `sync_log` row
      (counts, duration, status).
- Acceptance: simulate a 3-day gap, run sync, all symbols reach the latest
  trading day; re-running immediately changes nothing (idempotent).

## D2 — Rules extraction + parity (gates D4)

- [ ] `src/sta/live/rules.py`: pure functions — `initial_stop`,
      `update_trailing_stop` (prior-day high-water + prior-day ATR, monotonic,
      +5% floor after 10%, chandelier above 14%), `classify_stop`,
      `exit_signal`, `atr_series(window).shift(1)`.
- [ ] Refactor `backtest/engine.py` to import these; all existing tests green.
- [ ] Parity test: replay the last 250 trading days via BacktestEngine and via
      the live tracker on identical histories → identical trades (symbol,
      entry/exit dates and prices, reason).
- Acceptance: parity test passes exactly; a drift fails the build.

## D3 — Database + signal snapshots

- [ ] `src/sta/live/db.py` (stdlib sqlite3): tables
      `signals(id, signal_date, symbol, score, close, cap_class, entry_plan,
      stop_initial, created_at, UNIQUE(signal_date,symbol))`,
      `trades(id, signal_id, symbol, status, entry_date, entry_price, shares,
      stop_initial, stop_live, high_water, exit_date, exit_price,
      exit_reason, pnl, created_at, updated_at)`,
      `marks(trade_id, date, close, stop_live, high_water,
      unrealized_pnl, PRIMARY KEY(trade_id,date))`,
      `sync_log(...)`, `app_state(key,value)`.
- [ ] `src/sta/live/signals.py`: run MomentumScanner on the latest trading day,
      persist top-N with qty = floor(15000/price), stop_initial, risk ₹,
      distance-to-52w-high, cap class.
- [ ] `auto_track` setting: suggestions become `open` trades automatically at
      the next session's open (paper); otherwise the user clicks "Track".
- Acceptance: running the scan twice on the same date is idempotent.

## D4 — Tracker engine

- [ ] `src/sta/live/tracker.py` — `update_day(day)`:
      1. entries: fill `suggested` trades at that day's open (paper);
         expire unfilled suggestions after 1 trading day;
      2. open trades: ratchet stop from prior-day data (rules module), test
         exit against the day's bar, close with reason/price/costs
         (`CostModel`), persist;
      3. marks: write daily close, stop_live, high_water, unrealized P&L.
- [ ] Enforce max 4 concurrent slots, ₹15k sizing, and the v2 fix:
      no same-day re-entry into a symbol that exited that day.
- [ ] Catch-up replay: process missed days in order so a 3-day laptop gap
      produces the same state as daily runs.
- Acceptance: tracker over 2020–2026 reproduces the backtest summary within
  0.1%; order of replay (skipped days vs daily) produces identical results.

## D5 — Web app

- [ ] `src/sta/web/app.py` + Jinja templates + minimal CSS; `python -m sta.web`.
- [ ] Pages:
      `/` Today — data-freshness banner, signals table (score, close, qty for
      ₹15k, stop, risk, distance to high), Track/Skip buttons, "Run scan now".
      `/tracker` — open positions (current, unrealized %, stop_live,
      high-water, days held, exit badge) + closed trades (filters: year,
      reason, win/loss; CSV export).
      `/performance` — equity curve, yearly P&L bars, win rate / PF /
      expectancy, exit-reason breakdown (Chart.js vendored locally).
      `/data` — sync status, last run, failures, stale symbols, "Sync now".
      `/settings` — position size, slots, auto-track, schedule display.
- [ ] Long jobs (sync, scan) run in a background thread; UI polls `/api/jobs`
      for status. App startup kicks a catch-up sync if stale.
- Acceptance: cold start shows current state; UI usable offline (localhost).

## D6 — Scheduling (daily + laptop-start catch-up)

- [ ] `scripts/run_daily.ps1`: `sta sync` → `sta scan --commit` → `sta track`,
      logging to `data/logs/daily_YYYYMMDD.log`.
- [ ] Register once (documented, not auto-run):
      `schtasks /Create /SC DAILY /ST 18:30 /TN "STA Daily" /TR
      "powershell -ExecutionPolicy Bypass -File <repo>\scripts\run_daily.ps1"`.
- [ ] "At logon" task runs `sta sync --catch-up` (skips if the daily run
      already covered today).
- [ ] Locking: `data/.sync.lock` so app-start and scheduled runs never overlap;
      atomic parquet writes (temp file + replace) so a killed sync cannot
      corrupt history.
- Acceptance: shut the laptop for 3 days → on startup data fills the gap and
  the tracker replays the missed days; state matches continuous operation.

## D7 — Hardening & ops

- [ ] Nightly `data/app.db` backup to `data/backups/` (keep 14).
- [ ] Alerts (log + desktop toast) when a sync fails >10% of symbols or an open
      position cannot be marked for >5 trading days.
- [ ] Rotating logs in `data/logs/`.
- [ ] `config/settings.toml`: new `[live]` (position_size, max_positions,
      auto_track, scan_top_n) and `[schedule]` (time, timezone) sections.
- [ ] README: "Daily workflow" (open app → check data banner → review signals →
  track fills) and how to enable/disable the scheduled tasks.

## D8 — Tests

- [ ] Unit: rules parity, stop monotonicity, profit floors, time-stop
      suppression, same-day re-entry block.
- [ ] Integration: SQLite CRUD, sync idempotency (mocked yfinance), tracker
      replay on fixtures, API smoke tests via TestClient.
- [ ] Golden: last 12 months backtest vs tracker → identical closed trades.

## Dependencies to add

`fastapi`, `uvicorn`, `jinja2`, `python-multipart` (forms). SQLite is stdlib;
Chart.js vendored in `static/`. Scheduler uses Windows Task Scheduler (no
APScheduler, to avoid duplicate schedulers).

## Out of scope (v2.1+)

Broker integration / order placement, multi-user/auth, cloud hosting, intraday
signals, LLM/news layer, parameter re-optimization UI.

## Sequencing and estimate

D1 (1–2d) → D2 (0.5d) → D3 (0.5d) → D4 (1d) → D5 (2–3d) → D6 (0.5d) →
D7 (0.5d), with D8 written alongside each step. D2's parity test gates D4;
D1's idempotency test gates D6.
