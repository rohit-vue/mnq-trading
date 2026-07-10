#!/usr/bin/env python3
"""
Dump ALL fields from IBKR reqMktData ticker updates.

The existing scripts/check_live_price.py only prints last/bid/ask/close.
This script prints every attribute on the Ticker object so you can see
exactly what IB is sending.

Usage:
    python scripts/dump_tick_fields.py
    python scripts/dump_tick_fields.py --duration 30
    python scripts/dump_tick_fields.py --every-update
    python scripts/dump_tick_fields.py --local-symbol MNQU6 --live-only

Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from ib_async import IB, Future

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.load_env import load_project_dotenv

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("US/Eastern")
except ImportError:
    import pytz

    ET = pytz.timezone("US/Eastern")

# Dedicated id so this can run beside the bot (client_id=2) and round-trip test (77).
DEFAULT_CLIENT_ID = 88

# Common Ticker fields we always try to show first (then any extras).
PRIORITY_FIELDS = [
    "time",
    "marketDataType",
    "last",
    "lastSize",
    "bid",
    "bidSize",
    "ask",
    "askSize",
    "high",
    "low",
    "close",
    "open",
    "volume",
    "vwap",
    "halted",
    "prevAsk",
    "prevBid",
    "prevLast",
    "rtVolume",
    "rtTradeVolume",
    "rtTime",
    "avVolume",
    "impliedVolatility",
    "histVolatility",
    "modelGreeks",
    "lastGreeks",
    "bidGreeks",
    "askGreeks",
    "ticks",
    "tickByTicks",
    "domBids",
    "domAsks",
    "bboExchange",
    "snapshotPermissions",
]


def load_ibkr_config() -> dict:
    path = ROOT / "config" / "ibkr.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


def resolve_client_id(override: int | None) -> int:
    if override is not None:
        return override
    raw = os.environ.get("IB_CLIENT_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_CLIENT_ID


def _fmt(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return f"{value:.4f}".rstrip("0").rstrip(".") if abs(value) < 1e10 else str(value)
    if isinstance(value, datetime):
        ts = value
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " ET"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        # Keep lists readable; truncate long tick histories.
        if len(value) > 5:
            head = ", ".join(_fmt(v) for v in value[:3])
            return f"[{head}, ... +{len(value) - 3} more]"
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    text = str(value)
    if len(text) > 120:
        return text[:117] + "..."
    return text


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and (math.isnan(value) or value != value):
        return True
    if isinstance(value, (list, tuple, dict, set)) and len(value) == 0:
        return True
    return False


def collect_ticker_fields(ticker) -> dict[str, Any]:
    """Collect all public attributes from an ib_async Ticker."""
    fields: dict[str, Any] = {}

    # dataclass / slots style
    for name in getattr(ticker, "__dataclass_fields__", {}) or {}:
        if name.startswith("_"):
            continue
        try:
            fields[name] = getattr(ticker, name)
        except Exception as exc:
            fields[name] = f"<error: {exc}>"

    # fallback: dir()
    for name in dir(ticker):
        if name.startswith("_"):
            continue
        if name in fields:
            continue
        try:
            val = getattr(ticker, name)
        except Exception:
            continue
        if callable(val):
            continue
        fields[name] = val

    return fields


def print_ticker_dump(ticker, *, show_empty: bool, dump_n: int) -> None:
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    fields = collect_ticker_fields(ticker)

    print()
    print("=" * 78)
    print(f"TICK DUMP #{dump_n}  local={now} ET")
    print("=" * 78)

    # Priority fields first
    shown = set()
    for name in PRIORITY_FIELDS:
        if name not in fields:
            continue
        val = fields[name]
        shown.add(name)
        if _is_empty(val) and not show_empty:
            continue
        print(f"  {name:<22} = {_fmt(val)}")

    # Remaining fields
    extras = sorted(k for k in fields if k not in shown)
    if extras:
        print("  --- other attributes ---")
        for name in extras:
            val = fields[name]
            if _is_empty(val) and not show_empty:
                continue
            print(f"  {name:<22} = {_fmt(val)}")

    print("=" * 78, flush=True)


async def resolve_contract(ib: IB, symbol: str, local_symbol: str | None, expiry: str | None):
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
            raise RuntimeError(f"No {symbol} contract details")
        details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        contract = details[0].contract

    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise RuntimeError("Could not qualify contract")
    return qualified[0]


async def run(args: argparse.Namespace) -> int:
    load_project_dotenv()
    ibkr_cfg = load_ibkr_config()
    host = ibkr_cfg.get("connection", {}).get("host", "127.0.0.1")
    port = resolve_port(ibkr_cfg, args.mode)
    client_id = resolve_client_id(args.client_id)

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

    contract = await resolve_contract(ib, args.symbol, args.local_symbol, args.expiry)
    print(f"Contract: {contract.localSymbol} ({contract.lastTradeDateOrContractMonth})")

    # Prefer live; fall back to delayed only if config allows and not --live-only
    accept_delayed = (
        False
        if args.live_only
        else ibkr_cfg.get("data", {}).get("delayed_data", {}).get("accept", False)
    )
    types_to_try = [1] if not accept_delayed else [1, 3, 4]

    ticker = None
    for mkt_type in types_to_try:
        label = {1: "LIVE", 3: "DELAYED", 4: "DELAYED-FROZEN"}.get(mkt_type, str(mkt_type))
        print(f"reqMarketDataType({mkt_type}) — {label}")
        ib.reqMarketDataType(mkt_type)
        await asyncio.sleep(0.3)
        if ticker is not None:
            ib.cancelMktData(contract)
            await asyncio.sleep(0.2)
        ticker = ib.reqMktData(contract, "", False, False)
        for _ in range(20):
            await asyncio.sleep(0.5)
            last = getattr(ticker, "last", None)
            bid = getattr(ticker, "bid", None)
            if any(
                isinstance(v, (int, float)) and v == v and v > 0
                for v in (last, bid, getattr(ticker, "ask", None), getattr(ticker, "close", None))
            ):
                print(f"[OK] Quotes flowing (marketDataType={getattr(ticker, 'marketDataType', '?')})")
                break
        else:
            print(f"[!] No ticks yet for type {label}")
            continue
        break

    if ticker is None:
        print("[X] No ticker")
        ib.disconnect()
        return 1

    dump_n = 0
    last_sig: str | None = None

    def maybe_dump(force: bool = False) -> None:
        nonlocal dump_n, last_sig
        # Signature of key quote fields — dump on change or force
        sig = (
            f"{getattr(ticker, 'last', None)}|"
            f"{getattr(ticker, 'bid', None)}|"
            f"{getattr(ticker, 'ask', None)}|"
            f"{getattr(ticker, 'lastSize', None)}|"
            f"{getattr(ticker, 'volume', None)}|"
            f"{getattr(ticker, 'time', None)}"
        )
        if not force and not args.every_update and sig == last_sig:
            return
        last_sig = sig
        dump_n += 1
        print_ticker_dump(ticker, show_empty=args.show_empty, dump_n=dump_n)

    if args.every_update:
        ticker.updateEvent += lambda _t=ticker: maybe_dump(force=True)

    # First full dump after a short settle
    await asyncio.sleep(1.0)
    maybe_dump(force=True)

    deadline = asyncio.get_event_loop().time() + args.duration if args.duration > 0 else None
    print(
        f"\nDumping ticker fields every {args.interval}s "
        f"({'every update' if args.every_update else 'on change / interval'}). "
        "Ctrl+C to stop.\n"
    )
    try:
        while True:
            if not args.every_update:
                maybe_dump(force=False)
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                print(f"\nDuration {args.duration}s reached — exiting.")
                break
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        ib.cancelMktData(contract)
        ib.disconnect()
        print("[OK] Disconnected")

    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dump all IBKR reqMktData ticker fields")
    p.add_argument("--mode", choices=("paper", "live"), default="paper")
    p.add_argument("--symbol", default="MNQ")
    p.add_argument("--local-symbol", default="MNQU6")
    p.add_argument("--expiry", default=None)
    p.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    p.add_argument("--live-only", action="store_true", help="reqMarketDataType(1) only")
    p.add_argument(
        "--every-update",
        action="store_true",
        help="Dump on every ticker.updateEvent (can be very chatty)",
    )
    p.add_argument(
        "--show-empty",
        action="store_true",
        help="Also print None / nan / empty list fields",
    )
    p.add_argument("--interval", type=float, default=2.0, help="Heartbeat seconds (default 2)")
    p.add_argument("--duration", type=int, default=0, help="Stop after N seconds (0=forever)")
    return p.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
    main()
