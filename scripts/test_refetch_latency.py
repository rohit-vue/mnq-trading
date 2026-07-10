#!/usr/bin/env python3
"""
Measure IBKR historical refetch latency (same API path as RealtimeFeed.poll_refetch_and_emit).

Usage:
    python scripts/test_refetch_latency.py
    python scripts/test_refetch_latency.py --count 5
    python scripts/test_refetch_latency.py --watch-boundaries --duration 600
    python scripts/test_refetch_latency.py --bar-size "5 mins" --grace 1.0

Modes:
  default       Run --count refetches back-to-back (API round-trip only).
  --watch-boundaries
                At each primary bar boundary + --grace seconds, fire one refetch
                and print how long after the boundary IB returned the latest bar.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from ib_async import IB, Future

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.realtime_feed import bar_size_to_seconds, expected_closed_bar_ts
from utils.load_env import load_project_dotenv

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("US/Eastern")
except ImportError:
    import pytz

    ET = pytz.timezone("US/Eastern")


def load_ibkr_config() -> dict:
    path = ROOT / "config" / "ibkr.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_strategy_bar_size() -> str:
    path = ROOT / "config" / "strategy.yaml"
    if not path.exists():
        return "5 mins"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    primary = (cfg.get("timeframes") or {}).get("primary", "5m")
    mapping = {
        "5m": "5 mins",
        "10m": "10 mins",
        "15m": "15 mins",
        "30m": "30 mins",
        "45m": "45 mins",
        "1h": "1 hour",
    }
    return mapping.get(str(primary).lower(), "5 mins")


def resolve_port(ibkr_cfg: dict, mode: str) -> int:
    conn = ibkr_cfg.get("connection", {})
    ports = conn.get("ports", {})
    gateway = conn.get("default_gateway", "tws")
    if mode == "paper":
        if gateway == "gateway":
            return int(ports.get("gateway_paper", 4002))
        return int(ports.get("tws_paper", 7497))
    if gateway == "gateway":
        return int(ports.get("gateway_live", 4001))
    return int(ports.get("tws_live", 7496))


def resolve_client_id(ibkr_cfg: dict, override: int | None) -> int:
    if override is not None:
        return override
    raw = os.environ.get("IB_CLIENT_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    cfg_id = ibkr_cfg.get("connection", {}).get("client_id")
    if cfg_id is not None:
        return int(cfg_id) + 30
    return 99


def fmt_ts(dt: Any) -> str:
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        ts = dt
    else:
        ts = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S %Z")


async def resolve_contract(
    ib: IB,
    symbol: str,
    local_symbol: str | None,
    expiry: str | None,
):
    if expiry:
        contract = Future(
            symbol=symbol,
            lastTradeDateOrContractMonth=expiry,
            exchange="CME",
            currency="USD",
        )
    elif local_symbol:
        contract = Future(
            symbol=symbol,
            localSymbol=local_symbol,
            exchange="CME",
            currency="USD",
        )
    else:
        base = Future(symbol=symbol, exchange="CME", currency="USD")
        details = await ib.reqContractDetailsAsync(base)
        if not details:
            raise RuntimeError(f"No {symbol} contract details from IBKR")
        details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        contract = details[0].contract

    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise RuntimeError("Could not qualify contract")
    return qualified[0]


async def refetch_bars(
    ib: IB,
    contract,
    *,
    bar_size: str,
    lookback_days: int,
) -> tuple[list, float]:
    """Same one-shot historical request as poll_refetch_and_emit."""
    t0 = time.perf_counter()
    bars = await ib.reqHistoricalDataAsync(
        contract=contract,
        endDateTime="",
        durationStr=f"{lookback_days} D",
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
        keepUpToDate=False,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return list(bars or []), elapsed_ms


def summarize_bars(bars: list) -> dict:
    if not bars:
        return {
            "count": 0,
            "forming": None,
            "latest_closed": None,
            "forming_close": None,
            "closed_close": None,
        }
    forming = bars[-1]
    closed = bars[-2] if len(bars) >= 2 else None
    return {
        "count": len(bars),
        "forming": getattr(forming, "date", None),
        "latest_closed": getattr(closed, "date", None) if closed else None,
        "forming_close": float(getattr(forming, "close", 0) or 0),
        "closed_close": float(getattr(closed, "close", 0) or 0) if closed else None,
    }


def print_refetch_result(
    *,
    label: str,
    api_ms: float,
    summary: dict,
    request_wall: datetime,
    sec_since_boundary: Optional[float] = None,
) -> None:
    parts = [
        f"[{label}]",
        f"request_at={fmt_ts(request_wall)}",
        f"api_ms={api_ms:.0f}",
        f"bars={summary['count']}",
    ]
    if sec_since_boundary is not None:
        parts.append(f"sec_since_boundary={sec_since_boundary:.2f}")
    if summary["latest_closed"] is not None:
        parts.append(f"closed_bar={fmt_ts(summary['latest_closed'])}")
        parts.append(f"C={summary['closed_close']:.2f}")
    if summary["forming"] is not None:
        parts.append(f"forming_bar={fmt_ts(summary['forming'])}")
        parts.append(f"forming_C={summary['forming_close']:.2f}")
    print(" | ".join(parts), flush=True)


async def run_burst(ib: IB, contract, args: argparse.Namespace) -> None:
    print(f"\nBurst mode: {args.count} refetch(es), bar_size={args.bar_size!r}\n")
    times: list[float] = []
    for i in range(1, args.count + 1):
        request_wall = datetime.now(timezone.utc)
        bars, api_ms = await refetch_bars(
            ib,
            contract,
            bar_size=args.bar_size,
            lookback_days=args.lookback_days,
        )
        times.append(api_ms)
        print_refetch_result(
            label=f"#{i}",
            api_ms=api_ms,
            summary=summarize_bars(bars),
            request_wall=request_wall,
        )
        if i < args.count and args.pause > 0:
            await asyncio.sleep(args.pause)

    if times:
        avg = sum(times) / len(times)
        print(
            f"\nSummary: min={min(times):.0f}ms avg={avg:.0f}ms max={max(times):.0f}ms "
            f"({len(times)} calls)\n"
        )


async def run_watch_boundaries(ib: IB, contract, args: argparse.Namespace) -> None:
    interval_sec = bar_size_to_seconds(args.bar_size)
    grace = args.grace
    print(
        f"\nWatch mode: bar_size={args.bar_size!r} interval={interval_sec}s "
        f"grace={grace}s (refetch at boundary+{grace}s)\n"
    )
    deadline = time.time() + args.duration if args.duration > 0 else None
    last_fired_for: Optional[str] = None

    while True:
        now_et = datetime.now(ET)
        expected_closed, sec_into_bar = expected_closed_bar_ts(
            now_et, interval_sec, ET
        )
        boundary_key = expected_closed.isoformat()

        if (
            sec_into_bar >= grace
            and sec_into_bar <= grace + args.window
            and boundary_key != last_fired_for
        ):
            request_wall = datetime.now(timezone.utc)
            bars, api_ms = await refetch_bars(
                ib,
                contract,
                bar_size=args.bar_size,
                lookback_days=args.lookback_days,
            )
            print_refetch_result(
                label="BOUNDARY",
                api_ms=api_ms,
                summary=summarize_bars(bars),
                request_wall=request_wall,
                sec_since_boundary=sec_into_bar,
            )
            last_fired_for = boundary_key

        if deadline is not None and time.time() >= deadline:
            print(f"\nDuration {args.duration}s reached — exiting.\n")
            break
        await asyncio.sleep(0.25)


async def run(args: argparse.Namespace) -> int:
    load_project_dotenv()
    ibkr_cfg = load_ibkr_config()
    host = ibkr_cfg.get("connection", {}).get("host", "127.0.0.1")
    port = resolve_port(ibkr_cfg, args.mode)
    client_id = resolve_client_id(ibkr_cfg, args.client_id)

    ib = IB()

    def on_error(req_id, error_code, error_string, contract) -> None:
        if error_code in (10349, 2104, 2106, 2158):
            return
        print(f"[IB Error {error_code}] {error_string}", flush=True)

    ib.errorEvent += on_error

    print(f"Connecting to {host}:{port} (mode={args.mode}, client_id={client_id}) ...")
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    if not ib.isConnected():
        print("[X] Connection failed")
        return 1
    print("[OK] Connected")

    contract = await resolve_contract(
        ib, args.symbol, args.local_symbol, args.expiry
    )
    print(f"Contract: {contract.localSymbol} | refetch bar_size={args.bar_size}")

    try:
        if args.watch_boundaries:
            await run_watch_boundaries(ib, contract, args)
        else:
            await run_burst(ib, contract, args)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        ib.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benchmark IBKR historical refetch latency (poll_refetch path)."
    )
    p.add_argument("--mode", choices=("paper", "live"), default="paper")
    p.add_argument("--symbol", default="MNQ")
    p.add_argument("--local-symbol", default="MNQU6")
    p.add_argument("--expiry", default=None)
    p.add_argument("--client-id", type=int, default=None)
    p.add_argument(
        "--bar-size",
        default=None,
        help='IB bar size (default: from strategy.yaml primary, else "5 mins")',
    )
    p.add_argument("--lookback-days", type=int, default=1)
    p.add_argument(
        "--count",
        type=int,
        default=3,
        help="Burst mode: number of refetches (default: 3)",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=2.0,
        help="Seconds between burst refetches (default: 2)",
    )
    p.add_argument(
        "--watch-boundaries",
        action="store_true",
        help="Refetch at each bar boundary + grace (measures post-close timing)",
    )
    p.add_argument(
        "--grace",
        type=float,
        default=1.0,
        help="Seconds after boundary before refetch in watch mode (default: 1)",
    )
    p.add_argument(
        "--window",
        type=float,
        default=12.0,
        help="Seconds after grace to allow one refetch per boundary (default: 12)",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Watch mode: stop after N seconds (0 = until Ctrl+C)",
    )
    args = p.parse_args()
    if args.bar_size is None:
        args.bar_size = load_strategy_bar_size()
    return args


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
