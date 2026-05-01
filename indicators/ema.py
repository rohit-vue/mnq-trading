"""
=============================================================================
EMA (Exponential Moving Average) Indicator
=============================================================================
Pine Script Reference: Lines 22, 31, 38

This module implements the EMA calculation matching TradingView's ta.ema()
function for multi-timeframe usage in the strategy.

NOTE: The strategy uses EMA200 on the 1H timeframe as a trend filter.

IMPORTANT: TradingView uses RTH (Regular Trading Hours) only for futures
by default. To match TradingView's EMA values, filter to RTH hours (9:30-16:00 ET)
before calculating EMA.
=============================================================================
"""

import numpy as np
import pandas as pd
from typing import Union, Optional


def filter_rth_hours(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter DataFrame to Regular Trading Hours (RTH) only.
    
    RTH for futures: 9:30 AM - 4:00 PM Eastern Time, weekdays only.
    This matches TradingView's default session for futures charts.
    
    Parameters:
    -----------
    df : pd.DataFrame
        DataFrame with datetime index
    
    Returns:
    --------
    pd.DataFrame
        Filtered DataFrame with RTH hours only
    """
    if len(df) == 0:
        return df
    
    # Work with a copy to avoid modifying original
    df_copy = df.copy()
    
    # Ensure index is timezone-aware (assume US/Eastern if naive)
    if df_copy.index.tz is None:
        # Assume US/Eastern if timezone-naive
        df_copy.index = df_copy.index.tz_localize('US/Eastern')
    else:
        df_copy.index = df_copy.index.tz_convert('US/Eastern')
    
    # Filter to RTH: 9:30 AM - 4:00 PM ET, weekdays only
    rth_mask = (
        (df_copy.index.weekday < 5) &  # Monday-Friday (0-4)
        (df_copy.index.hour >= 9) &    # 9 AM or later
        ~((df_copy.index.hour == 9) & (df_copy.index.minute < 30)) &  # Not before 9:30
        (df_copy.index.hour < 16)      # Before 4 PM
    )
    
    # Apply filter
    filtered_df = df_copy.loc[rth_mask].copy()
    
    # Convert index back to timezone-naive if original was naive
    if df.index.tz is None:
        filtered_df.index = filtered_df.index.tz_localize(None)
    
    return filtered_df


def calculate_ema(
    series: Union[pd.Series, np.ndarray],
    length: int
) -> pd.Series:
    """
    Calculate Exponential Moving Average.
    
    Matches TradingView's ta.ema() function.
    
    Pine Script equivalent:
        ta.ema(close, emaLen)  // line 31
    
    Parameters:
    -----------
    series : pd.Series or np.ndarray
        Price series (typically close prices)
    length : int
        EMA period (200 in strategy config)
    
    Returns:
    --------
    pd.Series
        EMA values with same index as input
        
    Formula:
    --------
    EMA = Price(t) * k + EMA(y) * (1 – k)
    where k = 2 / (length + 1)
    """
    if isinstance(series, np.ndarray):
        series = pd.Series(series)
    
    # Use pandas ewm (exponential weighted mean) with span parameter
    # span = length means the decay factor alpha = 2/(span+1)
    # This matches TradingView's EMA calculation
    ema = series.ewm(span=length, adjust=False).mean()
    
    return ema


def ema_trend_filter(
    close_1h: pd.Series,
    ema_length: int = 200
) -> pd.DataFrame:
    """
    Calculate EMA-based trend filter signals from 1H timeframe.
    
    Pine Script Reference (lines 74-80):
        emaBull_1h = close_1h > ema200_1h
        emaBear_1h = close_1h < ema200_1h
        emaBullCross_1h = emaBull_1h and (prevClose_1h <= prevEma_1h or isNew1HCandle)
        emaBearCross_1h = emaBear_1h and (prevClose_1h >= prevEma_1h or isNew1HCandle)
    
    Parameters:
    -----------
    close_1h : pd.Series
        1H close prices
    ema_length : int
        EMA period (default 200)
    
    Returns:
    --------
    pd.DataFrame
        Columns: ema, ema_bull, ema_bear, ema_bull_cross, ema_bear_cross
    """
    ema = calculate_ema(close_1h, ema_length)
    
    # Current bar conditions (line 74-75)
    ema_bull = close_1h > ema  # Price above EMA = bullish
    ema_bear = close_1h < ema  # Price below EMA = bearish
    
    # Previous bar values for crossover detection
    prev_close = close_1h.shift(1)
    prev_ema = ema.shift(1)
    
    # Crossover conditions (lines 79-80)
    # Note: isNew1HCandle detection is handled at the 10m level
    # Here we calculate the basic cross condition
    ema_bull_cross = ema_bull & (prev_close <= prev_ema)
    ema_bear_cross = ema_bear & (prev_close >= prev_ema)
    
    return pd.DataFrame({
        'ema': ema,
        'ema_bull': ema_bull,
        'ema_bear': ema_bear,
        'ema_bull_cross': ema_bull_cross,
        'ema_bear_cross': ema_bear_cross
    })


def get_ema_at_bar(
    ema_series: pd.Series,
    bar_timestamp: pd.Timestamp,
    lookahead: bool = False
) -> float:
    """
    Get EMA value at a specific bar timestamp with lookahead protection.
    
    Pine Script Reference (line 31):
        request.security(..., lookahead = barmerge.lookahead_off)
    
    Parameters:
    -----------
    ema_series : pd.Series
        Complete EMA series with datetime index
    bar_timestamp : pd.Timestamp
        Timestamp to lookup
    lookahead : bool
        If False (default), use only confirmed (past) data
        If True, allows using current bar data (NOT recommended)
    
    Returns:
    --------
    float
        EMA value at the specified timestamp
    """
    if lookahead:
        # WARNING: This violates barmerge.lookahead_off
        # Should only be used for debugging
        return ema_series.asof(bar_timestamp)
    else:
        # Correct behavior: use last confirmed bar's value
        # This ensures no future data leakage
        valid_data = ema_series[ema_series.index < bar_timestamp]
        if len(valid_data) > 0:
            return valid_data.iloc[-1]
        return np.nan
