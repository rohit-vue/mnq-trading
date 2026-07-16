"""
=============================================================================
REAL-TIME DATA FEED
=============================================================================
Handles real-time bar updates from IBKR for live trading.
Ensures bar-close execution matching Pine Script's process_orders_on_close.

Key Features:
- reqHistoricalData with keepUpToDate=True for streaming bars
- Bar close detection for signal execution
- Multi-timeframe synchronization
=============================================================================
"""

import asyncio
import pandas as pd
import numpy as np
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional, Callable, Dict, Any, List, Tuple
import logging
import pytz
from collections import deque

from data.bar_index import bars_to_ohlcv_dataframe, normalize_bar_timestamp

logger = logging.getLogger(__name__)


def bar_size_to_seconds(bar_size: str) -> int:
    """Convert IB barSizeSetting (e.g. '5 mins', '1 hour') to seconds."""
    size = (bar_size or "10 mins").lower().strip()
    if size in ("1 hour", "1hour", "60 mins", "60 min"):
        return 3600
    m = re.match(r"(\d+)\s*min", size)
    if m:
        return max(60, int(m.group(1)) * 60)
    return 600


def expected_closed_bar_ts(
    now: datetime,
    bar_interval_sec: int,
    tz: Any,
) -> Tuple[pd.Timestamp, float]:
    """
    Return (expected latest closed bar start, seconds into the current forming bar).

    After a 5-min boundary at 16:35:00, the closed bar is the one that started 16:30:00.
    """
    ts = pd.Timestamp(now)
    if ts.tzinfo is None:
        ts = tz.localize(ts.to_pydatetime()) if hasattr(tz, "localize") else ts.tz_localize(tz)
    else:
        ts = ts.tz_convert(tz)

    epoch = int(ts.timestamp())
    floored = epoch - (epoch % int(bar_interval_sec))
    sec_into_bar = (ts.timestamp() - floored)
    current_start = pd.Timestamp(floored, unit="s", tz="UTC").tz_convert(tz)
    closed_start = current_start - pd.Timedelta(seconds=int(bar_interval_sec))
    return closed_start, float(sec_into_bar)


def should_boundary_refetch(
    *,
    now: datetime,
    bar_interval_sec: int,
    tz: Any,
    last_emitted: Optional[pd.Timestamp],
    last_boundary_refetch_for: Optional[pd.Timestamp],
    grace_sec: float = 1.0,
    window_sec: float = 12.0,
) -> Tuple[bool, pd.Timestamp, float]:
    """
    Stream-first gate for historical refetch failover.

    Returns (should_refetch, expected_closed_ts, sec_into_bar).
    Refetch only when the expected closed bar was not yet emitted and wall time is
    in [grace_sec, grace_sec + window_sec] after the bar boundary.
    """
    expected_closed, sec_into_bar = expected_closed_bar_ts(now, bar_interval_sec, tz)

    def _as_ts(value) -> Optional[pd.Timestamp]:
        if value is None:
            return None
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize(tz)
        else:
            ts = ts.tz_convert(tz)
        return ts

    emitted = _as_ts(last_emitted)
    if emitted is not None and emitted >= expected_closed:
        return False, expected_closed, sec_into_bar

    prev = _as_ts(last_boundary_refetch_for)
    if prev is not None and prev >= expected_closed:
        return False, expected_closed, sec_into_bar

    if sec_into_bar < grace_sec:
        return False, expected_closed, sec_into_bar
    if sec_into_bar > grace_sec + window_sec:
        return False, expected_closed, sec_into_bar

    return True, expected_closed, sec_into_bar


