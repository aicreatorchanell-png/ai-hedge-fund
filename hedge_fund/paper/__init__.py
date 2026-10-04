"""24/7 paper and testnet trading on NautilusTrader. PAPER/TESTNET ONLY.

    guard.py       refuses any configuration that could reach a production account:
                   only the sandbox execution client (simulated fills on live data) or
                   exchange TESTNET/DEMO environments, credentials only from AIHF_PAPER_*
                   variables, live lock (brokers.adapters.LIVE_TRADING_ENABLED) must be False
    config.py      PaperConfig -> Nautilus TradingNodeConfig (data client, execution client,
                   reconciliation, risk engine rate/notional limits)
    journal.py     append-only event journal and atomic state snapshots (restart recovery)
    supervisor.py  runs the node 24/7 with restart and backoff, heartbeat, KILL_SWITCH file
    dashboard.py   terminal dashboard of the snapshot and journal

Strategies are GuardedStrategy subclasses (hedge_fund.trading): bracket orders, stop-distance
sizing, governor (daily loss, drawdown kill, caps), health monitor (HALT blocks entries),
stale-data and portfolio-exposure guards. No AI component is in the order path.
"""
