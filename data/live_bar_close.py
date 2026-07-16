"""Shared live/paper bar-close row preparation."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from data.live_bar_alignment import enrich_10m_with_1h_like_backtest
from data.strategy_indicators import live_bar_indicator_slice

logger = logging.getLogger(__name__)


def prepare_bar_close_row(
    *,
    df: pd.DataFrame,
    mtf: Any,
    sides: Dict[str, Dict[str, Any]],
    strategy_cfg: dict,
    ema_cfg: dict,
    mode_label: str,
) -> Tuple[pd.Series, Dict[str, float]]:
    """Build the exact signal row used by live/paper bar-close processing."""
    timings: Dict[str, float] = {}

    t0 = time.perf_counter()
    inds = live_bar_indicator_slice(
        df,
        sides["long_supertrend_entry"],
        sides["short_supertrend_entry"],
        sides["long_adx"],
        sides["short_adx"],
        long_supertrend_exit=sides["long_supertrend_exit"],
        short_supertrend_exit=sides["short_supertrend_exit"],
        row_i=-2,
    )
    timings["indicators_ms"] = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    df_1h = mtf.aggregate_1h_from_10m(df)
    ema_len = int(ema_cfg.get("length", 200))
    if len(df_1h) < ema_len:
        logger.warning(
            "EMA_WARMUP | mode=%s | 1H bars=%s < EMA length=%s | "
            "1H EMA200 is NOT fully warmed (buffer=%s primary bars) — EMA filter may "
            "diverge from backtest until more history is loaded",
            mode_label,
            len(df_1h),
            ema_len,
            len(df),
        )
    df_aligned = enrich_10m_with_1h_like_backtest(df, df_1h, ema_len)
    timings["mtf_ms"] = (time.perf_counter() - t1) * 1000.0

    t2 = time.perf_counter()
    current_bar = df_aligned.iloc[-2].copy()
    for k, v in inds.items():
        current_bar[k] = v

    volume_ma_period = max(1, int(strategy_cfg.get("volume_ma_period", 20)))
    if "volume" in df.columns and len(df) >= volume_ma_period:
        current_bar["volume_ma"] = float(
            df["volume"]
            .rolling(volume_ma_period, min_periods=volume_ma_period)
            .mean()
            .iloc[-2]
        )
    else:
        current_bar["volume_ma"] = np.nan
    timings["row_ms"] = (time.perf_counter() - t2) * 1000.0
    timings["prepare_ms"] = sum(timings.values())
    return current_bar, timings

