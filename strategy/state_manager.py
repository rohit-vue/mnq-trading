"""
=============================================================================
STATE MANAGER
=============================================================================
Manages the strategy's state machine including:
- Position tracking
- Trade flags (tradedInBullTrend, tradedInBearTrend)
- Flag reset logic on Supertrend flips
- Pending EMA cross wait state after unaligned ST flips

Pine Script Reference:
- Lines 59-60: var bool tradedInBullTrend, tradedInBearTrend
- Lines 64-67: Reset logic on ST flip
- Lines 104-110: Flag set on entry
=============================================================================
"""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)


class PositionSide(Enum):
    """Position side enum."""
    FLAT = 0
    LONG = 1
    SHORT = -1


@dataclass
class StrategyState:
    """
    Strategy state container.
    
    This mirrors the Pine Script's `var` variables that persist across bars.
    
    Pine Script Reference:
    - Lines 59-60: var bool tradedInBullTrend = false
                   var bool tradedInBearTrend = false
    """
    
    # Position state
    position_size: int = 0  # 0=flat, 1=long, -1=short
    entry_price: float = 0.0
    entry_time: Optional[pd.Timestamp] = None
    
    # Exit levels (set on entry)
    stop_loss: float = 0.0
    take_profit: float = 0.0
    
    # =========================================================================
    # CRITICAL: Trade flags for re-entry blocking
    # Pine Script lines 59-60:
    #   var bool tradedInBullTrend = false
    #   var bool tradedInBearTrend = false
    # =========================================================================
    traded_in_bull_trend: bool = False
    traded_in_bear_trend: bool = False
    
    # Previous Supertrend direction for flip detection
    prev_st_direction: int = 0  # -1=bull, +1=bear
    
    # =========================================================================
    # Pending EMA cross wait: after an ST flip where the 1H candle is NOT
    # fully aligned with EMA200, we wait for a subsequent 1H Close to cross
    # above/below EMA200 (with ADX >= threshold) before entering.
    # Cancelled automatically if another ST flip occurs.
    # =========================================================================
    pending_long_ema_wait: bool = False   # Waiting for bullish EMA cross
    pending_short_ema_wait: bool = False  # Waiting for bearish EMA cross
    
    # =========================================================================
    # Pending ADX confirmation wait: after an ST flip (aligned) or EMA cross
    # where all conditions are met EXCEPT ADX < threshold, the system waits
    # up to 5 bars for ADX to rise above the threshold.
    # Cancelled automatically if another ST flip occurs.
    # =========================================================================
    pending_adx_long: bool = False        # Waiting for ADX confirmation for long
    pending_adx_short: bool = False       # Waiting for ADX confirmation for short
    adx_wait_bars_left_long: int = 0      # Bars remaining in ADX check window
    adx_wait_bars_left_short: int = 0     # Bars remaining in ADX check window
    adx_wait_trigger_long: str = ''       # Original trigger: 'st_flip' or 'ema_cross'
    adx_wait_trigger_short: str = ''      # Original trigger: 'st_flip' or 'ema_cross'
    
    # Pending volume confirmation (streaming): trigger bar failed volume; wait up to
    # volume_wait_bars_left_* more primary bars (see SignalEngine).
    pending_volume_long: bool = False
    pending_volume_short: bool = False
    volume_wait_bars_left_long: int = 0
    volume_wait_bars_left_short: int = 0
    volume_wait_trigger_long: str = ''
    volume_wait_trigger_short: str = ''
    # '' = st_flip only; 'ema' / 'adx' -> clear matching pending on confirm or expiry
    volume_wait_kind_long: str = ''
    volume_wait_kind_short: str = ''
    
    # Trade history
    trade_count: int = 0
    
    # Daily tracking
    daily_trades: int = 0
    daily_pnl: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert state to dictionary for serialization."""
        return {
            'position_size': self.position_size,
            'entry_price': self.entry_price,
            'entry_time': str(self.entry_time) if self.entry_time else None,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'traded_in_bull_trend': self.traded_in_bull_trend,
            'traded_in_bear_trend': self.traded_in_bear_trend,
            'prev_st_direction': self.prev_st_direction,
            'pending_long_ema_wait': self.pending_long_ema_wait,
            'pending_short_ema_wait': self.pending_short_ema_wait,
            'pending_adx_long': self.pending_adx_long,
            'pending_adx_short': self.pending_adx_short,
            'adx_wait_bars_left_long': self.adx_wait_bars_left_long,
            'adx_wait_bars_left_short': self.adx_wait_bars_left_short,
            'adx_wait_trigger_long': self.adx_wait_trigger_long,
            'adx_wait_trigger_short': self.adx_wait_trigger_short,
            'pending_volume_long': self.pending_volume_long,
            'pending_volume_short': self.pending_volume_short,
            'volume_wait_bars_left_long': self.volume_wait_bars_left_long,
            'volume_wait_bars_left_short': self.volume_wait_bars_left_short,
            'volume_wait_trigger_long': self.volume_wait_trigger_long,
            'volume_wait_trigger_short': self.volume_wait_trigger_short,
            'volume_wait_kind_long': self.volume_wait_kind_long,
            'volume_wait_kind_short': self.volume_wait_kind_short,
            'trade_count': self.trade_count,
            'daily_trades': self.daily_trades,
            'daily_pnl': self.daily_pnl
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StrategyState':
        """Create state from dictionary."""
        state = cls()
        state.position_size = data.get('position_size', 0)
        state.entry_price = data.get('entry_price', 0.0)
        
        entry_time = data.get('entry_time')
        state.entry_time = pd.Timestamp(entry_time) if entry_time else None
        
        state.stop_loss = data.get('stop_loss', 0.0)
        state.take_profit = data.get('take_profit', 0.0)
        state.traded_in_bull_trend = data.get('traded_in_bull_trend', False)
        state.traded_in_bear_trend = data.get('traded_in_bear_trend', False)
        state.prev_st_direction = data.get('prev_st_direction', 0)
        state.pending_long_ema_wait = data.get('pending_long_ema_wait', False)
        state.pending_short_ema_wait = data.get('pending_short_ema_wait', False)
        state.pending_adx_long = data.get('pending_adx_long', False)
        state.pending_adx_short = data.get('pending_adx_short', False)
        state.adx_wait_bars_left_long = data.get('adx_wait_bars_left_long', 0)
        state.adx_wait_bars_left_short = data.get('adx_wait_bars_left_short', 0)
        state.adx_wait_trigger_long = data.get('adx_wait_trigger_long', '')
        state.adx_wait_trigger_short = data.get('adx_wait_trigger_short', '')
        state.pending_volume_long = data.get('pending_volume_long', False)
        state.pending_volume_short = data.get('pending_volume_short', False)
        state.volume_wait_bars_left_long = data.get('volume_wait_bars_left_long', 0)
        state.volume_wait_bars_left_short = data.get('volume_wait_bars_left_short', 0)
        state.volume_wait_trigger_long = data.get('volume_wait_trigger_long', '')
        state.volume_wait_trigger_short = data.get('volume_wait_trigger_short', '')
        state.volume_wait_kind_long = data.get('volume_wait_kind_long', '')
        state.volume_wait_kind_short = data.get('volume_wait_kind_short', '')
        state.trade_count = data.get('trade_count', 0)
        state.daily_trades = data.get('daily_trades', 0)
        state.daily_pnl = data.get('daily_pnl', 0.0)
        
        return state


@dataclass
class Trade:
    """Individual trade record."""
    trade_id: int
    direction: str  # 'long' or 'short'
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_type: Optional[str] = None  # 'tp', 'sl', 'st_flip'
    pnl_points: Optional[float] = None
    pnl_dollars: Optional[float] = None
    contracts: int = 1
    entry_trigger: Optional[str] = None  # e.g. 'st_flip', 'ema_cross'
    ema_1h_at_entry: Optional[float] = None  # EMA200 value at entry time for verification
    volume_at_entry: Optional[float] = None
    volume_ma_at_entry: Optional[float] = None
    max_positive_points: Optional[float] = None
    max_negative_points: Optional[float] = None
    max_positive_pct: Optional[float] = None
    max_negative_pct: Optional[float] = None


class StateManager:
    """
    Manages strategy state machine with proper flag handling.
    
    Key Responsibilities:
    1. Track position state
    2. Handle trade flags for re-entry blocking
    3. Reset flags ONLY on Supertrend flips
    4. Manage pending EMA cross wait state
    5. Persist state for recovery
    
    Pine Script Logic Mapped:
    -------------------------
    Lines 64-67 (flag reset):
        if stBullFlip
            tradedInBullTrend := false
        if stBearFlip
            tradedInBearTrend := false
    
    Lines 104-110 (flag set on entry):
        if buyCond
            strategy.entry("BUY", strategy.long)
            tradedInBullTrend := true
        if sellCond
            strategy.entry("SELL", strategy.short)
            tradedInBearTrend := true
    """
    
    def __init__(
        self,
        state_file: Optional[str] = None,
        tick_value: float = 0.50,
        contracts_per_trade: int = 1
    ):
        """
        Initialize state manager.
        
        Parameters:
        -----------
        state_file : str, optional
            Path to state persistence file
        tick_value : float
            Value per tick for MNQ ($0.50)
        contracts_per_trade : int
            Number of contracts per trade
        """
        self.state = StrategyState()
        self.state_file = Path(state_file) if state_file else None
        self.tick_value = tick_value
        self.contracts = contracts_per_trade
        
        self.trades: List[Trade] = []
        
        # Load existing state if available
        if self.state_file and self.state_file.exists():
            self.load_state()
    
    def update_supertrend_state(
        self,
        st_bull_flip: bool,
        st_bear_flip: bool,
        current_direction: int
    ) -> None:
        """
        Update state based on Supertrend changes.
        
        CRITICAL: This implements the flag reset logic from Pine Script
        AND the cancellation rule for pending EMA waits.
        
        Pine Script Reference (lines 64-67):
            if stBullFlip
                tradedInBullTrend := false
            if stBearFlip
                tradedInBearTrend := false
        
        Cancellation Rule:
            If another ST flip occurs while waiting for an EMA cross,
            the pending setup is cancelled immediately. The system
            then evaluates the new ST flip.
        
        Parameters:
        -----------
        st_bull_flip : bool
            True if Supertrend just flipped to bullish
        st_bear_flip : bool
            True if Supertrend just flipped to bearish
        current_direction : int
            Current ST direction (-1=bull, +1=bear)
        """
        if st_bull_flip:
            logger.info("ST flipped BULLISH - resetting trade flags, cancelling pending waits")
            self.state.traded_in_bull_trend = False
            self.state.traded_in_bear_trend = False
            # Cancel any pending short EMA wait (opposite direction)
            self.state.pending_short_ema_wait = False
            # Cancel ALL pending ADX waits (new flip re-evaluates from scratch)
            self._clear_adx_wait_long_internal()
            self._clear_adx_wait_short_internal()
            self._clear_volume_wait_internal()
        
        if st_bear_flip:
            logger.info("ST flipped BEARISH - resetting trade flags, cancelling pending waits")
            self.state.traded_in_bear_trend = False
            self.state.traded_in_bull_trend = False
            # Cancel any pending long EMA wait (opposite direction)
            self.state.pending_long_ema_wait = False
            # Cancel ALL pending ADX waits (new flip re-evaluates from scratch)
            self._clear_adx_wait_long_internal()
            self._clear_adx_wait_short_internal()
            self._clear_volume_wait_internal()
        
        self.state.prev_st_direction = current_direction
    
    def _clear_volume_wait_internal(self) -> None:
        self.state.pending_volume_long = False
        self.state.pending_volume_short = False
        self.state.volume_wait_bars_left_long = 0
        self.state.volume_wait_bars_left_short = 0
        self.state.volume_wait_trigger_long = ''
        self.state.volume_wait_trigger_short = ''
        self.state.volume_wait_kind_long = ''
        self.state.volume_wait_kind_short = ''
    
    def set_pending_long_ema_wait(self) -> None:
        """Set pending long EMA wait (waiting for bullish EMA cross after unaligned ST flip)."""
        self.state.pending_long_ema_wait = True
        logger.info("Set pending_long_ema_wait: waiting for 1H Close > EMA200 with ADX >= threshold")
    
    def clear_pending_long_ema_wait(self) -> None:
        """Clear pending long EMA wait."""
        self.state.pending_long_ema_wait = False
        logger.debug("Cleared pending_long_ema_wait")
    
    def set_pending_short_ema_wait(self) -> None:
        """Set pending short EMA wait (waiting for bearish EMA cross after unaligned ST flip)."""
        self.state.pending_short_ema_wait = True
        logger.info("Set pending_short_ema_wait: waiting for 1H Close < EMA200 with ADX >= threshold")
    
    def clear_pending_short_ema_wait(self) -> None:
        """Clear pending short EMA wait."""
        self.state.pending_short_ema_wait = False
        logger.debug("Cleared pending_short_ema_wait")
    
    # ----- ADX wait methods -----
    
    def set_adx_wait_long(self, bars: int, trigger: str) -> None:
        """Start 5-bar ADX confirmation window for a long entry."""
        self.state.pending_adx_long = True
        self.state.adx_wait_bars_left_long = bars
        self.state.adx_wait_trigger_long = trigger
        logger.info(
            f"Set ADX wait LONG: trigger={trigger}, {bars} bars window, "
            f"waiting for ADX >= threshold"
        )
    
    def clear_adx_wait_long(self) -> None:
        """Clear pending ADX wait for long."""
        self._clear_adx_wait_long_internal()
        logger.debug("Cleared pending_adx_long")

    def _clear_adx_wait_long_internal(self) -> None:
        """Internal clear without logging (used by update_supertrend_state)."""
        self.state.pending_adx_long = False
        self.state.adx_wait_bars_left_long = 0
        self.state.adx_wait_trigger_long = ''

    def decrement_adx_wait_long(self) -> None:
        """Decrement ADX wait counter for long. Clears if expired."""
        self.state.adx_wait_bars_left_long -= 1
        if self.state.adx_wait_bars_left_long <= 0:
            logger.info("ADX wait LONG expired: 5-bar window exhausted")
            self._clear_adx_wait_long_internal()
    
    def set_adx_wait_short(self, bars: int, trigger: str) -> None:
        """Start 5-bar ADX confirmation window for a short entry."""
        self.state.pending_adx_short = True
        self.state.adx_wait_bars_left_short = bars
        self.state.adx_wait_trigger_short = trigger
        logger.info(
            f"Set ADX wait SHORT: trigger={trigger}, {bars} bars window, "
            f"waiting for ADX >= threshold"
        )
    
    def clear_adx_wait_short(self) -> None:
        """Clear pending ADX wait for short."""
        self._clear_adx_wait_short_internal()
        logger.debug("Cleared pending_adx_short")
    
    def _clear_adx_wait_short_internal(self) -> None:
        """Internal clear without logging (used by update_supertrend_state)."""
        self.state.pending_adx_short = False
        self.state.adx_wait_bars_left_short = 0
        self.state.adx_wait_trigger_short = ''
    
    def decrement_adx_wait_short(self) -> None:
        """Decrement ADX wait counter for short. Clears if expired."""
        self.state.adx_wait_bars_left_short -= 1
        if self.state.adx_wait_bars_left_short <= 0:
            logger.info("ADX wait SHORT expired: 5-bar window exhausted")
            self._clear_adx_wait_short_internal()
    
    def set_volume_wait_long(self, bars_left: int, trigger: str, kind: str) -> None:
        self.state.pending_volume_long = True
        self.state.volume_wait_bars_left_long = bars_left
        self.state.volume_wait_trigger_long = trigger
        self.state.volume_wait_kind_long = kind
        logger.info(
            f"Volume wait LONG: {bars_left} bar(s) left, trigger={trigger}, kind={kind}"
        )
    
    def set_volume_wait_short(self, bars_left: int, trigger: str, kind: str) -> None:
        self.state.pending_volume_short = True
        self.state.volume_wait_bars_left_short = bars_left
        self.state.volume_wait_trigger_short = trigger
        self.state.volume_wait_kind_short = kind
        logger.info(
            f"Volume wait SHORT: {bars_left} bar(s) left, trigger={trigger}, kind={kind}"
        )
    
    def clear_volume_wait_long(self) -> None:
        self.state.pending_volume_long = False
        self.state.volume_wait_bars_left_long = 0
        self.state.volume_wait_trigger_long = ''
        self.state.volume_wait_kind_long = ''
    
    def clear_volume_wait_short(self) -> None:
        self.state.pending_volume_short = False
        self.state.volume_wait_bars_left_short = 0
        self.state.volume_wait_trigger_short = ''
        self.state.volume_wait_kind_short = ''
    
    def decrement_volume_wait_long(self) -> None:
        self.state.volume_wait_bars_left_long -= 1
    
    def decrement_volume_wait_short(self) -> None:
        self.state.volume_wait_bars_left_short -= 1
    
    def on_entry(
        self,
        signal: 'Signal',
        stop_loss: float,
        take_profit: float
    ) -> None:
        """
        Handle entry signal - update state.
        
        Pine Script Reference (lines 104-110):
            if buyCond
                strategy.entry("BUY", strategy.long)
                tradedInBullTrend := true
            if sellCond
                strategy.entry("SELL", strategy.short)
                tradedInBearTrend := true
        
        Parameters:
        -----------
        signal : Signal
            Entry signal
        stop_loss : float
            Stop loss price
        take_profit : float
            Take profit price
        """
        from .signal_engine import SignalType
        
        is_long = signal.signal_type == SignalType.BUY
        
        # Update position state
        self.state.position_size = 1 if is_long else -1
        self.state.entry_price = signal.price
        self.state.entry_time = signal.timestamp
        self.state.stop_loss = stop_loss
        self.state.take_profit = take_profit
        
        # Set trade flag to block re-entry in same trend
        if is_long:
            self.state.traded_in_bull_trend = True
            self.state.pending_long_ema_wait = False
            self._clear_adx_wait_long_internal()
            self.clear_volume_wait_long()
            logger.info(f"LONG entry @ {signal.price:.2f} - tradedInBullTrend = True")
        else:
            self.state.traded_in_bear_trend = True
            self.state.pending_short_ema_wait = False
            self._clear_adx_wait_short_internal()
            self.clear_volume_wait_short()
            logger.info(f"SHORT entry @ {signal.price:.2f} - tradedInBearTrend = True")
        
        # Create trade record
        self.state.trade_count += 1
        self.state.daily_trades += 1
        
        trade = Trade(
            trade_id=self.state.trade_count,
            direction='long' if is_long else 'short',
            entry_time=signal.timestamp,
            entry_price=signal.price,
            contracts=self.contracts,
            entry_trigger=getattr(signal, 'trigger', None),
            ema_1h_at_entry=getattr(signal, 'ema_1h', None),
            volume_at_entry=getattr(signal, 'volume_at_entry', None),
            volume_ma_at_entry=getattr(signal, 'volume_ma_at_entry', None),
        )
        self.trades.append(trade)
        
        # Persist state
        self.save_state()
    
    def on_exit(self, exit_signal: 'ExitSignal') -> Trade:
        """
        Handle exit signal - update state.
        
        Note: Trade flags are NOT reset on exit.
        They only reset on Supertrend flip (lines 64-67).
        
        Parameters:
        -----------
        exit_signal : ExitSignal
            Exit signal
        
        Returns:
        --------
        Trade
            Completed trade record
        """
        from .signal_engine import ExitType
        
        trade = None
        pnl_points = 0
        pnl_dollars = 0
        
        # Get current trade if exists
        if self.trades:
            trade = self.trades[-1]
            
            # Calculate P&L
            if trade.direction == 'long':
                pnl_points = exit_signal.exit_price - trade.entry_price
            else:
                pnl_points = trade.entry_price - exit_signal.exit_price
            
            # Convert to dollars: points * $2 (MNQ multiplier)
            pnl_dollars = pnl_points * 2 * trade.contracts
            
            # Update trade record
            trade.exit_time = exit_signal.timestamp
            trade.exit_price = exit_signal.exit_price
            trade.exit_type = exit_signal.exit_type.value
            trade.pnl_points = pnl_points
            trade.pnl_dollars = pnl_dollars
            
            # Update daily P&L
            self.state.daily_pnl += pnl_dollars
            
            logger.info(f"EXIT ({exit_signal.exit_type.value}): {trade.direction.upper()} "
                       f"@ {exit_signal.exit_price:.2f}, P&L: {pnl_points:+.2f} pts (${pnl_dollars:+.2f})")
        else:
            logger.warning("No trade record found - resetting position state anyway")
        
        # ALWAYS clear position state (even if no trade record)
        self.state.position_size = 0
        self.state.entry_price = 0.0
        self.state.entry_time = None
        self.state.stop_loss = 0.0
        self.state.take_profit = 0.0
        
        # IMPORTANT: Do NOT reset trade flags here!
        # Pine Script explicitly only resets on ST flip (lines 64-67)
        
        # Persist state
        self.save_state()
        
        return trade
    
    def is_entry_allowed(self, is_long: bool) -> bool:
        """
        Check if entry is allowed based on trade flags.
        
        Pine Script Reference (lines 98-99):
            ... and not tradedInBullTrend ...
            ... and not tradedInBearTrend ...
        
        Parameters:
        -----------
        is_long : bool
            True for long entry, False for short
        
        Returns:
        --------
        bool
            True if entry is allowed
        """
        if self.state.position_size != 0:
            return False
        
        if is_long:
            allowed = not self.state.traded_in_bull_trend
            if not allowed:
                logger.debug("LONG entry blocked: tradedInBullTrend = True")
            return allowed
        else:
            allowed = not self.state.traded_in_bear_trend
            if not allowed:
                logger.debug("SHORT entry blocked: tradedInBearTrend = True")
            return allowed
    
    def reset_daily_stats(self) -> None:
        """Reset daily statistics (call at session start)."""
        logger.info(f"Daily reset - Previous: {self.state.daily_trades} trades, ${self.state.daily_pnl:.2f}")
        self.state.daily_trades = 0
        self.state.daily_pnl = 0.0
        self.save_state()
    
    def save_state(self) -> None:
        """Persist state to file."""
        if self.state_file:
            try:
                with open(self.state_file, 'w') as f:
                    json.dump(self.state.to_dict(), f, indent=2)
            except Exception as e:
                logger.error(f"Failed to save state: {e}")
    
    def load_state(self) -> None:
        """Load state from file."""
        if self.state_file and self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    self.state = StrategyState.from_dict(data)
                logger.info(f"State loaded: position={self.state.position_size}, "
                          f"bull_traded={self.state.traded_in_bull_trend}, "
                          f"bear_traded={self.state.traded_in_bear_trend}")
                self.repair_flat_dual_trade_flags()
            except Exception as e:
                logger.error(f"Failed to load state: {e}")

    def repair_flat_dual_trade_flags(self) -> None:
        """
        Reset impossible flat state where both trend trade flags are set.

        Can happen after a stale feed missed SuperTrend flip processing.
        """
        if (
            self.state.position_size == 0
            and self.state.traded_in_bull_trend
            and self.state.traded_in_bear_trend
        ):
            logger.warning(
                "Both traded_in_bull_trend and traded_in_bear_trend are true while "
                "flat — resetting flags (likely missed ST flip from stale feed)"
            )
            self.state.traded_in_bull_trend = False
            self.state.traded_in_bear_trend = False
            self.save_state()
    
    def get_state_summary(self) -> Dict[str, Any]:
        """Get current state summary for display."""
        return {
            'position': 'LONG' if self.state.position_size > 0 
                       else 'SHORT' if self.state.position_size < 0 
                       else 'FLAT',
            'entry_price': self.state.entry_price,
            'stop_loss': self.state.stop_loss,
            'take_profit': self.state.take_profit,
            'traded_in_bull_trend': self.state.traded_in_bull_trend,
            'traded_in_bear_trend': self.state.traded_in_bear_trend,
            'pending_long_ema_wait': self.state.pending_long_ema_wait,
            'pending_short_ema_wait': self.state.pending_short_ema_wait,
            'pending_adx_long': self.state.pending_adx_long,
            'pending_adx_short': self.state.pending_adx_short,
            'adx_wait_bars_left_long': self.state.adx_wait_bars_left_long,
            'adx_wait_bars_left_short': self.state.adx_wait_bars_left_short,
            'daily_trades': self.state.daily_trades,
            'daily_pnl': self.state.daily_pnl
        }
