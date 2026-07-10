#!/usr/bin/env python3
"""
Standalone IBKR round-trip test for MNQ.

Opens 1 contract (market), waits N seconds, then closes with a market order.

Usage (paper, default):
    python scripts/test_round_trip_trade.py

    python scripts/test_round_trip_trade.py --wait 15 --direction long

Uses client_id=77 by default (avoids clash with main bot client_id=2).
Prints place->fill latency in ms for entry and exit.

Requires TWS or IB Gateway running with API enabled.
Uses config/ibkr.yaml for host, port, and gateway vs TWS.

If you see Error 10197 or 354:
  1. Close any LIVE TWS/Gateway session (paper cannot share live CME data).
  2. TWS/Gateway -> Global Configuration -> Market Data -> enable delayed data.
  3. Client Portal -> Settings -> complete "Market Data API Acknowledgement".
  4. TWS -> Global Configuration -> API -> Precautions ->
     check "Bypass Order Precautions for API Orders" (required when using delayed quotes).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from ib_async import IB, Future, MarketOrder

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.load_env import load_project_dotenv


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


# Dedicated test client id — keeps this script off the main bot (usually client_id=2).
DEFAULT_TEST_CLIENT_ID = 77


def resolve_client_id(ibkr_cfg: dict, override: int | None) -> int:
    if override is not None:
        return override
    raw = os.environ.get("IB_CLIENT_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_TEST_CLIENT_ID


def _now_label() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]


def _valid_price(value) -> bool:
    if value is None:
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _ticker_price(ticker) -> float | None:
    for field in ("last", "close", "marketPrice", "bid", "ask"):
        val = getattr(ticker, field, None)
        if _valid_price(val):
            return float(val)
    return None


class IbErrorTracker:
    """Collect IB API errors during the test for actionable messages."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.messages: list[str] = []
        self.had_quote = False

    def on_error(self, req_id: int, error_code: int, error_string: str, contract) -> None:
        # 10349 is informational (TIF preset); 2104-2106 are farm connection OK
        if error_code in (10349, 2104, 2106, 2158):
            return
        self.codes.append(error_code)
        self.messages.append(f"Error {error_code}, reqId {req_id}: {error_string}")

    def saw(self, code: int) -> bool:
        return code in self.codes

    def print_recent(self) -> None:
        for msg in self.messages[-5:]:
            print(f"  {msg}")


def _market_data_fix_hints(tracker: IbErrorTracker, has_quote: bool = False) -> str:
    lines = [
        "IBKR blocked the order. Try:",
        "  1. Close LIVE TWS/Gateway — only one session gets CME live data (Error 10197).",
        "  2. Global Configuration -> Market Data -> enable delayed market data.",
        "  3. Client Portal -> Settings -> complete Market Data API Acknowledgement.",
        "  4. Global Configuration -> API -> Precautions ->",
        '     check "Bypass Order Precautions for API Orders".',
        "  5. Restart TWS/Gateway after changing settings.",
    ]
    if tracker.saw(10186):
        lines.insert(2, "  * Error 10186: delayed data is not enabled in TWS/Gateway.")
    if tracker.saw(354):
        if has_quote:
            lines.append(
                "  * Error 354 with quotes visible: delayed data does NOT satisfy IB's "
                "order precaution — use step 4 (bypass) or subscribe to CME real-time."
            )
        else:
            lines.append(
                "  * Error 354: no quote on this API session — fix market data first."
            )
    return "\n".join(lines)


async def _wait_for_ticker(
    ticker,
    label: str,
    *,
    loops: int = 40,
    sleep_sec: float = 0.5,
) -> float | None:
    """Wait until we have a usable price; prefer when IB reports marketDataType."""
    price: float | None = None
    for i in range(loops):
        await asyncio.sleep(sleep_sec)
        price = _ticker_price(ticker)
        if price is None:
            continue
        mdt = getattr(ticker, "marketDataType", None)
        if mdt is None and i < loops - 1:
            continue
        mdt_label = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}.get(
            int(mdt), "unknown" if mdt is None else str(mdt)
        )
        print(
            f"[OK] Market data ({label}, type={mdt_label}): last={ticker.last} "
            f"bid={ticker.bid} ask={ticker.ask} (using {price:.2f})"
        )
        if mdt is not None and int(mdt) in (3, 4):
            print(
                "[i] Delayed quote — IB may still block orders unless "
                '"Bypass Order Precautions for API Orders" is enabled in TWS.'
            )
        return price
    return None


