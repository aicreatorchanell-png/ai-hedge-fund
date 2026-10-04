"""Data layer tests: Binance parsing (ms/us), fence, checksums, quality, catalog, Dukascopy codec, costs."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

from hedge_fund.trading.data import binance, dukascopy
from hedge_fund.trading.data.catalog import Catalog, bar_type
from hedge_fund.trading.data.fence import last_research_day
from hedge_fund.trading.data.markets import CRYPTO, FX, MarketSpec, market
from hedge_fund.trading.data.quality import DataQualityError, quality_report, require_clean
from hedge_fund.validation.holdout_guard import HoldoutAccessDenied


def _zip(rows, name="X-1m-2024-01.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "\n".join(",".join(map(str, r)) for r in rows))
    return buf.getvalue()


def _row(t, o=100, h=101, lo=99, c=100.5, v=1.0):
    return [t, o, h, lo, c, v, t + 59_999, 100, 5, 0.5, 50, 0]


def test_parse_handles_millisecond_and_microsecond_timestamps(tmp_path):
    ms, us = tmp_path / "ms.zip", tmp_path / "us.zip"
    ms.write_bytes(_zip([_row(1733011200000), _row(1733011260000)]))
    us.write_bytes(_zip([_row(1735689600000000), _row(1735689660000000)]))
    a, b = binance.parse(ms), binance.parse(us)
    assert a.index[0] == pd.Timestamp("2024-12-01", tz="UTC") and b.index[0] == pd.Timestamp("2025-01-01", tz="UTC")
    assert list(a.columns) == ["open", "high", "low", "close", "volume", "trades"] and a["close"].iloc[0] == 100.5


def test_parse_skips_header_row(tmp_path):
    p = tmp_path / "h.zip"
    p.write_bytes(_zip([binance.COLUMNS, _row(1733011200000)]))
    assert len(binance.parse(p)) == 1


def test_months_and_fence():
    assert binance.months("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert last_research_day("crypto") == "2025-08-31"               # active-crypto-final, sealed 2025-09-01
    assert last_research_day("equity") == "2026-08-31"               # crypto scope does not fence stocks
    assert last_research_day("*") == "2025-08-31"
    assert binance.month_allowed("2025-08") and not binance.month_allowed("2025-09")
    with pytest.raises(PermissionError):
        binance.download_month("BTCUSDT", "2025-09")


def test_download_verifies_checksum(tmp_path):
    payload = _zip([_row(1733011200000)])

    class Resp:
        def __init__(self, content, status=200):
            self.content, self.status_code = content, status

        def raise_for_status(self):
            pass

    class Sess:
        def __init__(self, digest):
            self.digest = digest

        def get(self, url, timeout):
            return Resp(f"{self.digest}  f.zip".encode()) if url.endswith("CHECKSUM") else Resp(payload)

    good = hashlib.sha256(payload).hexdigest()
    assert binance.download_month("BTCUSDT", "2024-01", root=tmp_path, session=Sess(good)).exists()
    with pytest.raises(binance.DownloadError):
        binance.download_month("ETHUSDT", "2024-01", root=tmp_path, session=Sess("0" * 64), retries=1)


def _bars(n=10, start="2024-01-01"):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    c = 100 + np.arange(n, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c + 0.5, "volume": 1.0}, index=idx)


def test_quality_reports_gaps_and_rejects_bad_bars():
    df = _bars(10).drop(_bars(10).index[[3, 4]])
    rep = quality_report(df)
    assert rep["missing"] == 2 and rep["longest_gap_bars"] == 2 and rep["ok"]
    bad = _bars(5)
    bad.iloc[2, bad.columns.get_loc("high")] = 50.0
    with pytest.raises(DataQualityError):
        require_clean(bad)
    dup = pd.concat([_bars(3), _bars(1)])
    assert quality_report(dup.sort_index())["duplicates"] == 1


def test_catalog_round_trip_close_stamps_and_fence(tmp_path):
    cat = Catalog(tmp_path / "cat")
    inst = CRYPTO["BTCUSDT"].instrument()
    cat.write_instrument(inst)
    assert cat.write_bars(inst, _bars(10), "2024-01") == 10
    assert cat.write_bars(inst, _bars(10), "2024-01") == 0                  # ledger: idempotent
    bars = cat.load_bars("BTCUSDT.BINANCE", "2024-01-01", "2024-01-01")
    assert len(bars) == 10
    assert bars[0].ts_event == pd.Timestamp("2024-01-01 00:01", tz="UTC").value  # stamped at close
    assert str(bars[0].bar_type) == str(bar_type("BTCUSDT.BINANCE"))
    assert cat.summary()[str(bar_type("BTCUSDT.BINANCE"))]["rows"] == 10
    with pytest.raises(HoldoutAccessDenied):
        cat.load_bars("BTCUSDT.BINANCE", "2026-09-01", "2026-09-30")
    with pytest.raises(HoldoutAccessDenied):                                  # crypto final holdout
        cat.load_bars("BTCUSDT.BINANCE", "2025-08-31", "2025-09-01")
    with pytest.raises(HoldoutAccessDenied):                                  # unknown market: every holdout
        cat.load_bars("NOTLISTED.X", "2025-09-01", "2025-09-02")


def test_dukascopy_codec_round_trip_and_fence():
    day = date(2024, 1, 2)
    df = pd.DataFrame({"open": [1.10001, 1.10010], "high": [1.10020, 1.10030], "low": [1.09990, 1.10000],
                       "close": [1.10010, 1.10020], "volume": [12.5, 3.0]},
                      index=pd.DatetimeIndex([pd.Timestamp(day, tz="UTC"), pd.Timestamp(day, tz="UTC") +
                                              pd.Timedelta(minutes=1)]))
    out = dukascopy.decode(dukascopy.encode(df, day, 1e5), day, 1e5)
    assert np.allclose(out[["open", "high", "low", "close"]].to_numpy(), df[["open", "high", "low", "close"]].to_numpy())
    assert dukascopy.decode(b"", day, 1e5).empty
    with pytest.raises(PermissionError):
        dukascopy.DukascopyClient().fetch_day("EURUSD", date(2026, 9, 2))


def test_cost_model_fees_and_stress():
    btc = market("BTCUSDT")
    maker, taker = btc.fees()
    assert float(maker) == 0.001 and float(taker) == pytest.approx(0.0013)       # 10 bp + 1 bp + 2 bp
    assert float(btc.fees(2.0)[1]) == pytest.approx(0.0026)
    with pytest.raises(ValueError):
        btc.fees(0.5)
    inst = btc.instrument(2.0)
    assert float(inst.taker_fee) == pytest.approx(0.0026) and inst.price_precision == 2
    eur = FX["EURUSD"].instrument()
    assert str(eur.base_currency) == "EUR" and eur.price_precision == 5
    with pytest.raises(ValueError):
        MarketSpec(venue="CME", symbol="ES", asset_class="future", source="x", price_precision=2,
                   commission_bps=1, half_spread_bps=1, slippage_bps=1).instrument()


def test_catalog_ledger_merges_writes_from_another_loader(tmp_path):
    a, b = Catalog(tmp_path), Catalog(tmp_path)             # two loaders, each with its own snapshot
    a._record("bars:A:2020-01", {"rows": 1})
    b._record("bars:B:2020-01", {"rows": 2})
    assert Catalog(tmp_path).has("bars:A:2020-01") and Catalog(tmp_path).has("bars:B:2020-01")
