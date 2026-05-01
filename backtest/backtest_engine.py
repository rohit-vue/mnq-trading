"""
=============================================================================
BACKTEST ENGINE
=============================================================================
Candle-by-candle backtesting engine that mirrors Pine Script execution.

Features:
- Bar-close signal evaluation (matches process_orders_on_close)
- Realistic fill simulation
- Commission and slippage modeling
- Comprehensive metrics calculation

Pine Script Reference: Full strategy simulation matching TradingView behavior
=============================================================================
"""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
import logging
import json

from strategy import (
    SignalEngine, Signal, ExitSignal, SignalType, ExitType,
    StateManager, StrategyState, Trade
)
from indicators import get_supertrend_signals, ema_trend_filter, calculate_ema
from .metrics import calculate_metrics, generate_report

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Backtest configuration."""
    # Date range
    start_date: datetime
    end_date: datetime
    
    # Primary bar timeframe for Supertrend (e.g. "10m", "15m", "30m", "1h")
    primary_timeframe: str = "10m"
    
    # Strategy parameters (long/short from strategy.yaml)
    supertrend_atr_long: int = 10
    supertrend_mult_long: float = 3.0
    supertrend_atr_short: int = 10
    supertrend_mult_short: float = 3.0
    supertrend_atr_long_exit: int = 10
    supertrend_mult_long_exit: float = 3.0
    supertrend_atr_short_exit: int = 10
    supertrend_mult_short_exit: float = 3.0
    ema_length: int = 200
    sl_pct_long: float = 0.55
    tp_pct_long: float = 3.0
    sl_pct_short: float = 0.55
    tp_pct_short: float = 3.0
    
    # Contract specs
    tick_size: float = 0.25
    tick_value: float = 0.50
    multiplier: int = 2
    
    # Trading costs
    commission_per_contract: float = 0.62  # Per side
    slippage_ticks: int = 1  # Per side
    
    # Position sizing
    contracts: int = 1
    initial_capital: float = 100000
    
    # Session filter
    session: str = "ALL"  # "RTH", "ETH", "ALL"
    
    use_adx_long: bool = True
    use_adx_short: bool = True
    adx_wait_bars_long: int = 5
    adx_wait_bars_short: int = 5
    adx_threshold_long: float = 20.0
    adx_threshold_short: float = 20.0
    
    # Volume: 20-bar SMA on primary TF; lookahead window includes trigger bar
    volume_check: bool = False
    volume_candle_lookahead: int = 1
    # Run independent long/short books and merge at report layer.
    independent_books: bool = False
    # Internal side filters used by independent-books mode.
    enable_long_entries: bool = True
    enable_short_entries: bool = True


@dataclass
class BacktestResult:
    """Complete backtest results."""
    config: BacktestConfig
    trades: List[Trade]
    equity_curve: pd.Series
    signals_df: pd.DataFrame
    metrics: Dict[str, Any]


class BacktestEngine:
    """
    Backtesting engine with bar-by-bar simulation.
    
    This engine processes bars exactly as the Pine Script would:
    - Evaluate conditions at bar close
    - Apply entries/exits on the next bar open (or same bar close per Pine Script)
    - Track state variables (tradedInBullTrend, tradedInBearTrend)
    - Calculate TP/SL hit detection accurately
    """
    
    def __init__(self, config: BacktestConfig):
        """
        Initialize backtest engine.
        
        Parameters:
        -----------
        config : BacktestConfig
            Backtest configuration
        """
        self.config = config
        
        # Initialize signal engine
        self.signal_engine = SignalEngine(
            sl_pct_long=config.sl_pct_long,
            tp_pct_long=config.tp_pct_long,
            sl_pct_short=config.sl_pct_short,
            tp_pct_short=config.tp_pct_short,
            supertrend_atr_long=config.supertrend_atr_long,
            supertrend_mult_long=config.supertrend_mult_long,
            supertrend_atr_short=config.supertrend_atr_short,
            supertrend_mult_short=config.supertrend_mult_short,
            ema_length=config.ema_length,
            use_adx_long=config.use_adx_long,
            use_adx_short=config.use_adx_short,
            adx_wait_bars_long=config.adx_wait_bars_long,
            adx_wait_bars_short=config.adx_wait_bars_short,
            adx_threshold_long=config.adx_threshold_long,
            adx_threshold_short=config.adx_threshold_short,
            volume_check=config.volume_check,
            volume_candle_lookahead=config.volume_candle_lookahead,
        )
        
        # Initialize state manager (no persistence for backtest)
        self.state_manager = StateManager(
            state_file=None,
            tick_value=config.tick_value,
            contracts_per_trade=config.contracts
        )
        
        # Results storage
        self.trades: List[Trade] = []
        self.equity_history: List[Tuple[pd.Timestamp, float]] = []
        self.signal_log: List[Dict] = []
        self._run_df: Optional[pd.DataFrame] = None
        
        logger.info(f"Backtest Engine initialized: {config.start_date} to {config.end_date}")
    
    def run(self, df_10m: pd.DataFrame) -> BacktestResult:
        """
        Execute backtest on prepared data.
        
        Parameters:
        -----------
        df_10m : pd.DataFrame
            10-minute bars with all indicators pre-calculated.
            Required columns:
            - OHLCV: open, high, low, close, volume
            - Supertrend: supertrend, direction, st_bull, st_bear, st_bull_flip, st_bear_flip
            - EMA (1H mapped): ema_1h, close_1h, ema_bull, ema_bear, ema_bull_cross, ema_bear_cross
            - is_new_1h_candle
        
        Returns:
        --------
        BacktestResult
            Complete backtest results with trades, equity curve, and metrics
        """
        logger.info(f"Starting backtest: {len(df_10m)} bars")
        if self.config.independent_books:
            return self._run_independent_books(df_10m)
        
        # Reset state
        self.state_manager.state = StrategyState()
        self.trades = []
        self.equity_history = []
        self.signal_log = []
        self._run_df = df_10m
        
        # Initial equity
        equity = self.config.initial_capital
        
        # Iterate through bars
        for i in range(len(df_10m)):
            bar = df_10m.iloc[i]
            timestamp = bar.name
            
            # Skip if before start date
            if timestamp < self.config.start_date:
                continue
            
            # Stop if after end date
            if timestamp > self.config.end_date:
                break

            # =========================================================
            # CONTRACT ROLLOVER HANDLING (Databento stitched datasets)
            # - Exit any open trade at end of previous contract bar
            # - Reset pending state so new contract starts clean
            # =========================================================
            if i > 0:
                prev_bar = df_10m.iloc[i - 1]
                prev_contract = self._get_contract_label(prev_bar)
                curr_contract = self._get_contract_label(bar)
                if (prev_contract is not None and curr_contract is not None and
                        prev_contract != curr_contract):
                    state = self.state_manager.state
                    if state.position_size != 0:
                        roll_exit = ExitSignal(
                            exit_type=ExitType.CONTRACT_ROLL,
                            timestamp=prev_bar.name,
                            exit_price=float(prev_bar['close']),
                            entry_price=state.entry_price,
                            pnl_points=0.0,
                        )
                        trade = self._process_exit(roll_exit, prev_bar)
                        if trade:
                            pnl = trade.pnl_dollars - self._calculate_costs()
                            equity += pnl
                            self.equity_history.append((prev_bar.name, equity))
                    # Reset waits/flags for new contract start.
                    self.state_manager.state.traded_in_bull_trend = False
                    self.state_manager.state.traded_in_bear_trend = False
                    self.state_manager.clear_pending_long_ema_wait()
                    self.state_manager.clear_pending_short_ema_wait()
                    self.state_manager.clear_adx_wait_long()
                    self.state_manager.clear_adx_wait_short()
                    self.state_manager.clear_volume_wait_long()
                    self.state_manager.clear_volume_wait_short()
            
            # =========================================================
            # STEP 1: Update Supertrend state (flag resets)
            # Pine Script lines 64-67
            # =========================================================
            if self.config.enable_long_entries and self.config.enable_short_entries:
                st_bull_flip = bar.get('st_bull_flip', False)
                st_bear_flip = bar.get('st_bear_flip', False)
            elif self.config.enable_long_entries:
                st_bull_flip = bar.get('st_bull_flip_long', bar.get('st_bull_flip', False))
                st_bear_flip = bar.get('st_bear_flip_long', bar.get('st_bear_flip', False))
            else:
                st_bull_flip = bar.get('st_bull_flip_short', bar.get('st_bull_flip', False))
                st_bear_flip = bar.get('st_bear_flip_short', bar.get('st_bear_flip', False))
            
            # Log supertrend flips with detailed information
            if st_bull_flip or st_bear_flip:
                ema_1h = bar.get('ema_1h', np.nan)
                close_1h = bar.get('close_1h', np.nan)
                direction = "BULLISH" if st_bull_flip else "BEARISH"
                ema_str = f"{ema_1h:.2f}" if not pd.isna(ema_1h) else "N/A"
                close_str = f"{close_1h:.2f}" if not pd.isna(close_1h) else "N/A"
                timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f"=== SUPERTREND FLIP {direction} === DateTime: {timestamp_str}, EMA200: {ema_str}, 1H Close: {close_str}")
            
            self.state_manager.update_supertrend_state(
                st_bull_flip=st_bull_flip,
                st_bear_flip=st_bear_flip,
                current_direction=bar.get('direction', 0)
            )
            
            # =========================================================
            # STEP 2: Check exits if in position
            # Pine Script lines 115-123 (TP/SL), 131-135 (ST flip)
            # =========================================================
            state = self.state_manager.state
            
            if state.position_size != 0:
                exit_signal = self.signal_engine.check_exit_conditions(
                    bar=bar,
                    position_size=state.position_size,
                    entry_price=state.entry_price,
                    stop_loss=state.stop_loss,
                    take_profit=state.take_profit,
                    entry_time=state.entry_time
                )
                
                if exit_signal:
                    # Log exit with detailed information
                    exit_timestamp_str = exit_signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    exit_type = exit_signal.exit_type.value
                    direction = "LONG" if state.position_size > 0 else "SHORT"
                    logger.info(f"=== EXIT {direction} === Type: {exit_type}, DateTime: {exit_timestamp_str}, Exit Price: {exit_signal.exit_price:.2f}, Entry Price: {state.entry_price:.2f}, P&L: {exit_signal.pnl_points:+.2f} pts")
                    
                    trade = self._process_exit(exit_signal, bar)
                    if trade:
                        # Update equity
                        pnl = trade.pnl_dollars - self._calculate_costs()
                        equity += pnl
                        self.equity_history.append((timestamp, equity))
            
            # =========================================================
            # STEP 3: Check entries if flat
            # Pine Script lines 98-99 (conditions), 104-110 (execution)
            # =========================================================
            state = self.state_manager.state  # Refresh after potential exit
            
            if state.position_size == 0:
                vw = self.signal_engine.volume_confirmation_window_bars()
                vol_slice = df_10m.iloc[i : min(i + vw, len(df_10m))]
                entry_signal, entry_updates = self.signal_engine.evaluate_entry_conditions(
                    bar=bar,
                    position_size=0,
                    traded_in_bull_trend=(
                        state.traded_in_bull_trend if self.config.enable_long_entries else True
                    ),
                    traded_in_bear_trend=(
                        state.traded_in_bear_trend if self.config.enable_short_entries else True
                    ),
                    pending_long_ema_wait=(
                        state.pending_long_ema_wait if self.config.enable_long_entries else False
                    ),
                    pending_short_ema_wait=(
                        state.pending_short_ema_wait if self.config.enable_short_entries else False
                    ),
                    pending_adx_long=(
                        state.pending_adx_long if self.config.enable_long_entries else False
                    ),
                    pending_adx_short=(
                        state.pending_adx_short if self.config.enable_short_entries else False
                    ),
                    adx_wait_bars_left_long=(
                        state.adx_wait_bars_left_long if self.config.enable_long_entries else 0
                    ),
                    adx_wait_bars_left_short=(
                        state.adx_wait_bars_left_short if self.config.enable_short_entries else 0
                    ),
                    adx_wait_trigger_long=(
                        state.adx_wait_trigger_long if self.config.enable_long_entries else ''
                    ),
                    adx_wait_trigger_short=(
                        state.adx_wait_trigger_short if self.config.enable_short_entries else ''
                    ),
                    volume_window=vol_slice,
                    allow_volume_defer=False,
                    pending_volume_long=(
                        state.pending_volume_long if self.config.enable_long_entries else False
                    ),
                    pending_volume_short=(
                        state.pending_volume_short if self.config.enable_short_entries else False
                    ),
                    volume_wait_bars_left_long=(
                        state.volume_wait_bars_left_long if self.config.enable_long_entries else 0
                    ),
                    volume_wait_bars_left_short=(
                        state.volume_wait_bars_left_short if self.config.enable_short_entries else 0
                    ),
                    volume_wait_trigger_long=(
                        state.volume_wait_trigger_long if self.config.enable_long_entries else ''
                    ),
                    volume_wait_trigger_short=(
                        state.volume_wait_trigger_short if self.config.enable_short_entries else ''
                    ),
                    volume_wait_kind_long=(
                        state.volume_wait_kind_long if self.config.enable_long_entries else ''
                    ),
                    volume_wait_kind_short=(
                        state.volume_wait_kind_short if self.config.enable_short_entries else ''
                    )
                )
                # Apply state updates from signal engine
                if self.config.enable_long_entries and entry_updates.get("set_pending_long_ema_wait"):
                    self.state_manager.set_pending_long_ema_wait()
                if self.config.enable_long_entries and entry_updates.get("clear_pending_long_ema_wait"):
                    self.state_manager.clear_pending_long_ema_wait()
                if self.config.enable_short_entries and entry_updates.get("set_pending_short_ema_wait"):
                    self.state_manager.set_pending_short_ema_wait()
                if self.config.enable_short_entries and entry_updates.get("clear_pending_short_ema_wait"):
                    self.state_manager.clear_pending_short_ema_wait()
                # ADX wait updates
                if self.config.enable_long_entries and entry_updates.get("set_adx_wait_long"):
                    data = entry_updates["set_adx_wait_long"]
                    self.state_manager.set_adx_wait_long(data["bars"], data["trigger"])
                if self.config.enable_long_entries and entry_updates.get("clear_adx_wait_long"):
                    self.state_manager.clear_adx_wait_long()
                if self.config.enable_long_entries and entry_updates.get("decrement_adx_wait_long"):
                    self.state_manager.decrement_adx_wait_long()
                if self.config.enable_short_entries and entry_updates.get("set_adx_wait_short"):
                    data = entry_updates["set_adx_wait_short"]
                    self.state_manager.set_adx_wait_short(data["bars"], data["trigger"])
                if self.config.enable_short_entries and entry_updates.get("clear_adx_wait_short"):
                    self.state_manager.clear_adx_wait_short()
                if self.config.enable_short_entries and entry_updates.get("decrement_adx_wait_short"):
                    self.state_manager.decrement_adx_wait_short()
                if self.config.enable_long_entries and entry_updates.get("set_volume_wait_long"):
                    d = entry_updates["set_volume_wait_long"]
                    self.state_manager.set_volume_wait_long(
                        d["remaining"], d["trigger"], d["kind"]
                    )
                if self.config.enable_short_entries and entry_updates.get("set_volume_wait_short"):
                    d = entry_updates["set_volume_wait_short"]
                    self.state_manager.set_volume_wait_short(
                        d["remaining"], d["trigger"], d["kind"]
                    )
                if self.config.enable_long_entries and entry_updates.get("clear_pending_volume_long"):
                    self.state_manager.clear_volume_wait_long()
                if self.config.enable_short_entries and entry_updates.get("clear_pending_volume_short"):
                    self.state_manager.clear_volume_wait_short()
                if self.config.enable_long_entries and entry_updates.get("decrement_volume_wait_long"):
                    self.state_manager.decrement_volume_wait_long()
                if self.config.enable_short_entries and entry_updates.get("decrement_volume_wait_short"):
                    self.state_manager.decrement_volume_wait_short()
                if entry_signal:
                    if (
                        (entry_signal.signal_type == SignalType.BUY and not self.config.enable_long_entries)
                        or (entry_signal.signal_type == SignalType.SELL and not self.config.enable_short_entries)
                    ):
                        entry_signal = None
                if entry_signal:
                    # Log entry with detailed information
                    entry_timestamp_str = entry_signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    entry_type = entry_signal.trigger
                    ema_1h = entry_signal.ema_1h
                    close_1h = entry_signal.close_1h
                    direction = entry_signal.signal_type.value.upper()
                    ema_str = f"{ema_1h:.2f}" if not pd.isna(ema_1h) else "N/A"
                    close_str = f"{close_1h:.2f}" if not pd.isna(close_1h) else "N/A"
                    logger.info(f"=== ENTRY {direction} === Type: {entry_type}, DateTime: {entry_timestamp_str}, Entry Price: {entry_signal.price:.2f}, EMA200: {ema_str}, 1H Close: {close_str}")
                    
                    self._process_entry(entry_signal)
                    self.signal_log.append({
                        'timestamp': timestamp,
                        'signal': entry_signal.signal_type.value,
                        'price': entry_signal.price,
                        'trigger': entry_signal.trigger
                    })
            
            # Record equity even if no trade
            if not self.equity_history or self.equity_history[-1][0] != timestamp:
                # Update equity with any unrealized P&L
                if state.position_size != 0:
                    unrealized = self._calculate_unrealized(state, bar['close'])
                    self.equity_history.append((timestamp, equity + unrealized))
                else:
                    self.equity_history.append((timestamp, equity))
        
        # =========================================================
        # STEP 4: Close any open position at end of backtest
        # =========================================================
        if self.state_manager.state.position_size != 0:
            final_bar = df_10m.iloc[-1]
            exit_signal = ExitSignal(
                exit_type=ExitType.ST_FLIP,  # Mark as forced close
                timestamp=final_bar.name,
                exit_price=final_bar['close'],
                entry_price=self.state_manager.state.entry_price,
                pnl_points=0  # Will be calculated
            )
            trade = self._process_exit(exit_signal, final_bar)
            if trade:
                pnl = trade.pnl_dollars - self._calculate_costs()
                equity += pnl
                self.equity_history.append((final_bar.name, equity))
        
        # =========================================================
        # STEP 5: Calculate metrics and build result
        # =========================================================
        equity_curve = pd.Series(
            data=[e[1] for e in self.equity_history],
            index=[e[0] for e in self.equity_history],
            name='equity'
        )
        
        signals_df = pd.DataFrame(self.signal_log)
        
        metrics = calculate_metrics(
            trades=self.trades,
            equity_curve=equity_curve,
            initial_capital=self.config.initial_capital,
            multiplier=self.config.multiplier
        )
        
        result = BacktestResult(
            config=self.config,
            trades=self.trades,
            equity_curve=equity_curve,
            signals_df=signals_df,
            metrics=metrics
        )
        
        logger.info(f"Backtest complete: {len(self.trades)} trades, "
                   f"Final equity: ${equity:,.2f}")
        
        return result

    def _run_independent_books(self, df_10m: pd.DataFrame) -> BacktestResult:
        """
        Run long-only and short-only passes independently, then merge outputs.
        This keeps long trade generation invariant to short-side tuning.
        """
        base = replace(
            self.config,
            independent_books=False,
        )
        long_engine = BacktestEngine(
            replace(base, enable_long_entries=True, enable_short_entries=False)
        )
        short_engine = BacktestEngine(
            replace(base, enable_long_entries=False, enable_short_entries=True)
        )
        long_res = long_engine.run(df_10m)
        short_res = short_engine.run(df_10m)

        merged_trades = sorted(
            [*long_res.trades, *short_res.trades],
            key=lambda t: (t.exit_time or t.entry_time, t.trade_id),
        )
        for i, trade in enumerate(merged_trades, 1):
            trade.trade_id = i

        long_eq_raw = long_res.equity_curve.groupby(level=0).last()
        short_eq_raw = short_res.equity_curve.groupby(level=0).last()
        long_eq = long_eq_raw.reindex(df_10m.index).ffill().fillna(self.config.initial_capital)
        short_eq = short_eq_raw.reindex(df_10m.index).ffill().fillna(self.config.initial_capital)
        merged_eq = (long_eq + short_eq) - self.config.initial_capital
        merged_eq.name = "equity"

        signals_df = pd.concat([long_res.signals_df, short_res.signals_df], ignore_index=True)
        if not signals_df.empty and "timestamp" in signals_df.columns:
            signals_df = signals_df.sort_values("timestamp").reset_index(drop=True)

        metrics = calculate_metrics(
            trades=merged_trades,
            equity_curve=merged_eq,
            initial_capital=self.config.initial_capital,
            multiplier=self.config.multiplier,
        )

        return BacktestResult(
            config=self.config,
            trades=merged_trades,
            equity_curve=merged_eq,
            signals_df=signals_df,
            metrics=metrics,
        )
    
    def _process_entry(self, signal: Signal) -> None:
        """
        Process entry signal.
        
        Parameters:
        -----------
        signal : Signal
            Entry signal from signal engine
        """
        # Apply slippage
        slippage = self.config.slippage_ticks * self.config.tick_size
        if signal.signal_type == SignalType.BUY:
            fill_price = signal.price + slippage
        else:
            fill_price = signal.price - slippage
        
        # Calculate exit levels
        stop_loss, take_profit = self.signal_engine.calculate_exit_levels(
            entry_price=fill_price,
            is_long=(signal.signal_type == SignalType.BUY)
        )
        
        # Update signal with fill price
        signal.price = fill_price
        
        # Update state
        self.state_manager.on_entry(signal, stop_loss, take_profit)
        
        logger.debug(f"Entry: {signal.signal_type.value} @ {fill_price:.2f}, "
                    f"SL={stop_loss:.2f}, TP={take_profit:.2f}")
    
    def _process_exit(self, exit_signal: ExitSignal, bar: pd.Series) -> Optional[Trade]:
        """
        Process exit signal.
        
        Parameters:
        -----------
        exit_signal : ExitSignal
            Exit signal from signal engine
        bar : pd.Series
            Current bar data
        
        Returns:
        --------
        Optional[Trade]
            Completed trade record
        """
        # Apply slippage for market exits (ST flip)
        if exit_signal.exit_type == ExitType.ST_FLIP:
            slippage = self.config.slippage_ticks * self.config.tick_size
            is_long = self.state_manager.state.position_size > 0
            if is_long:
                exit_signal.exit_price = bar['close'] - slippage  # Selling
            else:
                exit_signal.exit_price = bar['close'] + slippage  # Buying to cover
            
            # Recalculate P&L
            if is_long:
                exit_signal.pnl_points = exit_signal.exit_price - exit_signal.entry_price
            else:
                exit_signal.pnl_points = exit_signal.entry_price - exit_signal.exit_price
        
        # Update state and get trade record
        trade = self.state_manager.on_exit(exit_signal)
        
        if trade:
            self._populate_trade_excursions(trade)
            self.trades.append(trade)
        
        return trade

    @staticmethod
    def _get_contract_label(bar: pd.Series) -> Optional[str]:
        """
        Return contract label if present in bar.
        Supports both 'contract_symbol' and legacy 'symbol' column names.
        """
        for col in ("contract_symbol", "symbol"):
            val = bar.get(col, None)
            if isinstance(val, str) and val:
                return val
        return None
    
    def _populate_trade_excursions(self, trade: Trade) -> None:
        """
        Populate max favorable/adverse excursion stats for a completed trade.
        These are logging metrics only and do not affect strategy execution.
        """
        if self._run_df is None or trade.entry_time is None or trade.exit_time is None:
            return
        if trade.entry_price is None or trade.entry_price == 0:
            return
        # Use full trade lifecycle (entry bar through exit bar inclusive)
        trade_slice = self._run_df.loc[trade.entry_time:trade.exit_time]
        if trade_slice is None or trade_slice.empty:
            return
        highest = float(trade_slice['high'].max())
        lowest = float(trade_slice['low'].min())
        entry = float(trade.entry_price)
        if trade.direction == 'long':
            max_pos_pts = highest - entry
            max_neg_pts = lowest - entry
        else:
            max_pos_pts = entry - lowest
            max_neg_pts = entry - highest
        trade.max_positive_points = max_pos_pts
        trade.max_negative_points = max_neg_pts
        trade.max_positive_pct = (max_pos_pts / entry) * 100.0
        trade.max_negative_pct = (max_neg_pts / entry) * 100.0
    
    def _calculate_costs(self) -> float:
        """Calculate round-trip trading costs."""
        # Commission: entry + exit
        commission = 2 * self.config.commission_per_contract * self.config.contracts
        
        return commission
    
    def _calculate_unrealized(self, state: StrategyState, current_price: float) -> float:
        """Calculate unrealized P&L for equity tracking."""
        if state.position_size == 0:
            return 0.0
        
        if state.position_size > 0:  # Long
            pnl_points = current_price - state.entry_price
        else:  # Short
            pnl_points = state.entry_price - current_price
        
        return pnl_points * self.config.multiplier * abs(state.position_size)
    
    def get_trade_summary(self) -> pd.DataFrame:
        """
        Get trades as DataFrame for analysis.
        
        Returns:
        --------
        pd.DataFrame
            Trade summary
        """
        if not self.trades:
            return pd.DataFrame()
        
        data = []
        for trade in self.trades:
            data.append({
                'trade_id': trade.trade_id,
                'direction': trade.direction,
                'entry_time': trade.entry_time,
                'entry_price': trade.entry_price,
                'exit_time': trade.exit_time,
                'exit_price': trade.exit_price,
                'signal_type': getattr(trade, 'entry_trigger', None) or '',
                'exit_type': trade.exit_type,
                'pnl_points': trade.pnl_points,
                'pnl_dollars': trade.pnl_dollars,
                'contracts': trade.contracts,
                'ema_1h_at_entry': getattr(trade, 'ema_1h_at_entry', None),
                'volume_at_entry': getattr(trade, 'volume_at_entry', None),
                'volume_ma_at_entry': getattr(trade, 'volume_ma_at_entry', None),
                'max_positive_points': getattr(trade, 'max_positive_points', None),
                'max_negative_points': getattr(trade, 'max_negative_points', None),
                'max_positive_pct': getattr(trade, 'max_positive_pct', None),
                'max_negative_pct': getattr(trade, 'max_negative_pct', None),
            })
        
        df = pd.DataFrame(data)
        # Output formatting only: keep internal calculations untouched, but
        # present trade-level numeric fields with 2 decimal places in CSV.
        if not df.empty:
            numeric_cols = [
                'entry_price', 'exit_price', 'pnl_points', 'pnl_dollars',
                'ema_1h_at_entry', 'volume_at_entry', 'volume_ma_at_entry',
                'max_positive_points', 'max_negative_points',
                'max_positive_pct', 'max_negative_pct',
            ]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').round(2)
        
        return df


def run_backtest(
    df_10m: pd.DataFrame,
    start_date: datetime,
    end_date: datetime,
    **kwargs
) -> BacktestResult:
    """
    Convenience function to run a backtest.
    
    Parameters:
    -----------
    df_10m : pd.DataFrame
        Prepared 10-minute data with indicators
    start_date : datetime
        Backtest start
    end_date : datetime
        Backtest end
    **kwargs
        Additional config parameters
    
    Returns:
    --------
    BacktestResult
        Complete results
    """
    config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        **kwargs
    )
    
    engine = BacktestEngine(config)
    return engine.run(df_10m)
