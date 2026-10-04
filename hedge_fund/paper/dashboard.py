"""Terminal dashboard for a paper run.

    python -m hedge_fund.paper.dashboard [STATE_DIR] [--once]

Reads STATE_DIR/snapshot.json (written by PaperMonitor) and refreshes every 2 seconds.
Read-only: it never sends orders or changes state.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table

from hedge_fund.paths import CACHE_DIR

COLORS = {"HEALTHY": "green", "WARNING": "yellow", "DEGRADED": "dark_orange", "HALT": "red"}


def _f(x, fmt="{:,.2f}"):
    return "—" if x is None else fmt.format(x)


def render(snap: dict) -> Group:
    age = time.time() - snap.get("wall", 0)
    head = Table.grid(padding=(0, 2))
    h = snap.get("node_health", "?")
    head.add_row(f"[bold]Node health[/] [{COLORS.get(h, 'white')}]{h}[/]", f"snapshot age {age:.0f}s",
                 f"realized {_f(snap.get('realized_pnl'))} (net of fees {_f(snap.get('realized_pnl_net'))})", f"unrealized {_f(snap.get('unrealized_pnl'))}",
                 f"fees {_f(snap.get('fees'))}", f"avg slippage {_f(snap.get('avg_slippage_bps'), '{:.2f} bp')}",
                 f"rejected/denied {snap.get('rejected_orders', 0)}")
    st = Table(title="Strategies", expand=True)
    for c in ("strategy", "market", "health", "connection", "data age", "equity", "drawdown", "last signal"):
        st.add_column(c)
    for s in snap.get("strategies", []):
        sig = s.get("last_signal") or {}
        st.add_row(s["strategy"], s["instrument"], f"[{COLORS.get(s['health'], 'white')}]{s['health']}[/]",
                   s["connection"], _f(s.get("data_age_secs"), "{:.0f}s"), _f(s.get("equity")),
                   _f(s.get("drawdown"), "{:.1%}"), f"{sig.get('kind', '—')} {sig.get('side', sig.get('reason', ''))}")
    pt = Table(title="Open positions", expand=True)
    for c in ("market", "side", "qty", "entry", "SL", "TP", "last", "unrealized", "fees"):
        pt.add_column(c)
    for p in snap.get("positions", []):
        pt.add_row(p["instrument"], p["side"], _f(p["quantity"], "{:g}"), _f(p["entry"]), _f(p["stop_loss"]),
                   _f(p["take_profit"]), _f(p["last"]), _f(p["unrealized_pnl"]), _f(p["fees"]))
    ev = Table(title="Recent events", expand=True)
    for c in ("kind", "strategy", "detail"):
        ev.add_column(c)
    for e in snap.get("recent_events", [])[-10:]:
        detail = {k: v for k, v in e.items() if k not in ("kind", "strategy", "wall", "ts", "instrument")}
        ev.add_row(e["kind"], e.get("strategy", ""), json.dumps(detail, default=str)[:90])
    return Group(head, st, pt, ev)


def main(argv: list[str]) -> int:
    once = "--once" in argv
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0] if args else CACHE_DIR / "paper") / "snapshot.json"
    console = Console()
    if not path.exists():
        console.print(f"no snapshot at {path}")
        return 1
    if once:
        console.print(render(json.loads(path.read_text())))
        return 0
    with Live(render(json.loads(path.read_text())), console=console, refresh_per_second=1) as live:
        while True:
            time.sleep(2)
            try:
                live.update(render(json.loads(path.read_text())))
            except (json.JSONDecodeError, FileNotFoundError):
                continue


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