async def enable_market_data(ib: IB, contract, tracker: IbErrorTracker) -> tuple[object | None, float | None]:
    """
    Subscribe to MNQ quotes (delayed if needed).

    Note: delayed quotes alone do not satisfy IB's order precaution (Error 354).
    Enable API precaution bypass in TWS or subscribe to CME real-time data.
    """
    data_cfg = load_ibkr_config().get("data", {})
    delayed_cfg = data_cfg.get("delayed_data", {})
    accept_delayed = delayed_cfg.get("accept", True)

    # Try delayed, then delayed-frozen when live competes with paper (Error 10197).
    types_to_try = [3, 4] if accept_delayed else [1]
    ticker = None
    price: float | None = None

    for mkt_type in types_to_try:
        label = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}.get(mkt_type, str(mkt_type))
        ib.reqMarketDataType(mkt_type)
        if ticker is not None:
            ib.cancelMktData(contract)
        ticker = ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(1.0)
        price = await _wait_for_ticker(ticker, label)
        if price is not None:
            return ticker, price
        if tracker.saw(10197) and mkt_type == 1:
            break

    # Snapshot fallback (sometimes works when streaming does not).
    try:
        snap = await ib.reqTickersAsync(contract)
        if snap:
            price = _ticker_price(snap[0])
            if price is not None:
                print(f"[OK] Snapshot quote: {price:.2f}")
                return snap[0], price
    except Exception as exc:
        print(f"[!] Snapshot quote failed: {exc}")

    print("[!] No MNQ ticks received on this API session.")
    if tracker.messages:
        print("    Recent IB messages:")
        tracker.print_recent()
    print(f"\n{_market_data_fix_hints(tracker)}\n")
    return ticker, None


async def get_front_mnq(ib: IB):
    base = Future(symbol="MNQ", exchange="CME", currency="USD")
    details = await ib.reqContractDetailsAsync(base)
    if not details:
        raise RuntimeError("No MNQ contract details from IBKR")
    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    contract = details[0].contract
    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise RuntimeError("Could not qualify MNQ contract")
    return qualified[0]


async def wait_for_fill(trade, tracker: IbErrorTracker, timeout_sec: float = 90.0):
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        status = trade.orderStatus.status
        if status == "Filled":
            return trade
        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            err = trade.advancedError or ""
            for entry in reversed(trade.log):
                if entry.errorCode and entry.errorCode not in (10349,):
                    err = entry.message or err
                    break
            hint = ""
            if tracker.saw(354) or tracker.saw(10197):
                hint = f"\n{_market_data_fix_hints(tracker, has_quote=tracker.had_quote)}"
            raise RuntimeError(f"Order ended with status={status}. {err}{hint}")
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"Timed out waiting for fill (last status={trade.orderStatus.status})"
    )


def _make_market_order(action: str, qty: int) -> MarketOrder:
    # Explicit DAY TIF avoids some TWS preset cancellations (Error 10349 noise).
    return MarketOrder(action=action, totalQuantity=qty, tif="DAY", transmit=True)


async def place_and_fill(
    ib: IB,
    contract,
    action: str,
    qty: int,
    tracker: IbErrorTracker,
    *,
    label: str,
) -> tuple[float, dict]:
    """
    Place a market order and wait for fill.

    Returns (fill_price, timing_dict) where timing has:
      place_ms  — time until placeOrder() returns
      fill_ms   — time from placeOrder until status=Filled
      total_ms  — place + fill wait
    """
    print(f"\n>>> [{label}] Placing MARKET {action} x{qty} @ {_now_label()} ...")
    order = _make_market_order(action, qty)

    t0 = time.perf_counter()
    trade = ib.placeOrder(contract, order)
    place_ms = (time.perf_counter() - t0) * 1000.0
    print(f"    placeOrder returned in {place_ms:.0f} ms (orderId={order.orderId})")

    t_wait = time.perf_counter()
    await wait_for_fill(trade, tracker)
    fill_ms = (time.perf_counter() - t_wait) * 1000.0
    total_ms = (time.perf_counter() - t0) * 1000.0

    px = float(trade.orderStatus.avgFillPrice)
    print(
        f"[OK] [{label}] Filled {action} x{qty} @ {px:.2f} | "
        f"place={place_ms:.0f}ms fill_wait={fill_ms:.0f}ms total={total_ms:.0f}ms "
        f"@ {_now_label()}"
    )
    return px, {
        "label": label,
        "action": action,
        "place_ms": place_ms,
        "fill_ms": fill_ms,
        "total_ms": total_ms,
        "fill_price": px,
        "order_id": order.orderId,
    }


