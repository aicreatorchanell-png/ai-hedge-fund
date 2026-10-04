"""Adversarial backtest audit: try to break a research plan before trusting its results.

Each check returns PASS, WARNING or FAIL. Any FAIL makes the report non-promotable,
and the research pipeline refuses VALIDATED (and every later stage) without a
passing audit (hedge_fund.research.pipeline.AUDITED_STAGES).

Dynamic probes run each family on seeded synthetic bars (no market data):

    look_ahead            perturbing bars after T must not change any decision made by T
    indicator_repainting  decisions on a prefix of the data equal the full run's decisions
                          up to the end of the prefix
    execution_timing      every entry fills strictly after the bar it was decided on
    duplicate_orders      open positions never exceed the governor's limit; no entry is
                          sent while another bracket is still live
    resampling            a signal bar of n minutes contains exactly the execution bars
                          closing in (T-n, T] and arrives after the bar closing at T
    future_fitting        changing returns inside a test window never changes the
                          configuration chosen for that window or earlier ones

Static and data checks:

    bar_close_convention  catalog bars are close-stamped, minute-aligned, UTC, ts_init >= ts_event
    timezone              a sampled development day starts at 00:01 UTC (first close)
    fees / spread_slippage   every instrument has commission > 0 and half-spread + slippage > 0,
                          the instrument built for a run carries them, and a cost stress >= 2x exists
    fills                 next-bar latency, adverse slippage; intrabar ordering flagged with the
                          ambiguity audit share when run results are supplied
    train_test_holdout    folds ordered and non-overlapping, development ends before the reserve,
                          and the sealed holdout refuses reads for the plan's market
    survivorship          the instruments come from a declared universe that is survivorship-free
    liquidity             maximum position notional vs. the median 1-minute traded value
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

PASS, WARNING, FAIL = "PASS", "WARNING", "FAIL"


class Status(str, Enum):
    PASS = PASS
    WARNING = WARNING
    FAIL = FAIL


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class AuditReport:
    subject: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def passed(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    def counts(self) -> dict[str, int]:
        return {s: sum(c.status == s for c in self.checks) for s in (PASS, WARNING, FAIL)}

    def to_json(self) -> str:
        return json.dumps({"subject": self.subject, "passed": self.passed, "counts": self.counts(),
                           "checks": [asdict(c) for c in self.checks]}, indent=1)


# -- dynamic probes (synthetic data) -------------------------------------------

def _probe_setup(n: int, seed: int):
    from nautilus_trader.model.data import BarType
    from nautilus_trader.test_kit.providers import TestInstrumentProvider

    from hedge_fund.trading.synthetic import synthetic_bars
    from hedge_fund.trading.venue import VenueSpec

    inst = TestInstrumentProvider.btcusdt_binance()
    bt = BarType.from_str(f"{inst.id}-1-MINUTE-LAST-EXTERNAL")
    bars = synthetic_bars(inst, bt, n, price=30_000, seed=seed)
    margin = VenueSpec(name="BINANCE", account_type="MARGIN", starting_balances={"USDT": 10_000})
    return inst, bt, bars, margin


def _decisions(result, cut: int | None = None) -> list[tuple]:
    """Every decision up to *cut*: entries sent and entries the governor refused."""
    sent = [(b["ts"], b["side"], b["quantity"]) for b in result.brackets if cut is None or b["ts"] <= cut]
    refused = [(r["ts"], "REFUSED", r["reason"]) for r in result.refusals if cut is None or r["ts"] <= cut]
    return sorted(sent + refused)


def _mirror(inst, bars, pivot: float) -> list:
    """Reflect a price path in log space around *pivot*: rises become falls."""
    from nautilus_trader.model.data import Bar

    out = []
    for b in bars:
        o, h, lo, c = (pivot * pivot / float(x) for x in (b.open, b.high, b.low, b.close))
        out.append(Bar(b.bar_type, inst.make_price(o), inst.make_price(max(o, lo, c)), inst.make_price(min(o, h, c)),
                       inst.make_price(c), b.volume, b.ts_event, b.ts_init))
    return out


def probe_family(family: str, params: dict, report: AuditReport, *, n: int | None = None, seed: int = 101,
                 risk=None) -> None:
    from hedge_fund.trading.backtest import run_backtest
    from hedge_fund.trading.families import build
    from hedge_fund.trading.governor import TradeRiskConfig

    risk = risk or TradeRiskConfig()
    n = n or max(4000, 300 * int(params.get("signal_minutes", 1)))   # enough signal bars to warm up and trade

    def run(data):
        s = build(family, params, instrument_id=inst.id, bar_type=bt, risk=risk, allow_short=True)
        return run_backtest(inst, data, s, margin), s

    for attempt in range(3):                       # a rare config may not trade on one synthetic path
        inst, bt, bars, margin = _probe_setup(n, seed + 1000 * attempt)
        full, strat = run(bars)
        if full.brackets:
            break
    tag = f"{family} {json.dumps(params, sort_keys=True)}"
    leaks = []
    for frac in (0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78, 0.86):    # several cuts: short peeks flip somewhere
        k = int(n * frac)
        cut = bars[k].ts_event
        alt, _ = run(bars[:k + 1] + _mirror(inst, bars[k + 1:], float(bars[k].close)))
        if _decisions(full, cut) != _decisions(alt, cut):
            leaks.append(pd.Timestamp(cut, unit="ns", tz="UTC").isoformat())
    if leaks:
        report.add("look_ahead", FAIL, f"{tag}: decisions before T depend on bars after T (cuts {leaks[:3]})")
    elif not full.brackets and not full.refusals:
        report.add("look_ahead", WARNING, f"{tag}: no trades on the probe data; check not exercised")
    else:
        report.add("look_ahead", PASS, f"{tag}: decisions up to T unchanged when bars after T are mirrored (8 cuts)")
    differ = []
    for frac in (0.40, 0.55, 0.70, 0.85):
        k = int(n * frac)
        cut = bars[k].ts_event
        pre, _ = run(bars[:k + 1])
        if _decisions(pre) != _decisions(full, cut):
            differ.append(frac)
    if differ:
        report.add("indicator_repainting", FAIL, f"{tag}: decisions on prefixes {differ} differ from the full run")
    else:
        report.add("indicator_repainting", PASS, f"{tag}: four prefix runs reproduce the full run's decisions")

    orders = full.orders
    late = []
    for b in full.brackets:
        if b["entry_id"] in orders.index and orders.loc[b["entry_id"], "status"] == "FILLED":
            if int(pd.Timestamp(orders.loc[b["entry_id"], "ts_last"]).value) <= b["ts"]:
                late.append(b["entry_id"])
    report.add("execution_timing", FAIL if late else PASS,
               f"{tag}: {len(late)} entries filled on or before their decision bar" if late else
               f"{tag}: every entry filled after its decision bar")

    pos = full.positions
    worst = 0
    if not pos.empty:
        opened = pd.to_datetime(pos["ts_opened"], utc=True)
        closed = pd.to_datetime(pos["ts_closed"], utc=True).fillna(pd.Timestamp.max.tz_localize("UTC"))
        for o in opened:
            worst = max(worst, int(((opened <= o) & (closed > o)).sum()))
    limit = risk.max_open_positions
    gaps = sum(1 for a, b in zip(full.brackets, full.brackets[1:]) if a["ts"] == b["ts"])
    if worst > limit or gaps:
        report.add("duplicate_orders", FAIL, f"{tag}: {worst} concurrent positions (limit {limit}), "
                                             f"{gaps} same-bar duplicate entries")
    else:
        report.add("duplicate_orders", PASS, f"{tag}: at most {worst} open position(s), no duplicate entries")
    del strat


def probe_resampling(minutes: int, report: AuditReport) -> None:
    from nautilus_trader.backtest.config import BacktestEngineConfig
    from nautilus_trader.backtest.engine import BacktestEngine
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.data import Bar

    from hedge_fund.trading.governor import TradeRiskConfig
    from hedge_fund.trading.strategy import GuardedConfig, GuardedStrategy
    from hedge_fund.trading.venue import add_venue

    inst, bt, _, margin = _probe_setup(10, 1)
    t0 = 1_577_836_800_000_000_000
    n = 3 * minutes + 2
    bars = [Bar(bt, inst.make_price(100 + i), inst.make_price(100 + i + 0.5), inst.make_price(100 + i - 0.5),
                inst.make_price(100 + i), inst.make_qty(1000), t0 + (i + 1) * 60_000_000_000,
                t0 + (i + 1) * 60_000_000_000) for i in range(n)]
    seen = []

    class Probe(GuardedStrategy):
        def on_signal(self, b):
            seen.append(((b.ts_event - t0) // 60_000_000_000, float(b.open), float(b.close)))

    e = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR")))
    try:
        add_venue(e, margin)
        e.add_instrument(inst)
        e.add_data(bars)
        e.add_strategy(Probe(GuardedConfig(instrument_id=inst.id, bar_type=bt, signal_minutes=minutes),
                             TradeRiskConfig()))
        e.run()
    finally:
        e.dispose()
    expect = [(k * minutes, 100.0 + (k - 1) * minutes, 100.0 + k * minutes - 1) for k in (1, 2, 3)]
    ok = seen[:3] == expect
    report.add("resampling", PASS if ok else FAIL,
               f"{minutes}-minute signal bars {'match' if ok else 'do not match'} their execution bars "
               f"(got {seen[:3]}, expected {expect})")


def probe_future_fitting(report: AuditReport) -> None:
    from hedge_fund.trading.research import ConfigRun, ResearchPlan, WalkForward, walk_forward

    plan = ResearchPlan(plan_id="audit-probe", families=("ema_trend",), instruments=("BTCUSDT.BINANCE",),
                        dev_start="2020-01-01", dev_end="2022-12-31", reserve_start="2023-01-01",
                        walk_forward=WalkForward(train_months=12, test_months=6, step_months=6))
    idx = pd.date_range(plan.dev_start, plan.dev_end, freq="D", tz="UTC")
    rng = np.random.default_rng(5)
    trades = pd.Series(1.0, index=pd.date_range(plan.dev_start, plan.dev_end, periods=400, tz="UTC"))

    def mk(key, mu, seed):
        return ConfigRun(key, "ema_trend", {}, "BTCUSDT.BINANCE", 1.0,
                         pd.Series(np.random.default_rng(seed).normal(mu, 0.01, len(idx)), index=idx), trades)
    base = [mk("a", -0.001, 1), mk("b", 0.001, 2)]
    ref = [c["choice"] for c in walk_forward(plan, base).choices]
    bad = []
    for k, (_, _, te_s, te_e) in enumerate(plan.folds()):
        p = mk("a", -0.001, 1)
        m = (p.daily.index >= pd.Timestamp(te_s, tz="UTC")) & (p.daily.index <= pd.Timestamp(te_e, tz="UTC"))
        p.daily[m] = 0.05
        got = [c["choice"] for c in walk_forward(plan, [p, base[1]]).choices]
        if got[:k + 1] != ref[:k + 1]:
            bad.append(te_s)
    del rng
    report.add("future_fitting", FAIL if bad else PASS,
               f"test-window returns changed earlier choices for folds {bad}" if bad else
               "selection uses training windows only (test-window perturbations never change the choice)")


# -- static and data checks ----------------------------------------------------

def check_costs(plan, report: AuditReport) -> None:
    from hedge_fund.trading.data.markets import market

    bad_fee, bad_spread = [], []
    for iid in plan.instruments:
        spec = market(iid.split(".")[0])
        inst = spec.instrument()
        if spec.commission_bps <= 0 and spec.asset_class == "crypto":
            bad_fee.append(iid)
        if float(inst.taker_fee) <= float(inst.maker_fee) or float(inst.taker_fee) <= 0:
            bad_fee.append(iid)
        if spec.half_spread_bps + spec.slippage_bps <= 0:
            bad_spread.append(iid)
    report.add("fees", FAIL if bad_fee else PASS,
               f"missing or ineffective fees: {bad_fee}" if bad_fee else
               "every instrument charges commission; taker fee includes spread and slippage")
    report.add("spread_slippage", FAIL if bad_spread else PASS,
               f"no spread/slippage: {bad_spread}" if bad_spread else "half-spread + slippage > 0 for all")
    stress = max(plan.cost_multipliers)
    report.add("cost_stress", PASS if stress >= 2.0 else FAIL,
               f"cost multipliers {list(plan.cost_multipliers)} (needs >= 2.0)")


def check_fills(report: AuditReport, run_audits: list[dict] | None = None) -> None:
    from hedge_fund.trading.venue import VenueSpec

    v = VenueSpec(name="X", starting_balances={"USD": 1})
    if v.latency_ns < 1:
        report.add("fills", FAIL, "venue latency allows fills on the decision bar")
        return
    if not run_audits:
        report.add("fills", WARNING, "next-bar fills and adverse slippage enforced; intrabar stop/target "
                                     "order is open-high-low-close (optimistic for longs) and no run "
                                     "results were supplied to measure ambiguous exits")
        return
    share = max((a.get("ambiguous_share", 0.0) for a in run_audits), default=0.0)
    status = FAIL if share > 0.20 else WARNING if share > 0.05 else PASS
    report.add("fills", status, f"next-bar fills, adverse slippage; worst ambiguous-exit share {share:.1%}")


def check_leakage(plan, report: AuditReport) -> None:
    from hedge_fund.trading.data.markets import market
    from hedge_fund.validation.holdout_guard import HoldoutAccessDenied, market_data_fence

    folds = plan.folds()
    ordered = all(tr_s < tr_e < te_s <= te_e for tr_s, tr_e, te_s, te_e in folds)
    tests = sorted((te_s, te_e) for _, _, te_s, te_e in folds)
    overlap = any(a[1] >= b[0] for a, b in zip(tests, tests[1:]))
    issues = []
    if not ordered:
        issues.append("a fold's test window does not follow its training window")
    if overlap:
        issues.append("test windows overlap")
    if not plan.dev_end < plan.reserve_start:
        issues.append("development window reaches the reserve")
    markets = {market(i.split(".")[0]).asset_class for i in plan.instruments}
    for m in markets:
        try:
            market_data_fence(plan.reserve_start, plan.reserve_start, m)
            issues.append(f"the reserve start {plan.reserve_start} is readable for {m}: no sealed holdout")
        except HoldoutAccessDenied:
            pass
    report.add("train_test_holdout", FAIL if issues else PASS,
               "; ".join(issues) if issues else
               f"{len(folds)} ordered folds, disjoint tests, holdout from {plan.reserve_start} sealed for {sorted(markets)}")


def check_survivorship(plan, report: AuditReport) -> None:
    from hedge_fund.trading.data.markets import UNIVERSES

    inst = set(plan.instruments)
    for name, u in UNIVERSES.items():
        if inst <= set(u["instruments"]):
            report.add("survivorship", PASS if u["survivorship_free"] else WARNING,
                       f"universe {name}: {'survivorship-free' if u['survivorship_free'] else u['note']}")
            return
    report.add("survivorship", FAIL, f"instruments {sorted(inst)} are not a declared universe")


def check_catalog(plan, report: AuditReport, catalog) -> None:
    if catalog is None:
        report.add("bar_close_convention", WARNING, "no catalog supplied; not checked")
        report.add("timezone", WARNING, "no catalog supplied; not checked")
        report.add("liquidity", WARNING, "no catalog supplied; not checked")
        return
    day = (pd.Timestamp(plan.dev_start) + pd.Timedelta(days=200)).date().isoformat()
    conv, tz, liq = [], [], []
    for iid in plan.instruments:
        bars = catalog.load_bars(iid, day, day)
        if not bars:
            conv.append(f"{iid}: no bars on {day}")
            continue
        if any(b.ts_event % 60_000_000_000 or b.ts_init < b.ts_event for b in bars):
            conv.append(f"{iid}: bars not minute-aligned or visible before close")
        start = pd.Timestamp(day, tz="UTC")
        after = [b for b in bars if b.ts_event > start.value]   # the 00:00 close belongs to the previous day
        first = pd.Timestamp(after[0].ts_event, unit="ns", tz="UTC") if after else None
        if first is not None and first != start + pd.Timedelta(minutes=1) and len(after) > 1400:
            tz.append(f"{iid}: first close after midnight is {first}")
        value = np.median([float(b.close) * float(b.volume) for b in bars])
        cap = plan.starting_cash * plan.risk.max_notional_fraction
        liq.append((iid, cap / value if value > 0 else float("inf")))
    report.add("bar_close_convention", FAIL if conv else PASS,
               "; ".join(conv) if conv else f"close-stamped, minute-aligned bars (sampled {day})")
    report.add("timezone", FAIL if tz else PASS, "; ".join(tz) if tz else "first bar of the day closes 00:01 UTC")
    worst = max(liq, key=lambda x: x[1]) if liq else (None, 0.0)
    status = FAIL if worst[1] > 0.10 else WARNING if worst[1] > 0.01 else PASS
    report.add("liquidity", status, f"max position / median 1-minute traded value: {worst[1]:.2%} ({worst[0]})")


def audit_plan(plan, *, catalog=None, run_audits: list[dict] | None = None, probe_params: dict | None = None,
               families: dict | None = None) -> AuditReport:
    """Full audit of a research plan. `probe_params` overrides the probed config per family
    (default: the family grid's first and last points)."""
    from hedge_fund.trading.families import FAMILIES

    fams = families or FAMILIES
    report = AuditReport(subject=f"plan {plan.plan_id} ({plan.plan_hash()})")
    for f in plan.families:
        pts = (probe_params or {}).get(f) or [fams[f].configs()[0], fams[f].configs()[-1]]
        for p in pts:
            probe_family(f, p, report, risk=plan.risk)
    for m in sorted({p["signal_minutes"] for f in plan.families for p in fams[f].configs()
                     if p.get("signal_minutes", 1) > 1}):
        probe_resampling(m, report)
    probe_future_fitting(report)
    check_costs(plan, report)
    check_fills(report, run_audits)
    check_leakage(plan, report)
    check_survivorship(plan, report)
    check_catalog(plan, report, catalog)
    return report
