"""Market specifications: instrument conventions and a per-market cost model.

Bar data carries no spread, so costs are booked through the instrument's fees
(Nautilus charges them on every fill):

    maker fee   commission                                  (resting limit orders: targets)
    taker fee   commission + half spread + slippage         (market and stop orders)

`cost_multiplier` scales all three for cost-stress tests (e.g. 2.0). The venue's
one-tick adverse slip (venue.VenueSpec) comes on top. The numbers are deliberately
conservative retail estimates and are recorded with every run; changing them is a
reviewed change, not a tuning knob.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair, Equity
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from pydantic import BaseModel, ConfigDict, Field


class MarketSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: str
    symbol: str
    asset_class: str = Field(pattern="^(crypto|fx|index|equity|etf|future)$")
    source: str
    base: str | None = None
    quote: str = "USD"
    price_precision: int = Field(ge=0, le=9)
    size_increment: str = "1"
    min_notional: float = 0.0
    commission_bps: float = Field(ge=0)
    half_spread_bps: float = Field(ge=0)
    slippage_bps: float = Field(ge=0)
    calendar: str = "24/7"
    note: str = ""
    # Financing of positions held on margin, in bp of notional per calendar day (swap /
    # borrow / funding). Spot crypto on a cash account borrows nothing and cannot short.
    financing_long_bps_day: float = Field(0.0, ge=0)
    financing_short_bps_day: float = Field(0.0, ge=0)
    # Perpetual futures: margin account (shorts allowed, no leverage beyond the risk limits),
    # financed by the venue's actual funding settlements (hedge_fund.trading.data.funding).
    perpetual: bool = False
    funding_symbol: str | None = None

    @property
    def margin(self) -> bool:
        return self.asset_class != "crypto" or self.perpetual

    @property
    def instrument_id(self) -> str:
        return f"{self.symbol}.{self.venue}"

    def fees(self, cost_multiplier: float = 1.0) -> tuple[Decimal, Decimal]:
        if cost_multiplier < 1.0:
            raise ValueError("costs may be stressed up, never down")
        maker = self.commission_bps * cost_multiplier / 1e4
        taker = (self.commission_bps + self.half_spread_bps + self.slippage_bps) * cost_multiplier / 1e4
        return Decimal(str(round(maker, 10))), Decimal(str(round(taker, 10)))

    def instrument(self, cost_multiplier: float = 1.0):
        maker, taker = self.fees(cost_multiplier)
        iid = InstrumentId(Symbol(self.symbol), Venue(self.venue))
        pp = self.price_precision
        tick = Price(10.0 ** -pp, pp)
        if self.asset_class in ("crypto", "fx", "index"):
            quote = Currency.from_str(self.quote)
            size = Quantity.from_str(self.size_increment)
            base = Currency.from_str(self.base) if self.base else USD
            return CurrencyPair(
                instrument_id=iid, raw_symbol=Symbol(self.symbol), base_currency=base, quote_currency=quote,
                price_precision=pp, size_precision=size.precision, price_increment=tick, size_increment=size,
                lot_size=None, max_quantity=None, min_quantity=size, max_notional=None,
                min_notional=Money(self.min_notional, quote) if self.min_notional else None,
                max_price=None, min_price=None, margin_init=Decimal(0), margin_maint=Decimal(0),
                maker_fee=maker, taker_fee=taker, ts_event=0, ts_init=0)
        if self.asset_class in ("equity", "etf"):
            return Equity(instrument_id=iid, raw_symbol=Symbol(self.symbol), currency=Currency.from_str(self.quote),
                          price_precision=pp, price_increment=tick, lot_size=Quantity.from_int(1),
                          maker_fee=maker, taker_fee=taker, ts_event=0, ts_init=0)
        raise ValueError(f"{self.asset_class} instruments need contract specs (not available yet)")


def _crypto(symbol, base, pp, step, half_spread, slip):
    return MarketSpec(venue="BINANCE", symbol=symbol, asset_class="crypto", source="binance_public", base=base,
                      quote="USDT", price_precision=pp, size_increment=step, min_notional=10.0,
                      commission_bps=10.0, half_spread_bps=half_spread, slippage_bps=slip, calendar="24/7",
                      note="Binance spot VIP0 taker 0.10%, no BNB discount")


# The crypto research set is fixed here, before any backtest. Selection rule: USDT
# pairs listed on Binance by 2018-04 whose coins were among the ten largest by market
# capitalization in January 2018 (so the set is not chosen with hindsight of later
# winners). Price precision keeps the tick below ~1 bp of the lowest price since 2018.
CRYPTO = {s.symbol: s for s in [
    _crypto("BTCUSDT", "BTC", 2, "0.00001", 1.0, 2.0),
    _crypto("ETHUSDT", "ETH", 2, "0.0001", 1.0, 2.0),
    _crypto("XRPUSDT", "XRP", 5, "0.1", 2.0, 3.0),
    _crypto("LTCUSDT", "LTC", 3, "0.001", 2.0, 3.0),
    _crypto("ADAUSDT", "ADA", 6, "0.1", 2.0, 3.0),
    _crypto("BNBUSDT", "BNB", 4, "0.001", 2.0, 3.0),
]}

# Universe declarations read by the backtest auditor (survivorship check).
UNIVERSES = {
    "crypto-binance-6": {
        "instruments": tuple(s.instrument_id for s in CRYPTO.values()),
        "survivorship_free": False,
        "note": ("fixed before testing from the January-2018 top-10 by market cap, but only coins that "
                 "had a Binance USDT pair by 2018-05 and still trade today; top-10 coins without such a "
                 "pair or later delisted (e.g. BCH's old pair, IOTA, XEM, DASH) are absent"),
    },
}

def _perp(symbol, base, pp, step, half_spread, slip):
    return MarketSpec(venue="BINANCE", symbol=f"{symbol}-PERP", asset_class="crypto", source="binance_public_um",
                      base=base, quote="USDT", price_precision=pp, size_increment=step, min_notional=5.0,
                      commission_bps=5.0, half_spread_bps=half_spread, slippage_bps=slip, calendar="24/7",
                      perpetual=True, funding_symbol=symbol,
                      note="Binance USD-M perpetual, VIP0 taker 0.05% (charged on every fill, maker too); "
                           "funding from the actual 8-hourly settlements")


# Perpetual sets, fixed before any backtest by written rules:
#   funding-crowding  the perpetuals of the crypto research set above (same selection rule)
#   vol-trend         every USD-M perpetual with archive data in 2020-01, minus the coins of
#                     the crypto research set (plan_crypto_v1); EOS was delisted in 2025
PERPS = {s.symbol: s for s in [
    _perp("BTCUSDT", "BTC", 2, "0.001", 1.0, 2.0),
    _perp("ETHUSDT", "ETH", 2, "0.001", 1.0, 2.0),
    _perp("XRPUSDT", "XRP", 4, "0.1", 2.0, 3.0),
    _perp("LTCUSDT", "LTC", 2, "0.001", 2.0, 3.0),
    _perp("ADAUSDT", "ADA", 5, "1", 2.0, 3.0),
    _perp("BNBUSDT", "BNB", 3, "0.01", 2.0, 3.0),
    _perp("BCHUSDT", "BCH", 2, "0.001", 2.0, 3.0),
    _perp("EOSUSDT", "EOS", 3, "0.1", 2.0, 3.0),
    _perp("TRXUSDT", "TRX", 5, "1", 2.0, 3.0),
    _perp("ETCUSDT", "ETC", 3, "0.01", 2.0, 3.0),
    _perp("LINKUSDT", "LINK", 3, "0.01", 2.0, 3.0),
    _perp("XLMUSDT", "XLM", 5, "1", 2.0, 3.0),
]}
FUNDING_CROWDING_SET = tuple(f"{s}-PERP" for s in CRYPTO)
VOL_TREND_CRYPTO_SET = ("BCHUSDT-PERP", "EOSUSDT-PERP", "TRXUSDT-PERP", "ETCUSDT-PERP", "LINKUSDT-PERP",
                        "XLMUSDT-PERP")

UNIVERSES["crypto-perp-research-6"] = {
    "instruments": tuple(f"{s}.BINANCE" for s in FUNDING_CROWDING_SET),
    "survivorship_free": False,
    "note": "perpetuals of the crypto-binance-6 set (same selection rule and the same caveat)",
}
UNIVERSES["crypto-perp-early-ex-v1"] = {
    "instruments": tuple(f"{s}.BINANCE" for s in VOL_TREND_CRYPTO_SET),
    "survivorship_free": False,
    "note": ("every USD-M perpetual with data in 2020-01 except the crypto-v1 coins; includes EOS, delisted "
             "in 2025, but the rule only sees perpetuals Binance listed early, which is itself a selection"),
}

FX = {s.symbol: s for s in [
    MarketSpec(venue="DUKASCOPY", symbol=f"{b}{q}", asset_class="fx", source="dukascopy", base=b, quote=q,
               price_precision=pp, size_increment="1000", commission_bps=0.2, half_spread_bps=0.5,
               slippage_bps=0.5, calendar="FX", note="ECN-style commission ~0.2 bp per side; swap ~3.65%/yr",
               financing_long_bps_day=1.0, financing_short_bps_day=1.0)
    for b, q, pp in [("EUR", "USD", 5), ("GBP", "USD", 5), ("USD", "JPY", 3), ("AUD", "USD", 5)]
]}

INDICES = {s.symbol: s for s in [
    MarketSpec(venue="DUKASCOPY", symbol=sym, asset_class="index", source="dukascopy", base=None, quote=q,
               price_precision=pp, size_increment="0.1", commission_bps=0.0, half_spread_bps=1.0,
               slippage_bps=1.0, calendar=cal, note="index CFD: costs in the spread; long ~5.5%/yr, short ~1.8%/yr",
               financing_long_bps_day=1.5, financing_short_bps_day=0.5)
    for sym, q, pp, cal in [("USA500IDXUSD", "USD", 2, "XNYS"), ("DEUIDXEUR", "EUR", 2, "XETR")]
]}


def market(symbol: str) -> MarketSpec:
    for book in (CRYPTO, PERPS, FX, INDICES):
        if symbol in book:
            return book[symbol]
    raise KeyError(symbol)
