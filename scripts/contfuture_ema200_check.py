"""One-off: MNQ ContFuture 30d -> 1H EMA200 (matches bot resample path)."""
import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from ib_async import IB, ContFuture
    from data.historical_loader import HistoricalDataLoader
    from indicators.ema import calculate_ema

    ibkr_cfg = yaml.safe_load((ROOT / "config" / "ibkr.yaml").read_text(encoding="utf-8"))
    conn = ibkr_cfg.get("connection", {})
    port = int(conn.get("ports", {}).get("tws_paper", 7497))
    host = conn.get("host", "127.0.0.1")
    client_id = 99

    ib = IB()
    print(f"Connecting to IBKR {host}:{port} (client_id={client_id})...")
    await ib.connectAsync(host, port, clientId=client_id, timeout=20)
    print("Connected.")

    cont = ContFuture(symbol="MNQ", exchange="CME", currency="USD")
    qualified = await ib.qualifyContractsAsync(cont)
    if not qualified:
        print("ERROR: Could not qualify ContFuture MNQ")
        ib.disconnect()
        return
    contract = qualified[0]
    print(f"Contract: {getattr(contract, 'localSymbol', 'MNQ')} secType={contract.secType}")

    loader = HistoricalDataLoader(ib_client=ib)
    use_rth = "--rth" in sys.argv
    df_10m = await loader.fetch_ibkr_bars(
        contract=contract,
        end_datetime=None,
        duration="30 D",
        bar_size="10 mins",
        use_rth=use_rth,
    )
    if df_10m.empty:
        print("ERROR: No bars returned")
        ib.disconnect()
        return

    print(f"10m bars: {len(df_10m)} | range: {df_10m.index.min()} -> {df_10m.index.max()}")

    df_1h = (
        df_10m.resample("1h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    ema = calculate_ema(df_1h["close"], 200)

    latest_ema = float(ema.iloc[-1])
    latest_close_1h = float(df_1h["close"].iloc[-1])
    latest_ts = df_1h.index[-1]

    prev_ema = float(ema.iloc[-2]) if len(df_1h) >= 2 else float("nan")
    prev_close = float(df_1h["close"].iloc[-2]) if len(df_1h) >= 2 else float("nan")
    prev_ts = df_1h.index[-2] if len(df_1h) >= 2 else None

    print()
    print("=" * 60)
    print("MNQ ContFuture | 30 days | 10m -> 1H resample | EMA(200) on close")
    print(f"useRTH={use_rth} ({'RTH' if use_rth else 'ETH'}, bot uses ETH)")
    print("=" * 60)
    print(f"1H bars total     : {len(df_1h)}")
    print(f"Latest 1H bar     : {latest_ts}")
    print(f"  close           : {latest_close_1h:.2f}")
    print(f"  EMA200 (latest) : {latest_ema:.2f}  (forming hour)")
    if prev_ts is not None:
        print(f"Prev 1H bar       : {prev_ts}")
        print(f"  close           : {prev_close:.2f}")
        print(f"  EMA200 (prev)   : {prev_ema:.2f}  (last completed hour)")
    print("=" * 60)

    ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
