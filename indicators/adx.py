# =============================================================================
# ADX (Average Directional Index) Indicator
# =============================================================================
# Pine Script Reference: ADX(14) on 10M timeframe
#
# Components:
#   - ADX: Trend strength (0-100)
#   - +DI: Positive Directional Indicator
#   - -DI: Negative Directional Indicator
#
# Threshold: ADX >= 20 indicates trending market
# =============================================================================

import numpy as np
import pandas as pd
from typing import Union, Tuple


def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    di_length: int = 14,
    adx_smoothing: int = 14
) -> pd.DataFrame:
    """
    Calculate ADX (Average Directional Index) with +DI and -DI.
    
    Matches TradingView's ta.dmi() function.
    
    Pine Script equivalent:
        [diplus, diminus, adx] = ta.dmi(diLen, adxLen)
    
    Parameters:
    -----------
    high : pd.Series
        High prices
    low : pd.Series
        Low prices
    close : pd.Series
        Close prices
    di_length : int
        DI Length for +DI/-DI calculation (default 14)
    adx_smoothing : int
        ADX smoothing period (default 14)
    
    Returns:
    --------
    pd.DataFrame
        Columns: adx, plus_di, minus_di
    """
    # Calculate True Range (TR)
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Calculate Directional Movement (+DM and -DM)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    
    # +DM: If up_move > down_move and up_move > 0, then +DM = up_move, else 0
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0),
        index=high.index
    )
    
    # -DM: If down_move > up_move and down_move > 0, then -DM = down_move, else 0
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0),
        index=high.index
    )
    
    # Wilder's Smoothing for DI (uses di_length)
    alpha_di = 1 / di_length
    
    # Smooth TR, +DM, -DM using Wilder's method
    tr_smooth = true_range.ewm(alpha=alpha_di, adjust=False).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=alpha_di, adjust=False).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=alpha_di, adjust=False).mean()
    
    # Calculate +DI and -DI
    plus_di = 100 * (plus_dm_smooth / tr_smooth)
    minus_di = 100 * (minus_dm_smooth / tr_smooth)
    
    # Calculate DX (Directional Index)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    
    # Wilder's Smoothing for ADX (uses adx_smoothing)
    alpha_adx = 1 / adx_smoothing
    adx = dx.ewm(alpha=alpha_adx, adjust=False).mean()
    
    return pd.DataFrame({
        'adx': adx,
        'plus_di': plus_di,
        'minus_di': minus_di
    })


def check_adx_threshold(
    adx_series: pd.Series,
    threshold: float = 20.0
) -> pd.Series:
    """
    Check if ADX is above threshold.
    
    Parameters:
    -----------
    adx_series : pd.Series
        ADX values
    threshold : float
        ADX threshold (default 20)
    
    Returns:
    --------
    pd.Series
        Boolean series where True = ADX >= threshold
    """
    return adx_series >= threshold


def check_adx_consecutive(
    adx_above_threshold: pd.Series,
    consecutive_count: int = 5
) -> pd.Series:
    """
    Check if ADX has been above threshold for N consecutive candles.
    
    This is for Case 1: SuperTrend flip with ADX confirmation.
    The 5 consecutive candles MUST include the current candle.
    
    Parameters:
    -----------
    adx_above_threshold : pd.Series
        Boolean series where True = ADX >= threshold
    consecutive_count : int
        Number of consecutive candles required (default 5)
    
    Returns:
    --------
    pd.Series
        Boolean series where True = ADX has been above threshold
        for N consecutive candles ending at current bar
    """
    # Rolling sum of True values in last N candles
    rolling_sum = adx_above_threshold.astype(int).rolling(
        window=consecutive_count, 
        min_periods=consecutive_count
    ).sum()
    
    # All N candles must be True
    return rolling_sum == consecutive_count
