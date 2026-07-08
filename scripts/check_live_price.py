#!/usr/bin/env python3
"""
Connect to IBKR TWS/Gateway and print MNQ quotes with timestamps.

Uses two official IB API paths (via ib_async):

1. reqMarketDataType + reqMktData
   - Types: 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-Frozen (IB docs)
   - Delayed is 15–20 min behind; ticker.marketDataType reports what IB sends.

2. reqRealTimeBars
   - 5-second OHLCV bars (TRADES / MIDPOINT / BID / ASK).
   - Separate from reqMktData; requires market data permissions.

Usage:
    python scripts/check_live_price.py
    python scripts/check_live_price.py --mode live
    python scripts/check_live_price.py --local-symbol MNQU6 --duration 120
    python scripts/check_live_price.py --mkt-only
    python scripts/check_live_price.py --rt-only

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
from zoneinfo import ZoneInfo

import yaml
from ib_async import IB, Future
from ib_async.objects import RealTimeBarList

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.load_env import load_project_dotenv

ET = ZoneInfo("US/Eastern")

# IB API marketDataType IDs (EWrapper.marketDataType)
MDT_LABELS = {
    1: "LIVE",
    2: "FROZEN",
    3: "DELAYED",
    4: "DELAYED-FROZEN",
}

MDT_DOC_NOTES = {
    1: "Live streaming (requires CME subscription)",
    2: "Frozen = last quote at market close",
    3: "Delayed 15–20 min (free); IB may still send LIVE if subscribed",
    4: "Delayed frozen fallback",
}


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


def resolve_client_id(ibkr_cfg: dict, override: int | None) -> int:
    if override is not None:
        return override
    raw = os.environ.get("IB_CLIENT_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    cfg_id = ibkr_cfg.get("connection", {}).get("client_id")
    if cfg_id is not None:
        return int(cfg_id) + 20
    return 98


def _valid_price(value) -> bool:
    if value is None:
        return False
    try:
        v = float(value)
        return math.isfinite(v) and v > 0
    except (TypeError, ValueError):
        return False


def _fmt_price(value) -> str:
    if not _valid_price(value):
        return "—"
    return f"{float(value):,.2f}"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def _lag_seconds(ib_time: datetime | None, local_now: datetime) -> str:
    if ib_time is None:
        return "—"
    if ib_time.tzinfo is None:
        ib_time = ib_time.replace(tzinfo=timezone.utc)
    lag = (local_now - ib_time.astimezone(timezone.utc)).total_seconds()
    if lag < 0:
        return f"{lag:+.1f}s (IB ahead of local clock)"
    return f"{lag:.1f}s"


def _mdt_label(mdt: Any) -> str:
    if mdt is None:
        return "UNKNOWN"
    try:
        return MDT_LABELS.get(int(mdt), str(mdt))
    except (TypeError, ValueError):
        return str(mdt)


async def resolve_contract(ib: IB, symbol: str, local_symbol: str | None, expiry: str | None):
    """Resolve MNQ: symbol=MNQ, localSymbol=MNQU6, exchange=CME (not symbol=MNQU6)."""
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
        label = local_symbol or expiry or symbol
        raise RuntimeError(f"Could not qualify {label} contract")
    return qualified[0]


def print_header(contract, *, use_mkt: bool, use_rt: bool) -> None:
    print()
    print("=" * 72)
    print("IBKR live price check (official reqMktData + reqRealTimeBars)")
    print("=" * 72)
    print(f"Contract : {contract.localSymbol} ({contract.lastTradeDateOrContractMonth})")
    if use_mkt:
        print("[MKT] reqMarketDataType -> reqMktData | columns: local | ib_tick | lag | type | last bid ask close")
    if use_rt:
        print("[RT5s] reqRealTimeBars (5s bars) | columns: local | bar_time | lag | O H L C vol")
    print("Press Ctrl+C to stop.")
    print("=" * 72)
    print()


def print_mkt_tick(ticker, *, force: bool = False) -> None:
    local_now = datetime.now(timezone.utc)
    ib_time = getattr(ticker, "time", None)
    mdt_label = _mdt_label(getattr(ticker, "marketDataType", None))

    last = getattr(ticker, "last", None)
    bid = getattr(ticker, "bid", None)
    ask = getattr(ticker, "ask", None)
    close = getattr(ticker, "close", None)

    has_quote = any(_valid_price(v) for v in (last, bid, ask, close))
    if not has_quote and not force:
        return

    line = (
        f"[MKT]  {_fmt_time(local_now):<24} | "
        f"{_fmt_time(ib_time):<24} | "
        f"lag {_lag_seconds(ib_time, local_now):<16} | "
        f"{mdt_label:<15} | "
        f"last={_fmt_price(last):>10} | "
        f"bid={_fmt_price(bid):>10} | "
        f"ask={_fmt_price(ask):>10} | "
        f"close={_fmt_price(close):>10}"
    )
    print(line, flush=True)


def print_rt_bar(bar) -> None:
    local_now = datetime.now(timezone.utc)
    bar_time = getattr(bar, "time", None)
    line = (
        f"[RT5s] {_fmt_time(local_now):<24} | "
        f"{_fmt_time(bar_time):<24} | "
        f"lag {_lag_seconds(bar_time, local_now):<16} | "
        f"O={_fmt_price(bar.open_):>10} | "
        f"H={_fmt_price(bar.high):>10} | "
        f"L={_fmt_price(bar.low):>10} | "
        f"C={_fmt_price(bar.close):>10} | "
        f"vol={int(bar.volume) if bar.volume == bar.volume else 0}"
    )
    print(line, flush=True)


async def subscribe_market_data(ib: IB, contract, *, live_only: bool) -> Any | None:
    """
    IB doc flow: reqMarketDataType(N) then reqMktData.
    Try types in order; ticker.marketDataType shows what IB actually sends.
    """
    data_cfg = load_ibkr_config().get("data", {})
    accept_delayed = data_cfg.get("delayed_data", {}).get("accept", True)

    if live_only or not accept_delayed:
        types_to_try = [1]
    else:
        # Official: type 3 may still deliver type 1 if user has live permissions
        types_to_try = [1, 3, 4]

    ticker = None
    for mkt_type in types_to_try:
        label = MDT_LABELS.get(mkt_type, str(mkt_type))
        note = MDT_DOC_NOTES.get(mkt_type, "")
        print(f"[MKT] reqMarketDataType({mkt_type}) — {label}: {note}")
        ib.reqMarketDataType(mkt_type)
        await asyncio.sleep(0.3)
        if ticker is not None:
            ib.cancelMktData(contract)
            await asyncio.sleep(0.2)
        ticker = ib.reqMktData(contract, "", False, False)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if any(
                _valid_price(getattr(ticker, f, None))
                for f in ("last", "bid", "ask", "close")
            ):
                reported = getattr(ticker, "marketDataType", mkt_type)
                active = _mdt_label(reported)
                requested = _mdt_label(mkt_type)
                if int(reported or mkt_type) != mkt_type:
                    print(
                        f"[MKT] Requested {requested}, IB reports marketDataType={active} "
                        f"(IB sends best available per subscription)"
                    )
                else:
                    print(f"[MKT] Receiving quotes — marketDataType={active}")
                return ticker
        print(f"[MKT] No ticks yet for requested type {label}")

    return ticker


async def subscribe_realtime_bars(
    ib: IB,
    contract,
    *,
    what_to_show: str,
    use_rth: bool,
) -> RealTimeBarList | None:
    """
    IB doc: reqRealTimeBars — 5-second bars only.
    Call reqMarketDataType(1) first when probing for live bars.
    """
    print("[RT5s] reqMarketDataType(1) before reqRealTimeBars (live preferred) ...")
    ib.reqMarketDataType(1)
    await asyncio.sleep(0.3)
    print(
        f"[RT5s] reqRealTimeBars(barSize=5, whatToShow={what_to_show}, "
        f"useRTH={use_rth}) ..."
    )
    bars = ib.reqRealTimeBars(contract, 5, what_to_show, use_rth, [])
    for _ in range(30):
        await asyncio.sleep(0.5)
        if len(bars) > 0:
            print(f"[RT5s] Receiving 5s bars (count={len(bars)})")
            print_rt_bar(bars[-1])
            return bars
    print("[RT5s] No real-time bars yet (needs market data subscription / live permissions)")
    return bars


async def run(args: argparse.Namespace) -> int:
    load_project_dotenv()
    ibkr_cfg = load_ibkr_config()
    host = ibkr_cfg.get("connection", {}).get("host", "127.0.0.1")
    port = resolve_port(ibkr_cfg, args.mode)
    client_id = resolve_client_id(ibkr_cfg, args.client_id)

    use_mkt = not args.rt_only
    use_rt = not args.mkt_only

    ib = IB()
    last_mdt_reported: dict[int, str] = {}

    def on_error(req_id, error_code, error_string, contract) -> None:
        if error_code in (10349, 2104, 2106, 2158):
            return
        print(f"[IB Error {error_code}] {error_string}", flush=True)

    def on_pending_tickers(tickers) -> None:
        for t in tickers:
            mdt = getattr(t, "marketDataType", None)
            if mdt is None:
                continue
            key = id(t)
            label = _mdt_label(mdt)
            if last_mdt_reported.get(key) != label:
                last_mdt_reported[key] = label
                print(f"[MKT] marketDataType callback -> {label} (id={mdt})", flush=True)

    ib.errorEvent += on_error
    ib.pendingTickersEvent += on_pending_tickers

    print(f"Connecting to {host}:{port} (mode={args.mode}, client_id={client_id}) ...")
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    if not ib.isConnected():
        print("[X] Connection failed — is TWS/Gateway running with API enabled?")
        return 1
    print("[OK] Connected")

    contract = await resolve_contract(
        ib, args.symbol, args.local_symbol, args.expiry
    )
    print_header(contract, use_mkt=use_mkt, use_rt=use_rt)

    ticker = None
    rt_bars: RealTimeBarList | None = None

    if use_mkt:
        ticker = await subscribe_market_data(
            ib, contract, live_only=args.live_only
        )
        if ticker is None:
            print("[MKT] Could not subscribe to market data")
        else:
            ticker.updateEvent += lambda _t=ticker: print_mkt_tick(_t)

    if use_rt:
        rt_bars = await subscribe_realtime_bars(
            ib,
            contract,
            what_to_show=args.what_to_show,
            use_rth=args.use_rth,
        )
        if rt_bars is not None:

            def on_rt_update(bars, has_new_bar, _rt=rt_bars) -> None:
                if has_new_bar and len(_rt) > 0:
                    print_rt_bar(_rt[-1])

            rt_bars.updateEvent += on_rt_update

    if ticker is None and (rt_bars is None or len(rt_bars) == 0):
        print("[X] No market data or real-time bars received")
        ib.disconnect()
        return 1

    deadline = asyncio.get_event_loop().time() + args.duration if args.duration > 0 else None
    try:
        while True:
            if ticker is not None:
                print_mkt_tick(ticker, force=True)
            if deadline is not None and asyncio.get_event_loop().time() >= deadline:
                print(f"\nDuration {args.duration}s reached — exiting.")
                break
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        if ticker is not None:
            ib.cancelMktData(contract)
        if rt_bars is not None:
            ib.cancelRealTimeBars(rt_bars)
        ib.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IBKR MNQ price check via reqMktData + reqRealTimeBars (ib_async)."
    )
    p.add_argument("--mode", choices=("paper", "live"), default="paper")
    p.add_argument("--symbol", default="MNQ", help="Futures root symbol (default: MNQ)")
    p.add_argument(
        "--local-symbol",
        default="MNQU6",
        help="IB local symbol (default: MNQU6 = Sep 2026)",
    )
    p.add_argument(
        "--expiry",
        default=None,
        help="Contract month YYYYMMDD (overrides --local-symbol if set)",
    )
    p.add_argument("--client-id", type=int, default=None, help="IB API client id")
    p.add_argument(
        "--live-only",
        action="store_true",
        help="reqMarketDataType(1) only — no delayed fallback",
    )
    p.add_argument(
        "--mkt-only",
        action="store_true",
        help="Only reqMktData (skip reqRealTimeBars)",
    )
    p.add_argument(
        "--rt-only",
        action="store_true",
        help="Only reqRealTimeBars (skip reqMktData)",
    )
    p.add_argument(
        "--what-to-show",
        default="TRADES",
        choices=("TRADES", "MIDPOINT", "BID", "ASK"),
        help="reqRealTimeBars whatToShow (default: TRADES)",
    )
    p.add_argument(
        "--use-rth",
        action="store_true",
        help="Real-time bars: regular trading hours only (default: include ETH)",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between [MKT] heartbeat prints (default: 2)",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Stop after N seconds (0 = run until Ctrl+C)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.mkt_only and args.rt_only:
        raise SystemExit("Use only one of --mkt-only or --rt-only")
    if args.mode == "live":
        print("WARNING: live mode uses real account port — quotes only, no orders placed.")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
