"""
=============================================================================
SUPERTREND Indicator
=============================================================================
Pine Script Reference: Lines 17-19, 43, 48-53

This module implements the Supertrend indicator matching TradingView's 
ta.supertrend() function with exact parameter replication.

Strategy Parameters:
    - ATR Length: 10  (set in config/strategy.yaml)
    - Multiplier: 3.0 (set in config/strategy.yaml)
    - Timeframe: 10-minute (primary chart)

Note: Original Pine Script (scripts/strategy.pine) used ATR=55, Multiplier=3.8 for Banknifty.
      MNQ parameters are adapted values configured via strategy.yaml.
=============================================================================
"""

import numpy as np
import pandas as pd
from typing import Tuple, Union


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int
) -> pd.Series:
    """
    Calculate Average True Range (ATR).
    
    Matches TradingView's ta.atr() which uses RMA (Wilder's MA).
    
    Parameters:
    -----------
    high, low, close : pd.Series
        OHLC data
    length : int
        ATR period (55 in strategy)
    
    Returns:
    --------
    pd.Series
        ATR values
    """
    # True Range calculation
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # RMA (Wilder's Moving Average) - same as EMA with alpha = 1/length
    # TradingView's ta.atr() uses RMA internally
    alpha = 1.0 / length
    atr = true_range.ewm(alpha=alpha, adjust=False).mean()
    
    return atr


def calculate_supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_length: int = 10,
    multiplier: float = 3.0
) -> Tuple[pd.Series, pd.Series]:
    """
    Calculate Supertrend indicator.
    
    Matches TradingView's ta.supertrend() function.
    Strategy config (strategy.yaml): atr_length=10, multiplier=3.
    
    Parameters:
    -----------
    high, low, close : pd.Series
        OHLC price data
    atr_length : int
        ATR period (default 10)
    multiplier : float
        Band multiplier (default 3.0)
    
    Returns:
    --------
    Tuple[pd.Series, pd.Series]
        (supertrend_line, direction)
        - supertrend_line: The Supertrend value
        - direction: -1 for bullish (price above ST), +1 for bearish (price below ST)
        
    Notes:
    ------
    TradingView's ta.supertrend returns:
    - st: The Supertrend line value
    - dir: Direction indicator (-1 = bullish, +1 = bearish)
    
    Pine Script reference (lines 48-49):
        stBull = stDir < 0  // Bullish when direction is negative
        stBear = stDir > 0  // Bearish when direction is positive
    """
    # Calculate ATR
    atr = calculate_atr(high, low, close, atr_length)
    
    # Calculate basic upper and lower bands
    hl2 = (high + low) / 2  # Median price
    
    basic_upper = hl2 + (multiplier * atr)
    basic_lower = hl2 - (multiplier * atr)
    
    # Initialize arrays
    n = len(close)
    upper_band = np.zeros(n)
    lower_band = np.zeros(n)
    supertrend = np.zeros(n)
    direction = np.zeros(n)
    
    # Convert to numpy for faster iteration
    close_arr = close.values
    basic_upper_arr = basic_upper.values
    basic_lower_arr = basic_lower.values
    
    # Initialize first values
    upper_band[0] = basic_upper_arr[0]
    lower_band[0] = basic_lower_arr[0]
    direction[0] = 1  # Start bearish
    supertrend[0] = upper_band[0]
    
    # Iterate through bars
    for i in range(1, n):
        # Upper band logic
        # If current basic upper < previous upper OR previous close > previous upper
        # then use current basic upper, else use previous upper
        if basic_upper_arr[i] < upper_band[i-1] or close_arr[i-1] > upper_band[i-1]:
            upper_band[i] = basic_upper_arr[i]
        else:
            upper_band[i] = upper_band[i-1]
        
        # Lower band logic
        # If current basic lower > previous lower OR previous close < previous lower
        # then use current basic lower, else use previous lower
        if basic_lower_arr[i] > lower_band[i-1] or close_arr[i-1] < lower_band[i-1]:
            lower_band[i] = basic_lower_arr[i]
        else:
            lower_band[i] = lower_band[i-1]
        
        # Direction and Supertrend value
        if direction[i-1] == 1:  # Was bearish
            if close_arr[i] > upper_band[i-1]:
                direction[i] = -1  # Flip to bullish
                supertrend[i] = lower_band[i]
            else:
                direction[i] = 1  # Stay bearish
                supertrend[i] = upper_band[i]
        else:  # Was bullish (direction == -1)
            if close_arr[i] < lower_band[i-1]:
                direction[i] = 1  # Flip to bearish
                supertrend[i] = upper_band[i]
            else:
                direction[i] = -1  # Stay bullish
                supertrend[i] = lower_band[i]
    
    # Convert back to pandas Series with original index
    supertrend_series = pd.Series(supertrend, index=close.index, name='supertrend')
    direction_series = pd.Series(direction, index=close.index, name='direction')
    
    return supertrend_series, direction_series


def get_supertrend_signals(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_length: int = 10,
    multiplier: float = 3.0
) -> pd.DataFrame:
    """
    Calculate Supertrend with all signal components.
    
    Strategy config (strategy.yaml): atr_length=10, multiplier=3.
    
    Parameters:
    -----------
    high, low, close : pd.Series
        OHLC price data
    atr_length : int
        ATR period (default 10)
    multiplier : float
        Band multiplier (default 3.0)
    
    Returns:
    --------
    pd.DataFrame
        Columns:
        - supertrend: The Supertrend line value
        - direction: Raw direction (-1 bullish, +1 bearish)
        - st_bull: True when in bullish trend
        - st_bear: True when in bearish trend
        - st_bull_flip: True on the bar ST flips to bullish
        - st_bear_flip: True on the bar ST flips to bearish
    """
    supertrend, direction = calculate_supertrend(
        high, low, close, atr_length, multiplier
    )
    
    # Bull/Bear state (lines 48-49)
    st_bull = direction < 0
    st_bear = direction > 0
    
    # Flip detection (lines 52-53)
    prev_direction = direction.shift(1)
    st_bull_flip = st_bull & (prev_direction > 0)  # Just turned bullish
    st_bear_flip = st_bear & (prev_direction < 0)  # Just turned bearish
    
    return pd.DataFrame({
        'supertrend': supertrend,
        'direction': direction,
        'st_bull': st_bull,
        'st_bear': st_bear,
        'st_bull_flip': st_bull_flip,
        'st_bear_flip': st_bear_flip
    })


def supertrend_at_bar(
    signals_df: pd.DataFrame,
    bar_timestamp: pd.Timestamp
) -> dict:
    """
    Get all Supertrend signals at a specific bar.
    
    Parameters:
    -----------
    signals_df : pd.DataFrame
        Output from get_supertrend_signals()
    bar_timestamp : pd.Timestamp
        Timestamp to lookup
    
    Returns:
    --------
    dict
        All Supertrend values and signals at that bar
    """
    if bar_timestamp not in signals_df.index:
        # Find closest prior bar
        valid_idx = signals_df.index[signals_df.index <= bar_timestamp]
        if len(valid_idx) == 0:
            return None
        bar_timestamp = valid_idx[-1]
    
    row = signals_df.loc[bar_timestamp]
    
    return {
        'supertrend': row['supertrend'],
        'direction': row['direction'],
        'st_bull': row['st_bull'],
        'st_bear': row['st_bear'],
        'st_bull_flip': row['st_bull_flip'],
        'st_bear_flip': row['st_bear_flip']
    }
