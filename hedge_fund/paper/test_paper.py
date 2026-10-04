"""Paper/testnet layer: paper-only guard, node config, deployment gate, hard risk guards,
partial-fill protection, state persistence across restarts, monitor, dashboard, supervisor."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from nautilus_trader.model.enums import OrderSide
from pydantic import ValidationError

from hedge_fund.paper import guard
from hedge_fund.paper.config import PaperConfig, build_node_config
from hedge_fund.paper.journal import Journal, StateStore
from hedge_fund.trading.backtest import run_backtest
from hedge_fund.trading.governor import KILL_SWITCH, TradeRiskConfig
from hedge_fund.trading.health import BacktestExpectations, HealthMonitor, load_thresholds
from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy
from hedge_fund.trading.test_trading import BT, INST, VENUE, bar, flat

EXP = BacktestExpectations(sharpe_annual=1.0, max_drawdown=0.1, win_rate=0.5, avg_win=2, avg_loss=1,
                           trades_per_day=1, slippage_bps=2, cost_per_trade=0.5, trades_per_year=365)


class Once(GuardedStrategy):
    """Enters long once on the given bar index."""

    def __init__(self, config, at=2, stop=99.0, tp=101.0, **kw):
        super().__init__(config, TradeRiskConfig(), **kw)
        self.at, self.stop_px, self.tp_px, self.i = at, stop, tp, -1

    def on_signal(self, b):
        self.i += 1
        if self.i == self.at:
            self.enter(OrderSide.BUY, self.stop_px, self.tp_px, b)


def cfg(**kw):
    return GuardedConfig(instrument_id=INST.id, bar_type=BT, **kw)


# -- paper-only guard ------------------------------------------------------------

def test_sandbox_config_builds_without_credentials_and_passes_the_guard():
    node = build_node_config(PaperConfig())
    assert list(node.exec_clients) == ["KRAKEN"] and type(node.exec_clients["KRAKEN"]).__name__ == \
        "SandboxExecutionClientConfig"
    assert node.risk_engine.max_order_submit_rate == "5/00:00:01"
    assert node.exec_engine.reconciliation is False                     # sandbox: cache is the truth


def test_guard_refuses_production_execution_and_data():
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType, BinanceEnvironment
    from nautilus_trader.adapters.binance.config import BinanceDataClientConfig, BinanceExecClientConfig
    from nautilus_trader.adapters.kraken.config import KrakenExecClientConfig
    from nautilus_trader.config import TradingNodeConfig
    from nautilus_trader.core.nautilus_pyo3.kraken import KrakenEnvironment
    live_binance = TradingNodeConfig(exec_clients={"B": BinanceExecClientConfig(
        api_key="x", api_secret="y", account_type=BinanceAccountType.USDT_FUTURES, environment=BinanceEnvironment.LIVE)})
    with pytest.raises(guard.PaperOnlyViolation):
        guard.assert_paper(live_binance)
    live_kraken = TradingNodeConfig(exec_clients={"K": KrakenExecClientConfig(api_key="x", api_secret="y",
                                                                              environment=KrakenEnvironment.LIVE)})
    with pytest.raises(guard.PaperOnlyViolation):
        guard.assert_paper(live_kraken)
    with pytest.raises(guard.PaperOnlyViolation):
        guard.assert_paper(TradingNodeConfig())                        # no execution client at all
    testnet = TradingNodeConfig(
        data_clients={"B": BinanceDataClientConfig(environment=BinanceEnvironment.LIVE)},
        exec_clients={"B": BinanceExecClientConfig(api_key="x", api_secret="y",
                                                   environment=BinanceEnvironment.TESTNET)})
    with pytest.raises(guard.PaperOnlyViolation, match="testnet"):
        guard.assert_paper(testnet)                                    # live data client with testnet exec


def test_guard_refuses_when_the_live_lock_is_open(monkeypatch):
    from hedge_fund.brokers import adapters
    monkeypatch.setattr(adapters, "LIVE_TRADING_ENABLED", True)
    with pytest.raises(guard.PaperOnlyViolation, match="live lock"):
        build_node_config(PaperConfig())


def test_testnet_modes_need_paper_prefixed_credentials(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AIHF_PAPER_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("BINANCE_API_KEY", "production-key-must-be-ignored")
    with pytest.raises(RuntimeError, match="AIHF_PAPER_BINANCE_TESTNET_API_KEY"):
        build_node_config(PaperConfig(mode="binance_futures_testnet", instruments=("BTCUSDT-PERP.BINANCE",)))
    monkeypatch.setenv("AIHF_PAPER_BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("AIHF_PAPER_BINANCE_TESTNET_API_SECRET", "s")
    node = build_node_config(PaperConfig(mode="binance_futures_testnet", instruments=("BTCUSDT-PERP.BINANCE",)))
    assert guard._env_name(node.exec_clients["BINANCE"]) == "TESTNET" and node.exec_engine.reconciliation
    assert guard.credentials_status("BINANCE_TESTNET_API_KEY") == {"AIHF_PAPER_BINANCE_TESTNET_API_KEY": "PRESENT"}


def test_paper_config_requires_hard_guards():
    for bad in ({"max_data_age_secs": 0}, {"max_total_exposure_fraction": 0}, {"max_notional_per_order": 0},
                {"mode": "live"}):
        with pytest.raises(ValidationError):
            PaperConfig(**bad)


# -- hard risk guards in the strategy ------------------------------------------

def test_stale_data_blocks_entries():
    class Clocked(Once):
        def data_age_secs(self):
            return 600.0                                                # the feed has been silent for 10 minutes
    r = run_backtest(INST, flat(6), Clocked(cfg(max_data_age_secs=120)), VENUE)
    assert r.refusals[0]["reason"] == "stale data" and r.orders.empty


def test_portfolio_exposure_cap_blocks_entries():
    r = run_backtest(INST, flat(6), Once(cfg(max_total_exposure_fraction=0.001)), VENUE)
    assert r.refusals[0]["reason"] == "portfolio exposure cap" and r.orders.empty


def test_order_notional_cap_keeps_orders_under_the_risk_engine_limit():
    r = run_backtest(INST, flat(6), Once(cfg(max_order_notional=500.0)), VENUE)
    assert r.brackets[0]["quantity"] * 100 <= 500.0 + 1e-6


def test_protection_follows_the_filled_position_and_exits_at_the_stop():
    bars = flat(3) + [bar(3, 100.0, 100.1, 99.9, 100.0), bar(4, 99.5, 99.6, 98.5, 98.7)] + flat(3, 98.7, start=5)
    s = Once(cfg(protect_on_fill=True), stop=99.0, tp=103.0)
    r = run_backtest(INST, bars, s, VENUE)
    o = r.orders
    assert set(o["type"]) >= {"MARKET", "STOP_MARKET", "LIMIT"}
    stops = o[o["type"] == "STOP_MARKET"]
    entry_qty = float(o[o["type"] == "MARKET"]["filled_qty"].iloc[0])
    assert float(stops["quantity"].iloc[0]) == pytest.approx(entry_qty)      # sized to the filled position
    # the stop arrived after the price had gapped through it and was refused: the strategy
    # must not stay unprotected, so it exits at market
    assert (stops["status"] == "REJECTED").all() and any(e["reason"] == "protection rejected" for e in s.exits)
    assert r.positions["ts_closed"].notna().all()


def test_protective_stop_fills_when_the_market_reaches_it():
    bars = flat(3) + [bar(3, 100.0, 100.1, 99.9, 100.0), bar(4, 100.0, 100.1, 99.9, 100.0),
                      bar(5, 99.8, 99.9, 98.0, 98.2)] + flat(3, 98.2, start=6)
    s = Once(cfg(protect_on_fill=True), stop=99.0, tp=103.0)
    r = run_backtest(INST, bars, s, VENUE)
    stops = r.orders[r.orders["type"] == "STOP_MARKET"]
    assert (stops["status"] == "FILLED").any() and r.positions["ts_closed"].notna().all()


def test_monitoring_failure_cannot_break_trading():
    class Broken:
        def event(self, *a, **k):
            raise OSError("disk full")
    r = run_backtest(INST, flat(6), Once(cfg(), journal=Broken()), VENUE)
    assert len(r.brackets) == 1


# -- persistence across restarts ---------------------------------------------------

def test_kill_switch_and_halt_survive_a_restart(tmp_path):
    store, journal = StateStore(tmp_path), Journal(tmp_path)
    risk = TradeRiskConfig(risk_per_trade=0.02, max_drawdown=0.005)
    bars = flat(3) + [bar(3, 100, 100.1, 99.9, 100), bar(4, 100, 100, 97, 97.2)] + flat(4, 97.2, start=5)

    class First(Once):
        def __init__(self, c, **kw):
            GuardedStrategy.__init__(self, c, risk, **kw)
            self.at, self.stop_px, self.tp_px, self.i = 2, 95.0, 110.0, -1
    s1 = First(cfg(strategy_id="S-001"), journal=journal, state_store=store)
    run_backtest(INST, bars, s1, VENUE)
    saved = store.load(str(s1.id))
    assert saved["governor"]["killed"]
    # "restart": a fresh process restores the latch and refuses new entries
    later = flat(6, t0=bars[-1].ts_event + 60 * 10**9)
    s2 = First(cfg(strategy_id="S-001"), journal=journal, state_store=store)
    r2 = run_backtest(INST, later, s2, VENUE)
    assert s2.governor.killed and r2.orders.empty
    assert any(e["kind"] == "state_restored" for e in journal.tail(100))
    h = HealthMonitor(load_thresholds(), EXP)
    for i in range(10):
        h.record_trade(i, -1.0)
    s3 = Once(cfg(strategy_id="T-001"), health=h, state_store=store, journal=journal)
    run_backtest(INST, flat(6), s3, VENUE)
    h2 = HealthMonitor(load_thresholds(), EXP)
    s4 = Once(cfg(strategy_id="T-001"), health=h2, state_store=store)
    r4 = run_backtest(INST, flat(6, t0=later[-1].ts_event), s4, VENUE)
    assert h2.latched and r4.refusals[0]["reason"] == "health HALT"


def test_state_store_writes_are_atomic(tmp_path):
    st = StateStore(tmp_path)
    st.save("a/b:c", {"x": 1})
    assert st.load("a/b:c") == {"x": 1} and not list((tmp_path / "state").glob("*.tmp"))


# -- monitor and dashboard ---------------------------------------------------------

def test_monitor_snapshot_and_dashboard(tmp_path):
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import LoggingConfig

    from hedge_fund.paper.dashboard import main as dash
    from hedge_fund.paper.monitor import PaperMonitor, PaperMonitorConfig
    from hedge_fund.trading.venue import add_venue

    journal = Journal(tmp_path)
    s = Once(cfg(protect_on_fill=True, modeled_taker_fee=0.004), journal=journal,
             health=HealthMonitor(load_thresholds(), EXP), stop=95.0, tp=110.0)
    mon = PaperMonitor(PaperMonitorConfig(), strategies=[s], state_dir=tmp_path)
    e = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    try:
        add_venue(e, VENUE)
        e.add_instrument(INST)
        e.add_data(flat(6))
        e.add_strategy(s)
        e.add_actor(mon)
        e.run()
        snap = mon.write_snapshot()
    finally:
        e.dispose()
    assert snap["node_health"] in ("HEALTHY", "WARNING", "DEGRADED", "HALT")
    (pos,) = snap["positions"]
    assert pos["stop_loss"] == 95.0 and pos["take_profit"] == 110.0 and pos["side"] == "LONG"
    assert snap["modeled_fees"] > 0 and snap["strategies"][0]["connection"] in ("OK", "STALE")
    assert json.loads((tmp_path / "snapshot.json").read_text())["positions"][0]["entry"] == pos["entry"]
    assert (tmp_path / "heartbeat.json").exists()
    assert dash([str(tmp_path), "--once"]) == 0


# -- deployment gate and supervisor -----------------------------------------------

def test_deployment_requires_human_approval_hypothesis_and_a_passed_validation(tmp_path):
    from hedge_fund.paper.deployment import DeploymentRejected, load_deployment
    entry = {"family": "ema_trend", "params": {}, "instrument": "BTCUSDT.BINANCE", "hypothesis_id": "H-XX",
             "validation_summary": "runs/active/research/crypto-v1/summary.json", "approved_by": "human:owner",
             "expectations": EXP.model_dump()}
    p = tmp_path / "d.yaml"
    import yaml
    p.write_text(yaml.safe_dump({"health_thresholds_version": "1.0.0", "strategies": [entry]}))
    with pytest.raises(DeploymentRejected, match="hypothesis"):
        load_deployment(p, hypotheses={})
    from hedge_fund.trading.hypothesis import EconomicHypothesis
    from hedge_fund.trading.test_hypothesis import GOOD
    h = EconomicHypothesis(**{**GOOD, "id": "H-XX", "families": ("ema_trend",)}).approve("human:owner")
    with pytest.raises(DeploymentRejected, match="PASSED"):              # crypto-v1 lines all failed
        load_deployment(p, hypotheses={"H-XX": h})
    p.write_text(yaml.safe_dump({"health_thresholds_version": "1.0.0",
                                 "strategies": [{**entry, "approved_by": "ai:claude"}]}))
    with pytest.raises(ValidationError):
        load_deployment(p, hypotheses={"H-XX": h})
    p.write_text(yaml.safe_dump({"health_thresholds_version": "0.9", "strategies": []}))
    with pytest.raises(DeploymentRejected, match="thresholds"):
        load_deployment(p)


def test_supervisor_restarts_after_crashes_and_stops_on_kill_switch(tmp_path, monkeypatch):
    from hedge_fund.paper import supervisor

    class FakeNode:
        runs = 0

        def __init__(self, config):
            self.trader = type("T", (), {"add_strategy": lambda *a: None, "add_actor": lambda *a: None})()

        def add_data_client_factory(self, *a):
            pass

        def add_exec_client_factory(self, *a):
            pass

        def build(self):
            pass

        def run(self):
            FakeNode.runs += 1
            if FakeNode.runs == 3:
                (tmp_path / KILL_SWITCH).touch()
            raise ConnectionError("websocket dropped")

        def stop(self):
            pass

        def dispose(self):
            pass
    import nautilus_trader.live.node as live_node
    monkeypatch.setattr(live_node, "TradingNode", FakeNode)
    monkeypatch.setattr(supervisor, "build_strategies", lambda *a, **k: [])
    code = supervisor.run_supervised(PaperConfig(state_dir=str(tmp_path)), plumbing=True, backoff=(0.01, 0.02))
    kinds = [e["kind"] for e in Journal(tmp_path).tail(100)]
    assert code == 2 and kinds.count("node_crash") == 3 and kinds.count("node_restart_scheduled") == 3
    assert kinds[-1] == "supervisor_stop" and "sandbox_account_reset" in kinds


def test_plumbing_check_refuses_testnet_modes(tmp_path):
    from hedge_fund.paper.supervisor import build_strategies
    with pytest.raises(PermissionError):
        build_strategies(PaperConfig(mode="kraken_futures_demo", state_dir=str(tmp_path)), Journal(tmp_path),
                         StateStore(tmp_path), plumbing=True)


def test_no_ai_component_in_the_paper_order_path():
    src = "".join(p.read_text() for p in Path(__file__).parent.glob("*.py") if not p.name.startswith("test_"))
    for banned in ("anthropic", "openai", "langchain", "hedge_fund.llm", "hedge_fund.agents"):
        assert banned not in src


def test_experimental_deployment_runs_unvalidated_but_is_labelled_and_never_validated(tmp_path):
    import yaml

    from hedge_fund.paper.deployment import DeploymentRejected, load_deployment
    from hedge_fund.trading.hypothesis import EconomicHypothesis
    from hedge_fund.trading.test_hypothesis import GOOD
    h = EconomicHypothesis(**{**GOOD, "id": "H-XX", "families": ("ema_trend",)}).approve("human:owner")
    entry = {"family": "ema_trend", "params": {}, "instrument": "BTCUSDT.BINANCE", "hypothesis_id": "H-XX",
             "validation_summary": "runs/active/research/crypto-v1/summary.json", "approved_by": "human:owner",
             "expectations": EXP.model_dump(), "status": "experimental_unvalidated"}
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump({"health_thresholds_version": "1.0.0", "strategies": [entry]}))
    d = load_deployment(p, hypotheses={"H-XX": h})
    assert not d.validated and d.strategies[0].label == "EXPERIMENTAL / UNVALIDATED"
    p.write_text(yaml.safe_dump({"health_thresholds_version": "1.0.0",
                                 "strategies": [{**entry, "validation_summary": "runs/active/research/none.json"}]}))
    with pytest.raises(DeploymentRejected, match="not found"):          # the plan must actually have been run
        load_deployment(p, hypotheses={"H-XX": h})
    with pytest.raises(DeploymentRejected, match="hypothesis"):         # still needs an approved hypothesis
        p.write_text(yaml.safe_dump({"health_thresholds_version": "1.0.0", "strategies": [entry]}))
        load_deployment(p, hypotheses={})


def test_repository_experimental_deployment_is_paper_only_and_unvalidated():
    from hedge_fund.paper.config import futures_sandbox_config
    from hedge_fund.paper.deployment import ROOT, load_deployment
    d = load_deployment(ROOT / "runs/active/paper/deployment_experimental.yaml")
    assert not d.validated and all(s.status == "experimental_unvalidated" for s in d.strategies)
    assert "UNVALIDATED" in (ROOT / "runs/active/paper/deployment_experimental.yaml").read_text().splitlines()[0]
    cfg = futures_sandbox_config(instruments=tuple(s.instrument for s in d.strategies))
    assert cfg.sandbox and cfg.account_type == "MARGIN"
    from hedge_fund.paper.config import build_node_config
    build_node_config(cfg)                                              # passes assert_paper


def test_kraken_funding_feed_drops_sealed_holdout_settlements():
    from hedge_fund.paper.funding_feed import parse
    import pandas as pd
    payload = {"rates": [{"timestamp": "2025-10-01T08:00:00Z", "relativeFundingRate": 1e-6},   # crypto final holdout
                         {"timestamp": "2026-09-02T01:00:00Z", "relativeFundingRate": 2e-6},   # forward holdout, past
                         {"timestamp": "2026-10-04T18:00:00Z", "relativeFundingRate": -1e-6},  # observed live
                         {"timestamp": "2026-10-04T19:00:00Z", "relativeFundingRate": 3e-6}]}
    got = parse(payload, observe_from_ns=pd.Timestamp("2026-10-04T17:30:00Z").value)
    assert [r for _, r in got] == [-1e-6, 3e-6]                          # only what real time reached after start


def test_live_funding_strategy_skips_decisions_when_the_feed_is_stale(monkeypatch):
    from hedge_fund.paper import funding_feed
    from hedge_fund.trading.backtest import run_backtest
    from hedge_fund.trading.families import build
    from hedge_fund.trading.governor import TradeRiskConfig
    from hedge_fund.trading.synthetic import synthetic_bars
    from hedge_fund.trading.test_trading import BT, INST, MARGIN
    bars = synthetic_bars(INST, BT, 600, price=30_000, seed=4)
    monkeypatch.setattr(funding_feed, "fetch", lambda sym, start, **kw: ((bars[0].ts_event - 10 * 3600 * 10**9, 1e-4),))
    events = []

    class J:
        def event(self, kind, **f):
            events.append(kind)

    s = build("funding_crowding", {"signal_minutes": 60, "funding_source": "kraken_futures", "funding_symbol": "X",
                                   "max_funding_age_secs": 3600}, instrument_id=INST.id, bar_type=BT,
              risk=TradeRiskConfig(), allow_short=True)
    s.journal = J()
    r = run_backtest(INST, bars, s, MARGIN)
    assert not r.brackets and "signal_skipped" in events
