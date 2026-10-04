# Open-source review for the 24/7 paper-trading system (2026-10-04)

- **Stats:** star counts, latest activity and licences come from shields.io/PyPI on 2026-10-04.
- **Capabilities:** checked against each project's documentation and, for NautilusTrader, against the installed 1.231.0 package.
- **Decision rule:** take mature components that fit our engine, and never install a second full trading engine.

| Project | Stars / activity / licence | What it offers us | Decision |
|---|---|---|---|
| **NautilusTrader** 1.231.0 | 30k, active today, LGPL-3.0 | One event engine for backtest and live. Live node, Kraken/Binance/OKX/Bybit adapters with TESTNET/DEMO environments, sandbox (simulated) execution, reconciliation, risk engine (rate and notional limits), cache | **REUSE COMPONENT**: the core, already installed |
| **CCXT** 4.5.85 | 44k, active, MIT | Public data from more than 100 exchanges (OHLCV, funding rates, order books) | **REUSE COMPONENT**: data and cross-checks only, already installed. Execution stays in Nautilus |
| **Hummingbot** | 20k, active, Apache-2.0 | Market making (Avellaneda–Stoikov, inventory skew), cross-exchange market making, arbitrage, funding-rate arbitrage | **REIMPLEMENT IDEA**: separate engine; ideas only, written as GuardedStrategy families |
| **Freqtrade** | 55k, active, GPL-3.0 | Dry-run, "protections" (cooldown, stop-loss guard, max-drawdown lock), look-ahead and recursive analysis, Telegram/FreqUI | **REFERENCE ONLY**: its GPL code must not be copied into this repository. Protection ideas are already covered by our governor, health monitor and audit |
| **QuantConnect LEAN** | 22k, active, Apache-2.0 | Separation of alpha, portfolio, risk and execution modules; brokerage reconciliation | **REJECT** as an engine (C#, own data stack). **REFERENCE** for module boundaries |
| **Qlib / RD-Agent** | 49k / 15k, active, MIT | ML factor research, model zoo; LLM-driven research loop | **REFERENCE ONLY** for now. A later ML-signal family may reuse Qlib's feature and data tools. RD-Agent's output would be proposal-only |
| **TradingAgents** | 110k, active, Apache-2.0 | Multi-agent LLM analysis (bull/bear debate) | **REFERENCE ONLY**: LLM output is a proposal or critique, never in the order path |
| **AI Hedge Fund** (upstream) | 64k, active, MIT | LLM analyst agents | **ALREADY HERE** (this fork): proposal source only |
| **hftbacktest** | 4.8k, Dec 2025, MIT | Queue-position and latency modelling for market making | **REFERENCE ONLY**: needs L2/tick history (paid). Revisit for market making |
| **arbitragelab** | 0.7k, stale since 2024, BSD-3 | Cointegration and pairs methods | **REIMPLEMENT IDEA**: Engle–Granger/half-life with numpy/scipy (already installed) |
| **skfolio** / Riskfolio-Lib | 2.5k / 4.5k, active, BSD-3 | HRP and risk-parity allocation across strategies | **REUSE COMPONENT LATER**: install only once at least two strategies are validated |
| **cryptofeed** | 2.9k, active, custom licence | Normalised exchange feeds | **REJECT**: duplicates Nautilus adapters |
| **Jesse / OctoBot / Superalgos** | 8.6k / 6.7k / 5.7k | Complete bot platforms | **REJECT**: second engines |
| **vectorbt** | 9.3k, Apache-2.0 + Commons Clause | Fast vectorised screening | **REJECT** for now: our engine is the single source of truth |
| **backtesting.py** | 9k, AGPL-3.0 | Simple backtests | **REJECT**: AGPL, single-asset |
| **quantstats** | 7.7k, Apache-2.0 | Tearsheets | **DEFER**: not needed yet |
| **rich / textual** | already dependencies, MIT | Terminal dashboard | **REUSE COMPONENT**: monitoring dashboard |
| **prometheus_client** + Grafana | 4.4k, Apache-2.0 | Metrics and alerting | **DEFER** until paper trading runs on a persistent host |

## Reachability from this environment (tested 2026-10-04)

| Endpoint | REST | WebSocket |
|---|---|---|
| Kraken public spot | ✅ | ✅ |
| Binance USDⓈ-M futures **testnet** | ✅ | ✅ |
| OKX | ✅ | ❌ (port 8443 times out) |
| Binance spot / spot testnet | ❌ 451 | — |
| Bybit | ❌ 403 | — |

A NautilusTrader node received live Kraken BTC/USD trades through the proxy with no credentials.

## Result: what the 24/7 loop reuses

- **Market stream:** Nautilus Kraken adapter (public, no keys) or Binance futures testnet adapter.
- **Strategy and risk:** our GuardedStrategy, governor and health monitor.
- **Order path:** Nautilus RiskEngine (rate and notional limits), then sandbox execution (simulated fills on live prices) or a testnet execution client.
- **Positions and recovery:** Nautilus cache plus reconciliation (inflight, open-order and position checks).
- **Monitoring:** our journal, snapshot and rich dashboard.
