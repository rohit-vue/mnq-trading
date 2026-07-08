"""
Standalone IBKR historical data fetcher.

Prompts for start/end dates, pulls stitched MNQ historical bars from IBKR (TWS
or Gateway), and saves a CSV under "IBKR historic data" in project root.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml

# Fix Unicode output on Windows
if sys.platform == "win32":
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_config(config_dir: str = "./config") -> dict:
    """Load all project YAML configs into a dict."""
    config_path = Path(config_dir)
    config = {}
    for filename in ["strategy.yaml", "ibkr.yaml", "mnq_contract.yaml", "risk.yaml"]:
        filepath = config_path / filename
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                config[filename.replace(".yaml", "")] = yaml.safe_load(f)
    return config


def get_connection_port(ibkr_cfg: dict, mode: str = "paper", gateway: str = "tws") -> int:
    """Resolve IBKR port from config for mode+gateway."""
    ports = ibkr_cfg.get("connection", {}).get("ports", {})
    key = f"{gateway}_{mode}"  # e.g. tws_paper / gateway_live
    if key in ports:
        return int(ports[key])
    return int(ports.get("tws_paper", 7497))


def prompt_date(label: str, default_date: datetime) -> datetime:
    """Prompt date in YYYY-MM-DD format with default."""
    while True:
        raw = input(f"{label} [{default_date.strftime('%Y-%m-%d')}]: ").strip()
        if raw == "":
            return default_date
        try:
            return datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            print("Invalid date format. Use YYYY-MM-DD.")


async def main() -> None:
    # Local imports so script stays standalone
    from ib_async import IB
    from data.contract_stitcher import fetch_stitched_data
    from timeframe_utils import get_primary_bar_size, get_primary_timeframe

    config = load_config()
    strategy_cfg = config.get("strategy", {})
    ibkr_cfg = config.get("ibkr", {})
    conn_cfg = ibkr_cfg.get("connection", {})

    default_mode = conn_cfg.get("default_mode", "paper").lower()
    default_gateway = conn_cfg.get("default_gateway", "tws").lower()
    default_host = conn_cfg.get("host", "127.0.0.1")
    default_client_id = int(2)

    print("=" * 72)
    print("IBKR HISTORICAL DATA EXPORT (STITCHED MNQ CONTRACTS)")
    print("=" * 72)

    end_default = datetime.now()
    start_default = end_default - timedelta(days=365)

    start_date = prompt_date("Start date", start_default)
    end_date = prompt_date("End date", end_default)
    if end_date < start_date:
        print("End date is before start date. Swapping values.")
        start_date, end_date = end_date, start_date

    # Use connection details directly from config (same behavior style as backtest).
    mode = default_mode
    gateway = default_gateway
    host = default_host
    client_id = default_client_id

    # Match IBKR backtest behavior: use strategy.yaml primary timeframe.
    primary_tf = get_primary_timeframe(strategy_cfg)
    bar_size = get_primary_bar_size(strategy_cfg)
    port = get_connection_port(ibkr_cfg, mode=mode, gateway=gateway)

    print("\n" + "-" * 72)
    print(f"Date range      : {start_date.strftime('%Y-%m-%d')} -> {end_date.strftime('%Y-%m-%d')}")
    print(f"Primary TF      : {primary_tf} (bar size: {bar_size})")
    print(f"IBKR endpoint   : {host}:{port} ({gateway.upper()} {mode.upper()})")
    print(f"Client ID       : {client_id} (from config)")
    print("-" * 72)

    ib = IB()
    try:
        print("Connecting to IBKR...")
        await ib.connectAsync(host, port, clientId=client_id)
        print("Connected.")

        print("Fetching stitched historical data from IBKR...")
        df = await fetch_stitched_data(
            ib_client=ib,
            start_date=start_date,
            end_date=end_date,
            bar_size=bar_size,
        )

        if df is None or len(df) == 0:
            print("No data returned from IBKR for this range.")
            return

        output_dir = Path("IBKR historic data")
        output_dir.mkdir(parents=True, exist_ok=True)

        run_id = f"IBKR-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:10].upper()}"
        run_dir = output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Save one CSV per calendar month (Databento-style naming pattern).
        month_groups = df.groupby(df.index.to_period("M"))
        saved_files = []
        for period, month_df in month_groups:
            month_start = period.start_time.strftime("%Y%m%d")
            month_end = period.end_time.strftime("%Y%m%d")
            filename = f"glbx-mdp3-{month_start}-{month_end}.ohlcv-{primary_tf}.csv"
            output_path = run_dir / filename
            month_df.to_csv(output_path, index=True)
            saved_files.append((output_path, len(month_df)))

        print("\nSaved IBKR historical data (monthly files):")
        print(f"  Folder  : {run_dir}")
        print(f"  Files   : {len(saved_files)}")
        print(f"  Rows    : {len(df):,}")
        print(f"  Range   : {df.index.min()} -> {df.index.max()}")
        if saved_files:
            print("  First   :", saved_files[0][0].name, f"({saved_files[0][1]:,} rows)")
            print("  Last    :", saved_files[-1][0].name, f"({saved_files[-1][1]:,} rows)")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Disconnected from IBKR.")


if __name__ == "__main__":
    asyncio.run(main())
