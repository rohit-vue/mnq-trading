#!/usr/bin/env python3
"""Regression checks for pending EMA-cross entries."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.signal_engine import SignalEngine, SignalType


def _short_pending_bar(
    *, is_1h_close_bar: bool, timestamp: str = "2026-07-02 02:55:00"
) -> pd.Series:
    return pd.Series(
        {
            "close": 29920.50,
            "volume": 5021,
            "st_bear_short": True,
            "st_bear_flip_short": False,
            "st_bull_flip_long": False,
            "supertrend_short": 30066.50,
            "direction_short": 1,
            "ema_1h": 30046.91,
            "close_1h": 29920.50,
            "high_1h": 30066.50,
            "low_1h": 29894.25,
            "close_1h_cross": 30050.25,
            "ema_1h_cross": 30048.18,
            "ema_bear_cross": False,
            "ema_bull_cross": False,
            "is_1h_close_bar": is_1h_close_bar,
            "adx": 41.3,
            "adx_above_threshold_short": True,
        },
        name=pd.Timestamp(timestamp, tz="US/Eastern"),
    )


def _long_pending_hour_close_bar_without_flag(
    *, timestamp: str = "2026-07-02 08:55:00", primary_bar_minutes: int = 5
) -> pd.Series:
    return pd.Series(
        {
            "close": 30182.50,
            "volume": 14765,
            "st_bull_long": True,
            "st_bull_flip_long": False,
            "st_bear_flip_short": False,
            "supertrend_long": 30076.86,
            "direction_long": -1,
            "ema_1h": 30045.26,
            "close_1h": 30182.50,
            "high_1h": 30319.75,
            "low_1h": 30021.75,
            "close_1h_cross": 30034.50,
            "ema_1h_cross": 30043.88,
            "ema_bear_cross": False,
            "ema_bull_cross": False,
            "primary_bar_minutes": primary_bar_minutes,
            "adx": 39.4,
            "adx_above_threshold_long": True,
        },
        name=pd.Timestamp(timestamp, tz="US/Eastern"),
    )


def test_pending_long_confirms_on_hour_close_timestamp_without_flag() -> None:
    engine = SignalEngine(use_adx_long=False, use_adx_short=False)
    signal, updates = engine.evaluate_entry_conditions(
        _long_pending_hour_close_bar_without_flag(),
        position_size=0,
        traded_in_bull_trend=False,
        traded_in_bear_trend=False,
        pending_long_ema_wait=True,
    )
    assert signal is not None
    assert signal.signal_type == SignalType.BUY
    assert signal.trigger == "ema_cross"
    assert signal.price == 30182.50
    assert updates.get("clear_pending_long_ema_wait") is True


def test_pending_long_uses_primary_bar_minutes_for_hour_close() -> None:
    engine = SignalEngine(use_adx_long=False, use_adx_short=False)
    signal, updates = engine.evaluate_entry_conditions(
        _long_pending_hour_close_bar_without_flag(
            timestamp="2026-07-02 08:50:00",
            primary_bar_minutes=10,
        ),
        position_size=0,
        traded_in_bull_trend=False,
        traded_in_bear_trend=False,
        pending_long_ema_wait=True,
    )
    assert signal is not None
    assert signal.signal_type == SignalType.BUY
    assert signal.price == 30182.50
    assert updates.get("clear_pending_long_ema_wait") is True


def test_pending_long_does_not_treat_0850_as_5m_hour_close() -> None:
    engine = SignalEngine(use_adx_long=False, use_adx_short=False)
    signal, updates = engine.evaluate_entry_conditions(
        _long_pending_hour_close_bar_without_flag(
            timestamp="2026-07-02 08:50:00",
            primary_bar_minutes=5,
        ),
        position_size=0,
        traded_in_bull_trend=False,
        traded_in_bear_trend=False,
        pending_long_ema_wait=True,
    )
    assert signal is None
    assert updates == {}


def test_pending_short_confirms_on_hour_close_bar() -> None:
    engine = SignalEngine(use_adx_long=False, use_adx_short=False)
    signal, updates = engine.evaluate_entry_conditions(
        _short_pending_bar(is_1h_close_bar=True),
        position_size=0,
        traded_in_bull_trend=False,
        traded_in_bear_trend=False,
        pending_short_ema_wait=True,
    )
    assert signal is not None
    assert signal.signal_type == SignalType.SELL
    assert signal.trigger == "ema_cross"
    assert signal.price == 29920.50
    assert updates.get("clear_pending_short_ema_wait") is True


def test_pending_short_waits_until_hour_close_bar() -> None:
    engine = SignalEngine(use_adx_long=False, use_adx_short=False)
    signal, updates = engine.evaluate_entry_conditions(
        _short_pending_bar(
            is_1h_close_bar=False,
            timestamp="2026-07-02 02:50:00",
        ),
        position_size=0,
        traded_in_bull_trend=False,
        traded_in_bear_trend=False,
        pending_short_ema_wait=True,
    )
    assert signal is None
    assert updates == {}


if __name__ == "__main__":
    test_pending_long_confirms_on_hour_close_timestamp_without_flag()
    test_pending_long_uses_primary_bar_minutes_for_hour_close()
    test_pending_long_does_not_treat_0850_as_5m_hour_close()
    test_pending_short_confirms_on_hour_close_bar()
    test_pending_short_waits_until_hour_close_bar()
    print("pending EMA entry tests OK")
