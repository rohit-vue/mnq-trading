"""
=============================================================================
MNQ SUPERTREND + EMA TRADING SYSTEM
=============================================================================
Interactive trading system for MNQ futures.
Run: python main.py

Modes:
1. Backtest - Test strategy on historical IBKR data
2. Paper Trade - Trade on IBKR paper account
3. Live Trade - Trade on IBKR live account (real money)
=============================================================================
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
import yaml
import pytz
import pandas as pd
import numpy as np

from utils.load_env import load_project_dotenv

load_project_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('trading.log', encoding='utf-8')
        # logging.StreamHandler()  # Also log to console
    ]
)
logger = logging.getLogger(__name__)

# Primary timeframe from strategy.yaml (e.g. 10m, 15m)
from timeframe_utils import (
    get_primary_bar_size,
    get_primary_resample_rule,
    get_primary_last_minute_of_hour,
    get_primary_bars_per_hour,
    get_primary_timeframe,
)

# Suppress verbose logs from strategy modules during backtest
logging.getLogger('strategy.signal_engine').setLevel(logging.INFO)  # INFO to see entry condition checks
logging.getLogger('strategy.state_manager').setLevel(logging.WARNING)
# Keep backtest.backtest_engine at INFO level to see flip and entry logs
logging.getLogger('backtest.backtest_engine').setLevel(logging.INFO)

# Suppress verbose ib_async logs (wrapper updates, portfolio updates, etc)
logging.getLogger('ib_async.wrapper').setLevel(logging.WARNING)
logging.getLogger('ib_async.client').setLevel(logging.WARNING)
logging.getLogger('ib_async.ib').setLevel(logging.WARNING)


def print_banner():
    """Print welcome banner."""
    print("\n" + "=" * 60)
    print("     MNQ SUPERTREND + EMA TRADING SYSTEM")
    print("=" * 60)
    print()


def load_config(config_dir: str = "./config") -> dict:
    """Load all configuration files."""
    config_path = Path(config_dir)
    config = {}
    
    config_files = ['strategy.yaml', 'mnq_contract.yaml', 'risk.yaml', 'ibkr.yaml']
    
    for filename in config_files:
        filepath = config_path / filename
        if filepath.exists():
            with open(filepath, 'r') as f:
                file_config = yaml.safe_load(f)
                config[filename.replace('.yaml', '')] = file_config
    
    return config


def get_menu_choice() -> str:
    """Display main menu and get user choice."""
    print("What would you like to do?\n")
    print("  [1] Backtest - Choose market (MNQ/MGC) and source (Databento/IBKR)")
    print("  [2] Paper Trade - Trade on IBKR paper account")
    print("  [3] Live Trade - Trade with REAL money")
    print("  [0] Exit")
    print()
    
    while True:
        choice = input("Enter your choice (0-3): ").strip()
        if choice in ['0', '1', '2', '3']:
            return choice
        print("Invalid choice. Please enter 0, 1, 2, or 3.")


def get_date_input(prompt: str, default: str = None) -> datetime:
    """Get date input from user."""
    tz = pytz.timezone('US/Eastern')
    
    while True:
        if default:
            user_input = input(f"{prompt} [{default}]: ").strip()
            if not user_input:
                user_input = default
        else:
            user_input = input(f"{prompt}: ").strip()
        
        try:
            date = datetime.strptime(user_input, '%Y-%m-%d')
            return date.replace(tzinfo=tz)
        except ValueError:
            print("Invalid date format. Please use YYYY-MM-DD (e.g., 2025-12-01)")


def get_backtest_dates() -> tuple:
    """Get backtest date range from user."""
    print("\n--- Backtest Date Range ---")
    print("Enter dates in format: YYYY-MM-DD\n")
    
    # Default: last 30 days
    default_end = datetime.now().strftime('%Y-%m-%d')
    default_start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    
    start_date = get_date_input("Start date (FROM)", default_start)
    end_date = get_date_input("End date (TO)", default_end)
    
    if start_date >= end_date:
        print("Error: Start date must be before end date!")
        return get_backtest_dates()
    
    return start_date, end_date


def get_contracts() -> int:
    """Get number of contracts from user."""
    while True:
        try:
            contracts = input("\nNumber of contracts to trade [1]: ").strip()
            if not contracts:
                return 1
            contracts = int(contracts)
            if contracts > 0:
                return contracts
            print("Must be at least 1 contract.")
        except ValueError:
            print("Please enter a valid number.")


def get_backtest_market_choice() -> str:
    """Select backtest market root."""
    print("\nSelect market for backtesting:\n")
    print("  [1] Nasdaq Micro E-mini (MNQ)")
    print("  [2] Micro Gold Futures (MGC)")
    print()
    while True:
        choice = input("Enter market choice (1-2): ").strip()
        if choice == "1":
            return "MNQ"
        if choice == "2":
            return "MGC"
        print("Invalid choice. Please enter 1 or 2.")


def get_backtest_source_choice() -> str:
    """Select data source for backtest."""
    print("\nSelect backtest data source:\n")
    print("  [1] Databento")
    print("  [2] IBKR")
    print()
    while True:
        choice = input("Enter source choice (1-2): ").strip()
        if choice in {"1", "2"}:
            return choice
        print("Invalid choice. Please enter 1 or 2.")


async def run_backtest_selection(config: dict) -> None:
    """Interactive market + source selector for backtesting."""
    market_root = get_backtest_market_choice()
    source_choice = get_backtest_source_choice()
    if source_choice == "1":
        await run_databento_backtest(config, market_root=market_root)
    else:
        await run_backtest(config, market_root=market_root)


async def run_databento_backtest(config: dict, market_root: str = "MNQ") -> None:
    """Run backtest using local Databento CSV files."""
    from data.databento_loader import (
        get_available_date_range, 
        prepare_databento_for_backtest,
        DATABENTO_DIR
    )
    from backtest import BacktestEngine, BacktestConfig
    from utils.strategy_side_config import signal_engine_kwargs
    
    market_root = (market_root or "MNQ").upper()
    market_name = "Nasdaq Micro E-mini (MNQ)" if market_root == "MNQ" else "Micro Gold Futures (MGC)"
    databento_dir = DATABENTO_DIR if market_root == "MNQ" else "monthly_splits"

    print("\n" + "=" * 60)
    print(f"      DATABENTO BACKTEST MODE - {market_name}")
    print("=" * 60)
    
    # Check if Databento data exists
    try:
        earliest, latest = get_available_date_range(databento_dir)
    except FileNotFoundError as e:
        print(f"\n[X] Error: {e}")
        print(f"[!] Please download Databento data to: {databento_dir}")
        return
    
    print(f"\n[INFO] Databento Data Available:")
    print(f"  From: {earliest.strftime('%Y-%m-%d')}")
    print(f"  To:   {latest.strftime('%Y-%m-%d')}")
    print(f"  Total: {(latest - earliest).days} days of data")
    
    # Get date range from user
    print("\n--- Backtest Date Range ---")
    print("Enter dates in format: YYYY-MM-DD\n")
    
    start_date = get_date_input(
        f"Start date (FROM) [{earliest.strftime('%Y-%m-%d')}]: ",
        default=earliest.strftime('%Y-%m-%d')
    )
    end_date = get_date_input(
        f"End date (TO) [{latest.strftime('%Y-%m-%d')}]: ",
        default=latest.strftime('%Y-%m-%d')
    )
    
    # Validate dates (make timezone-naive for comparison)
    start_date_naive = start_date.replace(tzinfo=None) if hasattr(start_date, 'tzinfo') and start_date.tzinfo else start_date
    end_date_naive = end_date.replace(tzinfo=None) if hasattr(end_date, 'tzinfo') and end_date.tzinfo else end_date
    
    if start_date_naive < earliest:
        print(f"[!] Adjusting start date to earliest available: {earliest.strftime('%Y-%m-%d')}")
        start_date = earliest
    else:
        start_date = start_date_naive
        
    if end_date_naive > latest:
        print(f"[!] Adjusting end date to latest available: {latest.strftime('%Y-%m-%d')}")
        end_date = latest
    else:
        end_date = end_date_naive
    
    contracts = get_contracts()
    
    print(f"\n[OK] Backtest period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"[OK] Days: {(end_date - start_date).days}")
    print(f"[OK] Contracts: {contracts}")
    print(f"[OK] Market: {market_name}")
    print(f"[OK] Data source: Databento CSV")
    
    # Get all config sections (matching IBKR backtest exactly)
    strategy_cfg = config.get('strategy', {})
    risk_cfg = config.get('risk', {})
    contract_cfg = config.get('mnq_contract', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    adx_cfg = strategy_cfg.get('adx', {})
    volume_ma_period = max(1, int(strategy_cfg.get('volume_ma_period', 20)))
    
    print("\n" + "-" * 60)
    print("Loading and processing Databento data...")
    print("-" * 60)
    
    try:
        # Load and prepare data with config parameters
        primary_resample = get_primary_resample_rule(strategy_cfg)
        df_10m = prepare_databento_for_backtest(
            start_date=start_date,
            end_date=end_date,
            symbol_filter=market_root,
            data_dir=databento_dir,
            contract_root=market_root,
            primary_resample_rule=primary_resample,
            strategy_cfg=strategy_cfg,
            ema_length=ema_cfg.get('length', 200),
            supertrend_atr=supertrend_cfg.get('atr_length', 10),
            supertrend_mult=supertrend_cfg.get('multiplier', 3.0),
            adx_di_length=adx_cfg.get('di_length', 14),
            adx_smoothing=adx_cfg.get('adx_smoothing', 14),
            adx_threshold=adx_cfg.get('threshold', 20.0),
            volume_ma_period=volume_ma_period,
            adx_consecutive=adx_cfg.get('consecutive_candles', 5),
            ema_overlap_margin_pct=ema_cfg.get('overlap_margin_pct', 0.1),
            zone_check_1h_margin_pct=ema_cfg.get('zone_check_1h_margin_pct', 0.1),
            zone_check_10m_margin_pct=ema_cfg.get('zone_check_10m_margin_pct', 0.15)
        )
        
        if len(df_10m) == 0:
            print("[X] Error: No data available for the requested period")
            return
        
        # Create backtest config
        # Note: Databento uses calibrated slippage (5 ticks) to account for
        # data source differences vs IBKR. This keeps results ~100-200 points
        # below IBKR as a conservative estimate.
        DATABENTO_SLIPPAGE_CALIBRATION = 5  # Calibrated for data source difference
        
        bt_config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            primary_timeframe=get_primary_timeframe(strategy_cfg),
            ema_length=ema_cfg.get('length', 200),
            tick_size=contract_cfg.get('contract', {}).get('tick_size', 0.25),
            tick_value=contract_cfg.get('contract', {}).get('tick_value', 0.50),
            multiplier=contract_cfg.get('contract', {}).get('multiplier', 2),
            commission_per_contract=risk_cfg.get('backtesting', {}).get('commission_per_contract', 0.62),
            slippage_ticks=DATABENTO_SLIPPAGE_CALIBRATION,  # Calibrated slippage for Databento
            contracts=contracts,
            initial_capital=100000.0,
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            independent_books=bool(strategy_cfg.get('execution', {}).get('independent_books', False)),
            **signal_engine_kwargs(strategy_cfg),
        )
        
        # Run backtest
        print("\n" + "-" * 60)
        print("Running backtest...")
        print("-" * 60)
        
        engine = BacktestEngine(bt_config)
        results = engine.run(df_10m)
        
        # Display results
        print("\n" + "=" * 60)
        print("           DATABENTO BACKTEST RESULTS")
        print("=" * 60)
        
        print(f"\nPeriod: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        print(f"Total Bars: {len(df_10m):,}")
        print(f"Data Source: Databento CSV (Stitched Continuous)\n")
        
        # Generate and print report
        from backtest.metrics import generate_report
        
        # Save results with full report format (same as IBKR)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        results_dir = Path("backtest/results")
        results_dir.mkdir(parents=True, exist_ok=True)
        
        results_file = results_dir / f"databento_backtest_{timestamp}.csv"
        
        # Generate report - console_only=True shows summary only in terminal
        # Full report with trades is saved to file
        summary = generate_report(results, output_path=str(results_file), console_only=True)
        print(summary)
        
        print(f"\n[OK] Results saved to: {results_file}")
        
    except Exception as e:
        logger.error(f"Databento backtest failed: {e}")
        print(f"\n[X] Error: {e}")
        import traceback
        traceback.print_exc()


async def run_backtest(config: dict, market_root: str = "MNQ") -> None:
    """Run backtest mode with interactive inputs."""
    from ib_async import IB, Future, ContFuture
    from data import HistoricalDataLoader
    from backtest import BacktestEngine, BacktestConfig
    from utils.strategy_side_config import signal_engine_kwargs
    
    market_root = (market_root or "MNQ").upper()
    if market_root == "MNQ":
        market_name = "Nasdaq Micro E-mini (MNQ)"
        ibkr_exchange = "CME"
        cont_symbol = "MNQ1! (Continuous)"
        stitched_symbol = "MNQ (Stitched Continuous)"
    else:
        market_name = "Micro Gold Futures (MGC)"
        ibkr_exchange = "COMEX"
        cont_symbol = "MGC1! (Continuous)"
        stitched_symbol = "MGC (Stitched Continuous)"

    print("\n" + "=" * 60)
    print(f"            BACKTEST MODE - {market_name}")
    print("=" * 60)
    
    # Get date range from user
    start_date, end_date = get_backtest_dates()
    contracts = get_contracts()
    
    print(f"\n[OK] Backtest period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"[OK] Contracts: {contracts}")
    print(f"[OK] Market: {market_name}")
    
    # Get configs
    strategy_cfg = config.get('strategy', {})
    risk_cfg = config.get('risk', {})
    contract_cfg = config.get('mnq_contract', {})
    ibkr_cfg = config.get('ibkr', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    adx_cfg = strategy_cfg.get('adx', {})
    volume_ma_period = max(1, int(strategy_cfg.get('volume_ma_period', 20)))
    
    # Connect to IBKR
    ib = IB()
    conn_cfg = ibkr_cfg.get('connection', {})
    port = conn_cfg.get('ports', {}).get('tws_paper', 7497)
    
    print(f"\nConnecting to IBKR on port {port}...")
    
    try:
        await ib.connectAsync('127.0.0.1', port, clientId=1)
        print("[OK] Connected to IBKR")
        
        # Check date range
        days_requested = (end_date - start_date).days
        MAX_CONTFUT_DAYS = 55  # ContFuture limit
        
        if days_requested <= MAX_CONTFUT_DAYS:
            # Short period: Use ContFuture (Continuous Futures)
            print(f"\n[INFO] Using Continuous Futures (ContFuture) - like TradingView {cont_symbol}!")
            
            cont_contract = ContFuture(symbol=market_root, exchange=ibkr_exchange, currency='USD')
            qualified = await ib.qualifyContractsAsync(cont_contract)
            
            if qualified:
                contract = qualified[0]
                contract_symbol = cont_symbol
                print(f"[OK] Contract: {contract_symbol}")
                
                # Fetch data normally
                print("\nFetching historical data from IBKR...")
                loader = HistoricalDataLoader(ib_client=ib, cache_dir="./data/cache")
                
                primary_bar_size = get_primary_bar_size(strategy_cfg)
                data = await loader.prepare_strategy_data(
                    contract=contract,
                    start_date=start_date,
                    end_date=end_date,
                    strategy_cfg=strategy_cfg,
                    ema_length=ema_cfg.get('length', 200),
                    ema_overlap_margin_pct=ema_cfg.get('overlap_margin_pct', 0.1),
                    zone_check_1h_margin_pct=ema_cfg.get('zone_check_1h_margin_pct', 0.1),
                    zone_check_10m_margin_pct=ema_cfg.get('zone_check_10m_margin_pct', 0.15),
                    supertrend_atr=supertrend_cfg.get('atr_length', 10),
                    supertrend_mult=supertrend_cfg.get('multiplier', 3.0),
                    volume_ma_period=volume_ma_period,
                    bar_size=primary_bar_size
                )
                df_10m = data['df_10m']
            else:
                print("[X] ContFuture failed, using stitched contracts instead...")
                days_requested = 999  # Force stitcher path
        
        if days_requested > MAX_CONTFUT_DAYS:
            # Long period: Use CONTRACT STITCHER
            # Fetches from multiple expired contracts for complete data
            print(f"\n[INFO] Date range is {days_requested} days")
            print("[INFO] Using CONTRACT STITCHER - fetching from multiple expired contracts...")
            if market_root == "MNQ":
                print("[INFO] This will fetch from MNQH5, MNQM5, MNQU5, MNQZ5, MNQH6, etc.")
            else:
                print("[INFO] This will fetch from MGCG6, MGCJ6, MGCM6, MGCQ6, MGCV6, MGCZ6, etc.")
            
            from data.contract_stitcher import fetch_stitched_data, get_contracts_for_date_range
            from indicators.ema import calculate_ema, ema_trend_filter
            from data.strategy_indicators import attach_long_short_indicators
            from utils.strategy_side_config import resolve_side_configs
            from datetime import timedelta
            import numpy as np
            
            # Calculate warmup period needed for EMA 200
            # EMA 200 requires 200 hours of 1H data. To ensure accuracy, we load
            # significantly more data to account for weekends, holidays, and ensure
            # the EMA is fully converged before the backtest period starts.
            ema_length = ema_cfg.get('length', 200)
            # Minimum: 200 hours / 24 = ~8.3 days, but add generous buffer for:
            # - Weekends (2 days per week)
            # - Holidays
            # - Ensuring EMA is fully converged
            # Using 30 days ensures we have plenty of data for accurate EMA calculation
            warmup_days = max(int(np.ceil(ema_length / 24)) + 5, 30)
            data_start_date = start_date - timedelta(days=warmup_days)
            
            print(f"\n[INFO] EMA({ema_length}) warmup period: {warmup_days} days")
            print(f"[INFO] Fetching data from {data_start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
            print(f"[INFO] (Backtest period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})")
            
            # Show which contracts will be used (with warmup)
            contracts_needed = get_contracts_for_date_range(data_start_date, end_date, root=market_root)
            print(f"\n[INFO] Will fetch from {len(contracts_needed)} contracts:")
            for symbol, fetch_start, fetch_end, expiry in contracts_needed:
                print(f"  → {symbol}: {fetch_start.strftime('%Y-%m-%d')} to {fetch_end.strftime('%Y-%m-%d')}")
            
            # Fetch stitched data WITH warmup period
            primary_bar_size = get_primary_bar_size(strategy_cfg)
            print("\nFetching stitched historical data from IBKR...")
            df_10m = await fetch_stitched_data(
                ib_client=ib,
                start_date=data_start_date,  # Include warmup
                end_date=end_date,
                bar_size=primary_bar_size,
                root=market_root,
                exchange=ibkr_exchange,
            )
            
            if len(df_10m) == 0:
                print("[X] Error: No data received from contract stitcher")
                ib.disconnect()
                return
            
            contract_symbol = stitched_symbol
            
            # Calculate indicators on stitched data (with warmup)
            print("\nCalculating indicators on stitched data...")
            
            # Create 1H bars from full data (with warmup)
            df_1h = df_10m.resample('1h').agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum'
            }).dropna()
            
            # Calculate 1H EMA (using full data with warmup ensures accurate EMA values)
            df_1h_indicators = ema_trend_filter(df_1h['close'], ema_length)
            df_1h = pd.concat([df_1h, df_1h_indicators], axis=1)
            
            # Filter df_10m to backtest date range (after EMA calculation)
            original_length = len(df_10m)
            df_10m = df_10m[df_10m.index >= start_date]
            df_10m = df_10m[df_10m.index <= end_date]
            dropped = original_length - len(df_10m)
            if dropped > 0:
                print(f"[INFO] Filtered to backtest date range: {len(df_10m)} bars (dropped {dropped} warmup bars)")
            
            # Map 1H values to 10m bars (floor mapping: each 10m bar sees the
            # current hour's close — used for ema_bull/bear/confirmed which gate
            # ALL entry types; do NOT change this mapping).
            hour_index = df_10m.index.floor('1h')
            df_10m['ema_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'ema'] if x in df_1h.index else np.nan)
            df_10m['close_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'close'] if x in df_1h.index else np.nan)
            # Map 1H high and low for zone checking when ST flips occur
            df_10m['high_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'high'] if x in df_1h.index else np.nan)
            df_10m['low_1h'] = hour_index.map(lambda x: df_1h.loc[x, 'low'] if x in df_1h.index else np.nan)
            
            # Detect new 1H candle - use pd.Series for shift
            hour_floor = pd.Series(df_10m.index.floor('1h'), index=df_10m.index)
            df_10m['is_new_1h_candle'] = (hour_floor != hour_floor.shift(1)).fillna(True)
            
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
            
            # Add EMA conditions (uses floor-mapped close_1h — unchanged)
            df_10m['ema_bull'] = df_10m['close_1h'] > df_10m['ema_1h']
            df_10m['ema_bear'] = df_10m['close_1h'] < df_10m['ema_1h']
            
            prev_close_1h = df_10m['close_1h'].shift(1)
            prev_ema_1h = df_10m['ema_1h'].shift(1)

            # ── EMA Cross Detection ────────────────────────────────────────
            # Build close_1h_cross / ema_1h_cross by shifting df_1h index
            # forward +1H so the 8am bar is only visible from 9am.
            # This avoids false cross triggers at every hour boundary.
            # close_1h_cross is used ONLY for ema_bull_cross / ema_bear_cross
            # detection and as the entry price for EMA cross signals.
            df_1h_cross_avail = df_1h.copy()
            df_1h_cross_avail.index = df_1h.index + pd.Timedelta('1h')
            df_10m['close_1h_cross'] = df_1h_cross_avail['close'].reindex(df_10m.index, method='ffill')
            df_10m['ema_1h_cross'] = df_1h_cross_avail['ema'].reindex(df_10m.index, method='ffill')

            prev_close_1h_cross = df_10m['close_1h_cross'].shift(1)
            prev_ema_1h_cross = df_10m['ema_1h_cross'].shift(1)
            # Cross fires exactly once at the first bar where the newly-closed
            # 1H bar crossed EMA (9am for the 8am bar). No false hourly triggers.
            df_10m['ema_bull_cross'] = (
                (df_10m['close_1h_cross'] > df_10m['ema_1h_cross']) &
                (prev_close_1h_cross <= prev_ema_1h_cross)
            )
            df_10m['ema_bear_cross'] = (
                (df_10m['close_1h_cross'] < df_10m['ema_1h_cross']) &
                (prev_close_1h_cross >= prev_ema_1h_cross)
            )
            
            print("[OK] Long/short Supertrend + ADX attached (strategy.yaml)")
            
            if 'volume' in df_10m.columns:
                df_10m['volume_ma'] = df_10m['volume'].rolling(
                    window=volume_ma_period, min_periods=volume_ma_period
                ).mean()
            
            # Drop NaN rows
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
        
        print(f"[OK] Loaded {len(df_10m)} bars")
        
        # Disconnect from IBKR (data is loaded)
        ib.disconnect()
        print("[OK] Disconnected from IBKR")
        
        print(f"[OK] Final dataset: {len(df_10m)} bars")
        
        # Create backtest config
        bt_config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            primary_timeframe=get_primary_timeframe(strategy_cfg),
            ema_length=ema_cfg.get('length', 200),
            tick_size=contract_cfg.get('contract', {}).get('tick_size', 0.25),
            tick_value=contract_cfg.get('contract', {}).get('tick_value', 0.50),
            multiplier=contract_cfg.get('contract', {}).get('multiplier', 2),
            commission_per_contract=risk_cfg.get('backtesting', {}).get('commission_per_contract', 0.62),
            slippage_ticks=risk_cfg.get('backtesting', {}).get('slippage_ticks', 1),
            contracts=contracts,
            initial_capital=100000,
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            independent_books=bool(strategy_cfg.get('execution', {}).get('independent_books', False)),
            **signal_engine_kwargs(strategy_cfg),
        )
        
        # Run backtest
        print("\nRunning backtest...")
        engine = BacktestEngine(bt_config)
        result = engine.run(df_10m)
        
        # Save results - Single clean CSV file
        output_dir = Path("./backtest/results")
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create single CSV with summary + trades
        csv_path = output_dir / f"backtest_{timestamp}.csv"
        
        trades_df = engine.get_trade_summary()
        metrics = result.metrics
        
        # Calculate long/short breakdown
        if not trades_df.empty:
            long_trades = trades_df[trades_df['direction'] == 'long']
            short_trades = trades_df[trades_df['direction'] == 'short']
            long_wins = len(long_trades[long_trades['pnl_dollars'] > 0])
            short_wins = len(short_trades[short_trades['pnl_dollars'] > 0])
            long_pnl = long_trades['pnl_dollars'].sum()
            short_pnl = short_trades['pnl_dollars'].sum()
        else:
            long_trades = short_trades = pd.DataFrame()
            long_wins = short_wins = 0
            long_pnl = short_pnl = 0
        
        with open(csv_path, 'w', newline='') as f:
            # Header
            f.write("=" * 50 + "\n")
            f.write("MNQ SUPERTREND + EMA STRATEGY - BACKTEST REPORT\n")
            f.write("=" * 50 + "\n\n")
            
            # Summary section
            f.write("PERFORMANCE SUMMARY\n")
            f.write("-" * 30 + "\n")
            f.write(f"Period,{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n")
            f.write(f"Contract,{contract_symbol}\n")
            f.write(f"Contracts per Trade,{contracts}\n")
            f.write(f"Initial Capital,$100000.00\n")
            f.write(f"Final Equity,${metrics.get('final_equity', 0):,.2f}\n")
            f.write(f"Net Profit/Loss,${metrics.get('net_profit', 0):,.2f}\n")
            f.write(f"Total P&L Points,{metrics.get('total_points', 0):.2f}\n")
            f.write(f"Total Profit Points,{metrics.get('total_profit_points', 0):.2f}\n")
            f.write(f"Total Loss Points,{metrics.get('total_loss_points', 0):.2f}\n")
            f.write(f"Total Return,{metrics.get('total_return_pct', 0):.2f}%\n")
            f.write(f"Max Drawdown,{metrics.get('max_drawdown_pct', 0):.2f}%\n")
            f.write(f"Sharpe Ratio,{metrics.get('sharpe_ratio', 0):.2f}\n")
            f.write("\n")
            
            # Trade Statistics
            f.write("TRADE STATISTICS\n")
            f.write("-" * 30 + "\n")
            f.write(f"Total Trades,{metrics.get('total_trades', 0)}\n")
            f.write(f"Winning Trades,{metrics.get('winning_trades', 0)}\n")
            f.write(f"Losing Trades,{metrics.get('losing_trades', 0)}\n")
            f.write(f"Win Rate,{metrics.get('win_rate', 0):.1f}%\n")
            f.write(f"Profit Factor,{metrics.get('profit_factor', 0):.2f}\n")
            f.write(f"Expectancy per Trade,${metrics.get('expectancy', 0):.2f}\n")
            f.write(f"Average Win,${metrics.get('avg_win', 0):.2f}\n")
            f.write(f"Average Loss,${metrics.get('avg_loss', 0):.2f}\n")
            f.write(f"Largest Win,${metrics.get('largest_win', 0):.2f}\n")
            f.write(f"Largest Loss,${metrics.get('largest_loss', 0):.2f}\n")
            f.write("\n")
            
            # Long vs Short Breakdown
            f.write("LONG vs SHORT BREAKDOWN\n")
            f.write("-" * 30 + "\n")
            f.write(f"Long Trades,{len(long_trades)}\n")
            f.write(f"Long Wins,{long_wins}\n")
            f.write(f"Long Win Rate,{(long_wins/len(long_trades)*100) if len(long_trades) > 0 else 0:.1f}%\n")
            f.write(f"Long P&L,${long_pnl:,.2f}\n")
            f.write(f"Short Trades,{len(short_trades)}\n")
            f.write(f"Short Wins,{short_wins}\n")
            f.write(f"Short Win Rate,{(short_wins/len(short_trades)*100) if len(short_trades) > 0 else 0:.1f}%\n")
            f.write(f"Short P&L,${short_pnl:,.2f}\n")
            f.write("\n")
            
            # Exit Type Breakdown
            f.write("EXIT TYPE BREAKDOWN\n")
            f.write("-" * 30 + "\n")
            f.write(f"Take Profit Exits,{metrics.get('tp_exits', 0)}\n")
            f.write(f"Stop Loss Exits,{metrics.get('sl_exits', 0)}\n")
            f.write(f"Supertrend Flip Exits,{metrics.get('st_flip_exits', 0)}\n")
            f.write("\n")
            
            # Settings
            from utils.strategy_side_config import resolve_side_configs
            _s = resolve_side_configs(strategy_cfg)
            _lst, _sst = _s["long_supertrend"], _s["short_supertrend"]
            _lr, _sr = _s["long_risk"], _s["short_risk"]
            _la, _sa = _s["long_adx"], _s["short_adx"]
            f.write("STRATEGY SETTINGS\n")
            f.write("-" * 30 + "\n")
            f.write(f"Primary Timeframe,{get_primary_timeframe(strategy_cfg)}\n")
            f.write(f"Long ST ATR,{_lst.get('atr_length', 10)}\n")
            f.write(f"Long ST Mult,{_lst.get('multiplier', 3)}\n")
            f.write(f"Short ST ATR,{_sst.get('atr_length', 10)}\n")
            f.write(f"Short ST Mult,{_sst.get('multiplier', 3)}\n")
            f.write(f"EMA Length (1H),{ema_cfg.get('length', 200)}\n")
            f.write(f"Stop Loss % Long,{_lr.get('stop_loss_pct', 0.4)}%\n")
            f.write(f"Take Profit % Long,{_lr.get('take_profit_pct', 1.2)}%\n")
            f.write(f"Stop Loss % Short,{_sr.get('stop_loss_pct', 0.4)}%\n")
            f.write(f"Take Profit % Short,{_sr.get('take_profit_pct', 1.2)}%\n")
            f.write(f"ADX Long threshold,{_la.get('threshold', 20)}\n")
            f.write(f"ADX Short threshold,{_sa.get('threshold', 20)}\n")
            f.write("\n")
            
            # P&L BY TRADE with running total
            f.write("P&L BY TRADE\n")
            f.write("-" * 30 + "\n")
            if not trades_df.empty:
                running_pnl = 0
                for _, trade in trades_df.iterrows():
                    running_pnl += trade['pnl_dollars']
                    f.write(f"Trade {trade['trade_id']},{trade['direction'].upper()},{trade['exit_type']},${trade['pnl_dollars']:,.2f},Running: ${running_pnl:,.2f}\n")
            f.write("\n")
            
            # ALL TRADES DETAIL - Full data for verification
            f.write("ALL TRADES DETAIL\n")
            f.write("-" * 30 + "\n")
            if not trades_df.empty:
                trades_df.to_csv(f, index=False, float_format="%.2f")
                # Summary row: total net P&L points and dollars for the backtest timeframe
                total_pts = metrics.get('total_points')
                if total_pts is None:
                    total_pts = float(trades_df['pnl_points'].sum())
                else:
                    total_pts = float(total_pts)
                total_dol = metrics.get('net_profit')
                if total_dol is None:
                    total_dol = float(trades_df['pnl_dollars'].sum())
                else:
                    total_dol = float(total_dol)
                # TOTAL row: empty fields for non-aggregated columns (see get_trade_summary columns)
                f.write(f"TOTAL,,,,,,,,{total_pts:.2f},{total_dol:.2f},,,,,,,,\n")
        
        # Print summary to console
        print("\n" + "=" * 60)
        print("                    BACKTEST RESULTS")
        print("=" * 60)
        print(f"\n  Period:        {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        print(f"  Contract:      {contract_symbol}")
        print(f"  Contracts:     {contracts}")
        
        print(f"\n  --- P&L SUMMARY ---")
        print(f"  Initial:       $100,000.00")
        print(f"  Final Equity:  ${metrics.get('final_equity', 0):,.2f}")
        print(f"  Net Profit:    ${metrics.get('net_profit', 0):,.2f}")
        print(f"  Total Return:  {metrics.get('total_return_pct', 0):.2f}%")
        print(f"  Max Drawdown:  {metrics.get('max_drawdown_pct', 0):.2f}%")
        
        print(f"\n  --- TRADE STATS ---")
        print(f"  Total Trades:  {metrics.get('total_trades', 0)}")
        print(f"  Win Rate:      {metrics.get('win_rate', 0):.1f}%")
        print(f"  Profit Factor: {metrics.get('profit_factor', 0):.2f}")
        print(f"  Expectancy:    ${metrics.get('expectancy', 0):.2f}/trade")
        
        print(f"\n  --- LONG vs SHORT ---")
        print(f"  Long Trades:   {len(long_trades)} ({(long_wins/len(long_trades)*100) if len(long_trades) > 0 else 0:.0f}% win) = ${long_pnl:,.2f}")
        print(f"  Short Trades:  {len(short_trades)} ({(short_wins/len(short_trades)*100) if len(short_trades) > 0 else 0:.0f}% win) = ${short_pnl:,.2f}")
        
        print("\n" + "=" * 60)
        
        print(f"\n[OK] Results saved to: {csv_path}")
        
    except Exception as e:
        logger.error(f"Backtest failed: {e}")
        if ib.isConnected():
            ib.disconnect()
        raise


async def run_paper_trading(config: dict) -> None:
    """Run paper trading mode."""
    from ib_async import IB, Future
    from data import RealtimeFeed, MultiTimeframeFeed
    from strategy import SignalEngine, StateManager, SignalType
    from execution import OrderManager, PositionTracker
    
    print("\n" + "=" * 60)
    print("                 PAPER TRADING MODE")
    print("=" * 60)
    
    contracts = get_contracts()
    
    # Get configs
    ibkr_cfg = config.get('ibkr', {})
    strategy_cfg = config.get('strategy', {})
    risk_cfg = config.get('risk', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    from utils.strategy_side_config import resolve_side_configs, signal_engine_init_kwargs
    from data.strategy_indicators import live_bar_indicator_slice, bar_flips_for_state_manager

    sides = resolve_side_configs(strategy_cfg)
    
    conn_cfg = ibkr_cfg.get('connection', {})
    port = conn_cfg.get('ports', {}).get('tws_paper', 7497)
    
    ib = IB()
    
    try:
        print(f"\nConnecting to IBKR Paper on port {port}...")
        await ib.connectAsync('127.0.0.1', port, clientId=1)
        print("[OK] Connected to IBKR Paper Account")
        
        # Get front-month MNQ contract
        base_contract = Future(symbol='MNQ', exchange='CME', currency='USD')
        contract_details = await ib.reqContractDetailsAsync(base_contract)
        
        if not contract_details:
            print("[X] Error: No MNQ contracts found")
            return
        
        sorted_contracts = sorted(
            contract_details,
            key=lambda x: x.contract.lastTradeDateOrContractMonth
        )
        contract = sorted_contracts[0].contract
        print(f"[OK] Trading: {contract.localSymbol}")
        
        # Initialize components
        signal_engine = SignalEngine(
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            **signal_engine_init_kwargs(strategy_cfg),
        )
        
        state_manager = StateManager(
            state_file="./data/paper_state.json",
            tick_value=0.50,
            contracts_per_trade=contracts
        )
        
        order_manager = OrderManager(
            ib_client=ib,
            contract=contract,
            default_qty=contracts
        )
        
        position_tracker = PositionTracker(ib_client=ib, contract=contract)
        await position_tracker.initialize()
        
        # CRITICAL: Add position sync callback to detect bracket order fills
        # When IBKR TP/SL orders fill, position changes but StateManager doesn't know
        def on_position_sync(position_info):
            """Sync IBKR position with StateManager."""
            ibkr_qty = position_info.quantity
            state_qty = state_manager.state.position_size
            
            # Detect position closed by IBKR (bracket order filled)
            if state_qty != 0 and ibkr_qty == 0:
                print(f"\\n⚠️ POSITION CLOSED BY IBKR (TP/SL hit)")
                # Create a synthetic exit signal
                from strategy.signal_engine import ExitSignal, ExitType
                exit_signal = ExitSignal(
                    exit_type=ExitType.TAKE_PROFIT if position_info.realized_pnl > 0 else ExitType.STOP_LOSS,
                    timestamp=pd.Timestamp.now(),
                    exit_price=position_info.market_price if position_info.market_price > 0 else state_manager.state.entry_price,
                    entry_price=state_manager.state.entry_price,
                    pnl_points=0  # Will be calculated
                )
                state_manager.on_exit(exit_signal)
                print(f"✅ StateManager synced: Position now FLAT")
            
            # Detect mismatch (IBKR has position but state doesn't)
            elif state_qty == 0 and ibkr_qty != 0:
                logger.warning(f"Position mismatch: IBKR has {ibkr_qty} but StateManager is FLAT")
                # Don't auto-sync entry to avoid confusion
        
        position_tracker.on_position_change(on_position_sync)
        
        # Primary timeframe from config (for bar size and hour-boundary logic)
        primary_bar_size = get_primary_bar_size(strategy_cfg)
        primary_last_minute = get_primary_last_minute_of_hour(strategy_cfg)
        primary_bars_per_hour = get_primary_bars_per_hour(strategy_cfg)
        
        # Create feeds
        primary_feed = RealtimeFeed(
            ib_client=ib,
            contract=contract,
            bar_size=primary_bar_size
        )
        mtf = MultiTimeframeFeed(
            primary_feed,
            ema_length=ema_cfg.get('length', 200),
            bars_per_hour=primary_bars_per_hour
        )
        
        # Bar close handler
        async def on_bar_close(df, bar):
            if df is None or len(df) < 60:
                return
            
            inds = live_bar_indicator_slice(
                df,
                sides["long_supertrend_entry"],
                sides["short_supertrend_entry"],
                sides["long_adx"],
                sides["short_adx"],
                long_supertrend_exit=sides["long_supertrend_exit"],
                short_supertrend_exit=sides["short_supertrend_exit"],
                row_i=-2,
            )
            ema_1h, close_1h = mtf.get_confirmed_1h_ema(df)
            
            current_bar = df.iloc[-2].copy()
            for k, v in inds.items():
                current_bar[k] = v
            
            if 'volume' in df.columns and len(df) >= max(1, int(strategy_cfg.get('volume_ma_period', 20))):
                current_bar['volume_ma'] = float(
                    df['volume'].rolling(
                        max(1, int(strategy_cfg.get('volume_ma_period', 20))),
                        min_periods=max(1, int(strategy_cfg.get('volume_ma_period', 20)))
                    ).mean().iloc[-2]
                )
            else:
                current_bar['volume_ma'] = np.nan
            
            current_bar['ema_1h'] = ema_1h
            current_bar['close_1h'] = close_1h
            current_bar['ema_bull'] = close_1h > ema_1h if not pd.isna(ema_1h) else False
            current_bar['ema_bear'] = close_1h < ema_1h if not pd.isna(ema_1h) else False
            
            # Get 1H high/low for ST flip alignment check
            df_1h = mtf.aggregate_1h_from_10m(df)
            current_hour = df.iloc[-2].name.floor('1h')
            if current_hour in df_1h.index:
                current_bar['high_1h'] = df_1h.loc[current_hour, 'high']
                current_bar['low_1h'] = df_1h.loc[current_hour, 'low']
            else:
                current_bar['high_1h'] = np.nan
                current_bar['low_1h'] = np.nan
            
            # Detect new 1H candle boundary (for partial cross detection)
            last_closed_10m_time = df.iloc[-2].name
            current_bar['is_new_1h_candle'] = (last_closed_10m_time.minute == 0)
            
            # Detect EMA cross (for deferred entries after unaligned ST flips)
            # Also set close_1h_cross / ema_1h_cross (previous completed hour's values)
            # used by signal engine to decide st_flip vs ema_cross on ST flip bars.
            if len(df_1h) >= 3:
                from indicators.ema import calculate_ema
                ema_series = calculate_ema(df_1h['close'], ema_cfg.get('length', 200))
                
                # Previous completed hour's close & EMA (for ST flip alignment check)
                # df_1h[-1] = current (partial) hour, df_1h[-2] = last completed hour
                current_bar['close_1h_cross'] = df_1h['close'].iloc[-2]
                current_bar['ema_1h_cross'] = ema_series.iloc[-2]
                
                last_primary_minute = last_closed_10m_time.minute
                if last_primary_minute == primary_last_minute:
                    prev_confirmed_idx = -2
                else:
                    prev_confirmed_idx = -3
                
                if len(df_1h) >= abs(prev_confirmed_idx):
                    prev_close_1h = df_1h['close'].iloc[prev_confirmed_idx]
                    prev_ema_1h = ema_series.iloc[prev_confirmed_idx]
                    
                    was_below_ema = prev_close_1h <= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                    was_above_ema = prev_close_1h >= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                    
                    current_bar['ema_bull_cross'] = current_bar['ema_bull'] and was_below_ema
                    current_bar['ema_bear_cross'] = current_bar['ema_bear'] and was_above_ema
                else:
                    current_bar['ema_bull_cross'] = False
                    current_bar['ema_bear_cross'] = False
            else:
                current_bar['ema_bull_cross'] = False
                current_bar['ema_bear_cross'] = False
            
            # Log bar close with indicator values (only when events happen)
            st_dir = "BULL" if current_bar.get('direction_long', current_bar.get('direction', 0)) == -1 else "BEAR"
            ema_status = "BULL" if current_bar['ema_bull'] else ("BEAR" if current_bar['ema_bear'] else "NEUTRAL")
            adx_val = current_bar.get('adx', 0)
            events = ""
            if current_bar.get('st_bull_flip', False):
                events += " [ST BULL FLIP!]"
            elif current_bar.get('st_bear_flip', False):
                events += " [ST BEAR FLIP!]"
            if current_bar.get('ema_bull_cross', False):
                events += " [EMA BULL CROSS!]"
            elif current_bar.get('ema_bear_cross', False):
                events += " [EMA BEAR CROSS!]"
            
            if events:
                print(f"📊 Bar: {current_bar.name} | Close: {current_bar['close']:.2f} | ST: {st_dir} | EMA: {ema_status} | ADX: {adx_val:.1f}{events}")
            
            bf, br, st_direction = bar_flips_for_state_manager(current_bar)
            state_manager.update_supertrend_state(
                st_bull_flip=bf,
                st_bear_flip=br,
                current_direction=st_direction
            )
            
            state = state_manager.state
            
            # Check exits
            if state.position_size != 0:
                exit_signal = signal_engine.check_exit_conditions(
                    bar=current_bar,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    stop_loss=state.stop_loss,
                    take_profit=state.take_profit,
                    entry_time=state.entry_time
                )
                
                if exit_signal:
                    print(f"\n🔴 EXIT: {exit_signal.exit_type.value}")
                    action = "SELL" if state.position_size > 0 else "BUY"
                    await order_manager.close_position(
                        action=action,
                        quantity=abs(state.position_size) * contracts,
                        reason=exit_signal.exit_type.value
                    )
                    state_manager.on_exit(exit_signal)
            
            # Check entries
            if state.position_size == 0:
                vol_win = (
                    SignalEngine.single_row_volume_window(current_bar)
                    if signal_engine.volume_check
                    else None
                )
                entry_signal, entry_updates = signal_engine.evaluate_entry_conditions(
                    bar=current_bar,
                    position_size=0,
                    traded_in_bull_trend=state.traded_in_bull_trend,
                    traded_in_bear_trend=state.traded_in_bear_trend,
                    pending_long_ema_wait=state.pending_long_ema_wait,
                    pending_short_ema_wait=state.pending_short_ema_wait,
                    pending_adx_long=state.pending_adx_long,
                    pending_adx_short=state.pending_adx_short,
                    adx_wait_bars_left_long=state.adx_wait_bars_left_long,
                    adx_wait_bars_left_short=state.adx_wait_bars_left_short,
                    adx_wait_trigger_long=state.adx_wait_trigger_long,
                    adx_wait_trigger_short=state.adx_wait_trigger_short,
                    volume_window=vol_win,
                    allow_volume_defer=True,
                    pending_volume_long=state.pending_volume_long,
                    pending_volume_short=state.pending_volume_short,
                    volume_wait_bars_left_long=state.volume_wait_bars_left_long,
                    volume_wait_bars_left_short=state.volume_wait_bars_left_short,
                    volume_wait_trigger_long=state.volume_wait_trigger_long,
                    volume_wait_trigger_short=state.volume_wait_trigger_short,
                    volume_wait_kind_long=state.volume_wait_kind_long,
                    volume_wait_kind_short=state.volume_wait_kind_short,
                )
                if entry_updates.get("set_pending_long_ema_wait"):
                    state_manager.set_pending_long_ema_wait()
                if entry_updates.get("clear_pending_long_ema_wait"):
                    state_manager.clear_pending_long_ema_wait()
                if entry_updates.get("set_pending_short_ema_wait"):
                    state_manager.set_pending_short_ema_wait()
                if entry_updates.get("clear_pending_short_ema_wait"):
                    state_manager.clear_pending_short_ema_wait()
                # ADX wait updates
                if entry_updates.get("set_adx_wait_long"):
                    data = entry_updates["set_adx_wait_long"]
                    state_manager.set_adx_wait_long(data["bars"], data["trigger"])
                if entry_updates.get("clear_adx_wait_long"):
                    state_manager.clear_adx_wait_long()
                if entry_updates.get("decrement_adx_wait_long"):
                    state_manager.decrement_adx_wait_long()
                if entry_updates.get("set_adx_wait_short"):
                    data = entry_updates["set_adx_wait_short"]
                    state_manager.set_adx_wait_short(data["bars"], data["trigger"])
                if entry_updates.get("clear_adx_wait_short"):
                    state_manager.clear_adx_wait_short()
                if entry_updates.get("decrement_adx_wait_short"):
                    state_manager.decrement_adx_wait_short()
                if entry_updates.get("set_volume_wait_long"):
                    d = entry_updates["set_volume_wait_long"]
                    state_manager.set_volume_wait_long(d["remaining"], d["trigger"], d["kind"])
                if entry_updates.get("set_volume_wait_short"):
                    d = entry_updates["set_volume_wait_short"]
                    state_manager.set_volume_wait_short(d["remaining"], d["trigger"], d["kind"])
                if entry_updates.get("clear_pending_volume_long"):
                    state_manager.clear_volume_wait_long()
                if entry_updates.get("clear_pending_volume_short"):
                    state_manager.clear_volume_wait_short()
                if entry_updates.get("decrement_volume_wait_long"):
                    state_manager.decrement_volume_wait_long()
                if entry_updates.get("decrement_volume_wait_short"):
                    state_manager.decrement_volume_wait_short()
                if entry_signal:
                    is_long = entry_signal.signal_type == SignalType.BUY
                    print(f"\n🟢 ENTRY: {'LONG' if is_long else 'SHORT'} @ {entry_signal.price:.2f}")
                    
                    stop_loss, take_profit = signal_engine.calculate_exit_levels(
                        entry_price=entry_signal.price,
                        is_long=is_long
                    )
                    
                    await order_manager.place_bracket_order(
                        action="BUY" if is_long else "SELL",
                        quantity=contracts,
                        take_profit_price=take_profit,
                        stop_loss_price=stop_loss,
                        entry_type="MKT"
                    )
                    
                    state_manager.on_entry(entry_signal, stop_loss, take_profit)
        
        primary_feed.on_bar_close(lambda df, bar: asyncio.create_task(on_bar_close(df, bar)))
        
        await primary_feed.start(initial_lookback_days=15)
        
        print("\n[OK] Paper trading is ACTIVE")
        print("[OK] Waiting for signals on 10-minute bar closes...")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
        
        await primary_feed.stop()
        await position_tracker.shutdown()
        ib.disconnect()
        print("[OK] Paper trading stopped")
        
    except Exception as e:
        logger.error(f"Paper trading error: {e}")
        if ib.isConnected():
            ib.disconnect()
        raise


async def run_live_trading(config: dict) -> None:
    """Run live trading mode."""
    from ib_async import IB, Future
    from data import RealtimeFeed, MultiTimeframeFeed
    from strategy import SignalEngine, StateManager, SignalType
    from execution import OrderManager, PositionTracker
    
    print("\n" + "=" * 60)
    print("          [!]  LIVE TRADING MODE - REAL MONEY  [!]")
    print("=" * 60)
    
    print("\n[!]  WARNING: This will trade with REAL MONEY!")
    print("[!]  Make sure you understand the risks involved.\n")
    
    confirm = input("Type 'CONFIRM LIVE TRADING' to proceed: ").strip()
    
    if confirm != 'CONFIRM LIVE TRADING':
        print("Live trading cancelled.")
        return
    
    print("\n[!]  Live trading starting in 5 seconds...")
    print("    Press Ctrl+C to abort\n")
    
    await asyncio.sleep(5)
    
    contracts = get_contracts()
    
    # Get configs
    ibkr_cfg = config.get('ibkr', {})
    strategy_cfg = config.get('strategy', {})
    risk_cfg = config.get('risk', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    from utils.strategy_side_config import resolve_side_configs, signal_engine_init_kwargs
    from data.strategy_indicators import live_bar_indicator_slice, bar_flips_for_state_manager

    sides = resolve_side_configs(strategy_cfg)
    
    conn_cfg = ibkr_cfg.get('connection', {})
    # LIVE port: 7496 (TWS) or 4001 (Gateway)
    port = conn_cfg.get('ports', {}).get('tws_live', 7496)
    
    ib = IB()
    
    try:
        print(f"\nConnecting to IBKR LIVE on port {port}...")
        await ib.connectAsync('127.0.0.1', port, clientId=1)
        print("[OK] Connected to IBKR LIVE Account")
        print("[!] ⚠️  REAL MONEY MODE - Orders will execute on live account!")
        
        # Get front-month MNQ contract
        base_contract = Future(symbol='MNQ', exchange='CME', currency='USD')
        contract_details = await ib.reqContractDetailsAsync(base_contract)
        
        if not contract_details:
            print("[X] Error: No MNQ contracts found")
            return
        
        sorted_contracts = sorted(
            contract_details,
            key=lambda x: x.contract.lastTradeDateOrContractMonth
        )
        contract = sorted_contracts[0].contract
        print(f"[OK] Trading: {contract.localSymbol}")
        
        # Initialize components
        signal_engine = SignalEngine(
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            **signal_engine_init_kwargs(strategy_cfg),
        )
        
        state_manager = StateManager(
            state_file="./data/live_state.json",  # Separate state file for live
            tick_value=0.50,
            contracts_per_trade=contracts
        )
        
        order_manager = OrderManager(
            ib_client=ib,
            contract=contract,
            default_qty=contracts
        )
        
        position_tracker = PositionTracker(ib_client=ib, contract=contract)
        await position_tracker.initialize()
        
        # CRITICAL: Add position sync callback to detect bracket order fills
        # When IBKR TP/SL orders fill, position changes but StateManager doesn't know
        def on_position_sync(position_info):
            """Sync IBKR position with StateManager."""
            ibkr_qty = position_info.quantity
            state_qty = state_manager.state.position_size
            
            # Detect position closed by IBKR (bracket order filled)
            if state_qty != 0 and ibkr_qty == 0:
                print(f"\\n⚠️ LIVE: POSITION CLOSED BY IBKR (TP/SL hit)")
                # Create a synthetic exit signal
                from strategy.signal_engine import ExitSignal, ExitType
                exit_signal = ExitSignal(
                    exit_type=ExitType.TAKE_PROFIT if position_info.realized_pnl > 0 else ExitType.STOP_LOSS,
                    timestamp=pd.Timestamp.now(),
                    exit_price=position_info.market_price if position_info.market_price > 0 else state_manager.state.entry_price,
                    entry_price=state_manager.state.entry_price,
                    pnl_points=0  # Will be calculated
                )
                state_manager.on_exit(exit_signal)
                print(f"✅ LIVE: StateManager synced: Position now FLAT")
            
            # Detect mismatch (IBKR has position but state doesn't)
            elif state_qty == 0 and ibkr_qty != 0:
                logger.warning(f"LIVE Position mismatch: IBKR has {ibkr_qty} but StateManager is FLAT")
        
        position_tracker.on_position_change(on_position_sync)
        
        # Primary timeframe from config (for bar size and hour-boundary logic)
        primary_bar_size = get_primary_bar_size(strategy_cfg)
        primary_last_minute = get_primary_last_minute_of_hour(strategy_cfg)
        primary_bars_per_hour = get_primary_bars_per_hour(strategy_cfg)
        
        # Create feeds
        primary_feed = RealtimeFeed(
            ib_client=ib,
            contract=contract,
            bar_size=primary_bar_size
        )
        mtf = MultiTimeframeFeed(
            primary_feed,
            ema_length=ema_cfg.get('length', 200),
            bars_per_hour=primary_bars_per_hour
        )
        
        # Bar close handler
        async def on_bar_close(df, bar):
            if df is None or len(df) < 60:
                return
            
            inds = live_bar_indicator_slice(
                df,
                sides["long_supertrend_entry"],
                sides["short_supertrend_entry"],
                sides["long_adx"],
                sides["short_adx"],
                long_supertrend_exit=sides["long_supertrend_exit"],
                short_supertrend_exit=sides["short_supertrend_exit"],
                row_i=-2,
            )
            ema_1h, close_1h = mtf.get_confirmed_1h_ema(df)
            
            current_bar = df.iloc[-2].copy()
            for k, v in inds.items():
                current_bar[k] = v
            
            if 'volume' in df.columns and len(df) >= max(1, int(strategy_cfg.get('volume_ma_period', 20))):
                current_bar['volume_ma'] = float(
                    df['volume'].rolling(
                        max(1, int(strategy_cfg.get('volume_ma_period', 20))),
                        min_periods=max(1, int(strategy_cfg.get('volume_ma_period', 20)))
                    ).mean().iloc[-2]
                )
            else:
                current_bar['volume_ma'] = np.nan
            
            current_bar['ema_1h'] = ema_1h
            current_bar['close_1h'] = close_1h
            current_bar['ema_bull'] = close_1h > ema_1h if not pd.isna(ema_1h) else False
            current_bar['ema_bear'] = close_1h < ema_1h if not pd.isna(ema_1h) else False
            
            # Get 1H high/low for ST flip alignment check
            df_1h = mtf.aggregate_1h_from_10m(df)
            current_hour = df.iloc[-2].name.floor('1h')
            if current_hour in df_1h.index:
                current_bar['high_1h'] = df_1h.loc[current_hour, 'high']
                current_bar['low_1h'] = df_1h.loc[current_hour, 'low']
            else:
                current_bar['high_1h'] = np.nan
                current_bar['low_1h'] = np.nan
            
            # Detect new 1H candle boundary (for partial cross detection)
            last_closed_10m_time = df.iloc[-2].name
            current_bar['is_new_1h_candle'] = (last_closed_10m_time.minute == 0)
            
            # Detect EMA cross (for deferred entries after unaligned ST flips)
            # Also set close_1h_cross / ema_1h_cross (previous completed hour's values)
            # used by signal engine to decide st_flip vs ema_cross on ST flip bars.
            if len(df_1h) >= 3:
                from indicators.ema import calculate_ema
                ema_series = calculate_ema(df_1h['close'], ema_cfg.get('length', 200))
                
                # Previous completed hour's close & EMA (for ST flip alignment check)
                # df_1h[-1] = current (partial) hour, df_1h[-2] = last completed hour
                current_bar['close_1h_cross'] = df_1h['close'].iloc[-2]
                current_bar['ema_1h_cross'] = ema_series.iloc[-2]
                
                last_primary_minute = last_closed_10m_time.minute
                if last_primary_minute == primary_last_minute:
                    prev_confirmed_idx = -2
                else:
                    prev_confirmed_idx = -3
                
                if len(df_1h) >= abs(prev_confirmed_idx):
                    prev_close_1h = df_1h['close'].iloc[prev_confirmed_idx]
                    prev_ema_1h = ema_series.iloc[prev_confirmed_idx]
                    
                    was_below_ema = prev_close_1h <= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                    was_above_ema = prev_close_1h >= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                    
                    current_bar['ema_bull_cross'] = current_bar['ema_bull'] and was_below_ema
                    current_bar['ema_bear_cross'] = current_bar['ema_bear'] and was_above_ema
                else:
                    current_bar['ema_bull_cross'] = False
                    current_bar['ema_bear_cross'] = False
            else:
                current_bar['ema_bull_cross'] = False
                current_bar['ema_bear_cross'] = False
            
            # Log bar close with indicator values (only when events happen)
            st_dir = "BULL" if current_bar.get('direction_long', current_bar.get('direction', 0)) == -1 else "BEAR"
            ema_status = "BULL" if current_bar['ema_bull'] else ("BEAR" if current_bar['ema_bear'] else "NEUTRAL")
            adx_val = current_bar.get('adx', 0)
            events = ""
            if current_bar.get('st_bull_flip', False):
                events += " [ST BULL FLIP!]"
            elif current_bar.get('st_bear_flip', False):
                events += " [ST BEAR FLIP!]"
            if current_bar.get('ema_bull_cross', False):
                events += " [EMA BULL CROSS!]"
            elif current_bar.get('ema_bear_cross', False):
                events += " [EMA BEAR CROSS!]"
            
            if events:
                print(f"📊 LIVE: {current_bar.name} | Close: {current_bar['close']:.2f} | ST: {st_dir} | EMA: {ema_status} | ADX: {adx_val:.1f}{events}")
            
            bf, br, st_direction = bar_flips_for_state_manager(current_bar)
            state_manager.update_supertrend_state(
                st_bull_flip=bf,
                st_bear_flip=br,
                current_direction=st_direction
            )
            
            state = state_manager.state
            
            # Check exits
            if state.position_size != 0:
                exit_signal = signal_engine.check_exit_conditions(
                    bar=current_bar,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    stop_loss=state.stop_loss,
                    take_profit=state.take_profit,
                    entry_time=state.entry_time
                )
                
                if exit_signal:
                    print(f"\n🔴 LIVE EXIT: {exit_signal.exit_type.value}")
                    action = "SELL" if state.position_size > 0 else "BUY"
                    await order_manager.close_position(
                        action=action,
                        quantity=abs(state.position_size) * contracts,
                        reason=exit_signal.exit_type.value
                    )
                    state_manager.on_exit(exit_signal)
            
            # Check entries
            if state.position_size == 0:
                vol_win = (
                    SignalEngine.single_row_volume_window(current_bar)
                    if signal_engine.volume_check
                    else None
                )
                entry_signal, entry_updates = signal_engine.evaluate_entry_conditions(
                    bar=current_bar,
                    position_size=0,
                    traded_in_bull_trend=state.traded_in_bull_trend,
                    traded_in_bear_trend=state.traded_in_bear_trend,
                    pending_long_ema_wait=state.pending_long_ema_wait,
                    pending_short_ema_wait=state.pending_short_ema_wait,
                    pending_adx_long=state.pending_adx_long,
                    pending_adx_short=state.pending_adx_short,
                    adx_wait_bars_left_long=state.adx_wait_bars_left_long,
                    adx_wait_bars_left_short=state.adx_wait_bars_left_short,
                    adx_wait_trigger_long=state.adx_wait_trigger_long,
                    adx_wait_trigger_short=state.adx_wait_trigger_short,
                    volume_window=vol_win,
                    allow_volume_defer=True,
                    pending_volume_long=state.pending_volume_long,
                    pending_volume_short=state.pending_volume_short,
                    volume_wait_bars_left_long=state.volume_wait_bars_left_long,
                    volume_wait_bars_left_short=state.volume_wait_bars_left_short,
                    volume_wait_trigger_long=state.volume_wait_trigger_long,
                    volume_wait_trigger_short=state.volume_wait_trigger_short,
                    volume_wait_kind_long=state.volume_wait_kind_long,
                    volume_wait_kind_short=state.volume_wait_kind_short,
                )
                if entry_updates.get("set_pending_long_ema_wait"):
                    state_manager.set_pending_long_ema_wait()
                if entry_updates.get("clear_pending_long_ema_wait"):
                    state_manager.clear_pending_long_ema_wait()
                if entry_updates.get("set_pending_short_ema_wait"):
                    state_manager.set_pending_short_ema_wait()
                if entry_updates.get("clear_pending_short_ema_wait"):
                    state_manager.clear_pending_short_ema_wait()
                # ADX wait updates
                if entry_updates.get("set_adx_wait_long"):
                    data = entry_updates["set_adx_wait_long"]
                    state_manager.set_adx_wait_long(data["bars"], data["trigger"])
                if entry_updates.get("clear_adx_wait_long"):
                    state_manager.clear_adx_wait_long()
                if entry_updates.get("decrement_adx_wait_long"):
                    state_manager.decrement_adx_wait_long()
                if entry_updates.get("set_adx_wait_short"):
                    data = entry_updates["set_adx_wait_short"]
                    state_manager.set_adx_wait_short(data["bars"], data["trigger"])
                if entry_updates.get("clear_adx_wait_short"):
                    state_manager.clear_adx_wait_short()
                if entry_updates.get("decrement_adx_wait_short"):
                    state_manager.decrement_adx_wait_short()
                if entry_updates.get("set_volume_wait_long"):
                    d = entry_updates["set_volume_wait_long"]
                    state_manager.set_volume_wait_long(d["remaining"], d["trigger"], d["kind"])
                if entry_updates.get("set_volume_wait_short"):
                    d = entry_updates["set_volume_wait_short"]
                    state_manager.set_volume_wait_short(d["remaining"], d["trigger"], d["kind"])
                if entry_updates.get("clear_pending_volume_long"):
                    state_manager.clear_volume_wait_long()
                if entry_updates.get("clear_pending_volume_short"):
                    state_manager.clear_volume_wait_short()
                if entry_updates.get("decrement_volume_wait_long"):
                    state_manager.decrement_volume_wait_long()
                if entry_updates.get("decrement_volume_wait_short"):
                    state_manager.decrement_volume_wait_short()
                if entry_signal:
                    is_long = entry_signal.signal_type == SignalType.BUY
                    print(f"\n🟢 LIVE ENTRY: {'LONG' if is_long else 'SHORT'} @ {entry_signal.price:.2f}")
                    
                    stop_loss, take_profit = signal_engine.calculate_exit_levels(
                        entry_price=entry_signal.price,
                        is_long=is_long
                    )
                    
                    await order_manager.place_bracket_order(
                        action="BUY" if is_long else "SELL",
                        quantity=contracts,
                        take_profit_price=take_profit,
                        stop_loss_price=stop_loss,
                        entry_type="MKT"
                    )
                    
                    state_manager.on_entry(entry_signal, stop_loss, take_profit)
        
        primary_feed.on_bar_close(lambda df, bar: asyncio.create_task(on_bar_close(df, bar)))
        
        await primary_feed.start(initial_lookback_days=15)
        
        print("\n" + "=" * 60)
        print("[OK] 🔴 LIVE TRADING IS ACTIVE - REAL MONEY")
        print("=" * 60)
        print("[OK] Strategy: MNQ EMA Supertrend")
        print(f"[OK] Contract: {contract.localSymbol}")
        print(f"[OK] Contracts per trade: {contracts}")
        print("[OK] Waiting for signals on 10-minute bar closes...")
        print("\nPress Ctrl+C to stop\n")
        
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down LIVE trading...")
        
        await primary_feed.stop()
        await position_tracker.shutdown()
        ib.disconnect()
        print("[OK] LIVE trading stopped")
        
    except Exception as e:
        logger.error(f"LIVE trading error: {e}")
        if ib.isConnected():
            ib.disconnect()
        raise


async def main_async():
    """Main async entry point."""
    print_banner()
    
    # Load config
    config = load_config()
    
    # Show current settings
    strategy_cfg = config.get('strategy', {})
    from utils.strategy_side_config import resolve_side_configs
    _side = resolve_side_configs(strategy_cfg)
    _lst, _sst = _side["long_supertrend"], _side["short_supertrend"]
    _lr, _sr = _side["long_risk"], _side["short_risk"]
    _la, _sa = _side["long_adx"], _side["short_adx"]
    
    print("Current Strategy Settings:")
    print(f"  Long ST:  ATR={_lst.get('atr_length', 10)}, Mult={_lst.get('multiplier', 3)}")
    print(f"  Short ST: ATR={_sst.get('atr_length', 10)}, Mult={_sst.get('multiplier', 3)}")
    print(f"  Long SL/TP: {_lr.get('stop_loss_pct', 0.4)}% / {_lr.get('take_profit_pct', 1.2)}%")
    print(f"  Short SL/TP: {_sr.get('stop_loss_pct', 0.4)}% / {_sr.get('take_profit_pct', 1.2)}%")
    print(f"  ADX long:  thresh={_la.get('threshold', 20)}, wait={_la.get('consecutive_candles', 5)}")
    print(f"  ADX short: thresh={_sa.get('threshold', 20)}, wait={_sa.get('consecutive_candles', 5)}")
    print()
    
    # Get user choice
    choice = get_menu_choice()
    
    if choice == '0':
        print("\nGoodbye!")
        return
    elif choice == '1':
        await run_backtest_selection(config)
    elif choice == '2':
        await run_paper_trading(config)
    elif choice == '3':
        await run_live_trading(config)
    
    print("\n" + "=" * 60)
    print("Session ended. Run 'python main.py' to start again.")
    print("=" * 60)


def main():
    """Main entry point."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise


if __name__ == '__main__':
    main()
