"""
Build primary-timeframe OHLC bars from live reqMktData ticks.

This is an optional fast path for clock-boundary execution. It deliberately uses
live last-trade ticks, so the resulting OHLC can differ from IB's later
historical/keepUpToDate bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

import pandas as pd
import pytz

from data.realtime_feed import bar_size_to_seconds, expected_closed_bar_ts


@dataclass
class TickBuiltBar:
    start: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    bar_count: int = 0

    def to_bar(self) -> SimpleNamespace:
        return SimpleNamespace(
            date=self.start.to_pydatetime(),
            open=float(self.open),
            high=float(self.high),
            low=float(self.low),
            close=float(self.close),
            volume=float(self.volume),
            average=float(self.close),
            barCount=int(self.bar_count),
        )


class TickBarBuilder:
    """Accumulate live last-trade ticks into primary bars."""

    def __init__(self, *, bar_size: str, timezone: str = "US/Eastern") -> None:
        self.bar_size = bar_size
        self.timezone = pytz.timezone(timezone)
        self.interval_sec = bar_size_to_seconds(bar_size)
        self._forming: Optional[TickBuiltBar] = None
        self._last_finalized_start: Optional[pd.Timestamp] = None
        self._finalized: dict[pd.Timestamp, TickBuiltBar] = {}

    @property
    def last_finalized_start(self) -> Optional[pd.Timestamp]:
        return self._last_finalized_start

    def _normalize_ts(self, when: Any) -> pd.Timestamp:
        ts = pd.Timestamp(when or datetime.now(self.timezone))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(self.timezone)

    def _bar_start_for(self, when: Any) -> pd.Timestamp:
        ts = self._normalize_ts(when)
        epoch = int(ts.timestamp())
        start_epoch = epoch - (epoch % self.interval_sec)
        return pd.Timestamp(start_epoch, unit="s", tz="UTC").tz_convert(self.timezone)

    @staticmethod
    def _valid_price(value: Any) -> bool:
        try:
            v = float(value)
            return v == v and v > 0
        except (TypeError, ValueError):
            return False

    def update_from_ticker(self, ticker: Any) -> Optional[TickBuiltBar]:
        """Update the forming bar from a reqMktData ticker."""
        price = getattr(ticker, "last", None)
        if not self._valid_price(price):
            return self._forming

        tick_time = getattr(ticker, "time", None) or datetime.now(self.timezone)
        start = self._bar_start_for(tick_time)
        price_f = float(price)
        size = getattr(ticker, "lastSize", 0) or 0
        try:
            size_f = float(size)
        except (TypeError, ValueError):
            size_f = 0.0

        if self._forming is None or self._forming.start != start:
            if self._forming is not None:
                self._finalized[self._forming.start] = self._forming
            self._forming = TickBuiltBar(
                start=start,
                open=price_f,
                high=price_f,
                low=price_f,
                close=price_f,
                volume=max(0.0, size_f),
                bar_count=1,
            )
            return self._forming

        self._forming.high = max(self._forming.high, price_f)
        self._forming.low = min(self._forming.low, price_f)
        self._forming.close = price_f
        self._forming.volume += max(0.0, size_f)
        self._forming.bar_count += 1
        return self._forming

    def finalize_expected_closed(self, now: Optional[datetime] = None) -> Optional[TickBuiltBar]:
        """
        Return the tick-built bar expected to have just closed.

        The caller controls boundary timing; this method only checks whether the
        forming/finalized bar for that expected timestamp exists and has not
        already been emitted.
        """
        expected, _ = expected_closed_bar_ts(
            now or datetime.now(self.timezone),
            self.interval_sec,
            self.timezone,
        )
        if self._last_finalized_start is not None and self._last_finalized_start >= expected:
            return None

        bar = self._finalized.get(expected)
        if bar is None and self._forming is not None and self._forming.start == expected:
            bar = self._forming
            self._finalized[expected] = bar

        if bar is None:
            return None

        self._last_finalized_start = expected
        return bar

    def seconds_until_next_emit(self, *, grace_sec: float, now: Optional[datetime] = None) -> float:
        ts = self._normalize_ts(now or datetime.now(self.timezone))
        epoch = ts.timestamp()
        next_boundary = epoch - (epoch % self.interval_sec) + self.interval_sec
        return max(0.0, next_boundary + float(grace_sec) - epoch)

