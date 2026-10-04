"""Paper/testnet-only guard. Nothing in this package can reach a production account.

Allowed execution clients:
    SandboxExecutionClientConfig     simulated fills on live market data (no credentials)
    BinanceExecClientConfig          only with environment TESTNET or DEMO
    KrakenExecClientConfig           only with environment DEMO
Every Binance/Kraken data client must also be TESTNET/DEMO, except a Kraken data client
without credentials (public live market data, read-only, used with the sandbox).

Credentials are read only from variables prefixed AIHF_PAPER_ (never the generic
BINANCE_API_KEY etc.), and their values are never logged.
"""

from __future__ import annotations

import os

PAPER_ENV_PREFIX = "AIHF_PAPER_"


class PaperOnlyViolation(PermissionError):
    pass


def _env_name(cfg) -> str | None:
    env = getattr(cfg, "environment", None)
    if env is None:
        return None
    return str(getattr(env, "name", env)).upper().split(".")[-1]


def assert_paper(node_config) -> None:
    from nautilus_trader.adapters.binance.config import BinanceDataClientConfig, BinanceExecClientConfig
    from nautilus_trader.adapters.kraken.config import KrakenDataClientConfig, KrakenExecClientConfig
    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig

    from hedge_fund.brokers import adapters

    if adapters.LIVE_TRADING_ENABLED:
        raise PaperOnlyViolation("the live lock is open; paper trading refuses to run")
    if not node_config.exec_clients:
        raise PaperOnlyViolation("no execution client: a paper node needs the sandbox or a testnet client")
    for name, cfg in node_config.exec_clients.items():
        if isinstance(cfg, SandboxExecutionClientConfig):
            continue
        if isinstance(cfg, BinanceExecClientConfig) and _env_name(cfg) in ("TESTNET", "DEMO"):
            continue
        if isinstance(cfg, KrakenExecClientConfig) and _env_name(cfg) == "DEMO":
            continue
        raise PaperOnlyViolation(f"execution client {name!r} ({type(cfg).__name__}, environment "
                                 f"{_env_name(cfg)}) is not sandbox/testnet/demo")
    for name, cfg in (node_config.data_clients or {}).items():
        if isinstance(cfg, BinanceDataClientConfig) and _env_name(cfg) not in ("TESTNET", "DEMO"):
            raise PaperOnlyViolation(f"data client {name!r} is not on the Binance testnet")
        if isinstance(cfg, KrakenDataClientConfig) and _env_name(cfg) not in (None, "DEMO") and cfg.api_key:
            raise PaperOnlyViolation(f"data client {name!r} carries credentials outside DEMO")


def paper_credential(name: str) -> str | None:
    """Value of AIHF_PAPER_<name>. Only testnet/demo credentials belong there."""
    if not name.isupper() or name.startswith(PAPER_ENV_PREFIX):
        raise ValueError("pass the suffix, e.g. 'BINANCE_TESTNET_API_KEY'")
    return os.environ.get(PAPER_ENV_PREFIX + name) or None


def credentials_status(*names: str) -> dict[str, str]:
    return {PAPER_ENV_PREFIX + n: ("PRESENT" if paper_credential(n) else "ABSENT") for n in names}
