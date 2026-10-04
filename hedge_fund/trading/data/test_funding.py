from __future__ import annotations

import hashlib
import io
import zipfile

import pandas as pd
import pytest

from hedge_fund.trading.data import funding
from hedge_fund.trading.data.fence import last_research_day

CSV = "calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,0.00037409\n1704096000000,8,-0.0001\n"


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BTCUSDT-fundingRate-2024-01.csv", text)
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(self.status_code)


class _Session:
    def __init__(self, files: dict[str, bytes]):
        self.files, self.calls = files, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return _Resp(self.files[url]) if url in self.files else _Resp(b"", 404)


def test_download_parse_and_point_in_time(tmp_path):
    data = _zip(CSV)
    url = f"{funding.BASE}/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    s = _Session({url: data, url + ".CHECKSUM": f"{hashlib.sha256(data).hexdigest()}  x.zip".encode()})
    p = funding.download_month("BTCUSDT", "2024-01", root=tmp_path, session=s)
    assert p and p.is_relative_to(tmp_path)
    df = funding.load("BTCUSDT", ["2024-01", "2024-02"], root=tmp_path)
    assert list(df["rate"]) == [0.00037409, -0.0001] and str(df.index.tz) == "UTC"
    first = df.index[0]
    assert len(funding.known_at(df, first)) == 1                          # later settlement not yet known
    assert len(funding.known_at(df, first - pd.Timedelta("1ns"))) == 0
    assert funding.download_month("BTCUSDT", "2024-01", root=tmp_path, session=s) == p
    assert len(s.calls) == 2                                              # verified file not refetched


def test_checksum_mismatch_rejected(tmp_path):
    from hedge_fund.trading.data.binance import DownloadError
    data = _zip(CSV)
    url = f"{funding.BASE}/BTCUSDT/BTCUSDT-fundingRate-2024-01.zip"
    s = _Session({url: data, url + ".CHECKSUM": b"0" * 64})
    with pytest.raises(DownloadError):
        funding.download_month("BTCUSDT", "2024-01", root=tmp_path, session=s, retries=1)


def test_missing_month_is_none_and_holdout_months_refused(tmp_path):
    assert funding.download_month("NOPEUSDT", "2024-01", root=tmp_path, session=_Session({})) is None
    last = last_research_day("crypto")
    sealed = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y-%m")
    s = _Session({})
    with pytest.raises(PermissionError):
        funding.download_month("BTCUSDT", sealed, root=tmp_path, session=s)
    assert s.calls == []                                                  # nothing fetched


def test_default_cache_is_outside_the_repo():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    assert not funding.RAW_DIR.resolve().is_relative_to(repo)
