"""Run the paper node 24/7: restart with backoff, kill switch, heartbeat, journal.

    python -m hedge_fund.paper.supervisor --plumbing-check [--max-runtime-secs N]
    python -m hedge_fund.paper.supervisor --deployment runs/active/paper/deployment.yaml

Recovery layers:
    inside the node   Nautilus adapters reconnect websockets and retry HTTP; the live
                      execution engine reconciles orders and positions with the venue at
                      start and checks in-flight / open orders and positions periodically
    strategy          stale-data guard, governor and health latches restored from state
    supervisor        a crashed or stopped node is rebuilt after 5s, 10s, ... up to 300s;
                      `KILL_SWITCH` in the state dir stops the loop; SIGTERM stops cleanly

In sandbox mode the simulated account lives in memory, so a restart starts it fresh;
the journal records this as `sandbox_account_reset`. Testnet modes reconcile with the venue.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from hedge_fund.paper.config import PaperConfig, build_node_config, client_factories
from hedge_fund.paper.journal import Journal, StateStore
from hedge_fund.trading.governor import KILL_SWITCH, TradeRiskConfig

_stop = threading.Event()


def build_strategies(cfg: PaperConfig, journal: Journal, store: StateStore, *, plumbing: bool,
                     deployment=None) -> list:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.identifiers import InstrumentId

    from hedge_fund.trading.health import HealthMonitor, load_thresholds

    risk = TradeRiskConfig()
    guards = dict(max_data_age_secs=cfg.max_data_age_secs, max_total_exposure_fraction=cfg.max_total_exposure_fraction,
                  max_order_notional=0.95 * cfg.max_notional_per_order,   # stay under the risk engine's hard limit
                  protect_on_fill=True,                                   # SL/TP follow partial fills
                  modeled_taker_fee=cfg.modeled_taker_fee, modeled_maker_fee=cfg.modeled_maker_fee)
    out = []
    if plumbing:
        if not cfg.sandbox:
            raise PermissionError("the plumbing check runs only in the sandbox")
        from hedge_fund.paper.plumbing import PlumbingCheck, PlumbingConfig
        iid = cfg.instruments[0]
        out.append(PlumbingCheck(PlumbingConfig(instrument_id=InstrumentId.from_str(iid),
                                                bar_type=BarType.from_str(cfg.bar_type_str(iid)),
                                                max_hold_bars=3, strategy_id="PlumbingCheck-001", **guards),
                                 risk, kill_dir=cfg.state_dir, journal=journal, state_store=store))
    for i, s in enumerate(deployment.strategies if deployment else ()):
        from hedge_fund.trading.families import build
        st = build(s.family, {**s.params, **guards}, instrument_id=InstrumentId.from_str(s.instrument),
                   bar_type=BarType.from_str(cfg.bar_type_str(s.instrument)), risk=risk, kill_dir=cfg.state_dir)
        st.health = HealthMonitor(load_thresholds(), s.expectations)
        st.journal, st.state_store = journal, store
        st.deployment_label = s.label
        out.append(st)
    return out


def run_supervised(cfg: PaperConfig, *, plumbing: bool = False, deployment=None,
                   max_runtime_secs: float | None = None, max_restarts: int | None = None,
                   backoff: tuple[float, float] = (5.0, 300.0)) -> int:
    from nautilus_trader.live.node import TradingNode

    from hedge_fund.paper.monitor import PaperMonitor, PaperMonitorConfig

    state = Path(cfg.state_dir)
    journal, store = Journal(state), StateStore(state)
    started, attempt = time.monotonic(), 0
    journal.event("supervisor_start", mode=cfg.mode, instruments=list(cfg.instruments), plumbing=plumbing,
                  real_money=False,
                  labels=sorted({s.label for s in deployment.strategies}) if deployment else [])
    while not _stop.is_set():
        if (state / KILL_SWITCH).exists():
            journal.event("supervisor_stop", reason="KILL_SWITCH file present")
            return 2
        node = TradingNode(config=build_node_config(cfg))
        data_f, exec_f = client_factories(cfg)
        for k, f in data_f.items():
            node.add_data_client_factory(k, f)
        for k, f in exec_f.items():
            node.add_exec_client_factory(k, f)
        strategies = build_strategies(cfg, journal, store, plumbing=plumbing, deployment=deployment)
        for s in strategies:
            node.trader.add_strategy(s)
        node.trader.add_actor(PaperMonitor(PaperMonitorConfig(snapshot_every_secs=cfg.snapshot_every_secs,
                                                              stale_after_secs=cfg.max_data_age_secs),
                                           strategies=strategies, state_dir=state))
        node.build()
        if attempt and cfg.sandbox:
            journal.event("sandbox_account_reset", attempt=attempt)
        timers = []
        if max_runtime_secs is not None:
            left = max(1.0, max_runtime_secs - (time.monotonic() - started))
            timers.append(threading.Timer(left, node.stop))
        watcher = threading.Thread(target=lambda: (_stop.wait(), node.stop()), daemon=True)
        watcher.start()
        for t in timers:
            t.start()
        try:
            node.run()
            journal.event("node_stopped", attempt=attempt)
        except Exception as exc:                       # noqa: BLE001 - every failure is journaled
            journal.event("node_crash", attempt=attempt, error=type(exc).__name__, message=str(exc)[:300])
        finally:
            for t in timers:
                t.cancel()
            node.dispose()
        if max_runtime_secs is not None and time.monotonic() - started >= max_runtime_secs - 1:
            journal.event("supervisor_stop", reason="max runtime reached")
            return 0
        attempt += 1
        if max_restarts is not None and attempt > max_restarts:
            journal.event("supervisor_stop", reason="max restarts reached")
            return 1
        wait = min(backoff[1], backoff[0] * 2 ** (attempt - 1))
        journal.event("node_restart_scheduled", attempt=attempt, wait_secs=wait)
        _stop.wait(wait)
    journal.event("supervisor_stop", reason="signal")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plumbing-check", action="store_true")
    ap.add_argument("--deployment")
    ap.add_argument("--mode", default="sandbox_kraken")
    ap.add_argument("--max-runtime-secs", type=float)
    a = ap.parse_args(argv)
    from hedge_fund.paper.config import futures_sandbox_config
    if not a.plumbing_check and not a.deployment:
        ap.error("nothing to run: --plumbing-check (sandbox) or --deployment <approved manifest>")
    deployment = None
    if a.deployment:
        from hedge_fund.paper.deployment import load_deployment
        deployment = load_deployment(a.deployment)
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    if a.mode == "sandbox_kraken_futures":
        insts = tuple(sorted({s.instrument for s in deployment.strategies})) if deployment else ("PF_XBTUSD.KRAKEN",)
        cfg = futures_sandbox_config(instruments=insts)
    else:
        cfg = PaperConfig(mode=a.mode)
    return run_supervised(cfg, plumbing=a.plumbing_check, deployment=deployment, max_runtime_secs=a.max_runtime_secs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
