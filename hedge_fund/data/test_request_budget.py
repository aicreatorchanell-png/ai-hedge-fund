"""Tiingo request budget and bounded downloads."""

from __future__ import annotations

import gzip
import json

import pytest

from hedge_fund.data import tiingo as tiingo_mod
from hedge_fund.data.request_budget import BudgetExhausted, Limits, RequestBudget
from hedge_fund.data.test_tiingo import FakeTiingo, bar
from hedge_fund.data.tiingo import TiingoClient

T0 = 1_791_000_000.0          # 2026-10-02 (Eastern) — a fixed clock


class Clock:
    def __init__(self, t=T0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def budget(tmp_path, clock, **kw):
    limits = Limits(**{**dict(unique_symbols_per_month=3, bytes_per_month=10_000, requests_per_day=100,
                              requests_per_hour=5), **kw})
    return RequestBudget(tmp_path / "ledger.jsonl", limits, clock=clock, sleep=clock.sleep)


def test_unique_symbol_cap_counts_distinct_symbols_and_prior_usage(tmp_path):
    c = Clock()
    b = budget(tmp_path, c)
    b.record("SPY", 1_000, 200, note="prior manual measurement")
    for s in ("AAA", "BBB"):
        b.before(s)
        b.record(s, 100, 200)
    b.before("AAA")                                   # already used this month: free
    with pytest.raises(BudgetExhausted, match="unique-symbol"):
        b.before("CCC")
    assert b.usage()["unique_symbols"] == 3


def test_bandwidth_and_daily_caps(tmp_path):
    c = Clock()
    b = budget(tmp_path, c, unique_symbols_per_month=100, bytes_per_month=1_000)
    b.record("AAA", 1_000, 200)
    with pytest.raises(BudgetExhausted, match="bandwidth"):
        b.before("BBB")
    b2 = budget(tmp_path / "x", c, unique_symbols_per_month=100, requests_per_day=2)
    b2.record("A", 1, 200)
    b2.record("B", 1, 200)
    with pytest.raises(BudgetExhausted, match="daily"):
        b2.before("C")


def test_hourly_rate_sleeps_instead_of_exceeding(tmp_path):
    c = Clock()
    b = budget(tmp_path, c, unique_symbols_per_month=100)
    for i in range(5):
        b.before(f"S{i}")
        b.record(f"S{i}", 1, 200)
        c.t += 10
    b.before("S5")                                    # 5 in the last hour: must wait
    assert c.slept and c.t - T0 >= 3600


def test_ledger_survives_a_new_process(tmp_path):
    c = Clock()
    budget(tmp_path, c).record("AAA", 10, 200)
    assert budget(tmp_path, c).usage()["symbols"] == ["AAA"]


def test_client_downloads_only_the_requested_window_and_records_every_attempt(tmp_path, monkeypatch):
    rows = [bar(d, 10.0) for d in ("2007-12-31", "2008-01-02", "2015-06-01", "2026-08-31", "2026-09-15")]
    fake = FakeTiingo({"AAA": rows})
    monkeypatch.setattr(tiingo_mod.requests, "Session", lambda: fake)
    tiingo_mod.clear_process_cache()
    c = Clock()
    b = budget(tmp_path, c, unique_symbols_per_month=100)
    tc = TiingoClient(api_key="k", cache_dir=tmp_path / "t", history_start="2008-01-01",
                      history_end="2026-08-31", budget=b, max_age_hours=None)
    tc.history_range("AAA")
    (url, params), = fake.calls
    assert params["startDate"] == "2008-01-01" and params["endDate"] == "2026-08-31"
    with gzip.open(tmp_path / "t" / "AAA.json.gz", "rt") as fh:
        stored = json.load(fh)
    assert stored["requested"] == {"start": "2008-01-01", "end": "2026-08-31"}
    assert [r["symbol"] for r in b.entries()] == ["AAA"]
    tiingo_mod.clear_process_cache()
    TiingoClient(api_key="k", cache_dir=tmp_path / "t", history_start="2008-01-01", history_end="2026-08-31",
                 budget=b).history_range("AAA")
    assert len(fake.calls) == 1                         # cached: never re-requested, never extended


def test_client_stops_at_the_budget_without_a_request(tmp_path, monkeypatch):
    fake = FakeTiingo({"AAA": [bar("2010-01-04", 1.0)]})
    monkeypatch.setattr(tiingo_mod.requests, "Session", lambda: fake)
    tiingo_mod.clear_process_cache()
    c = Clock()
    b = budget(tmp_path, c, unique_symbols_per_month=1)
    b.record("SPY", 1, 200)
    tc = TiingoClient(api_key="k", cache_dir=tmp_path / "t", history_end="2026-08-31", budget=b)
    with pytest.raises(BudgetExhausted):
        tc.history_range("AAA")
    assert fake.calls == []
