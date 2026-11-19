"""CLI entry point: ``python -m sta <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .core.config import load_settings
from .data.pipeline import DataPipeline


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sta", description="Swing Trading Agent")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--config", default=None, help="path to settings.toml")

    sub = parser.add_subparsers(dest="command", required=True)

    uni = sub.add_parser("universe", parents=[common],
                         help="market-cap scan -> data/universe/membership.parquet")
    uni.add_argument("--workers", type=int, default=6,
                     help="parallel market-cap fetchers (lower = gentler on Yahoo)")

    dl = sub.add_parser("download", parents=[common],
                        help="download max-history parquet for universe symbols")
    dl.add_argument("--all", action="store_true",
                    help="download every ticker in Equity.csv, not just the cap-filtered universe")
    dl.add_argument("--workers", type=int, default=None, help="parallel fetchers")
    dl.add_argument("--limit", type=int, default=None, help="cap symbols (test)")
    dl.add_argument("--force", action="store_true", help="re-download cached tickers")

    bt = sub.add_parser("backtest", parents=[common],
                        help="run the momentum strategy backtest")
    bt.add_argument("--start", required=True, help="YYYY-MM-DD")
    bt.add_argument("--end", required=True, help="YYYY-MM-DD")
    bt.add_argument("--limit", type=int, default=None,
                    help="cap symbols (smoke test)")
    bt.add_argument("--variant", default="trailing",
                    choices=["baseline", "kama", "rma", "trailing",
                             "trailing_kama", "trailing_rma", "trend_exit"],
                    help="strategy variant to run")
    bt.add_argument("--rma-fast", type=int, default=25,
                    help="fast RMA length for --variant rma/trailing_rma")
    bt.add_argument("--rma-slow", type=int, default=75,
                    help="slow RMA length for --variant rma/trailing_rma")

    sy = sub.add_parser("sync", parents=[common],
                        help="incremental data refresh (parquet cache + app.db)")
    sy.add_argument("--full", action="store_true",
                    help="refetch every symbol from 1991 (slow, use after a gap)")
    sy.add_argument("--workers", type=int, default=None, help="batch workers")
    sy.add_argument("--limit", type=int, default=None, help="cap symbols (test)")
    sy.add_argument("--catch-up", action="store_true",
                    help="skip when the last successful sync already covered today")

    sc = sub.add_parser("scan", parents=[common],
                        help="scan the latest session for trade suggestions")
    sc.add_argument("--day", default=None, help="YYYY-MM-DD (default: latest)")
    sc.add_argument("--top", type=int, default=None, help="suggestions to keep")
    sc.add_argument("--commit", action="store_true",
                    help="persist suggestions (otherwise preview only)")

    tr = sub.add_parser("track", parents=[common],
                        help="advance the paper-trade tracker")
    tr.add_argument("--day", default=None, help="YYYY-MM-DD (default: latest)")
    tr.add_argument("--catch-up", action="store_true",
                    help="replay every missed session in order")

    web = sub.add_parser("web", parents=[common], help="serve the local app")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--host", default="127.0.0.1")

    sub.add_parser("backup", parents=[common],
                   help="copy data/app.db into data/backups/ (keeps 14)")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    settings = load_settings(Path(args.config) if args.config else None)
    settings.ensure_dirs()
    from .live.ops import setup_file_logging
    setup_file_logging(settings)

    if args.command == "backup":
        from .live.ops import backup_db
        target = backup_db(settings)
        print(f"backup: {target or 'nothing to back up'}")
        return 0

    if args.command in ("sync", "scan", "track"):
        from .live.db import Database
        db = Database(settings.db_path).init()

    if args.command == "sync":
        from .data.sync import DataSync
        from .live.ops import check_sync_health
        result = DataSync(settings, db,
                          workers=args.workers or settings.download.workers)
        outcome = result.run(full=args.full, limit=args.limit,
                             catch_up=args.catch_up)
        check_sync_health(settings, outcome)
        print(json.dumps(outcome.as_dict(), indent=2, default=str))
        return 0 if outcome.status in ("ok", "skipped") else 1

    if args.command == "scan":
        from .live.settings import load_live_settings
        from .live.signals import run_scan
        outcome = run_scan(settings, db, load_live_settings(settings, db),
                           day=args.day, top_n=args.top, commit=args.commit)
        if args.commit:
            print(json.dumps(outcome.as_dict(), indent=2, default=str))
        else:
            for row in outcome.candidates:
                print(f"{row['symbol']:<14} {row['score']:.3f} "
                      f"close {row['close']:>9.2f}  qty {row['qty']:>4}  "
                      f"stop {row['stop_initial']:>8.2f}  {row['cap_class']}")
            print(json.dumps(outcome.as_dict(), indent=2, default=str))
        return 0

    if args.command == "track":
        from .live.tracker import Tracker
        tracker = Tracker(settings, db)
        if args.catch_up:
            for day in tracker.catch_up(args.day):
                print(json.dumps(day.as_dict(), default=str))
        else:
            day = args.day or db.get_state("sync.last_day") or db.last_day()
            if day is None:
                parser.error("no trading day known — run `python -m sta sync` first")
            print(json.dumps(tracker.update_day(day).as_dict(), default=str))
        return 0

    if args.command == "web":
        from .web.app import serve
        serve(settings, host=args.host, port=args.port)
        return 0

    pipeline = DataPipeline(settings)

    if args.command == "universe":
        membership = pipeline.build_universe(workers=args.workers)
        print(f"universe: {len(membership)} symbols "
              f"({membership['cap_class'].value_counts().to_dict()})")
        return 0
    if args.command == "download":
        if args.all:
            from .data.universe import load_tickers
            symbols = load_tickers(settings.root / settings.universe.tickers_file)
        else:
            membership_path = settings.universe_dir / "membership.parquet"
            if not membership_path.exists():
                parser.error("run `python -m sta universe` first (or use --all)")
            symbols = pd_read_symbols(membership_path)
        if args.limit:
            symbols = symbols[:args.limit]
        logging.getLogger("sta.cli").info("downloading %d tickers", len(symbols))
        stats = pipeline.download(symbols, workers=args.workers or settings.download.workers,
                                  force=args.force or settings.download.force)
        print(f"download done: {stats}")
        return 0
    if args.command == "backtest":
        from .backtest.engine import StrategyParams
        from .backtest.runner import run_backtest
        from .scanner.momentum import ScannerParams
        is_trailing = args.variant in ("trailing", "trailing_kama", "trailing_rma")
        if args.variant.endswith("kama"):
            trend_mode = "kama"
        elif args.variant.endswith("rma"):
            trend_mode = "rma"
        else:
            trend_mode = "sma"
        strategy = StrategyParams(
            trailing=is_trailing,
            ma_exit_window=(20 if args.variant == "trend_exit" else None))
        scanner = ScannerParams(trend_mode=trend_mode,
                                rma_fast=args.rma_fast,
                                rma_slow=args.rma_slow)
        tag = args.variant
        if trend_mode == "rma":
            tag += f"_rma{args.rma_fast}_{args.rma_slow}"
        summary = run_backtest(settings, args.start, args.end,
                               strategy=strategy, scanner_params=scanner,
                               symbols_limit=args.limit,
                               out_dir=f"backtest_{tag}")
        print(json.dumps(summary, indent=2, default=str))
        return 0
    parser.error(f"unknown command {args.command}")
    return 1


def pd_read_symbols(path: Path) -> list[str]:
    import pandas as pd
    return sorted(pd.read_parquet(path)["symbol"].astype(str))


if __name__ == "__main__":
    raise SystemExit(main())
