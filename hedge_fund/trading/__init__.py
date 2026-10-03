"""Active multi-market trading layer, built on NautilusTrader.

NautilusTrader supplies the event engine, order management (bracket/OCO/stop
orders), simulated venues and broker adapters. This package wraps it with the
project's rules, which the engine does not enforce by itself:

    venue.py       simulated venues whose fills happen after the decision bar
                   (latency >= 1 ns -> next bar's open), with adverse slippage and fees
    sizing.py      position size from the stop distance (risk per trade), venue minimums
    governor.py    frozen trade-risk limits: daily loss, drawdown kill, trade/position caps,
                   KILL_SWITCH file
    strategy.py    GuardedStrategy: every entry is a bracket (stop-loss + take-profit),
                   sized and approved by the governor
    backtest.py    deterministic backtest runner, holdout fence on the data range, and an
                   audit of bars where stop-loss and take-profit were both reachable
    breakout.py    reference strategy (engine demonstration, not a research result)
    synthetic.py   seeded synthetic bars for tests
    instruments.py bridge from hedge_fund.core instruments to Nautilus instruments

Backtest only. Nothing here builds a live trading node or imports a live broker
adapter (test_safety.py enforces it); paper trading comes in a later, approved phase.
"""
