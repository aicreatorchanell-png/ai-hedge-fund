"""Tiingo listing intervals: served vs reused vs not listed."""

from hedge_fund.data.tiingo_listings import Listing, TiingoListings


def listings():
    return TiingoListings({
        "MON": [Listing("MON", "NYSE", "Stock", "2000-10-18", "2018-06-08"),
                Listing("MON", "NASDAQ", "Stock", "2021-03-16", "2022-12-23")],
        "CB": [Listing("CB", "NYSE", "Stock", "1984-09-07", "2026-10-01")],
        "BRK-B": [Listing("BRK-B", "NYSE", "Stock", "1996-05-09", "2026-10-01")],
        "DD": [Listing("DD", "NYSE", "Stock", "2017-08-03", "2019-05-31"),
               Listing("DD", "NYSE", "Stock", "1962-01-02", "2026-10-01")],
    })


def test_only_the_served_listing_counts():
    t = listings()
    assert t.status("MON", "2012-07-02") == "reused"           # old Monsanto history is unreachable
    assert t.status("MON", "2022-01-03") == "served"
    assert t.status("MON", "2019-07-01") == "not_listed"
    assert t.status("CB", "2012-07-02") == "served"
    assert t.status("BRK.B", "2012-07-02") == "served"          # vendor's dash form
    assert t.status("ZZZ", "2012-07-02") == "not_listed"
    assert t.covers("MON", "2022-12-30")                        # grace after the last listed day
    assert t.status("DD", "2012-07-02") == "served"             # the active listing, not the latest start
