"""Unit checks for stitched EMA warmup detection (no IBKR required)."""

import sys
from datetime import datetime
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.feed_warmup import compute_ema_warmup_days, needs_stitched_ema_warmup

CONTRACT_CFG = {
    "rollover": {
        "enabled": True,
        "method": "volume",
        "roll_window_days": 10,
        "confirmation_days": 1,
        "timezone": "US/Eastern",
    }
}


def test_warmup_days_matches_backtest():
    assert compute_ema_warmup_days(200) == 30


def test_needs_stitch_inside_roll_window():
    from types import SimpleNamespace

    # June 2026 expiry window: MNQM6 third Friday is 2026-06-19
    contract = SimpleNamespace(lastTradeDateOrContractMonth="20260619")
    as_of = pytz.timezone("US/Eastern").localize(datetime(2026, 6, 15, 12, 0))
    assert needs_stitched_ema_warmup(contract, CONTRACT_CFG, 200, as_of=as_of)


def test_no_stitch_far_from_roll():
    from types import SimpleNamespace

    contract = SimpleNamespace(lastTradeDateOrContractMonth="20260918")
    as_of = pytz.timezone("US/Eastern").localize(datetime(2026, 3, 1, 12, 0))
    assert not needs_stitched_ema_warmup(contract, CONTRACT_CFG, 200, as_of=as_of)


def test_build_dataframe_mixed_bar_date_types():
    """Merged IB strings + stitched Timestamps must not break _build_dataframe."""
    from types import SimpleNamespace

    import pandas as pd
    import pytz

    from data.realtime_feed import RealtimeFeed

    tz = pytz.timezone("US/Eastern")
    feed = RealtimeFeed.__new__(RealtimeFeed)
    feed.timezone = tz
    feed._bars = [
        SimpleNamespace(
            date="20260629 10:20:00",
            open=29382.0,
            high=29437.0,
            low=29337.0,
            close=29402.0,
            volume=100.0,
            average=29402.0,
            barCount=10,
        ),
        SimpleNamespace(
            date=pd.Timestamp("2026-06-29 10:25:00", tz="US/Eastern"),
            open=29402.0,
            high=29450.0,
            low=29390.0,
            close=29442.0,
            volume=120.0,
            average=29442.0,
            barCount=12,
        ),
    ]
    feed._build_dataframe()
    assert feed._df is not None
    assert len(feed._df) == 2
    assert isinstance(feed._df.index, pd.DatetimeIndex)
    assert feed._df.index.tz is not None


if __name__ == "__main__":
    test_warmup_days_matches_backtest()
    test_needs_stitch_inside_roll_window()
    test_no_stitch_far_from_roll()
    test_build_dataframe_mixed_bar_date_types()
    print("feed_warmup tests OK")
