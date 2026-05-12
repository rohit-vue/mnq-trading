"""
Align live/real-time 10m bars with 1H EMA and cross logic — same as HistoricalDataLoader.prepare_strategy_data.

Paper/live previously used MultiTimeframeFeed.get_confirmed_1h_ema() (latest/partial 1H bar),
which does NOT match backtest / IBKR historical prep and can block or distort entries.
"""

from __future__ import annotations

import pandas as pd

from .historical_loader import HistoricalDataLoader
from indicators.ema import ema_trend_filter


def enrich_10m_with_1h_like_backtest(
    df_10m: pd.DataFrame,
    df_1h_ohlcv: pd.DataFrame,
    ema_length: int,
) -> pd.DataFrame:
    """
    Apply the same 1H → 10m mapping, is_new_1h_candle, and ema_*_cross columns as
    HistoricalDataLoader.prepare_strategy_data (before attach_long_short_indicators).

    Parameters
    ----------
    df_10m : DataFrame
        Primary OHLCV bars (RealtimeFeed index, US/Eastern).
    df_1h_ohlcv : DataFrame
        1H OHLCV from resampling df_10m (e.g. mtf.aggregate_1h_from_10m).
    ema_length : int
        EMA period on 1H close (e.g. 200).

    Returns
    -------
    DataFrame
        Copy of df_10m with ema_1h, close_1h, high_1h, low_1h, is_new_1h_candle,
        ema_bull, ema_bear, close_1h_cross, ema_1h_cross, ema_bull_cross, ema_bear_cross.
    """
    df_1h_ind = ema_trend_filter(df_1h_ohlcv["close"], ema_length)
    df_1h_full = pd.concat([df_1h_ohlcv, df_1h_ind], axis=1)

    loader = HistoricalDataLoader(ib_client=None)
    ema_1h, close_1h, high_1h, low_1h = loader.get_1h_values_for_10m_bars(df_10m, df_1h_full)
    is_new_1h = loader.detect_new_1h_candle(df_10m)

    out = df_10m.copy()
    out = pd.concat([out, ema_1h, close_1h, high_1h, low_1h, is_new_1h], axis=1)

    out["ema_bull"] = out["close_1h"] > out["ema_1h"]
    out["ema_bear"] = out["close_1h"] < out["ema_1h"]

    df_1h_cross_avail = df_1h_full.copy()
    df_1h_cross_avail.index = df_1h_full.index + pd.Timedelta("1h")
    out["close_1h_cross"] = df_1h_cross_avail["close"].reindex(out.index, method="ffill")
    out["ema_1h_cross"] = df_1h_cross_avail["ema"].reindex(out.index, method="ffill")

    prev_close_1h_cross = out["close_1h_cross"].shift(1)
    prev_ema_1h_cross = out["ema_1h_cross"].shift(1)
    out["ema_bull_cross"] = (out["close_1h_cross"] > out["ema_1h_cross"]) & (
        prev_close_1h_cross <= prev_ema_1h_cross
    )
    out["ema_bear_cross"] = (out["close_1h_cross"] < out["ema_1h_cross"]) & (
        prev_close_1h_cross >= prev_ema_1h_cross
    )

    return out
