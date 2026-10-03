"""Bridge: hedge_fund.core.instruments.Instrument -> Nautilus instrument.

    crypto, fx    CurrencyPair; base/quote from "BASE/QUOTE" or the base_currency argument
    equity, etf   Equity
Futures and options come later with their data source (contract specs, expiries).
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair, Equity
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

from hedge_fund.core.instruments import AssetClass, Instrument


def _precision(step: float) -> int:
    return max(0, -Decimal(str(step)).normalize().as_tuple().exponent)


def to_nautilus(inst: Instrument, venue: str, *, base_currency: str | None = None,
                maker_fee: float = 0.0, taker_fee: float = 0.0):
    iid = InstrumentId(Symbol(inst.symbol), Venue(venue))
    pp = _precision(inst.tick_size)
    if inst.asset_class in (AssetClass.CRYPTO, AssetClass.FX):
        base = base_currency or (inst.symbol.split("/")[0] if "/" in inst.symbol else None)
        if base is None:
            raise ValueError(f"{inst.symbol}: base currency needed (use BASE/QUOTE or base_currency=)")
        sp = _precision(inst.quantity_step)
        quote = Currency.from_str(inst.currency)
        return CurrencyPair(
            instrument_id=iid, raw_symbol=Symbol(inst.symbol), base_currency=Currency.from_str(base),
            quote_currency=quote, price_precision=pp, size_precision=sp,
            price_increment=Price(inst.tick_size, pp), size_increment=Quantity(inst.quantity_step, sp),
            lot_size=None, max_quantity=None,
            min_quantity=Quantity(inst.min_quantity, sp) if inst.min_quantity else None,
            max_notional=None, min_notional=Money(inst.min_notional, quote) if inst.min_notional else None,
            max_price=None, min_price=None, margin_init=Decimal(0), margin_maint=Decimal(0),
            maker_fee=Decimal(str(maker_fee)), taker_fee=Decimal(str(taker_fee)), ts_event=0, ts_init=0)
    if inst.asset_class in (AssetClass.EQUITY, AssetClass.ETF):
        if inst.quantity_step != 1.0:
            raise ValueError("fractional equities are not mapped yet")
        return Equity(instrument_id=iid, raw_symbol=Symbol(inst.symbol), currency=Currency.from_str(inst.currency),
                      price_precision=pp, price_increment=Price(inst.tick_size, pp), lot_size=Quantity.from_int(1),
                      maker_fee=Decimal(str(maker_fee)), taker_fee=Decimal(str(taker_fee)), ts_event=0, ts_init=0)
    raise ValueError(f"{inst.asset_class.value} instruments are not mapped yet")
