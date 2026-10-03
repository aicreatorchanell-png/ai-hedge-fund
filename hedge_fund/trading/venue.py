"""Simulated venues with the project's execution rules.

    latency_ns >= 1   an order decided on bar t reaches the venue after bar t has been
                      processed, so a market entry fills at bar t+1's open, never at the
                      price the strategy observed (CLAUDE.md: next eligible event)
    prob_slippage     probability of a one-tick adverse slip per fill (default: always)
    fees              the instrument's maker/taker fees (Nautilus fee model)
    liquidity         a market order larger than the bar's volume fills tick by tick
                      through worse prices (Nautilus bar execution)

Bars are executed open -> high -> low -> close (fixed, not adaptive). For a long
position whose stop and target are both inside one bar, that ordering reaches the
target first, which is optimistic; backtest.ambiguity_audit counts those exits and
re-prices them at the stop.
"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FillModel, LatencyModel
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Currency, Money
from pydantic import BaseModel, ConfigDict, Field


class VenueSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    account_type: str = Field("CASH", pattern="^(CASH|MARGIN)$")
    oms_type: str = Field("NETTING", pattern="^(NETTING|HEDGING)$")
    starting_balances: dict[str, float]
    base_currency: str | None = None
    leverage: float = Field(1.0, ge=1.0, le=1.0, description="no leverage until a human raises the cap")
    latency_ns: int = Field(1_000_000, ge=1)
    prob_slippage: float = Field(1.0, ge=0.0, le=1.0)
    prob_fill_on_limit: float = Field(0.5, ge=0.0, le=1.0, description="limit fills when price only touches it")
    seed: int = 42


def add_venue(engine: BacktestEngine, spec: VenueSpec) -> Venue:
    venue = Venue(spec.name)
    engine.add_venue(
        venue,
        OmsType[spec.oms_type],
        AccountType[spec.account_type],
        [Money(v, Currency.from_str(c)) for c, v in sorted(spec.starting_balances.items())],
        base_currency=Currency.from_str(spec.base_currency) if spec.base_currency else None,
        default_leverage=Decimal(str(spec.leverage)),
        fill_model=FillModel(prob_fill_on_limit=spec.prob_fill_on_limit, prob_slippage=spec.prob_slippage,
                             random_seed=spec.seed),
        latency_model=LatencyModel(base_latency_nanos=spec.latency_ns),
        bar_execution=True,
        bar_adaptive_high_low_ordering=False,
    )
    return venue
