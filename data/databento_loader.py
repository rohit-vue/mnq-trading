# =============================================================================
# DATABENTO DATA LOADER
# =============================================================================
# Loads historical data from Databento CSV files
# Converts 1-minute OHLCV to 10-minute timeframe
# Supports backtesting from local CSV files
# =============================================================================

import os
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List
import logging

logger = logging.getLogger(__name__)

# Default Databento data directory
# DATABENTO_DIR = "GLBX-20260101-FS6BSKCTYP"
DATABENTO_DIR = "GLBX-20260228-53VA3TKQXT"


def get_available_date_range(data_dir: str = DATABENTO_DIR) -> Tuple[datetime, datetime]:
    """
    Scan Databento CSV files and return available date range.
    
    Returns:
    --------
    Tuple[datetime, datetime]
        (earliest_date, latest_date)
    """
    csv_files = sorted(Path(data_dir).glob("glbx-mdp3-*.ohlcv-1m.csv"))
    
    if not csv_files:
        raise FileNotFoundError(f"No Databento CSV files found in {data_dir}")
    
    # Parse dates from filenames
    # Format: glbx-mdp3-20190501-20190531.ohlcv-1m.csv
    dates = []
    for f in csv_files:
        name = f.stem  # glbx-mdp3-20190501-20190531.ohlcv-1m
        parts = name.split('-')
        if len(parts) >= 4:
            start_str = parts[2]  # 20190501
            end_str = parts[3].split('.')[0]  # 20190531
            dates.append(datetime.strptime(start_str, '%Y%m%d'))
            dates.append(datetime.strptime(end_str, '%Y%m%d'))
    
    return min(dates), max(dates)


def get_files_for_date_range(
    start_date: datetime,
    end_date: datetime,
    data_dir: str = DATABENTO_DIR
) -> List[Path]:
    """
    Get list of CSV files that cover the requested date range.
    
    Parameters:
    -----------
    start_date : datetime
        Start of backtest period
    end_date : datetime
        End of backtest period
    data_dir : str
        Path to Databento data directory
    
    Returns:
    --------
    List[Path]
        List of CSV file paths
    """
    csv_files = sorted(Path(data_dir).glob("glbx-mdp3-*.ohlcv-1m.csv"))
    matching_files = []
    
    for f in csv_files:
        name = f.stem
        parts = name.split('-')
        if len(parts) >= 4:
            file_start_str = parts[2]
            file_end_str = parts[3].split('.')[0]
            file_start = datetime.strptime(file_start_str, '%Y%m%d')
            file_end = datetime.strptime(file_end_str, '%Y%m%d')
            
            # Check if file overlaps with requested range
            if file_end >= start_date and file_start <= end_date:
                matching_files.append(f)
    
    return matching_files


