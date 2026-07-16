#!/usr/bin/env python3
"""Unit tests for tick-built primary bars (no IB connection)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.tick_bar_builder import TickBarBuilder
from data.realtime_feed import RealtimeFeed

ET = pytz.timezone("US/Eastern")


def _dt(hh: int, mm: int, ss: int) -> datetime:
    return ET.localize(datetime(2026, 7, 10, hh, mm, ss))


def _tick(price: float, hh: int, mm: int, ss: int, size: int = 1):
    return SimpleNamespace(last=price, lastSize=size, time=_dt(hh, mm, ss))


def test_builds_ohlc_for_one_bar() -> None:
    b = TickBarBuilder(bar_size="5 mins")
    b.update_from_ticker(_tick(100.0, 9, 30, 1, 2))
    b.update_from_ticker(_tick(102.0, 9, 31, 1, 3))
    b.update_from_ticker(_tick(99.5, 9, 32, 1, 4))
    b.update_from_ticker(_tick(101.0, 9, 34, 59, 5))

    bar = b.finalize_expected_closed(_dt(9, 35, 1))
    assert bar is not None
    assert bar.start == ET.localize(datetime(2026, 7, 10, 9, 30, 0))
    assert bar.open == 100.0
    assert bar.high == 102.0
    assert bar.low == 99.5
    assert bar.close == 101.0
    assert bar.volume == 14.0
    assert bar.bar_count == 4


def test_does_not_finalize_same_bar_twice() -> None:
    b = TickBarBuilder(bar_size="5 mins")
    b.update_from_ticker(_tick(100.0, 9, 30, 1))
    assert b.finalize_expected_closed(_dt(9, 35, 1)) is not None
    assert b.finalize_expected_closed(_dt(9, 35, 2)) is None


def test_rolls_to_next_forming_bar() -> None:
    b = TickBarBuilder(bar_size="5 mins")
    b.update_from_ticker(_tick(100.0, 9, 30, 1))
    b.update_from_ticker(_tick(101.0, 9, 35, 1))

    first = b.finalize_expected_closed(_dt(9, 35, 2))
    assert first is not None
    assert first.close == 100.0

    b.update_from_ticker(_tick(102.0, 9, 36, 1))
    second = b.finalize_expected_closed(_dt(9, 40, 1))
    assert second is not None
    assert second.open == 101.0
    assert second.close == 102.0


def test_feed_external_bar_adds_forming_placeholder() -> None:
    feed = RealtimeFeed(None, None, bar_size="5 mins")
    feed._is_running = True
    seen = []

    def on_close(df, bar):
        seen.append((df.index[-2], df.index[-1], float(df.iloc[-2]["close"])))

    feed.on_bar_close(on_close)
    bar = SimpleNamespace(
        date=_dt(9, 30, 0),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10,
        average=101.0,
        barCount=4,
    )

    assert feed.emit_external_bar(bar, source="tick") is True
    assert len(seen) == 1
    closed_idx, forming_idx, close = seen[0]
    assert closed_idx == ET.localize(datetime(2026, 7, 10, 9, 30, 0))
    assert forming_idx == ET.localize(datetime(2026, 7, 10, 9, 35, 0))
    assert close == 101.0


def main() -> int:
    tests = [
        test_builds_ohlc_for_one_bar,
        test_does_not_finalize_same_bar_twice,
        test_rolls_to_next_forming_bar,
        test_feed_external_bar_adds_forming_placeholder,
    ]
    for fn in tests:
        fn()
        print(f"  OK {fn.__name__}")
    print("All tick bar builder tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

