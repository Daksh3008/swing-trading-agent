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

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    settings = load_settings(Path(args.config) if args.config else None)
    settings.ensure_dirs()
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