async def run_test(args: argparse.Namespace) -> int:
    load_project_dotenv()
    ibkr_cfg = load_ibkr_config()
    host = ibkr_cfg.get("connection", {}).get("host", "127.0.0.1")
    port = resolve_port(ibkr_cfg, args.mode)
    client_id = resolve_client_id(ibkr_cfg, args.client_id)

    if args.mode == "live" and not args.confirm_live:
        print("Live mode requires --confirm-live (real money).")
        return 1

    ib = IB()
    tracker = IbErrorTracker()
    ib.errorEvent += tracker.on_error

    print(f"Connecting to IBKR {args.mode.upper()} at {host}:{port} (client_id={client_id}) ...")
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    if not ib.isConnected():
        print("[X] Connection failed")
        return 1
    print("[OK] Connected")

    try:
        contract = await get_front_mnq(ib)
        print(f"[OK] Contract: {contract.localSymbol}")

        ticker, quote = await enable_market_data(ib, contract, tracker)
        tracker.had_quote = quote is not None
        if quote is None and not args.force:
            print(
                "[X] Aborting before order — no market data on this session.\n"
                "    Re-run with --force to attempt the order anyway (likely Error 354)."
            )
            return 1

        # Keep streaming subscription active; IB precaution checks the open quote feed.
        if ticker is not None:
            await asyncio.sleep(2.0)

        open_action = "BUY" if args.direction == "long" else "SELL"
        close_action = "SELL" if args.direction == "long" else "BUY"

        trip_t0 = time.perf_counter()
        entry_px, entry_timing = await place_and_fill(
            ib, contract, open_action, args.contracts, tracker, label="ENTRY"
        )
        print(f"\n--- Holding for {args.wait} seconds ---")
        await asyncio.sleep(args.wait)
        exit_px, exit_timing = await place_and_fill(
            ib, contract, close_action, args.contracts, tracker, label="EXIT"
        )
        trip_ms = (time.perf_counter() - trip_t0) * 1000.0

        if args.direction == "long":
            pnl_pts = exit_px - entry_px
        else:
            pnl_pts = entry_px - exit_px
        pnl_usd = pnl_pts * 2 * args.contracts  # MNQ $2/point

        print("\n========== ROUND TRIP COMPLETE ==========")
        print(f"Direction : {args.direction.upper()}")
        print(f"Client ID : {client_id}")
        print(f"Entry     : {entry_px:.2f}  (orderId={entry_timing['order_id']})")
        print(
            f"  ENTRY ms : place={entry_timing['place_ms']:.0f}  "
            f"fill_wait={entry_timing['fill_ms']:.0f}  "
            f"total={entry_timing['total_ms']:.0f}"
        )
        print(f"Exit      : {exit_px:.2f}  (orderId={exit_timing['order_id']})")
        print(
            f"  EXIT ms  : place={exit_timing['place_ms']:.0f}  "
            f"fill_wait={exit_timing['fill_ms']:.0f}  "
            f"total={exit_timing['total_ms']:.0f}"
        )
        print(
            f"Orders ms : entry+exit={entry_timing['total_ms'] + exit_timing['total_ms']:.0f}  "
            f"(excludes {args.wait}s hold)"
        )
        print(f"Wall ms   : {trip_ms:.0f}  (includes {args.wait}s hold)")
        print(f"P&L       : {pnl_pts:+.2f} pts (${pnl_usd:+.2f} approx, before fees)")
        print("=========================================\n")
        return 0
    finally:
        ib.disconnect()
        print("[OK] Disconnected")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MNQ market round-trip test (open, wait, close)")
    p.add_argument("--mode", choices=("paper", "live"), default="paper")
    p.add_argument("--direction", choices=("long", "short"), default="long")
    p.add_argument("--contracts", type=int, default=1)
    p.add_argument(
        "--wait",
        type=int,
        default=15,
        help="Seconds to hold before closing (default: 15)",
    )
    p.add_argument(
        "--client-id",
        type=int,
        default=DEFAULT_TEST_CLIENT_ID,
        help=f"IB API client ID (default: {DEFAULT_TEST_CLIENT_ID}, bot usually uses 2)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Place orders even when no quote was received (usually fails with Error 354)",
    )
    p.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required when --mode live",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.contracts < 1:
        print("contracts must be >= 1")
        sys.exit(1)
    if args.wait < 0:
        print("wait must be >= 0")
        sys.exit(1)
    try:
        code = asyncio.run(run_test(args))
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    except Exception as e:
        print(f"\n[X] Error: {e}")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
