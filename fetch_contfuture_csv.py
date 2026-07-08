"""
Download MNQ continuous futures (ContFuture / MNQ1!-style) from IBKR to CSV.

Default: last 30 days of 5-minute bars (ETH). Requires TWS or IB Gateway running.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

if sys.platform == "win32":
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_ibkr_config(config_dir: str = "./config") -> dict:
    path = Path(config_dir) / "ibkr.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def connection_params(ibkr_cfg: dict) -> tuple[str, int, str, str]:
    conn = ibkr_cfg.get("connection", {})
    mode = str(conn.get("default_mode", "paper")).lower()
    gateway = str(conn.get("default_gateway", "tws")).lower()
    host = conn.get("host", "127.0.0.1")
    ports = conn.get("ports", {})
    key = f"{gateway}_{mode}"
    port = int(ports.get(key, ports.get("tws_paper", 7497)))
    return host, port, mode, gateway


async def fetch_contfuture_bars(
    *,
    days: int = 30,
    bar_size: str = "5 mins",
    use_rth: bool = False,
    client_id: int = 97,
) -> tuple[object, object]:
    """Connect, qualify ContFuture MNQ, return (ib, dataframe)."""
    from ib_async import IB, ContFuture
    from data.historical_loader import HistoricalDataLoader

    ibkr_cfg = load_ibkr_config()
    host, port, mode, gateway = connection_params(ibkr_cfg)

    ib = IB()
    print(f"Connecting to IBKR {host}:{port} ({gateway.upper()} {mode.upper()}, client_id={client_id})...")
    await ib.connectAsync(host, port, clientId=client_id, timeout=25)
    print("Connected.")

    cont = ContFuture(symbol="MNQ", exchange="CME", currency="USD")
    qualified = await ib.qualifyContractsAsync(cont)
    if not qualified:
        ib.disconnect()
        raise RuntimeError("Could not qualify MNQ ContFuture on IBKR")

    contract = qualified[0]
    print(f"Contract: {getattr(contract, 'localSymbol', 'MNQ')} | secType={contract.secType}")

    loader = HistoricalDataLoader(ib_client=ib)
    print(f"Fetching {days} D of {bar_size} bars (useRTH={use_rth})...")
    df = await loader.fetch_ibkr_bars(
        contract=contract,
        end_datetime=None,
        duration=f"{days} D",
        bar_size=bar_size,
        use_rth=use_rth,
    )
    return ib, df


def save_csv(df, *, bar_size_label: str, days: int, use_rth: bool, output: Path | None) -> Path:
    if df is None or df.empty:
        raise ValueError("No bars returned from IBKR")

    output_dir = Path("IBKR historic data")
    output_dir.mkdir(parents=True, exist_ok=True)

    if output is None:
        session = "rth" if use_rth else "eth"
        run_id = datetime.now().strftime("%Y%m%d")
        filename = f"MNQ-CONTFUT-{days}d-{bar_size_label.replace(' ', '')}-{session}-{run_id}.csv"
        output = output_dir / filename
    else:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(output, index=True)
    return output


async def main_async(args: argparse.Namespace) -> int:
    ib = None
    try:
        ib, df = await fetch_contfuture_bars(
            days=args.days,
            bar_size=args.bar_size,
            use_rth=args.rth,
            client_id=args.client_id,
        )

        bar_label = args.bar_size.replace(" ", "")
        out = save_csv(
            df,
            bar_size_label=args.bar_size,
            days=args.days,
            use_rth=args.rth,
            output=Path(args.output) if args.output else None,
        )

        print()
        print("=" * 72)
        print("SAVED MNQ ContFuture historical data")
        print("=" * 72)
        print(f"  File   : {out.resolve()}")
        print(f"  Rows   : {len(df):,}")
        print(f"  Range  : {df.index.min()} -> {df.index.max()}")
        print(f"  Columns: {', '.join(df.columns)}")
        print("=" * 72)
        return 0
    except Exception as exc:
        print(f"\nError: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()
            print("Disconnected from IBKR.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download MNQ ContFuture (continuous) OHLCV from IBKR to CSV."
    )
    p.add_argument("--days", type=int, default=30, help="Lookback days (default: 30)")
    p.add_argument("--bar-size", default="5 mins", help='IB bar size (default: "5 mins")')
    p.add_argument("--rth", action="store_true", help="Regular trading hours only (default: ETH)")
    p.add_argument("--client-id", type=int, default=97, help="IB API client id (default: 97)")
    p.add_argument(
        "--output",
        default="",
        help="Output CSV path (default: IBKR historic data/MNQ-CONTFUT-...csv)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
