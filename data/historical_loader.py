"""
=============================================================================
HISTORICAL DATA LOADER
=============================================================================
Handles fetching historical bar data from IBKR and local CSV cache.
Ensures proper alignment with TradingView timeframes.

Key Features:
- IBKR Historical Data API integration via ib-insync
- CSV caching for backtesting
- Multi-timeframe support (10m primary, 1H for EMA)
- Proper bar alignment and timezone handling
=============================================================================
"""

import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import logging
import pytz

logger = logging.getLogger(__name__)


class HistoricalDataLoader:
    """
    Loads historical OHLCV data for backtesting and indicator calculation.
    
    Supports:
    - IBKR Historical Data API
    - Local CSV cache
    - Multi-timeframe aggregation
    """
    
    def __init__(
        self,
        ib_client=None,
        cache_dir: str = "./data/cache",
        timezone: str = "US/Eastern"
    ):
        """
        Initialize the data loader.
        
        Parameters:
        -----------
        ib_client : IB
            Connected ib-insync IB client (optional for CSV-only mode)
        cache_dir : str
            Directory for CSV cache storage
        timezone : str
            Timezone for bar alignment (default: US/Eastern for CME)
        """
        self.ib = ib_client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timezone = pytz.timezone(timezone)
        
    async def fetch_ibkr_bars(
        self,
        contract,
        end_datetime: datetime = None,
        duration: str = "30 D",
        bar_size: str = "10 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False
    ) -> pd.DataFrame:
        """
        Fetch historical bars from IBKR.
        
        Parameters:
        -----------
        contract : Contract
            IBKR Contract object (MNQ futures)
        end_datetime : datetime or None
            End date for historical data request
            For ContFuture, use None or empty string to get latest data
        duration : str
            Duration string (e.g., "30 D", "1 Y")
        bar_size : str
            Bar size (e.g., "10 mins", "1 hour")
        what_to_show : str
            Data type: TRADES, MIDPOINT, BID, ASK
        use_rth : bool
            If True, only return regular trading hours
        
        Returns:
        --------
        pd.DataFrame
            OHLCV data with datetime index
        """
        if self.ib is None:
            raise RuntimeError("IBKR client not connected")
        
        # Check if this is a ContFuture (continuous futures)
        is_cont_future = hasattr(contract, 'secType') and contract.secType == 'CONTFUT'
        
        # For ContFuture, IBKR requires empty endDateTime
        if is_cont_future:
            end_dt_param = ''
            logger.info(f"Fetching {bar_size} bars for {contract.symbol} (ContFuture), "
                       f"duration={duration}, end=NOW (latest)")
        else:
            end_dt_param = end_datetime if end_datetime else ''
            logger.info(f"Fetching {bar_size} bars for {contract.symbol}, "
                       f"duration={duration}, end={end_datetime}")
        
        # Request historical data
        bars = await self.ib.reqHistoricalDataAsync(
            contract=contract,
            endDateTime=end_dt_param,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=2  # UTC format
        )
        
        if not bars:
            logger.warning("No bars returned from IBKR")
            return pd.DataFrame()
        
        # Convert to DataFrame
        df = self._bars_to_dataframe(bars)
        logger.info(f"Received {len(df)} bars from IBKR")
        
        return df
    
    def _bars_to_dataframe(self, bars) -> pd.DataFrame:
        """
        Convert IBKR BarData objects to pandas DataFrame.
        """
        from data.bar_index import bars_to_ohlcv_dataframe

        return bars_to_ohlcv_dataframe(bars, tz=self.timezone)
    
    def save_to_csv(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        start_date: datetime,
        end_date: datetime
    ) -> Path:
        """
        Save data to CSV cache.
        
        Parameters:
        -----------
        df : pd.DataFrame
            OHLCV data to save
        symbol : str
            Contract symbol (e.g., "MNQ")
        timeframe : str
            Bar size (e.g., "10m", "1H")
        start_date, end_date : datetime
            Date range
        
        Returns:
        --------
        Path
            Path to saved CSV file
        """
        filename = f"{symbol}_{timeframe}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv"
        filepath = self.cache_dir / filename
        
        df.to_csv(filepath)
        logger.info(f"Saved {len(df)} bars to {filepath}")
        
        return filepath
    
    def load_from_csv(self, filepath: str) -> pd.DataFrame:
        """
        Load data from CSV cache.
        """
        from data.bar_index import ensure_datetime_index

        df = pd.read_csv(filepath, index_col='datetime')
        df = ensure_datetime_index(df, tz=self.timezone, datetime_col=None)
        logger.info(f"Loaded {len(df)} bars from {filepath}")
        return df
    
    def resample_to_higher_tf(
        self,
        df_10m: pd.DataFrame,
        target_tf: str = "1H"
    ) -> pd.DataFrame:
        """
        Resample 10-minute bars to higher timeframe.
        
        Used for calculating 1H EMA from 10m data.
        
        Pine Script Reference (line 31):
            request.security(syminfo.tickerid, "60", ...)
        
        Parameters:
        -----------
        df_10m : pd.DataFrame
            10-minute OHLCV data
        target_tf : str
            Target timeframe ("1H", "4H", "1D", etc.)
        
        Returns:
        --------
        pd.DataFrame
            Resampled OHLCV data
        """
        # Map common timeframe strings to pandas offset
        tf_map = {
            "1H": "1h",
            "1h": "1h",
            "60": "1h",
            "4H": "4h",
            "4h": "4h",
            "1D": "1D",
            "1d": "1D"
        }
        
        offset = tf_map.get(target_tf, target_tf)
        
        # Resample using standard OHLCV aggregation
        resampled = df_10m.resample(offset).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna()
        
        logger.info(f"Resampled {len(df_10m)} 10m bars to {len(resampled)} {target_tf} bars")
        
        return resampled
    
    def get_1h_values_for_10m_bars(
        self,
        df_10m: pd.DataFrame,
        df_1h: pd.DataFrame
    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Map 1H EMA values to 10m bars (forward-fill without lookahead).
        
        Pine Script Reference (line 31):
            request.security(..., lookahead = barmerge.lookahead_off)
        
        This ensures each 10m bar only sees the COMPLETED 1H bar's values,
        not the current in-progress 1H bar.
        
        Parameters:
        -----------
        df_10m : pd.DataFrame
            10-minute bars
        df_1h : pd.DataFrame
            1-hour bars with EMA calculated
        
        Returns:
        --------
        Tuple[pd.Series, pd.Series, pd.Series, pd.Series]
            (ema_1h, close_1h, high_1h, low_1h) mapped to 10m bar timestamps
        """
        # Get 1H bar close times
        h1_close_times = df_1h.index
        
        # For each 10m bar, find the last COMPLETED 1H bar
        ema_for_10m = []
        close_for_10m = []
        high_for_10m = []
        low_for_10m = []
        
        for ts in df_10m.index:
            # Find all 1H bars that closed BEFORE this 10m bar
            # This is the lookahead_off behavior
            completed_1h = h1_close_times[h1_close_times < ts]
            
            if len(completed_1h) > 0:
                last_completed = completed_1h[-1]
                ema_for_10m.append(df_1h.loc[last_completed, 'ema'])
                close_for_10m.append(df_1h.loc[last_completed, 'close'])
                high_for_10m.append(df_1h.loc[last_completed, 'high'])
                low_for_10m.append(df_1h.loc[last_completed, 'low'])
            else:
                ema_for_10m.append(np.nan)
                close_for_10m.append(np.nan)
                high_for_10m.append(np.nan)
                low_for_10m.append(np.nan)
        
        return (
            pd.Series(ema_for_10m, index=df_10m.index, name='ema_1h'),
            pd.Series(close_for_10m, index=df_10m.index, name='close_1h'),
            pd.Series(high_for_10m, index=df_10m.index, name='high_1h'),
            pd.Series(low_for_10m, index=df_10m.index, name='low_1h')
        )
    
    def detect_new_1h_candle(self, df_10m: pd.DataFrame) -> pd.Series:
        """
        Detect when a new 1H candle starts on the 10m chart.
        
        Pine Script Reference (line 34):
            isNew1HCandle = ta.change(time("60")) != 0
        
        Parameters:
        -----------
        df_10m : pd.DataFrame
            10-minute bars with datetime index
        
        Returns:
        --------
        pd.Series
            Boolean series, True when 10m bar starts a new 1H candle
        """
        # Get the hour of each bar
        hours = pd.Series(df_10m.index.floor('h'), index=df_10m.index)
        
        # A new 1H candle starts when the floored hour changes
        is_new_1h = hours != hours.shift(1)
        
        return pd.Series(is_new_1h, index=df_10m.index, name='is_new_1h_candle')
    
    async def prepare_strategy_data(
        self,
        contract,
        start_date: datetime,
        end_date: datetime,
        strategy_cfg: Optional[Dict[str, Any]] = None,
        ema_length: int = 200,
        di_length: int = 14,
        adx_smoothing: int = 14,
        adx_threshold: float = 20.0,
        supertrend_atr: int = 10,
        supertrend_mult: float = 3.0,
        # Legacy parameters (kept for backwards compatibility, no longer used)
        adx_consecutive: int = 5,
        volume_ma_period: int = 20,
        ema_overlap_margin_pct: float = 0.1,
        zone_check_1h_margin_pct: float = 0.1,
        zone_check_10m_margin_pct: float = 0.15,
        from_cache: bool = True,
        bar_size: str = "10 mins"
    ) -> Dict[str, Any]:
        """
        Prepare all data needed for strategy execution.
        
        Fetches 10m bars, creates 1H bars, calculates EMA, 
        and aligns everything properly.
        
        Parameters:
        -----------
        contract : Contract
            IBKR Contract object
        start_date : datetime
            Start of backtest period
        end_date : datetime
            End of backtest period
        ema_length : int
            EMA period (200)
        di_length : int
            DI Length for +DI/-DI calculation (default 14)
        adx_smoothing : int
            ADX smoothing period (default 14)
        adx_threshold : float
            ADX threshold for entry (default 20)
        supertrend_atr : int
            Supertrend ATR length (default 10, match strategy.yaml)
        supertrend_mult : float
            Supertrend multiplier (default 3.0, match strategy.yaml)
        from_cache : bool
            If True, try to load from cache first
        
        Returns:
        --------
        Dict containing:
            - df_10m: 10-minute bars with all indicators
            - df_1h: 1-hour bars with EMA
        """
        from indicators.ema import calculate_ema, ema_trend_filter
        from data.strategy_indicators import attach_long_short_indicators
        from utils.strategy_side_config import resolve_side_configs
        
        # Try loading from cache
        cache_file = self.cache_dir / f"{contract.symbol}_10m_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.csv"
        
        if from_cache and cache_file.exists():
            df_10m = self.load_from_csv(str(cache_file))
        else:
            # Calculate duration
            days = (end_date - start_date).days
            # Add extra days for EMA warmup
            # EMA 200 requires 200 hours of 1H data. To ensure accuracy, we load
            # significantly more data to account for weekends, holidays, and ensure
            # the EMA is fully converged before the backtest period starts.
            # Minimum: 200 hours / 24 = ~8.3 days, but add generous buffer for:
            # - Weekends (2 days per week)
            # - Holidays
            # - Ensuring EMA is fully converged
            # Using 30 days ensures we have plenty of data for accurate EMA calculation
            warmup_days = max(int(np.ceil(ema_length / 24)) + 5, 30)
            total_days = days + warmup_days
            
            # Check if this is a ContFuture (continuous futures)
            is_cont_future = hasattr(contract, 'secType') and contract.secType == 'CONTFUT'
            
            if is_cont_future:
                # ContFuture: IBKR only supports empty endDateTime
                # Also: 10-min bars have a limit of ~60 days before timeout
                MAX_CONTFUT_DAYS = 60  # Avoid IBKR timeout
                
                if total_days > MAX_CONTFUT_DAYS:
                    logger.warning(f"ContFuture: {total_days} days requested, but limit is {MAX_CONTFUT_DAYS} days")
                    logger.warning("For longer date ranges, the bot will fall back to regular Future contract")
                    logger.warning("This may result in limited historical data for older dates")
                    # Fall through to regular Future handling
                    is_cont_future = False
                else:
                    logger.info(f"ContFuture detected - fetching {total_days} days in single request...")
                    
                    df_10m = await self.fetch_ibkr_bars(
                        contract=contract,
                        end_datetime=None,  # Empty for ContFuture
                        duration=f"{total_days} D",
                        bar_size=bar_size
                    )
                    
                    # Don't filter yet - need warmup data for EMA calculation
                    if len(df_10m) > 0:
                        logger.info(f"Fetched {len(df_10m)} bars (includes {warmup_days} days warmup for EMA)")
            
            if not is_cont_future:
                # Regular Future: use chunked requests with specific end dates
                MAX_DAYS_PER_REQUEST = 14  # 2 weeks per chunk
                
                if total_days <= MAX_DAYS_PER_REQUEST:
                    # Single request
                    df_10m = await self.fetch_ibkr_bars(
                        contract=contract,
                        end_datetime=end_date,
                        duration=f"{total_days} D",
                        bar_size=bar_size
                    )
                else:
                    # Fetch in chunks
                    logger.info(f"Large date range ({total_days} days) - fetching in chunks...")
                    all_dfs = []
                    
                    chunk_end = end_date
                    remaining_days = total_days
                    
                    while remaining_days > 0:
                        chunk_days = min(remaining_days, MAX_DAYS_PER_REQUEST)
                        
                        logger.info(f"  Fetching {chunk_days} days ending {chunk_end.strftime('%Y-%m-%d')}...")
                        
                        chunk_df = await self.fetch_ibkr_bars(
                            contract=contract,
                            end_datetime=chunk_end,
                            duration=f"{chunk_days} D",
                            bar_size=bar_size
                        )
                        
                        if len(chunk_df) > 0:
                            all_dfs.append(chunk_df)
                        
                        # Move to next chunk
                        chunk_end = chunk_end - timedelta(days=chunk_days)
                        remaining_days -= chunk_days
                        
                        # Small delay to avoid IBKR pacing limits
                        await asyncio.sleep(1)
                    
                    if all_dfs:
                        df_10m = pd.concat(all_dfs)
                        df_10m = df_10m[~df_10m.index.duplicated(keep='first')]
                        df_10m = df_10m.sort_index()
                        logger.info(f"Combined {len(all_dfs)} chunks: {len(df_10m)} total bars")
                    else:
                        df_10m = pd.DataFrame()
            
            # Check if we got data
            if len(df_10m) == 0:
                raise ValueError("No data received from IBKR. Please check:\n"
                                "  1. TWS/Gateway is running and connected\n"
                                "  2. You have market data subscription for MNQ\n"
                                "  3. The date range is valid (not too far in the past)")
            
            # Store full data with warmup for EMA calculation
            df_10m_full = df_10m.copy()
            
            # Cache the filtered data (for backtest period only)
            df_10m_filtered = df_10m[df_10m.index >= start_date].copy()
            df_10m_filtered = df_10m_filtered[df_10m_filtered.index <= end_date]
            self.save_to_csv(df_10m_filtered, contract.symbol, "10m", start_date, end_date)
        
        # Check data again after loading
        if len(df_10m) == 0:
            raise ValueError("No data available for the specified date range")
        
        # Create 1H bars from 10m data (use FULL data with warmup for accurate EMA)
        df_1h = self.resample_to_higher_tf(df_10m, "1H")
        
        # Calculate 1H EMA (using full data with warmup ensures accurate EMA values)
        df_1h_indicators = ema_trend_filter(df_1h['close'], ema_length)
        df_1h = pd.concat([df_1h, df_1h_indicators], axis=1)
        
        # Now filter df_10m to backtest date range (after EMA calculation)
        if 'df_10m_full' in locals():
            # We fetched fresh data - filter it now
            original_length = len(df_10m)
            df_10m = df_10m[df_10m.index >= start_date]
            df_10m = df_10m[df_10m.index <= end_date]
            dropped = original_length - len(df_10m)
            if dropped > 0:
                logger.info(f"Filtered to backtest date range: {len(df_10m)} bars (dropped {dropped} warmup bars)")
        else:
            # Loaded from cache - already filtered, but EMA might be inaccurate
            logger.warning("Loaded from cache - EMA may be inaccurate if cache doesn't include warmup data")
        
        # Map 1H values to 10m bars (lookahead_off)
        ema_1h, close_1h, high_1h, low_1h = self.get_1h_values_for_10m_bars(df_10m, df_1h)
        
        # Detect new 1H candle starts
        is_new_1h = self.detect_new_1h_candle(df_10m)
        
        # Combine EMA / 1H mapping first (Supertrend + ADX added below)
        df_10m = pd.concat([
            df_10m,
            ema_1h,
            close_1h,
            high_1h,
            low_1h,
            is_new_1h
        ], axis=1)
        
        # Calculate EMA bull/bear conditions mapped to 10m
        df_10m['ema_bull'] = df_10m['close_1h'] > df_10m['ema_1h']
        df_10m['ema_bear'] = df_10m['close_1h'] < df_10m['ema_1h']
        
        # Previous 1H values for crossover detection
        prev_close_1h = df_10m['close_1h'].shift(1)
        prev_ema_1h = df_10m['ema_1h'].shift(1)
        
        # ── EMA Cross Detection ────────────────────────────────────────────────────
        # Build close_1h_cross / ema_1h_cross by shifting df_1h index forward +1H
        # so the 8am bar is only visible from 9am.
        # This avoids false cross triggers at every hour boundary.
        # close_1h_cross is used ONLY for ema_bull_cross / ema_bear_cross
        # detection and as the entry price for EMA cross signals.
        df_1h_cross_avail = df_1h.copy()
        df_1h_cross_avail.index = df_1h.index + pd.Timedelta('1h')
        df_10m['close_1h_cross'] = df_1h_cross_avail['close'].reindex(df_10m.index, method='ffill')
        df_10m['ema_1h_cross'] = df_1h_cross_avail['ema'].reindex(df_10m.index, method='ffill')

        prev_close_1h_cross = df_10m['close_1h_cross'].shift(1)
        prev_ema_1h_cross = df_10m['ema_1h_cross'].shift(1)
        # Cross fires exactly once: at the first 10m bar after the 1H bar closes
        # (i.e. at 9:00am for the 8am-9am 1H bar). prev values hold the previous
        # 1H bar so the real direction change is detected without any hour-boundary
        # false triggers.
        df_10m['ema_bull_cross'] = (
            (df_10m['close_1h_cross'] > df_10m['ema_1h_cross']) &
            (prev_close_1h_cross <= prev_ema_1h_cross)
        )
        df_10m['ema_bear_cross'] = (
            (df_10m['close_1h_cross'] < df_10m['ema_1h_cross']) &
            (prev_close_1h_cross >= prev_ema_1h_cross)
        )
        
        if strategy_cfg is not None:
            sides = resolve_side_configs(strategy_cfg)
            df_10m = attach_long_short_indicators(
                df_10m,
                sides["long_supertrend_entry"],
                sides["short_supertrend_entry"],
                sides["long_adx"],
                sides["short_adx"],
                long_supertrend_exit=sides["long_supertrend_exit"],
                short_supertrend_exit=sides["short_supertrend_exit"],
            )
            lst, sst = sides["long_supertrend_entry"], sides["short_supertrend_entry"]
            la, sa = sides["long_adx"], sides["short_adx"]
            logger.info(
                f"Long ST ATR={lst.get('atr_length')} mult={lst.get('multiplier')}, "
                f"Short ST ATR={sst.get('atr_length')} mult={sst.get('multiplier')}"
            )
            logger.info(
                f"ADX long DI={la.get('di_length')} smooth={la.get('adx_smoothing')} "
                f"threshold={la.get('threshold')}; "
                f"short DI={sa.get('di_length')} smooth={sa.get('adx_smoothing')} "
                f"threshold={sa.get('threshold')}"
            )
        else:
            legacy_st = {"atr_length": supertrend_atr, "multiplier": supertrend_mult}
            legacy_adx = {
                "di_length": di_length,
                "adx_smoothing": adx_smoothing,
                "threshold": adx_threshold,
                "use_adx": True,
                "consecutive_candles": 5,
            }
            df_10m = attach_long_short_indicators(
                df_10m, legacy_st, legacy_st, legacy_adx, legacy_adx
            )
            logger.info(
                f"ADX(DI={di_length}, Smooth={adx_smoothing}) "
                f"long/short threshold={adx_threshold}"
            )

        # Volume SMA on primary bars (independent per resampled timeframe)
        if 'volume' in df_10m.columns:
            ma_period = max(1, int(volume_ma_period))
            df_10m['volume_ma'] = df_10m['volume'].rolling(
                window=ma_period, min_periods=ma_period
            ).mean()
        
        # Trim to requested date range
        df_10m = df_10m[df_10m.index >= start_date]
        df_10m = df_10m[df_10m.index <= end_date]
        
        logger.info(f"Prepared {len(df_10m)} bars for strategy from {df_10m.index[0]} to {df_10m.index[-1]}")
        
        return {
            'df_10m': df_10m,
            'df_1h': df_1h
        }
