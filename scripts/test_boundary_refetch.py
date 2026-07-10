#!/usr/bin/env python3
"""Unit tests for stream-first boundary refetch gates (no IB connection)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.realtime_feed import (
    bar_size_to_seconds,
    expected_closed_bar_ts,
    should_boundary_refetch,
)

ET = pytz.timezone("US/Eastern")


def _dt(y, m, d, hh, mm, ss=0) -> datetime:
    return ET.localize(datetime(y, m, d, hh, mm, ss))


def test_bar_size_to_seconds() -> None:
    assert bar_size_to_seconds("5 mins") == 300
    assert bar_size_to_seconds("10 mins") == 600
    assert bar_size_to_seconds("1 hour") == 3600


def test_expected_closed_at_boundary() -> None:
    # Exactly on a 5-min boundary: forming bar has just started; previous closed.
    now = _dt(2026, 7, 8, 16, 35, 0)
    closed, sec = expected_closed_bar_ts(now, 300, ET)
    assert sec == 0.0
    assert closed == ET.localize(datetime(2026, 7, 8, 16, 30, 0))


def test_expected_closed_one_sec_in() -> None:
    now = _dt(2026, 7, 8, 16, 35, 1)
    closed, sec = expected_closed_bar_ts(now, 300, ET)
    assert abs(sec - 1.0) < 1e-6
    assert closed == ET.localize(datetime(2026, 7, 8, 16, 30, 0))


def test_stream_already_emitted_skips() -> None:
    now = _dt(2026, 7, 8, 16, 35, 2)
    emitted = ET.localize(datetime(2026, 7, 8, 16, 30, 0))
    ok, _, _ = should_boundary_refetch(
        now=now,
        bar_interval_sec=300,
        tz=ET,
        last_emitted=emitted,
        last_boundary_refetch_for=None,
        grace_sec=1.0,
        window_sec=12.0,
    )
    assert ok is False


def test_before_grace_skips() -> None:
    now = _dt(2026, 7, 8, 16, 35, 0)
    ok, _, sec = should_boundary_refetch(
        now=now,
        bar_interval_sec=300,
        tz=ET,
        last_emitted=None,
        last_boundary_refetch_for=None,
        grace_sec=1.0,
        window_sec=12.0,
    )
    assert sec == 0.0
    assert ok is False


def test_grace_plus_one_allows() -> None:
    now = _dt(2026, 7, 8, 16, 35, 1)
    ok, expected, sec = should_boundary_refetch(
        now=now,
        bar_interval_sec=300,
        tz=ET,
        last_emitted=None,
        last_boundary_refetch_for=None,
        grace_sec=1.0,
        window_sec=12.0,
    )
    assert abs(sec - 1.0) < 1e-6
    assert ok is True
    assert expected == ET.localize(datetime(2026, 7, 8, 16, 30, 0))


def test_after_window_skips_boundary() -> None:
    now = _dt(2026, 7, 8, 16, 35, 20)
    ok, _, _ = should_boundary_refetch(
        now=now,
        bar_interval_sec=300,
        tz=ET,
        last_emitted=None,
        last_boundary_refetch_for=None,
        grace_sec=1.0,
        window_sec=12.0,
    )
    assert ok is False


def test_once_per_boundary() -> None:
    now = _dt(2026, 7, 8, 16, 35, 3)
    already = ET.localize(datetime(2026, 7, 8, 16, 30, 0))
    ok, _, _ = should_boundary_refetch(
        now=now,
        bar_interval_sec=300,
        tz=ET,
        last_emitted=None,
        last_boundary_refetch_for=already,
        grace_sec=1.0,
        window_sec=12.0,
    )
    assert ok is False


def main() -> int:
    tests = [
        test_bar_size_to_seconds,
        test_expected_closed_at_boundary,
        test_expected_closed_one_sec_in,
        test_stream_already_emitted_skips,
        test_before_grace_skips,
        test_grace_plus_one_allows,
        test_after_window_skips_boundary,
        test_once_per_boundary,
    ]
    for fn in tests:
        fn()
        print(f"  OK {fn.__name__}")
    print("All boundary-refetch gate tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
