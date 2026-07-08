"""
Timezone-safe datetime index helpers for IBKR bar DataFrames.

IB returns bar.date as strings (formatDate=2), Python datetimes, or Timestamps.
Mixing these in one buffer produces a plain pandas Index without .tz — guard here.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Union

import pandas as pd
import pytz

TzLike = Union[str, pytz.BaseTzInfo]


def _as_tz(tz: TzLike) -> pytz.BaseTzInfo:
    return pytz.timezone(tz) if isinstance(tz, str) else tz


def normalize_bar_timestamp(bar_date: Any, tz: TzLike = "US/Eastern") -> pd.Timestamp:
    """Normalize any IBKR bar.date value to a timezone-aware Timestamp."""
    target = _as_tz(tz)
    ts = pd.Timestamp(bar_date)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(target)


def ensure_datetime_index(
    df: pd.DataFrame,
    *,
    tz: TzLike = "US/Eastern",
    datetime_col: Optional[str] = "datetime",
) -> pd.DataFrame:
    """
    Return a copy with a timezone-aware DatetimeIndex in ``tz``.

    Safe when the index is a plain Index, object dtype, or mixed timestamps.
    """
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()

    out = df.copy()
    target = _as_tz(tz)

    if datetime_col is not None and datetime_col in out.columns:
        out[datetime_col] = pd.to_datetime(out[datetime_col], utc=True)
        out = out.set_index(datetime_col)
    elif not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, utc=True)
    else:
        out.index = pd.to_datetime(out.index, utc=True)

    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    out.index = out.index.tz_convert(target)
    return out.sort_index()


def bars_to_ohlcv_dataframe(
    bars: Iterable[Any],
    *,
    tz: TzLike = "US/Eastern",
) -> pd.DataFrame:
    """Convert IBKR BarData / SimpleNamespace bars to an OHLCV DataFrame."""
    rows = []
    for bar in bars:
        rows.append(
            {
                "datetime": normalize_bar_timestamp(bar.date, tz),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(getattr(bar, "volume", 0) or 0),
                "average": float(getattr(bar, "average", bar.close)),
                "bar_count": int(getattr(bar, "barCount", 0) or 0),
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return ensure_datetime_index(df, tz=tz, datetime_col="datetime")
