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
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any, List, Tuple
import logging
import pytz
from collections import deque

logger = logging.getLogger(__name__)


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
        
        # 1H bar tracking for multi-timeframe
        self._1h_bars: deque = deque(maxlen=300)
        self._current_1h_start: Optional[datetime] = None
        
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

        # Store initial bars
        self._bars = list(self._subscription)
        self._build_dataframe()

        self._is_running = True
        logger.info(f"Real-time feed started with {len(self._bars)} initial bars")
        
    async def stop(self) -> None:
        """Stop the real-time feed and clean up."""
        self._is_running = False
        
        if self._subscription:
            self.ib.cancelHistoricalData(self._subscription)
            self._subscription = None
            
        logger.info("Real-time feed stopped")
    
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
        
        current_bar = bars[-1]
        
        # Check if new bar added (previous bar closed)
        if has_new_bar:
            # A new bar means the previous bar is now CONFIRMED/CLOSED
            # This is when we should execute signals
            self._bars = list(bars)
            self._build_dataframe()
            
            # Notify bar close callbacks
            closed_bar = bars[-2] if len(bars) >= 2 else None
            if closed_bar:
                self._emit_bar_close(closed_bar)
            
            # Check for 1H bar close
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
        
        data = []
        for bar in self._bars:
            data.append({
                'datetime': bar.date,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'average': bar.average,
                'bar_count': bar.barCount
            })
        
        df = pd.DataFrame(data)
        
        if isinstance(df['datetime'].iloc[0], str):
            df['datetime'] = pd.to_datetime(df['datetime'])
        
        df.set_index('datetime', inplace=True)
        df.sort_index(inplace=True)
        
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df.index = df.index.tz_convert(self.timezone)
        
        self._df = df
    
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
