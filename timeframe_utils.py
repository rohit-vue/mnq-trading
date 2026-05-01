"""
Map strategy.yaml timeframes.primary to IBKR bar size, pandas resample rule,
and hour-boundary logic.

Primary timeframe = bar size used for Supertrend (chart/strategy execution).
EMA200 is always calculated on 1-hour bars (timeframes.ema_filter: "1H"); only
the Supertrend (and ADX) bar size is changed by timeframes.primary.

Supported primary values: 5m, 10m, 15m, 30m, 45m, 1h.
"""
from typing import Dict, Any

# strategy.yaml primary value (e.g. "10m") -> IBKR barSizeSetting (e.g. "10 mins")
# Primary = bar size for Supertrend / chart; EMA is always on 1H.
PRIMARY_TO_IBKR_BAR_SIZE: Dict[str, str] = {
    "5m": "5 mins",
    "10m": "10 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "45m": "45 mins",
    "1h": "1 hour",
}

# strategy.yaml primary -> pandas resample rule (e.g. "10min")
PRIMARY_TO_PANDAS_RESAMPLE: Dict[str, str] = {
    "5m": "5min",
    "10m": "10min",
    "15m": "15min",
    "30m": "30min",
    "45m": "45min",
    "1h": "1h",
}

# Last minute of the hour for the final primary bar in that hour (10m: :50, 15m: :45, 1h: :00)
PRIMARY_TO_LAST_MINUTE_OF_HOUR: Dict[str, int] = {
    "5m": 55,
    "10m": 50,
    "15m": 45,
    "30m": 30,
    "45m": 45,
    "1h": 0,
}

# Bars per hour (for "min bars needed for 1H" checks)
PRIMARY_TO_BARS_PER_HOUR: Dict[str, int] = {
    "5m": 12,
    "10m": 6,
    "15m": 4,
    "30m": 2,
    "45m": 2,
    "1h": 1,
}

DEFAULT_PRIMARY = "10m"


def get_primary_timeframe(strategy_cfg: Dict[str, Any]) -> str:
    """Return timeframes.primary from strategy config; default '10m'."""
    timeframes = strategy_cfg.get("timeframes") or {}
    primary = (timeframes.get("primary") or DEFAULT_PRIMARY).strip().lower()
    return primary if primary in PRIMARY_TO_IBKR_BAR_SIZE else DEFAULT_PRIMARY


def get_primary_bar_size(strategy_cfg: Dict[str, Any]) -> str:
    """IBKR bar size string for RealtimeFeed / historical / stitcher (e.g. '10 mins')."""
    primary = get_primary_timeframe(strategy_cfg)
    return PRIMARY_TO_IBKR_BAR_SIZE.get(primary, PRIMARY_TO_IBKR_BAR_SIZE[DEFAULT_PRIMARY])


def get_primary_resample_rule(strategy_cfg: Dict[str, Any]) -> str:
    """Pandas resample rule for 1m->primary (e.g. '10min')."""
    primary = get_primary_timeframe(strategy_cfg)
    return PRIMARY_TO_PANDAS_RESAMPLE.get(primary, PRIMARY_TO_PANDAS_RESAMPLE[DEFAULT_PRIMARY])


def get_primary_last_minute_of_hour(strategy_cfg: Dict[str, Any]) -> int:
    """Minute of the last primary bar in each hour (10m->50, 15m->45, etc.)."""
    primary = get_primary_timeframe(strategy_cfg)
    return PRIMARY_TO_LAST_MINUTE_OF_HOUR.get(primary, PRIMARY_TO_LAST_MINUTE_OF_HOUR[DEFAULT_PRIMARY])


def get_primary_bars_per_hour(strategy_cfg: Dict[str, Any]) -> int:
    """Number of primary bars per hour (10m->6, 15m->4, etc.)."""
    primary = get_primary_timeframe(strategy_cfg)
    return PRIMARY_TO_BARS_PER_HOUR.get(primary, PRIMARY_TO_BARS_PER_HOUR[DEFAULT_PRIMARY])
