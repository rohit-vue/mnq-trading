"""
Quick script to run Databento backtest with same parameters as IBKR.
"""
import asyncio
import sys
import os
from datetime import datetime
from pathlib import Path

# Fix Unicode output on Windows (avoids cp1252 encoding errors for log messages)
if sys.platform == 'win32':
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    # Reconfigure logging handlers to use utf-8
    import logging
    for handler in logging.root.handlers[:]:
        if isinstance(handler, logging.StreamHandler) and hasattr(handler.stream, 'reconfigure'):
            handler.stream.reconfigure(encoding='utf-8', errors='replace')

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

async def main():
    from main import load_config, run_databento_backtest
    from timeframe_utils import get_primary_resample_rule, get_primary_timeframe
    
    # Load config
    config = load_config()
    
    # Override with specific dates matching IBKR backtest
    # The run_databento_backtest function expects interactive input,
    # so we'll create a modified version
    
    from data.databento_loader import (
        get_available_date_range, 
        prepare_databento_for_backtest,
        DATABENTO_DIR
    )
    from backtest import BacktestEngine, BacktestConfig
    from utils.strategy_side_config import signal_engine_kwargs
    
    # Set dates matching IBKR backtest
    start_date = datetime(2025, 1, 1)
    end_date = datetime(2025, 12, 31)
    contracts = 1
    
    print("=" * 60)
    print("       DATABENTO BACKTEST - FIXED ROLLOVER SCHEDULE")
    print("=" * 60)
    print(f"\nPeriod: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Contracts: {contracts}")
    print(f"Data source: Databento CSV (Fixed Rollover Schedule)")
    
    # Get all config sections (matching IBKR backtest exactly)
    strategy_cfg = config.get('strategy', {})
    risk_cfg = config.get('risk', {})
    contract_cfg = config.get('mnq_contract', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    adx_cfg = strategy_cfg.get('adx', {})
    risk_params = strategy_cfg.get('risk', {})
    
    print("\n" + "-" * 60)
    print("Loading and processing Databento data...")
    print("-" * 60)
    
    # Get EMA overlap margin configuration
    ema_overlap_margin_pct = ema_cfg.get('overlap_margin_pct', 0.1)
    volume_ma_period = max(1, int(strategy_cfg.get('volume_ma_period', 20)))
    primary_resample = get_primary_resample_rule(strategy_cfg)
    
    # Load and prepare data
    df_10m = prepare_databento_for_backtest(
        start_date=start_date,
        end_date=end_date,
        symbol_filter="MNQ",
        data_dir=DATABENTO_DIR,
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
        ema_overlap_margin_pct=ema_overlap_margin_pct,
        zone_check_1h_margin_pct=ema_cfg.get('zone_check_1h_margin_pct', 0.1),
        zone_check_10m_margin_pct=ema_cfg.get('zone_check_10m_margin_pct', 0.15)
    )
    
    if len(df_10m) == 0:
        print("[X] Error: No data available for the requested period")
        return
    
    # Create backtest config - matching IBKR exactly
    # Note: Added extra slippage (4 ticks total vs IBKR's 1 tick) to calibrate for
    # data source differences. Databento raw data differs from IBKR's adjusted
    # continuous contract, so we add slippage to account for this variance.
    # This brings Databento results ~150-200 points below IBKR as a conservative estimate.
    DATABENTO_SLIPPAGE_CALIBRATION = 1  # Match IBKR slippage (1 tick = 0.25)
    
    bt_config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        primary_timeframe=get_primary_timeframe(strategy_cfg),
        ema_length=ema_cfg.get('length', 200),
        tick_size=contract_cfg.get('contract', {}).get('tick_size', 0.25),
        tick_value=contract_cfg.get('contract', {}).get('tick_value', 0.50),
        multiplier=contract_cfg.get('contract', {}).get('multiplier', 2),
        commission_per_contract=risk_cfg.get('backtesting', {}).get('commission_per_contract', 0.62),
        slippage_ticks=DATABENTO_SLIPPAGE_CALIBRATION,  # Calibrated for data source difference
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
    print("           DATABENTO BACKTEST RESULTS (FIXED)")
    print("=" * 60)
    
    print(f"\nPeriod: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Total Bars: {len(df_10m):,}")
    print(f"Data Source: Databento CSV (Fixed Rollover Schedule)\n")
    
    # Generate and print report
    from backtest.metrics import generate_report
    
    # Save results
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    results_dir = Path("backtest/results")
    results_dir.mkdir(parents=True, exist_ok=True)
    
    results_file = results_dir / f"databento_backtest_fixed_{timestamp}.csv"
    
    summary = generate_report(results, output_path=str(results_file), console_only=True)
    print(summary)
    
    print(f"\n[OK] Results saved to: {results_file}")
    
    # Print comparison with IBKR
    print("\n" + "=" * 60)
    print("           COMPARISON WITH IBKR RESULTS")
    print("=" * 60)
    print("\nIBKR Results (reference):")
    print("  - Net Profit/Loss: $15,480.97")
    print("  - Total Trades: 448")
    print("  - Win Rate: 40.2%")
    print("  - Profit Factor: 1.50")
    
    metrics = results.metrics
    print(f"\nDatabento Results (fixed rollover):")
    print(f"  - Net Profit/Loss: ${metrics.get('net_profit', 0):,.2f}")
    print(f"  - Total Trades: {metrics.get('total_trades', 0)}")
    print(f"  - Win Rate: {metrics.get('win_rate', 0):.1f}%")
    print(f"  - Profit Factor: {metrics.get('profit_factor', 0):.2f}")

if __name__ == "__main__":
    asyncio.run(main())
