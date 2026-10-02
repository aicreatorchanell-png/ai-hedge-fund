"""Tiingo listing intervals (supported_tickers.zip): which history a ticker serves.

Tiingo publishes every listing it knows — ticker, exchange, asset type, first and
last date — including old listings of tickers that were later reused (MON 2000-2018
and MON 2021-2022; DOW 1972-2017 and DOW 2019-). The prices API serves a ticker's
*current* listing only, so the history behind a reused ticker cannot be fetched under
that ticker. Renamed companies keep their whole history under the new ticker (TFC
from 1990 carries BB&T; TPR from 2000 carries Coach).

`covers(ticker, day)` is True only if the listing the API serves for *ticker* spans
*day*. The file is vendor metadata (no prices); it is kept in the private cache.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from hedge_fund.data.tiingo import tiingo_symbol
from hedge_fund.paths import CACHE_DIR

DEFAULT_PATH = CACHE_DIR / "tiingo_meta" / "supported_tickers.zip"
GRACE_DAYS = 7          # a listing's last date may trail its last trade by a few sessions


@dataclass(frozen=True)
class Listing:
    ticker: str
    exchange: str
    asset_type: str
    start: str
    end: str


class TiingoListings:
    def __init__(self, listings: dict[str, list[Listing]]) -> None:
        self._by_ticker = listings

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> TiingoListings:
        with zipfile.ZipFile(path) as z:
            rows = list(csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]))))
        out: dict[str, list[Listing]] = {}
        for r in rows:
            if not r.get("startDate") or not r.get("endDate"):
                continue
            key = r["ticker"].upper()
            out.setdefault(key, []).append(Listing(key, r["exchange"], r["assetType"], r["startDate"], r["endDate"]))
        return cls(out)

    def listings(self, ticker: str) -> list[Listing]:
        return sorted(self._by_ticker.get(tiingo_symbol(ticker), []), key=lambda x: x.start)

    def served(self, ticker: str) -> Listing | None:
        """The listing the prices API returns for *ticker*: the active one (latest end date,
        then latest start). DD lists 1962-2026 and a 2017-2019 DowDuPont segment: 1962-2026."""
        ls = self.listings(ticker)
        return max(ls, key=lambda x: (x.end, x.start)) if ls else None

    def covers(self, ticker: str, day: str) -> bool:
        s = self.served(ticker)
        return s is not None and s.start <= day <= _plus(s.end, GRACE_DAYS)

    def status(self, ticker: str, day: str) -> str:
        """'served' | 'reused' (only an older, unreachable listing spans day) | 'not_listed'."""
        if self.covers(ticker, day):
            return "served"
        served = self.served(ticker)
        if any(x != served and x.start <= day <= _plus(x.end, GRACE_DAYS) for x in self.listings(ticker)):
            return "reused"
        return "not_listed"


def _plus(day: str, n: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()
