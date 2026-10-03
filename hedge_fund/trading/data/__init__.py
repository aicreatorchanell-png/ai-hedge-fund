"""Market data for the active-trading layer: sources -> quality checks -> Nautilus catalog.

    sources       binance.py (spot klines, data.binance.vision), dukascopy.py (FX, indices,
                  CFDs), ccxt_check.py (independent cross-check from other exchanges),
                  tiingo_daily.py (stocks/ETFs from the existing Tiingo cache)
    quality.py    gaps, duplicates, OHLC consistency, non-positive prices, extreme moves
    markets.py    instrument specs per market (precision, minimums, cost model)
    catalog.py    writes bars to a Nautilus ParquetDataCatalog in the private cache and
                  reads them back through the holdout fence

Bars are stamped at their close (ts_event = ts_init = open time + bar length): a bar
is known only once it has closed. Raw files and the catalog live under
hedge_fund.paths.CACHE_DIR (outside git); nothing here commits or uploads data.
Downloads stop before any sealed holdout window (configs/holdouts.yaml).
"""
