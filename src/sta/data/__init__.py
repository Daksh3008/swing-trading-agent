"""Data layer: per-ticker yfinance history cached as parquet.

Replicates the ``updated_stock_finder`` pipeline (``src/fetch.py``) with two
changes: history is stored as parquet instead of CSV, and the ticker universe
is filtered by market cap.
"""
