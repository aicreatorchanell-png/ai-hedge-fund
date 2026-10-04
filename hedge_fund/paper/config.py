"""Paper/testnet node configuration -> NautilusTrader TradingNodeConfig.

Modes (all paper):

    sandbox_kraken           live public Kraken spot data, simulated fills (Nautilus sandbox
                             execution client). No credentials. Positions live in memory: a
                             restart starts the simulated account fresh (journaled).
    binance_futures_testnet  Binance USD-M futures TESTNET data and order routing. Needs
                             AIHF_PAPER_BINANCE_TESTNET_API_KEY / _API_SECRET.
    kraken_futures_demo      Kraken futures DEMO data and order routing. Needs
                             AIHF_PAPER_KRAKEN_DEMO_API_KEY / _API_SECRET.

Reused from Nautilus: reconnecting websocket clients, startup reconciliation and periodic
in-flight / open-order / position checks against the venue, the risk engine's order rate
and per-order notional limits. Every built config passes guard.assert_paper.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hedge_fund.paths import CACHE_DIR
from hedge_fund.paper.guard import assert_paper, paper_credential


class PaperConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["sandbox_kraken", "binance_futures_testnet", "kraken_futures_demo"] = "sandbox_kraken"
    trader_id: str = "AIHF-PAPER-001"
    instruments: tuple[str, ...] = ("BTC/USD.KRAKEN",)
    state_dir: str = str(CACHE_DIR / "paper")
    starting_balances: tuple[str, ...] = ("10000 ZUSD", "0 XXBT")   # Kraken currency codes (USD, BTC)
    account_type: Literal["CASH", "MARGIN"] = "CASH"
    max_order_submit_rate: str = "5/00:00:01"
    max_notional_per_order: float = Field(2_000.0, gt=0)
    max_data_age_secs: int = Field(180, gt=0, le=3600)
    max_total_exposure_fraction: float = Field(0.5, gt=0, le=1.0)
    reconciliation_lookback_mins: int = Field(1440, ge=60)
    snapshot_every_secs: int = Field(10, ge=1)
    # Venue fee schedule applied in the journal when the venue reports zero commission
    # (the sandbox does). Kraken spot base tier: 0.40% taker, 0.25% maker.
    modeled_taker_fee: float = Field(0.0040, ge=0)
    modeled_maker_fee: float = Field(0.0025, ge=0)

    def bar_type_str(self, instrument: str) -> str:
        """1-minute execution bars aggregated inside Nautilus from trade ticks (venue-agnostic)."""
        return f"{instrument}-1-MINUTE-LAST-INTERNAL"


def build_node_config(cfg: PaperConfig):
    from nautilus_trader.common.config import InstrumentProviderConfig
    from nautilus_trader.config import LiveRiskEngineConfig, LoggingConfig, TradingNodeConfig
    from nautilus_trader.model.identifiers import InstrumentId

    proxy = os.environ.get("HTTPS_PROXY") or None
    ids = frozenset(InstrumentId.from_str(i) for i in cfg.instruments)
    provider = InstrumentProviderConfig(load_ids=ids)
    if cfg.mode == "sandbox_kraken":
        from nautilus_trader.adapters.kraken.config import KrakenDataClientConfig
        from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
        from nautilus_trader.core.nautilus_pyo3.kraken import KrakenProductType
        data = {"KRAKEN": KrakenDataClientConfig(product_types=(KrakenProductType.SPOT,), proxy_url=proxy,
                                                 instrument_provider=provider)}
        execs = {"KRAKEN": SandboxExecutionClientConfig(venue="KRAKEN", starting_balances=list(cfg.starting_balances),
                                                       account_type=cfg.account_type, oms_type="NETTING",
                                                       instrument_provider=provider)}
    elif cfg.mode == "binance_futures_testnet":
        from nautilus_trader.adapters.binance.common.enums import BinanceAccountType, BinanceEnvironment
        from nautilus_trader.adapters.binance.config import BinanceDataClientConfig, BinanceExecClientConfig
        key, secret = paper_credential("BINANCE_TESTNET_API_KEY"), paper_credential("BINANCE_TESTNET_API_SECRET")
        if not (key and secret):
            raise RuntimeError("binance_futures_testnet needs AIHF_PAPER_BINANCE_TESTNET_API_KEY and _API_SECRET")
        common = dict(api_key=key, api_secret=secret, account_type=BinanceAccountType.USDT_FUTURES,
                      environment=BinanceEnvironment.TESTNET, proxy_url=proxy, instrument_provider=provider)
        data = {"BINANCE": BinanceDataClientConfig(**common)}
        execs = {"BINANCE": BinanceExecClientConfig(**common)}
    else:
        from nautilus_trader.adapters.kraken.config import KrakenDataClientConfig, KrakenExecClientConfig
        from nautilus_trader.core.nautilus_pyo3.kraken import KrakenEnvironment, KrakenProductType
        key, secret = paper_credential("KRAKEN_DEMO_API_KEY"), paper_credential("KRAKEN_DEMO_API_SECRET")
        if not (key and secret):
            raise RuntimeError("kraken_futures_demo needs AIHF_PAPER_KRAKEN_DEMO_API_KEY and _API_SECRET")
        common = dict(api_key=key, api_secret=secret, environment=KrakenEnvironment.DEMO,
                      product_types=(KrakenProductType.FUTURES,), proxy_url=proxy, instrument_provider=provider)
        data = {"KRAKEN": KrakenDataClientConfig(**common)}
        execs = {"KRAKEN": KrakenExecClientConfig(**common)}
    node = TradingNodeConfig(
        trader_id=cfg.trader_id,
        logging=LoggingConfig(log_level="INFO", log_directory=str(cfg.state_dir) + "/logs", log_level_file="INFO",
                              log_file_format="json"),
        data_clients=data,
        exec_clients=execs,
        exec_engine=_exec_engine(cfg),
        risk_engine=LiveRiskEngineConfig(max_order_submit_rate=cfg.max_order_submit_rate,
                                         max_notional_per_order={i: int(cfg.max_notional_per_order)
                                                                 for i in cfg.instruments}),
    )
    assert_paper(node)
    return node


def _exec_engine(cfg: PaperConfig):
    """Venue reconciliation for testnet/demo. The sandbox venue is simulated in memory and does
    not report spot holdings, so periodic venue checks would infer phantom external fills
    (observed 2026-10-04: an inferred SELL closed a live sandbox position). In sandbox mode
    the node's own cache is the source of truth and the venue checks are off."""
    from nautilus_trader.config import LiveExecEngineConfig

    if cfg.mode == "sandbox_kraken":
        return LiveExecEngineConfig(reconciliation=False, inflight_check_interval_ms=2_000,
                                    inflight_check_threshold_ms=5_000, graceful_shutdown_on_exception=True)
    return LiveExecEngineConfig(reconciliation=True, reconciliation_lookback_mins=cfg.reconciliation_lookback_mins,
                                inflight_check_interval_ms=2_000, inflight_check_threshold_ms=5_000,
                                open_check_interval_secs=10, position_check_interval_secs=60,
                                graceful_shutdown_on_exception=True)


def client_factories(cfg: PaperConfig) -> tuple[dict, dict]:
    """(data factories, exec factories) for TradingNode.add_*_client_factory."""
    if cfg.mode == "sandbox_kraken":
        from nautilus_trader.adapters.kraken.factories import KrakenLiveDataClientFactory
        from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
        return {"KRAKEN": KrakenLiveDataClientFactory}, {"KRAKEN": SandboxLiveExecClientFactory}
    if cfg.mode == "binance_futures_testnet":
        from nautilus_trader.adapters.binance.factories import (BinanceLiveDataClientFactory,
                                                                BinanceLiveExecClientFactory)
        return {"BINANCE": BinanceLiveDataClientFactory}, {"BINANCE": BinanceLiveExecClientFactory}
    from nautilus_trader.adapters.kraken.factories import KrakenLiveDataClientFactory, KrakenLiveExecClientFactory
    return {"KRAKEN": KrakenLiveDataClientFactory}, {"KRAKEN": KrakenLiveExecClientFactory}
