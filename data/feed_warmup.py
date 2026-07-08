"""
Stitched EMA warmup for paper/live RealtimeFeed.

When the EMA lookback window crosses a quarterly rollover, a single-outright
IBKR history request distorts EMA200. This module preloads volume-stitched bars
(the same path as backtest) before the live keepUpToDate subscription merges in.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd
import pytz

from data.contract_rollover import days_to_expiry, rollover_settings
from data.contract_stitcher import fetch_stitched_data, get_contracts_for_date_range

logger = logging.getLogger(__name__)


def compute_ema_warmup_days(ema_length: int) -> int:
    """Match backtest / HistoricalDataLoader warmup sizing."""
    return max(int(np.ceil(ema_length / 24)) + 5, 30)


def needs_stitched_ema_warmup(
    contract: Any,
    contract_cfg: Mapping[str, Any] | None,
    ema_length: int,
    *,
    as_of: Optional[datetime] = None,
) -> bool:
    """
    True when EMA warmup should use multi-contract stitching.

    Triggers when the warmup window spans more than one quarterly contract, or
    when the active outright is inside the configured roll window.
    """
    cfg = rollover_settings(contract_cfg)
    if not cfg["enabled"] or cfg["method"] != "volume":
        return False

    warmup_days = compute_ema_warmup_days(ema_length)
    tz = pytz.timezone(cfg["timezone"])
    end = as_of or datetime.now(tz)
    if end.tzinfo is None:
        end = tz.localize(end)
    else:
        end = end.astimezone(tz)

    start = end - timedelta(days=warmup_days)
    overlap = cfg["roll_window_days"]
    contracts = get_contracts_for_date_range(start, end, overlap_days=overlap)
    if len(contracts) > 1:
        return True

    if contract is not None and days_to_expiry(contract, end) <= cfg["roll_window_days"]:
        return True

    return False


def dataframe_to_bar_objects(df: pd.DataFrame) -> list:
    """Convert stitched OHLCV rows into bar-like objects for RealtimeFeed."""
    bars = []
    if df is None or df.empty:
        return bars

    work = df.sort_index()
    for ts, row in work.iterrows():
        bars.append(
            SimpleNamespace(
                date=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
                average=float(row.get("average", row["close"])),
                barCount=int(row.get("bar_count", 0) or 0),
            )
        )
    return bars


async def preload_stitched_ema_warmup(
    feed: Any,
    *,
    ib: Any,
    contract: Any,
    contract_cfg: Mapping[str, Any] | None,
    ema_length: int,
    bar_size: str,
    exchange: str = "CME",
) -> bool:
    """
    Fetch volume-stitched history and merge into the feed buffer for EMA200.

    Returns True when stitched bars were loaded.
    """
    if not needs_stitched_ema_warmup(contract, contract_cfg, ema_length):
        return False

    warmup_days = compute_ema_warmup_days(ema_length)
    cfg = rollover_settings(contract_cfg)
    tz = pytz.timezone(cfg["timezone"])
    end = datetime.now(tz)
    start = end - timedelta(days=warmup_days)

    logger.info(
        "EMA_WARMUP_STITCH | fetching %s-day stitched history (%s -> %s) for EMA%s",
        warmup_days,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
        ema_length,
    )

    try:
        df = await fetch_stitched_data(
            ib_client=ib,
            start_date=start,
            end_date=end,
            bar_size=bar_size,
            exchange=exchange,
            contract_cfg=dict(contract_cfg) if contract_cfg else None,
        )
    except Exception as exc:
        logger.warning("EMA_WARMUP_STITCH | fetch failed: %s", exc)
        return False

    if df is None or df.empty:
        logger.warning("EMA_WARMUP_STITCH | no stitched bars returned")
        return False

    bars = dataframe_to_bar_objects(df)
    if not bars:
        return False

    preload_fn = getattr(feed, "preload_bars", None)
    if preload_fn is None:
        logger.warning("EMA_WARMUP_STITCH | feed has no preload_bars()")
        return False

    preload_fn(bars)
    logger.info(
        "EMA_WARMUP_STITCH | preloaded %s stitched bars into feed buffer (for EMA%s)",
        len(bars),
        ema_length,
    )
    return True