def load_databento_data(
    start_date: datetime,
    end_date: datetime,
    symbol_filter: str = "MNQ",
    data_dir: str = DATABENTO_DIR,
    contract_root: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load Databento CSV files and combine into single DataFrame.
    
    Parameters:
    -----------
    start_date : datetime
        Start of backtest period
    end_date : datetime
        End of backtest period
    symbol_filter : str
        Symbol prefix to filter (e.g., "MNQ" for all MNQ contracts)
    data_dir : str
        Path to Databento data directory
    
    Returns:
    --------
    pd.DataFrame
        Combined 1-minute OHLCV data
    """
    root = (contract_root or symbol_filter or "MNQ").upper()
    files = get_files_for_date_range(start_date, end_date, data_dir)
    
    if not files:
        raise FileNotFoundError(
            f"No data files found for date range {start_date.date()} to {end_date.date()}"
        )
    
    logger.info(f"Loading {len(files)} Databento files for {start_date.date()} to {end_date.date()}")
    print(f"\n[INFO] Loading {len(files)} Databento CSV files...")
    
    all_data = []
    total_spreads_removed = 0
    
    for file_path in files:
        logger.info(f"  Loading {file_path.name}...")
        print(f"  -> {file_path.name}")
        
        df = pd.read_csv(file_path)
        
        # Filter by symbol root (e.g., MNQH5 / MGCM6)
        if symbol_filter:
            df = df[df['symbol'].str.startswith(symbol_filter)]
        
        # === FILTER OUT SPREADS ===
        # Remove spread contracts (e.g., "MNQH6-MNQM6") - these are not individual contracts
        # TradingView and IBKR continuous contracts don't include spreads
        initial_count = len(df)
        df = df[~df['symbol'].str.contains('-', na=False)]
        spreads_removed = initial_count - len(df)
        total_spreads_removed += spreads_removed
        if spreads_removed > 0:
            logger.debug(f"  Removed {spreads_removed} spread rows from {file_path.name}")
        
        if len(df) > 0:
            all_data.append(df)
    
    if not all_data:
        raise ValueError(f"No data found for symbol filter '{symbol_filter}'")
    
    # Combine all files
    combined_df = pd.concat(all_data, ignore_index=True)
    
    # Report total spreads filtered (if any)
    if total_spreads_removed > 0:
        print(f"[INFO] Filtered out {total_spreads_removed:,} spread rows total (e.g., MNQH6-MNQM6)")
        logger.info(f"Filtered out {total_spreads_removed} spread rows total")
    
    # Parse timestamp - Databento data is in UTC
    combined_df['timestamp'] = pd.to_datetime(combined_df['ts_event'])
    
    # Convert UTC to US Eastern timezone (to match IBKR data)
    print("\n[INFO] Converting UTC timestamps to US Eastern timezone...")
    if combined_df['timestamp'].dt.tz is None:
        combined_df['timestamp'] = combined_df['timestamp'].dt.tz_localize('UTC')
    combined_df['timestamp'] = combined_df['timestamp'].dt.tz_convert('US/Eastern')
    # Remove timezone info for consistency with backtest engine
    combined_df['timestamp'] = combined_df['timestamp'].dt.tz_localize(None)
    
    # === FILTER OUT BAD PRICE DATA ===
    # Keep a broad, root-aware sanity range to catch corrupted rows.
    price_bounds = {
        "MNQ": (10_000, 50_000),
        "MGC": (500, 20_000),
    }
    min_price, max_price = price_bounds.get(root, (0, 10_000_000))
    initial_count = len(combined_df)
    combined_df = combined_df[
        (combined_df['close'] >= min_price) & 
        (combined_df['close'] <= max_price) &
        (combined_df['open'] >= min_price) &
        (combined_df['open'] <= max_price)
    ]
    filtered_count = initial_count - len(combined_df)
    if filtered_count > 0:
        print(f"[WARN] Filtered out {filtered_count:,} rows with invalid price data")
        logger.warning(f"Filtered out {filtered_count} rows with invalid price data")
    
    # === CREATE STITCHED CONTINUOUS CONTRACT ===
    # Use same contract boundary generation as IBKR stitcher.
    print("\n[INFO] Creating stitched continuous contract (CME expiry boundaries)...")
    from data.contract_stitcher import get_contracts_for_date_range
    contracts_for_range = get_contracts_for_date_range(start_date, end_date, root=root)
    
    def get_front_month_contract(timestamp):
        """Get the front-month contract for a given timestamp.
        On dates where contracts overlap, the FIRST matching contract wins.
        This matches IBKR behavior where the old contract is preferred until its end date/time.
        Special handling for Dec 15, 2025: MNQZ5 ends at 21:58:59, MNQH6 starts at 21:59:00.
        """
        ts = pd.Timestamp(timestamp)
        
        for contract, start_ts, end_ts, _expiry in contracts_for_range:
            # Check if timestamp falls within this contract's range (inclusive)
            if start_ts <= ts <= end_ts:
                return contract
        
        return None
    
    # Add front_month column based on rollover schedule
    combined_df['front_month'] = combined_df['timestamp'].apply(get_front_month_contract)
    
    # Filter to only keep rows where symbol matches the front month contract
    # This ensures we use the same contract at each point in time as IBKR
    combined_df = combined_df[combined_df['symbol'] == combined_df['front_month']].copy()
    
    if len(combined_df) == 0:
        # Fallback: If exact match fails, try matching contract prefix
        print("[WARN] Exact contract match failed, trying prefix match...")
        combined_df = pd.concat(all_data, ignore_index=True)
        combined_df['timestamp'] = pd.to_datetime(combined_df['ts_event'])
        if combined_df['timestamp'].dt.tz is None:
            combined_df['timestamp'] = combined_df['timestamp'].dt.tz_localize('UTC')
        combined_df['timestamp'] = combined_df['timestamp'].dt.tz_convert('US/Eastern')
        combined_df['timestamp'] = combined_df['timestamp'].dt.tz_localize(None)
        combined_df = combined_df[
            (combined_df['close'] >= min_price) & 
            (combined_df['close'] <= max_price) &
            (combined_df['open'] >= min_price) &
            (combined_df['open'] <= max_price)
        ]
        combined_df['front_month'] = combined_df['timestamp'].apply(get_front_month_contract)
        # Match by prefix (MNQH5 matches MNQH5)
        combined_df = combined_df[combined_df.apply(
            lambda row: row['symbol'].startswith(row['front_month'][:4]) if row['front_month'] else False, 
            axis=1
        )].copy()
    
    # For any remaining duplicates at same timestamp, use volume-based selection
    # This matches TradingView behavior: highest volume contract wins
    # Sort by timestamp, then by volume (descending), then keep first
    duplicates_before = combined_df.duplicated(subset=['timestamp']).sum()
    combined_df = combined_df.sort_values(['timestamp', 'volume', 'symbol'], ascending=[True, False, True])
    combined_df = combined_df.drop_duplicates(subset=['timestamp'], keep='first')
    
    # Log if we had any duplicates that were resolved by volume
    if duplicates_before > 0:
        logger.info(f"Resolved {duplicates_before} timestamp duplicates using volume-based selection")
        print(f"[INFO] Resolved {duplicates_before} timestamp duplicates using volume-based selection")
    
    # Clean up helper column
    combined_df = combined_df.drop(columns=['front_month'], errors='ignore')
    
    combined_df.set_index('timestamp', inplace=True)
    combined_df.sort_index(inplace=True)
    
    # Filter to requested date range (use date boundaries)
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    combined_df = combined_df[(combined_df.index >= start_ts) & (combined_df.index <= end_ts)]
    
    # Select OHLCV columns
    combined_df = combined_df[['open', 'high', 'low', 'close', 'volume', 'symbol']]
    
    # Log contract distribution
    contract_counts = combined_df['symbol'].value_counts()
    print(f"[OK] Contract distribution:")
    for sym, count in contract_counts.head(5).items():
        print(f"     {sym}: {count:,} bars")
    
    logger.info(f"Loaded {len(combined_df)} 1-minute bars (stitched continuous)")
    print(f"[OK] Loaded {len(combined_df):,} 1-minute bars")
    
    return combined_df


def resample_1m_to_primary(df_1m: pd.DataFrame, resample_rule: str = "10min") -> pd.DataFrame:
    """
    Resample 1-minute data to primary timeframe (e.g. 10min, 15min).
    
    Parameters:
    -----------
    df_1m : pd.DataFrame
        1-minute OHLCV data
    resample_rule : str
        Pandas resample rule (e.g. '10min', '15min'). From strategy timeframes.primary.
    
    Returns:
    --------
    pd.DataFrame
        Primary-timeframe OHLCV data
    """
    logger.info(f"Resampling 1M to {resample_rule}...")
    print(f"\n[INFO] Resampling 1M -> {resample_rule} timeframe...")
    
    df_primary = df_1m.resample(resample_rule).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum',
        # Keep front-month contract label at bar close for rollover handling.
        'symbol': 'last',
    }).dropna()

    # Normalize contract label column name used by backtest engine.
    if 'symbol' in df_primary.columns:
        df_primary = df_primary.rename(columns={'symbol': 'contract_symbol'})
    
    logger.info(f"Resampled to {len(df_primary)} {resample_rule} bars")
    print(f"[OK] Created {len(df_primary):,} {resample_rule} bars")
    
    return df_primary


def resample_to_10min(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Resample 1m to 10m. Kept for backward compatibility; prefer resample_1m_to_primary(..., resample_rule)."""
    return resample_1m_to_primary(df_1m, resample_rule="10min")


def prepare_databento_for_backtest(
    start_date: datetime,
    end_date: datetime,
    symbol_filter: str = "MNQ",
    data_dir: str = DATABENTO_DIR,
    contract_root: Optional[str] = None,
    # Primary timeframe (from strategy timeframes.primary, e.g. "10min", "15min")
    primary_resample_rule: str = "10min",
    # Strategy parameters (from config)
    strategy_cfg: Optional[dict] = None,
    ema_length: int = 200,
    supertrend_atr: int = 10,
    supertrend_mult: float = 3.0,
    adx_di_length: int = 14,
    adx_smoothing: int = 14,
    adx_threshold: float = 20.0,
    volume_ma_period: int = 20,
    # Legacy parameters (kept for backwards compatibility, no longer used)
    adx_consecutive: int = 5,
    ema_overlap_margin_pct: float = 0.1,
    zone_check_1h_margin_pct: float = 0.1,
    zone_check_10m_margin_pct: float = 0.15
) -> pd.DataFrame:
    """
    Load and prepare Databento data for backtesting.
    
    This is the main function to call for backtest data preparation.
    It loads 1M data, resamples to 10M, and calculates all indicators.
    
    Parameters:
    -----------
    start_date : datetime
        Start of backtest period
    end_date : datetime
        End of backtest period
    symbol_filter : str
        Symbol prefix to filter
    data_dir : str
        Path to Databento data directory
    ema_length : int
        EMA period for 1H trend filter (default 200)
    supertrend_atr : int
        ATR length for Supertrend (default 10)
    supertrend_mult : float
        Multiplier for Supertrend (default 3.0)
    adx_di_length : int
        DI length for ADX calculation (default 14)
    adx_smoothing : int
        ADX smoothing period (default 14)
    adx_threshold : float
        Minimum ADX value for entry (default 20.0)
    adx_consecutive : int
        Consecutive candles above threshold (default 5)
    
    Returns:
    --------
    pd.DataFrame
        10-minute bars with all indicators calculated
    """
    from indicators.ema import ema_trend_filter
    from data.strategy_indicators import attach_long_short_indicators
    from utils.strategy_side_config import resolve_side_configs
    from datetime import timedelta
    
    # Calculate warmup period needed for EMA 200
    # EMA 200 requires 200 hours of 1H data. To ensure accuracy, we load
    # significantly more data to account for weekends, holidays, and ensure
    # the EMA is fully converged before the backtest period starts.
    # Minimum: 200 hours / 24 = ~8.3 days, but add generous buffer for:
    # - Weekends (2 days per week)
    # - Holidays
    # - Ensuring EMA is fully converged
    # Using 30 days ensures we have plenty of data for accurate EMA calculation
    warmup_days = max(int(np.ceil(ema_length / 24)) + 5, 30)
    data_start_date = start_date - timedelta(days=warmup_days)
    
    print(f"\n[INFO] EMA({ema_length}) warmup period: {warmup_days} days")
    print(f"[INFO] Loading data from {data_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"[INFO] (Backtest period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})")
    
    # Load 1M data WITH warmup period
    df_1m = load_databento_data(
        data_start_date,
        end_date,
        symbol_filter=symbol_filter,
        data_dir=data_dir,
        contract_root=contract_root,
    )
    
    # Resample to primary timeframe (e.g. 10m, 15m)
    df_10m = resample_1m_to_primary(df_1m, resample_rule=primary_resample_rule)
    
    # Create 1H bars for EMA
    print("\n[INFO] Creating 1H bars and calculating EMA...")
    df_1h = df_10m.resample('1h').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    
    # Calculate 1H EMA (using configured length)
    # This will have NaN for the first ~200 hours, which is correct
    df_1h_indicators = ema_trend_filter(df_1h['close'], ema_length)
    df_1h = pd.concat([df_1h, df_1h_indicators], axis=1)
    
    print(f"[OK] 1H EMA({ema_length}) calculated on {len(df_1h)} bars")
    print(f"[INFO] EMA warmup: First {ema_length} hours will have NaN values (expected)")
    
    # Map 1H EMA to 10M bars
    hour_index = df_10m.index.floor('1h')
    df_10m['ema_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'ema'] if x in df_1h.index else np.nan)
    df_10m['close_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'close'] if x in df_1h.index else np.nan)
    # Map 1H high and low for zone checking when ST flips occur
    df_10m['high_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'high'] if x in df_1h.index else np.nan)
    df_10m['low_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'low'] if x in df_1h.index else np.nan)
    
    # Detect new 1H candle
    hour_floor = pd.Series(df_10m.index.floor('1h'), index=df_10m.index)
    df_10m['is_new_1h_candle'] = (hour_floor != hour_floor.shift(1)).fillna(True)
    
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
        print("[OK] Long/short Supertrend + ADX calculated (strategy.yaml)")
    else:
        legacy_st = {"atr_length": supertrend_atr, "multiplier": supertrend_mult}
        legacy_adx = {
            "di_length": adx_di_length,
            "adx_smoothing": adx_smoothing,
            "threshold": adx_threshold,
            "use_adx": True,
            "consecutive_candles": 5,
        }
        print(f"[INFO] Calculating Supertrend (ATR={supertrend_atr}, Mult={supertrend_mult})...")
        df_10m = attach_long_short_indicators(
            df_10m, legacy_st, legacy_st, legacy_adx, legacy_adx
        )
        print("[OK] Supertrend calculated")
    
    # Add EMA conditions
    df_10m['ema_bull'] = df_10m['close_1h'] > df_10m['ema_1h']
    df_10m['ema_bear'] = df_10m['close_1h'] < df_10m['ema_1h']
    
    prev_close_1h = df_10m['close_1h'].shift(1)
    prev_ema_1h = df_10m['ema_1h'].shift(1)

    # ── EMA Cross Detection ────────────────────────────────────────────────────
    # Build close_1h_cross / ema_1h_cross by shifting the 1H index forward 1H
    # so the 8am 1H bar (8am-9am) is only visible from 9am onwards.
    # This avoids false cross triggers at every hour boundary.
    # close_1h_cross is used ONLY for ema_bull_cross / ema_bear_cross
    # detection and as the entry price for EMA cross signals.
    df_1h_cross_avail = df_1h.copy()
    df_1h_cross_avail.index = df_1h.index + pd.Timedelta('1h')
    df_10m['close_1h_cross'] = df_1h_cross_avail['close'].reindex(df_10m.index, method='ffill')
    df_10m['ema_1h_cross'] = df_1h_cross_avail['ema'].reindex(df_10m.index, method='ffill')

    prev_close_1h_cross = df_10m['close_1h_cross'].shift(1)
    prev_ema_1h_cross = df_10m['ema_1h_cross'].shift(1)
    # Cross fires exactly once: at the first 10m bar after the 1H bar truly closes.
    df_10m['ema_bull_cross'] = (
        (df_10m['close_1h_cross'] > df_10m['ema_1h_cross']) &
        (prev_close_1h_cross <= prev_ema_1h_cross)
    )
    df_10m['ema_bear_cross'] = (
        (df_10m['close_1h_cross'] < df_10m['ema_1h_cross']) &
        (prev_close_1h_cross >= prev_ema_1h_cross)
    )
    
    if strategy_cfg is None:
        print(f"[OK] ADX(DI={adx_di_length}, Smooth={adx_smoothing}) calculated")
    
    # Volume SMA on primary bars (independent per resampled timeframe)
    if 'volume' in df_10m.columns:
        ma_period = max(1, int(volume_ma_period))
        df_10m['volume_ma'] = df_10m['volume'].rolling(
            window=ma_period, min_periods=ma_period
        ).mean()
    
    # Filter to backtest date range (remove warmup period from final output)
    # Keep warmup data for EMA calculation, but don't include it in final results
    before_filter_len = len(df_10m)
    df_10m = df_10m[df_10m.index >= start_date]
    df_10m = df_10m[df_10m.index <= end_date]
    after_date_filter_len = len(df_10m)
    
    # Drop NaN rows (remaining EMA warmup period that might overlap with start_date)
    df_10m = df_10m.dropna(
        subset=[
            'ema_1h',
            'close_1h',
            'supertrend_long',
            'direction_long',
            'supertrend_short',
            'direction_short',
        ]
    )
    final_len = len(df_10m)
    warmup_dropped = before_filter_len - after_date_filter_len
    nan_dropped = after_date_filter_len - final_len
    
    print(f"\n[OK] Filtered to backtest date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"[OK] Warmup period removed: {warmup_dropped:,} bars")
    if nan_dropped > 0:
        print(f"[OK] NaN rows dropped (EMA warmup): {nan_dropped:,} bars")
    print(f"[OK] Final dataset: {final_len:,} bars")
    if final_len > 0:
        print(f"[OK] Date range: {df_10m.index[0]} to {df_10m.index[-1]}")
    else:
        print(f"[WARN] No valid bars remaining after filtering - check date range and data availability")
    
    return df_10m