class RealtimeFeed:
    """
    Real-time bar data feed from IBKR.
    
    Uses reqHistoricalData with keepUpToDate=True to receive
    streaming bar updates while maintaining historical context.
    
    Key Principle:
    - Signals are ONLY emitted on confirmed bar closes
    - This matches Pine Script's process_orders_on_close = true
    """
    
    def __init__(
        self,
        ib_client,
        contract,
        bar_size: str = "10 mins",
        lookback_bars: int = 500,
        timezone: str = "US/Eastern"
    ):
        """
        Initialize the real-time feed.
        
        Parameters:
        -----------
        ib_client : IB
            Connected ib-insync IB client
        contract : Contract
            IBKR Contract object (MNQ futures)
        bar_size : str
            Bar size (default "10 mins" per strategy requirement)
        lookback_bars : int
            Number of historical bars to maintain in memory
        timezone : str
            Timezone for bar timestamps
        """
        self.ib = ib_client
        self.contract = contract
        self.bar_size = bar_size
        self.lookback_bars = lookback_bars
        self.timezone = pytz.timezone(timezone)
        
        # Bar storage
        self._bars: List = []
        self._df: Optional[pd.DataFrame] = None
        
        # Subscriptions
        self._subscription = None
        self._on_bar_close_callbacks: List[Callable] = []
        self._on_bar_update_callbacks: List[Callable] = []
        
        # State
        self._is_running = False
        self._last_bar_time: Optional[datetime] = None
        self._last_wall_update: Optional[datetime] = None
        self._bar_update_count: int = 0
        # Stream health (wall-clock); do not confuse with delayed bar timestamps.
        self._last_stream_activity_wall: Optional[datetime] = None
        self._last_emitted_closed_ts: Optional[pd.Timestamp] = None
        self._last_seen_bar_count: int = 0
        self._last_restart_wall: Optional[datetime] = None
        # Boundary-aligned refetch failover when keepUpToDate stream misses a close.
        self._last_refetch_wall: Optional[datetime] = None
        self._last_boundary_refetch_for: Optional[pd.Timestamp] = None
        self._synthetic_closed_bars: Dict[pd.Timestamp, Any] = {}
        self.reconcile_synthetic_official: bool = True
        self._max_buffer_bars: int = 8000

        # 1H bar tracking for multi-timeframe
        self._1h_bars: deque = deque(maxlen=300)
        self._current_1h_start: Optional[datetime] = None

        # Optional stitched EMA warmup (paper/live near rollover)
        self._contract_cfg: Optional[Dict[str, Any]] = None
        self._ema_length: int = 200

    def set_ema_warmup_context(
        self,
        contract_cfg: Optional[Dict[str, Any]],
        ema_length: int = 200,
    ) -> None:
        """Enable volume-stitched EMA preload when near quarterly rollover."""
        self._contract_cfg = contract_cfg
        self._ema_length = int(ema_length)

    def preload_bars(self, bars: List) -> None:
        """Merge historical bars into the buffer (e.g. stitched EMA warmup)."""
        if not bars:
            return
        if self._bars:
            self._merge_bars(bars)
        else:
            self._bars = list(bars)
            if len(self._bars) > self._max_buffer_bars:
                self._bars = self._bars[-self._max_buffer_bars :]
        self._build_dataframe()

    async def _maybe_preload_stitched_ema_warmup(self) -> None:
        if self._contract_cfg is None:
            return
        from data.feed_warmup import preload_stitched_ema_warmup

        await preload_stitched_ema_warmup(
            self,
            ib=self.ib,
            contract=self.contract,
            contract_cfg=self._contract_cfg,
            ema_length=self._ema_length,
            bar_size=self.bar_size,
        )

    async def start(self, initial_lookback_days: int = 10) -> None:
        """
        Start the real-time data feed.

        Fetches initial historical data then subscribes to updates.
        Cleans up any existing dead subscription before starting.

        Parameters:
        -----------
        initial_lookback_days : int
            Days of history to fetch initially
        """
        # Clean up any existing subscription first (e.g., after reconnect)
        if self._subscription is not None:
            logger.info("Cleaning up previous feed subscription before restart...")
            try:
                self._subscription.updateEvent -= self._on_bar_update
            except (ValueError, AttributeError):
                pass  # Handler wasn't registered or subscription is dead
            try:
                self.ib.cancelHistoricalData(self._subscription)
            except Exception as e:
                logger.debug(f"Could not cancel old subscription (may already be dead): {e}")
            self._subscription = None

        logger.info(f"Starting real-time feed for {self.contract.symbol}")

        # Near rollover: preload volume-stitched history so EMA200 matches backtest.
        await self._maybe_preload_stitched_ema_warmup()

        # Subscribe with keepUpToDate
        self._subscription = await self.ib.reqHistoricalDataAsync(
            contract=self.contract,
            endDateTime='',  # Empty = now
            durationStr=f"{initial_lookback_days} D",
            barSizeSetting=self.bar_size,
            whatToShow='TRADES',
            useRTH=False,
            formatDate=2,
            keepUpToDate=True
        )

        # Register update handler
        self._subscription.updateEvent += self._on_bar_update

        # Merge fresh bars into the existing buffer so a restart/reconnect does NOT
        # discard the long history needed to warm the 1H EMA200. A restart re-fetches
        # only a few days; replacing the buffer would shrink it and distort the EMA.
        # On the first start the buffer is empty, so this just stores the fetched bars.
        fresh_bars = list(self._subscription)
        if self._bars:
            prev_len = len(self._bars)
            self._merge_bars(fresh_bars)
            logger.info(
                "Feed restart merged %s fetched bars into buffer (%s -> %s bars, history preserved)",
                len(fresh_bars),
                prev_len,
                len(self._bars),
            )
        else:
            self._bars = fresh_bars
        self._build_dataframe()

        self._is_running = True
        now = datetime.now(self.timezone)
        self._last_wall_update = now
        self._touch_stream_activity(now)
        self._last_seen_bar_count = len(self._bars)
        first_init = self._last_emitted_closed_ts is None
        if self._bars:
            self._last_bar_time = self._normalize_bar_ts(self._bars[-1].date)
            if len(self._bars) >= 2:
                latest_closed = self._bars[-2]
                closed_ts = self._normalize_bar_ts(latest_closed.date)
                if first_init:
                    # First start: do not re-fire strategy on historical bars.
                    self._last_emitted_closed_ts = closed_ts
                elif closed_ts > self._last_emitted_closed_ts:
                    # Restart/reconnect: a new bar formed during the gap — process it.
                    self._process_closed_bar(latest_closed, source="restart")
        logger.info(
            "Real-time feed started with %s initial bars (last closed=%s)",
            len(self._bars),
            self._last_emitted_closed_ts,
        )
        
    async def restart(self, initial_lookback_days: int = 10) -> None:
        """Stop and resubscribe to historical streaming bars (recover from stale feed)."""
        logger.info("Restarting real-time feed subscription...")
        self.mark_restarted()
        await self.stop()
        await self.start(initial_lookback_days=initial_lookback_days)
        
    async def stop(self) -> None:
        """Stop the real-time feed and clean up."""
        self._is_running = False
        
        if self._subscription:
            self.ib.cancelHistoricalData(self._subscription)
            self._subscription = None
            
        logger.info("Real-time feed stopped")

    def _normalize_bar_ts(self, bar_date) -> pd.Timestamp:
        return normalize_bar_timestamp(bar_date, self.timezone)

    def _touch_stream_activity(self, when: Optional[datetime] = None) -> None:
        self._last_stream_activity_wall = when or datetime.now(self.timezone)

    def _process_closed_bar(self, closed_bar, *, source: str) -> None:
        """Run bar-close callbacks once per completed primary bar."""
        closed_ts = self._normalize_bar_ts(closed_bar.date)
        if (
            self._last_emitted_closed_ts is not None
            and closed_ts <= self._last_emitted_closed_ts
        ):
            if (
                self.reconcile_synthetic_official
                and source != "tick"
                and closed_ts in self._synthetic_closed_bars
            ):
                tick_bar = self._synthetic_closed_bars[closed_ts]
                logger.info(
                    "OFFICIAL_BAR_RECONCILE | ts=%s | source=%s | "
                    "tick_O=%.2f tick_H=%.2f tick_L=%.2f tick_C=%.2f | "
                    "official_O=%.2f official_H=%.2f official_L=%.2f official_C=%.2f",
                    closed_ts,
                    source,
                    float(getattr(tick_bar, "open", 0) or 0),
                    float(getattr(tick_bar, "high", 0) or 0),
                    float(getattr(tick_bar, "low", 0) or 0),
                    float(getattr(tick_bar, "close", 0) or 0),
                    float(getattr(closed_bar, "open", 0) or 0),
                    float(getattr(closed_bar, "high", 0) or 0),
                    float(getattr(closed_bar, "low", 0) or 0),
                    float(getattr(closed_bar, "close", 0) or 0),
                )
            return

        self._last_emitted_closed_ts = closed_ts
        if source == "tick":
            self._synthetic_closed_bars[closed_ts] = closed_bar
        self._touch_stream_activity()
        logger.info(
            "Bar closed (%s): %s | O=%.2f H=%.2f L=%.2f C=%.2f | bars=%s",
            source,
            closed_bar.date,
            closed_bar.open,
            closed_bar.high,
            closed_bar.low,
            closed_bar.close,
            len(self._bars),
        )
        self._emit_bar_close(closed_bar)

    def emit_external_bar(self, bar, *, source: str = "tick") -> bool:
        """Merge and emit a locally built closed bar (used by tick fast path)."""
        if not self._is_running:
            return False
        closed_ts = self._normalize_bar_ts(bar.date)
        if (
            self._last_emitted_closed_ts is not None
            and closed_ts <= self._last_emitted_closed_ts
        ):
            return False

        merge_bars = [bar]
        if not self._bars or self._normalize_bar_ts(self._bars[-1].date) <= closed_ts:
            # on_bar_close expects the signal bar at df.iloc[-2] and a forming
            # bar at df.iloc[-1]. Add a zero-volume placeholder if the tick path
            # beats the first tick/stream update of the next bar.
            next_ts = closed_ts + pd.Timedelta(seconds=bar_size_to_seconds(self.bar_size))
            merge_bars.append(
                SimpleNamespace(
                    date=next_ts.to_pydatetime(),
                    open=float(bar.close),
                    high=float(bar.close),
                    low=float(bar.close),
                    close=float(bar.close),
                    volume=0.0,
                    average=float(bar.close),
                    barCount=0,
                )
            )

        self._merge_bars(merge_bars)
        self._build_dataframe()
        if self._bars:
            self._last_bar_time = self._normalize_bar_ts(self._bars[-1].date)
        self._last_seen_bar_count = len(self._bars)
        self._process_closed_bar(bar, source=source)
        return True

    def _merge_bars(self, new_bars) -> bool:
        """
        Merge freshly fetched bars into the existing buffer (keyed by timestamp).

        Preserves the long history needed for EMA200/indicator warmup while adding
        newly completed bars. Returns True if a new bar timestamp appeared.
        """
        if not new_bars:
            return False
        by_ts: Dict[pd.Timestamp, Any] = {}
        for b in self._bars:
            by_ts[self._normalize_bar_ts(b.date)] = b
        last_before = max(by_ts) if by_ts else None

        for b in new_bars:
            by_ts[self._normalize_bar_ts(b.date)] = b

        merged = [by_ts[k] for k in sorted(by_ts)]
        if len(merged) > self._max_buffer_bars:
            merged = merged[-self._max_buffer_bars:]
        self._bars = merged

        last_after = self._normalize_bar_ts(merged[-1].date) if merged else None
        if last_before is None:
            return True
        return last_after is not None and last_after > last_before

    async def poll_refetch_and_emit(
        self,
        lookback_days: int = 1,
        min_interval_sec: float = 45.0,
        only_when_market_open: bool = True,
        grace_sec: float = 1.0,
        window_sec: float = 12.0,
        idle_catchup_sec: float = 10.0,
    ) -> int:
        """
        Stream-first bar-close failover via a one-shot historical refetch.

        Primary path: keepUpToDate stream emits ``Bar closed (stream)``.
        Failover: if that close was not emitted by candle-boundary + grace_sec,
        refetch once in a short post-boundary window (default 1–13s into the new bar).

        ``idle_catchup_sec`` throttles rare catch-up refetches when we are still behind
        after the boundary window (stale stream / missed maintenance tick).
        ``min_interval_sec`` is kept for API compatibility (used as a floor for idle).
        """
        if not self._is_running:
            return 0
        if only_when_market_open and not self.is_market_hours(check_rth=False):
            return 0

        now = datetime.now(self.timezone)
        interval_sec = bar_size_to_seconds(self.bar_size)

        do_boundary, expected_closed, sec_into_bar = should_boundary_refetch(
            now=now,
            bar_interval_sec=interval_sec,
            tz=self.timezone,
            last_emitted=self._last_emitted_closed_ts,
            last_boundary_refetch_for=self._last_boundary_refetch_for,
            grace_sec=grace_sec,
            window_sec=window_sec,
        )

        # Already caught up (stream won or prior refetch emitted expected close).
        if self._last_emitted_closed_ts is not None:
            emitted = pd.Timestamp(self._last_emitted_closed_ts)
            if emitted.tzinfo is None:
                emitted = emitted.tz_localize(self.timezone)
            else:
                emitted = emitted.tz_convert(self.timezone)
            if emitted >= expected_closed:
                return 0

        do_idle_catchup = False
        if not do_boundary:
            # Outside the tight boundary window but still missing the closed bar —
            # rare catch-up so a missed window does not wait a full bar.
            idle_floor = min(idle_catchup_sec, min_interval_sec) if min_interval_sec else idle_catchup_sec
            if (
                self._last_refetch_wall is None
                or (now - self._last_refetch_wall).total_seconds() >= idle_floor
            ):
                # Only catch up if we are past the boundary window for the current expected close.
                if sec_into_bar > grace_sec + window_sec:
                    do_idle_catchup = True
            if not do_idle_catchup:
                return 0

        if do_boundary:
            logger.info(
                "Boundary refetch failover: expected_closed=%s sec_into_bar=%.1f "
                "(stream miss; grace=%.1fs)",
                expected_closed,
                sec_into_bar,
                grace_sec,
            )
        else:
            logger.info(
                "Idle catch-up refetch: expected_closed=%s sec_into_bar=%.1f",
                expected_closed,
                sec_into_bar,
            )

        self._last_refetch_wall = now
        # Claim this boundary only after we start the request so a failed attempt
        # can still retry inside the same post-boundary window.
        if do_boundary:
            self._last_boundary_refetch_for = expected_closed

        try:
            new_bars = await self.ib.reqHistoricalDataAsync(
                contract=self.contract,
                endDateTime='',
                durationStr=f"{lookback_days} D",
                barSizeSetting=self.bar_size,
                whatToShow='TRADES',
                useRTH=False,
                formatDate=2,
                keepUpToDate=False,
            )
        except Exception as e:
            logger.debug("Poll refetch failed: %s", e)
            # Allow another try within the window on the next maintenance tick.
            if do_boundary and self._last_boundary_refetch_for == expected_closed:
                self._last_boundary_refetch_for = None
            return 0

        if not new_bars:
            if do_boundary and self._last_boundary_refetch_for == expected_closed:
                self._last_boundary_refetch_for = None
            return 0

        advanced = self._merge_bars(new_bars)
        if not advanced:
            # Buffer may already contain the bar from a partial stream update;
            # still attempt emit from the merged pointer.
            pass

        self._build_dataframe()
        if self._bars:
            self._last_bar_time = self._normalize_bar_ts(self._bars[-1].date)
        self._last_seen_bar_count = len(self._bars)

        emitted = 0
        if len(self._bars) >= 2:
            latest_closed = self._bars[-2]
            closed_ts = self._normalize_bar_ts(latest_closed.date)
            if (
                self._last_emitted_closed_ts is None
                or closed_ts > self._last_emitted_closed_ts
            ):
                self._process_closed_bar(latest_closed, source="refetch")
                emitted = 1
        return emitted

    def minutes_since_stream_activity(self) -> Optional[float]:
        """Wall-clock minutes since last IB callback or detected bar advance."""
        if self._last_stream_activity_wall is None:
            return None
        now = datetime.now(self.timezone)
        return (now - self._last_stream_activity_wall).total_seconds() / 60.0

    def minutes_since_last_closed_bar(self) -> Optional[float]:
        """Minutes since the last completed bar timestamp (lags with delayed data)."""
        closed = self.get_latest_closed_bar()
        if not closed:
            return None
        ts = closed["datetime"]
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if getattr(ts, "tzinfo", None) is None:
            ts = self.timezone.localize(ts)
        now = datetime.now(self.timezone)
        return (now - ts).total_seconds() / 60.0

    def is_stream_stale(self, max_idle_minutes: float) -> bool:
        """
        True when the IB stream has had no callbacks and no bar advances
        for max_idle_minutes. Uses wall-clock idle time, not bar timestamp lag
        (delayed quotes are always ~15 min behind and must not trigger restart).
        """
        idle = self.minutes_since_stream_activity()
        if idle is None:
            return False
        return idle >= max_idle_minutes

    def can_restart(self, cooldown_minutes: float = 5.0) -> bool:
        if self._last_restart_wall is None:
            return True
        elapsed = (datetime.now(self.timezone) - self._last_restart_wall).total_seconds() / 60.0
        return elapsed >= cooldown_minutes

    def mark_restarted(self) -> None:
        self._last_restart_wall = datetime.now(self.timezone)
    
    def _on_bar_update(self, bars, has_new_bar: bool) -> None:
        """
        Handle bar updates from IBKR.
        
        Parameters:
        -----------
        bars : BarDataList
            Current bar list
        has_new_bar : bool
            True if a new bar has been added (previous bar closed)
        """
        if not bars:
            return

        self._last_wall_update = datetime.now(self.timezone)
        self._touch_stream_activity()
        self._bar_update_count += 1
        current_bar = bars[-1]
        self._last_bar_time = self._normalize_bar_ts(current_bar.date)
        prev_count = len(self._bars)
        new_bar_added = has_new_bar or len(bars) > prev_count

        # Check if new bar added (previous bar closed)
        if new_bar_added:
            # A new bar means the previous bar is now CONFIRMED/CLOSED.
            # Merge (don't replace) so streamed bars never shrink the buffer below
            # the history needed to warm the 1H EMA200.
            self._merge_bars(list(bars))
            self._build_dataframe()
            self._last_seen_bar_count = len(self._bars)

            closed_bar = bars[-2] if len(bars) >= 2 else None
            if closed_bar:
                self._process_closed_bar(closed_bar, source="stream")

            self._check_1h_bar_close(current_bar)
        else:
            # Intrabar update - just update the last bar
            if self._bars:
                self._bars[-1] = current_bar
                self._update_last_bar(current_bar)
            
            # Notify update callbacks (for UI refresh, etc.)
            for callback in self._on_bar_update_callbacks:
                try:
                    callback(current_bar)
                except Exception as e:
                    logger.error(f"Error in bar update callback: {e}")
    
    def _emit_bar_close(self, bar) -> None:
        """
        Emit bar close event to all registered callbacks.
        
        This is the ONLY point where trade signals should be evaluated.
        Matches Pine Script's process_orders_on_close behavior.
        
        Parameters:
        -----------
        bar : BarData
            The closed bar
        """
        logger.debug(f"Bar closed: {bar.date} | O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
        
        for callback in self._on_bar_close_callbacks:
            try:
                # Pass the full dataframe for indicator calculation
                callback(self._df, bar)
            except Exception as e:
                logger.error(f"Error in bar close callback: {e}")
    
    def _check_1h_bar_close(self, current_bar) -> None:
        """
        Check if a new 1H bar has started (meaning previous 1H closed).
        
        Pine Script Reference (line 34):
            isNew1HCandle = ta.change(time("60")) != 0
        
        Parameters:
        -----------
        current_bar : BarData
            The current (new) bar
        """
        bar_time = pd.Timestamp(current_bar.date)
        if bar_time.tzinfo is None:
            bar_time = bar_time.tz_localize('UTC')
        bar_time = bar_time.tz_convert(self.timezone)
        
        # Get the hour start
        current_1h_start = bar_time.floor('h')
        
        if self._current_1h_start is None:
            self._current_1h_start = current_1h_start
        elif current_1h_start != self._current_1h_start:
            # New 1H candle started
            logger.debug(f"New 1H candle started at {current_1h_start}")
            self._current_1h_start = current_1h_start
    
    def _build_dataframe(self) -> None:
        """Build pandas DataFrame from bars."""
        if not self._bars:
            self._df = pd.DataFrame()
            return
        self._df = bars_to_ohlcv_dataframe(self._bars, tz=self.timezone)
    
    def _update_last_bar(self, bar) -> None:
        """Update just the last bar in the DataFrame (for intrabar updates)."""
        if self._df is None or len(self._df) == 0:
            return
        
        self._df.iloc[-1] = {
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume,
            'average': bar.average,
            'bar_count': bar.barCount
        }
    
    def on_bar_close(self, callback: Callable) -> None:
        """
        Register a callback for bar close events.
        
        This is the primary event for signal evaluation.
        Pine Script equivalent: Evaluating conditions at bar close.
        
        Parameters:
        -----------
        callback : Callable
            Function to call when bar closes: callback(df, bar)
        """
        self._on_bar_close_callbacks.append(callback)
    
    def on_bar_update(self, callback: Callable) -> None:
        """
        Register a callback for intrabar updates.
        
        NOTE: Do NOT use this for signal evaluation!
        Only use for UI updates or monitoring.
        
        Parameters:
        -----------
        callback : Callable
            Function to call on updates: callback(bar)
        """
        self._on_bar_update_callbacks.append(callback)
    
    def last_bar_datetime(self) -> Optional[datetime]:
        """Timestamp of the latest bar in the buffer (may be the forming bar)."""
        if self._last_bar_time is not None:
            return self._last_bar_time.to_pydatetime()
        if self._df is not None and len(self._df) > 0:
            ts = self._df.index[-1]
            return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        return None

    def minutes_since_last_bar(self) -> Optional[float]:
        """Minutes since the latest bar timestamp (None if unknown)."""
        last = self.last_bar_datetime()
        if last is None:
            return None
        now = datetime.now(self.timezone)
        if last.tzinfo is None:
            last = self.timezone.localize(last)
        return (now - last).total_seconds() / 60.0

    def get_dataframe(self) -> Optional[pd.DataFrame]:
        """
        Get the current bar DataFrame.
        
        Returns:
        --------
        pd.DataFrame
            Current bars with OHLCV data
        """
        return self._df.copy() if self._df is not None else None
    
    def get_latest_closed_bar(self) -> Optional[Dict[str, Any]]:
        """
        Get the most recent CLOSED bar (not the current forming bar).
        
        For signal evaluation, we need the last confirmed bar.
        
        Returns:
        --------
        dict
            Bar data or None
        """
        if self._df is None or len(self._df) < 2:
            return None
        
        # Second to last bar is the last CLOSED bar
        bar = self._df.iloc[-2]
        return {
            'datetime': bar.name,
            'open': bar['open'],
            'high': bar['high'],
            'low': bar['low'],
            'close': bar['close'],
            'volume': bar['volume']
        }
    
    def is_market_hours(self, check_rth: bool = False) -> bool:
        """
        Check if currently in trading hours.
        
        Parameters:
        -----------
        check_rth : bool
            If True, check RTH only. If False, check ETH (full session).
        
        Returns:
        --------
        bool
            True if in trading hours
        """
        now = datetime.now(self.timezone)
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=Monday, 6=Sunday
        
        if check_rth:
            # RTH: 9:30 - 16:00 Eastern, weekdays only
            if weekday >= 5:  # Weekend
                return False
            if hour < 9 or (hour == 9 and minute < 30):
                return False
            if hour >= 16:
                return False
            return True
        else:
            # ETH: Sunday 18:00 to Friday 17:00
            # Daily maintenance: 17:00 - 18:00
            if weekday == 5:  # Saturday
                return False
            if weekday == 6:  # Sunday
                return hour >= 18
            if weekday == 4:  # Friday
                return hour < 17
            # Mon-Thu: All hours except 17:00-18:00
            if hour == 17:
                return False
            return True


class MultiTimeframeFeed:
    """
    Manages multi-timeframe data synchronization.
    
    Coordinates 10m primary feed with 1H EMA calculations
    while maintaining lookahead protection.
    """
    
    def __init__(
        self,
        primary_feed: RealtimeFeed,
        ema_length: int = 200,
        bars_per_hour: int = 6
    ):
        """
        Initialize multi-timeframe handler.
        
        Parameters:
        -----------
        primary_feed : RealtimeFeed
            The primary feed (e.g. 10m bars)
        ema_length : int
            EMA period for 1H timeframe
        bars_per_hour : int
            Number of primary bars per hour (10m=6, 15m=4, 5m=12, 30m=2)
        """
        self.primary_feed = primary_feed
        self.ema_length = ema_length
        self.bars_per_hour = bars_per_hour
        
        # 1H bar aggregation
        self._1h_bars: List[Dict] = []
        self._current_1h_bar: Optional[Dict] = None
        self._last_ema_1h: float = np.nan
        self._last_close_1h: float = np.nan
        
    def aggregate_1h_from_10m(self, df_10m: pd.DataFrame) -> pd.DataFrame:
        """
        Build 1H bars from 10m data.
        
        Parameters:
        -----------
        df_10m : pd.DataFrame
            10-minute OHLCV data
        
        Returns:
        --------
        pd.DataFrame
            1H OHLCV data
        """
        return df_10m.resample('1h').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
    
    def get_confirmed_1h_ema(self, df_10m: pd.DataFrame) -> Tuple[float, float]:
        """
        Get the current 1H EMA and close — matching Pine Script behavior.
        
        Pine Script Reference (line 31):
            request.security(..., lookahead = barmerge.lookahead_off)
        
        Uses ALL available 10m data (including forming bar) to aggregate
        into 1H bars, then calculates EMA on the full 1H series.
        This matches TradingView's real-time behavior exactly.
        
        Parameters:
        -----------
        df_10m : pd.DataFrame
            Current 10-minute data
        
        Returns:
        --------
        Tuple[float, float]
            (ema_1h, close_1h) from the latest 1H bar
        """
        from indicators.ema import calculate_ema
        
        if len(df_10m) < self.bars_per_hour:  # Need at least 1 hour of data
            return np.nan, np.nan
        
        # Aggregate ALL 10m bars into 1H bars (including forming bar)
        df_1h = self.aggregate_1h_from_10m(df_10m)
        
        if len(df_1h) < 2:
            return np.nan, np.nan
        
        # Calculate EMA on full 1H series
        ema = calculate_ema(df_1h['close'], self.ema_length)
        
        # Return the latest 1H bar's EMA and close
        return ema.iloc[-1], df_1h['close'].iloc[-1]


# Helper to create feed
def create_realtime_feed(
    ib_client,
    contract,
    bar_size: str = "10 mins",
    timezone: str = "US/Eastern"
) -> Tuple[RealtimeFeed, MultiTimeframeFeed]:
    """
    Factory function to create real-time feeds.
    
    Parameters:
    -----------
    ib_client : IB
        Connected IBKR client
    contract : Contract
        Futures contract
    bar_size : str
        Primary bar size
    timezone : str
        Display timezone
    
    Returns:
    --------
    Tuple[RealtimeFeed, MultiTimeframeFeed]
        Primary and multi-TF feed handlers
    """
    primary = RealtimeFeed(ib_client, contract, bar_size, timezone=timezone)
    mtf = MultiTimeframeFeed(primary)
    
    return primary, mtf
