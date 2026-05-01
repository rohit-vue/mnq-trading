"""
Long/short Supertrend and ADX columns for prepared primary bars and live bar rows.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from indicators.adx import calculate_adx, check_adx_threshold
from indicators.supertrend import get_supertrend_signals

_ST_BASE_COLS = (
    "supertrend",
    "direction",
    "st_bull",
    "st_bear",
    "st_bull_flip",
    "st_bear_flip",
)


def _rename_st_columns(df: pd.DataFrame, side: str) -> pd.DataFrame:
    return df.rename(columns={c: f"{c}_{side}" for c in _ST_BASE_COLS})


def attach_long_short_indicators(
    df: pd.DataFrame,
    long_supertrend_entry: Dict[str, Any],
    short_supertrend_entry: Dict[str, Any],
    long_adx: Dict[str, Any],
    short_adx: Dict[str, Any],
    long_supertrend_exit: Dict[str, Any] | None = None,
    short_supertrend_exit: Dict[str, Any] | None = None,
) -> pd.DataFrame:
    """
    Add per-side entry/exit Supertrend columns, ADX columns, and compatibility aliases.
    """
    long_supertrend_exit = long_supertrend_exit or long_supertrend_entry
    short_supertrend_exit = short_supertrend_exit or short_supertrend_entry

    atr_l = int(long_supertrend_entry.get("atr_length", 10))
    mult_l = float(long_supertrend_entry.get("multiplier", 3.0))
    atr_s = int(short_supertrend_entry.get("atr_length", 10))
    mult_s = float(short_supertrend_entry.get("multiplier", 3.0))
    atr_lx = int(long_supertrend_exit.get("atr_length", 10))
    mult_lx = float(long_supertrend_exit.get("multiplier", 3.0))
    atr_sx = int(short_supertrend_exit.get("atr_length", 10))
    mult_sx = float(short_supertrend_exit.get("multiplier", 3.0))

    st_l = _rename_st_columns(
        get_supertrend_signals(
            df["high"], df["low"], df["close"], atr_length=atr_l, multiplier=mult_l
        ),
        "long",
    )
    st_s = _rename_st_columns(
        get_supertrend_signals(
            df["high"], df["low"], df["close"], atr_length=atr_s, multiplier=mult_s
        ),
        "short",
    )
    st_lx = _rename_st_columns(
        get_supertrend_signals(
            df["high"], df["low"], df["close"], atr_length=atr_lx, multiplier=mult_lx
        ),
        "long_exit",
    )
    st_sx = _rename_st_columns(
        get_supertrend_signals(
            df["high"], df["low"], df["close"], atr_length=atr_sx, multiplier=mult_sx
        ),
        "short_exit",
    )
    out = pd.concat([df, st_l, st_s, st_lx, st_sx], axis=1)

    di_l = int(long_adx.get("di_length", 14))
    sm_l = int(long_adx.get("adx_smoothing", 14))
    th_l = float(long_adx.get("threshold", 20.0))
    di_s = int(short_adx.get("di_length", 14))
    sm_s = int(short_adx.get("adx_smoothing", 14))
    th_s = float(short_adx.get("threshold", 20.0))

    if di_l == di_s and sm_l == sm_s:
        adx_df = calculate_adx(
            df["high"], df["low"], df["close"], di_length=di_l, adx_smoothing=sm_l
        )
        out["adx"] = adx_df["adx"]
        out["plus_di"] = adx_df["plus_di"]
        out["minus_di"] = adx_df["minus_di"]
        out["adx_above_threshold_long"] = check_adx_threshold(out["adx"], threshold=th_l)
        out["adx_above_threshold_short"] = check_adx_threshold(
            out["adx"], threshold=th_s
        )
    else:
        adx_l = calculate_adx(
            df["high"], df["low"], df["close"], di_length=di_l, adx_smoothing=sm_l
        )
        adx_s = calculate_adx(
            df["high"], df["low"], df["close"], di_length=di_s, adx_smoothing=sm_s
        )
        out["adx_long"] = adx_l["adx"]
        out["adx_short"] = adx_s["adx"]
        out["plus_di_long"] = adx_l["plus_di"]
        out["minus_di_long"] = adx_l["minus_di"]
        out["plus_di_short"] = adx_s["plus_di"]
        out["minus_di_short"] = adx_s["minus_di"]
        out["adx"] = out["adx_long"]
        out["plus_di"] = out["plus_di_long"]
        out["minus_di"] = out["minus_di_long"]
        out["adx_above_threshold_long"] = check_adx_threshold(
            out["adx_long"], threshold=th_l
        )
        out["adx_above_threshold_short"] = check_adx_threshold(
            out["adx_short"], threshold=th_s
        )

    # Entry aliases (legacy behavior for existing signal code paths)
    out["supertrend"] = out["supertrend_long"]
    out["direction"] = out["direction_long"]
    out["st_bull_flip"] = out["st_bull_flip_long"] | out["st_bull_flip_short"]
    out["st_bear_flip"] = out["st_bear_flip_long"] | out["st_bear_flip_short"]
    out["adx_above_threshold"] = out["adx_above_threshold_long"]

    return out


def bar_flips_for_state_manager(bar: pd.Series) -> Tuple[bool, bool, Any]:
    """Bull flip, bear flip, direction for StateManager.update_supertrend_state."""
    bf = bool(
        bar.get("st_bull_flip_long", False)
        or bar.get("st_bull_flip_short", False)
        or bar.get("st_bull_flip_long_exit", False)
        or bar.get("st_bull_flip_short_exit", False)
        or bar.get("st_bull_flip", False)
    )
    br = bool(
        bar.get("st_bear_flip_long", False)
        or bar.get("st_bear_flip_short", False)
        or bar.get("st_bear_flip_long_exit", False)
        or bar.get("st_bear_flip_short_exit", False)
        or bar.get("st_bear_flip", False)
    )
    direction = bar.get("direction_long", bar.get("direction", 0))
    return bf, br, direction


def live_bar_indicator_slice(
    df: pd.DataFrame,
    long_supertrend_entry: Dict[str, Any],
    short_supertrend_entry: Dict[str, Any],
    long_adx: Dict[str, Any],
    short_adx: Dict[str, Any],
    long_supertrend_exit: Dict[str, Any] | None = None,
    short_supertrend_exit: Dict[str, Any] | None = None,
    row_i: int = -2,
) -> Dict[str, Any]:
    """
    Compute long/short ST and ADX on full `df`, return field dict for one bar (default: last closed).
    """
    long_supertrend_exit = long_supertrend_exit or long_supertrend_entry
    short_supertrend_exit = short_supertrend_exit or short_supertrend_entry

    atr_l = int(long_supertrend_entry.get("atr_length", 10))
    mult_l = float(long_supertrend_entry.get("multiplier", 3.0))
    atr_s = int(short_supertrend_entry.get("atr_length", 10))
    mult_s = float(short_supertrend_entry.get("multiplier", 3.0))
    atr_lx = int(long_supertrend_exit.get("atr_length", 10))
    mult_lx = float(long_supertrend_exit.get("multiplier", 3.0))
    atr_sx = int(short_supertrend_exit.get("atr_length", 10))
    mult_sx = float(short_supertrend_exit.get("multiplier", 3.0))

    st_l = get_supertrend_signals(
        df["high"], df["low"], df["close"], atr_length=atr_l, multiplier=mult_l
    )
    st_s = get_supertrend_signals(
        df["high"], df["low"], df["close"], atr_length=atr_s, multiplier=mult_s
    )
    st_lx = get_supertrend_signals(
        df["high"], df["low"], df["close"], atr_length=atr_lx, multiplier=mult_lx
    )
    st_sx = get_supertrend_signals(
        df["high"], df["low"], df["close"], atr_length=atr_sx, multiplier=mult_sx
    )

    di_l = int(long_adx.get("di_length", 14))
    sm_l = int(long_adx.get("adx_smoothing", 14))
    th_l = float(long_adx.get("threshold", 20.0))
    di_s = int(short_adx.get("di_length", 14))
    sm_s = int(short_adx.get("adx_smoothing", 14))
    th_s = float(short_adx.get("threshold", 20.0))

    out: Dict[str, Any] = {}
    for c in _ST_BASE_COLS:
        out[f"{c}_long"] = st_l[c].iloc[row_i]
        out[f"{c}_short"] = st_s[c].iloc[row_i]
        out[f"{c}_long_exit"] = st_lx[c].iloc[row_i]
        out[f"{c}_short_exit"] = st_sx[c].iloc[row_i]

    if di_l == di_s and sm_l == sm_s:
        adx_df = calculate_adx(
            df["high"], df["low"], df["close"], di_length=di_l, adx_smoothing=sm_l
        )
        out["adx"] = float(adx_df["adx"].iloc[row_i])
        out["plus_di"] = float(adx_df["plus_di"].iloc[row_i])
        out["minus_di"] = float(adx_df["minus_di"].iloc[row_i])
        ab_l = check_adx_threshold(adx_df["adx"], threshold=th_l)
        ab_s = check_adx_threshold(adx_df["adx"], threshold=th_s)
        out["adx_above_threshold_long"] = bool(ab_l.iloc[row_i])
        out["adx_above_threshold_short"] = bool(ab_s.iloc[row_i])
    else:
        adx_l = calculate_adx(
            df["high"], df["low"], df["close"], di_length=di_l, adx_smoothing=sm_l
        )
        adx_s = calculate_adx(
            df["high"], df["low"], df["close"], di_length=di_s, adx_smoothing=sm_s
        )
        out["adx_long"] = float(adx_l["adx"].iloc[row_i])
        out["adx_short"] = float(adx_s["adx"].iloc[row_i])
        out["adx"] = out["adx_long"]
        out["plus_di"] = float(adx_l["plus_di"].iloc[row_i])
        out["minus_di"] = float(adx_l["minus_di"].iloc[row_i])
        ab_l = check_adx_threshold(adx_l["adx"], threshold=th_l)
        ab_s = check_adx_threshold(adx_s["adx"], threshold=th_s)
        out["adx_above_threshold_long"] = bool(ab_l.iloc[row_i])
        out["adx_above_threshold_short"] = bool(ab_s.iloc[row_i])

    out["supertrend"] = out["supertrend_long"]
    out["direction"] = out["direction_long"]
    out["st_bull_flip"] = bool(out["st_bull_flip_long"] or out["st_bull_flip_short"])
    out["st_bear_flip"] = bool(out["st_bear_flip_long"] or out["st_bear_flip_short"])
    out["adx_above_threshold"] = out["adx_above_threshold_long"]

    return out
