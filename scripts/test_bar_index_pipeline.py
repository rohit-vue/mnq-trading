#!/usr/bin/env python3
"""
Validate bar datetime handling end-to-end (no IBKR required).

Simulates:
- IBKR string bar.date (formatDate=2)
- tz-aware stitched timestamps
- merge + preload + dashboard indicator refresh
- optional live IBKR historical fetch (--live)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytz
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.bar_index import bars_to_ohlcv_dataframe, ensure_datetime_index
from data.feed_warmup import dataframe_to_bar_objects
from data.live_bar_alignment import enrich_10m_with_1h_like_backtest
from data.realtime_feed import RealtimeFeed


def _ib_style_bar(date_str: str, close: float) -> SimpleNamespace:
    return SimpleNamespace(
        date=date_str,
        open=close - 1,
        high=close + 2,
        low=close - 2,
        close=close,
        volume=100.0,
        average=close,
        barCount=10,
    )


def test_mixed_ib_and_stitched_merge() -> None:
    tz = pytz.timezone("US/Eastern")
    feed = RealtimeFeed.__new__(RealtimeFeed)
    feed.timezone = tz
    feed._max_buffer_bars = 8000
    feed._bars = [
        _ib_style_bar("20260629 10:20:00", 29402.0),
        _ib_style_bar("20260629 10:25:00", 29442.0),
    ]
    feed._build_dataframe()
    assert isinstance(feed._df.index, pd.DatetimeIndex)
    assert feed._df.index.tz is not None

    idx = pd.date_range("2026-06-29 09:00", periods=24, freq="5min", tz="US/Eastern")
    stitched = pd.DataFrame(
        {
            "open": [29400.0] * len(idx),
            "high": [29450.0] * len(idx),
            "low": [29350.0] * len(idx),
            "close": [29410.0] * len(idx),
            "volume": [1000.0] * len(idx),
        },
        index=idx,
    )
    feed.preload_bars(dataframe_to_bar_objects(stitched))
    assert isinstance(feed._df.index, pd.DatetimeIndex)
    assert feed._df.index.tz is not None
    print(f"  merge OK: {len(feed._df)} bars, last={feed._df.index[-1]}")


def test_dashboard_indicator_refresh_from_csv() -> None:
    csv_path = ROOT / "IBKR historic data" / "MNQ-CONTFUT-30d-5mins-eth-20260626.csv"
    if not csv_path.exists():
        print(f"  skip CSV test (missing {csv_path.name})")
        return

    raw = pd.read_csv(csv_path, index_col="datetime")
    df = ensure_datetime_index(raw.tail(500), tz="US/Eastern", datetime_col=None)
    df_1h = df.resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    enriched = enrich_10m_with_1h_like_backtest(df, df_1h, 200)
    assert "ema_1h" in enriched.columns
    assert isinstance(enriched.index, pd.DatetimeIndex)
    print(f"  dashboard enrich OK: {len(enriched)} rows, ema_1h={enriched['ema_1h'].iloc[-2]:.2f}")


def test_broken_plain_index_recovery() -> None:
    """Plain Index (the production bug) must be recoverable."""
    broken = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [0.5, 1.5],
            "close": [1.5, 2.5],
            "volume": [10.0, 20.0],
        },
        index=pd.Index(["2026-06-29 10:20:00", "2026-06-29 10:25:00"]),
    )
    fixed = ensure_datetime_index(broken, tz="US/Eastern", datetime_col=None)
    assert isinstance(fixed.index, pd.DatetimeIndex)
    assert fixed.index.tz is not None
    df_1h = fixed.resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    enrich_10m_with_1h_like_backtest(fixed, df_1h, 200)
    print("  plain Index recovery OK")


async def test_live_ibkr_bars() -> None:
    from ib_async import IB, Future

    from utils.load_env import load_project_dotenv

    load_project_dotenv()
    cfg_path = ROOT / "config" / "ibkr.yaml"
    with open(cfg_path) as f:
        ibkr = yaml.safe_load(f)
    conn = ibkr.get("connection", ibkr.get("ibkr", {}).get("connection", {}))
    ports = conn.get("ports", {})
    port = ports.get("tws_paper", 7497)
    client_id = int(conn.get("client_id", 10)) + 50

    ib = IB()
    print(f"  connecting IBKR paper port={port} client_id={client_id}...")
    await ib.connectAsync("127.0.0.1", port, clientId=client_id, timeout=15)
    try:
        contract = Future(symbol="MNQ", exchange="CME", currency="USD", localSymbol="MNQU6")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            print("  skip live test: could not qualify MNQU6")
            return
        contract = qualified[0]
        print(f"  qualified {contract.localSymbol}")

        bars = await ib.reqHistoricalDataAsync(
            contract=contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
            keepUpToDate=False,
        )
        if not bars:
            print("  skip live test: no bars returned")
            return

        sample = bars[:3]
        print("  IB bar.date types:", [(type(b.date).__name__, repr(b.date)) for b in sample])

        df = bars_to_ohlcv_dataframe(bars, tz="US/Eastern")
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        print(f"  live IBKR convert OK: {len(df)} bars, last={df.index[-1]}")

        feed = RealtimeFeed(ib, contract, bar_size="5 mins")
        feed.preload_bars(bars)
        assert isinstance(feed.get_dataframe().index, pd.DatetimeIndex)
        print(f"  live preload OK: {len(feed.get_dataframe())} bars")
    finally:
        ib.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Also fetch bars from IBKR")
    args = parser.parse_args()

    print("test_mixed_ib_and_stitched_merge")
    test_mixed_ib_and_stitched_merge()
    print("test_dashboard_indicator_refresh_from_csv")
    test_dashboard_indicator_refresh_from_csv()
    print("test_broken_plain_index_recovery")
    test_broken_plain_index_recovery()
    if args.live:
        print("test_live_ibkr_bars")
        asyncio.run(test_live_ibkr_bars())
    print("ALL bar_index pipeline tests OK")


if __name__ == "__main__":
    main()
