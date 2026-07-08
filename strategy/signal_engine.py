"""
=============================================================================
SIGNAL ENGINE
=============================================================================
Core signal generation engine for the MNQ Supertrend + EMA strategy.

Entry Logic:
-----------
1. Supertrend Flip -- Full Alignment (trigger = st_flip)
   When an ST flip occurs, evaluate the current 1H candle vs EMA200:
     Bullish: 1H Low > EMA200  AND  1H Close > EMA200  AND  ADX >= threshold  -> BUY
     Bearish: 1H High < EMA200  AND  1H Close < EMA200  AND  ADX >= threshold -> SELL
   If aligned but ADX < threshold, start a 5-bar ADX confirmation window.

2. EMA Cross (Deferred Entry -- trigger = ema_cross)
   If the ST flip candle's close is NOT aligned with EMA200 (Close on wrong
   side), the system waits for a subsequent 1H Close to cross the EMA.
   Also handles "partial cross" cases where Close is aligned but Low/High
   is not (e.g., Low <= EMA but Close > EMA for bullish). These are deferred
   to the next hour boundary and entered at the confirmed 1H close price.
     Bullish: 1H Close > EMA200  AND  ADX >= threshold -> BUY
     Bearish: 1H Close < EMA200  AND  ADX >= threshold -> SELL
   If the EMA cross occurs but ADX < threshold, start a 5-bar ADX window.

3. ADX 5-Bar Window
   After either an aligned ST flip or an EMA cross where ADX was below
   threshold, the system checks ADX on each subsequent bar (up to 5 bars).
   If ADX >= threshold within the window, entry is triggered.
   If 5 bars pass without ADX confirmation, the setup expires.

Cancellation: If another ST flip occurs while waiting (EMA or ADX), all
pending waits are cancelled and the new ST flip is evaluated instead.

Exit Logic:
-----------
- Take Profit / Stop Loss (percentage-based)
- Supertrend Flip exit (close position on opposing ST flip)
=============================================================================
"""

import pandas as pd
import numpy as np
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Signal types matching Pine Script strategy."""
    NONE = "none"
    BUY = "buy"           # strategy.entry("BUY", strategy.long)
    SELL = "sell"         # strategy.entry("SELL", strategy.short)


class ExitType(Enum):
    """Exit signal types."""
    NONE = "none"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    ST_FLIP = "st_flip"   # Supertrend flip exit
    CONTRACT_ROLL = "contract_roll"


@dataclass
class Signal:
    """
    Trade signal container.
    """
    signal_type: SignalType
    timestamp: pd.Timestamp
    price: float  # Entry price at signal bar
    
    # Extra context
    supertrend_value: float
    supertrend_direction: int  # -1 bullish, +1 bearish
    ema_1h: float
    close_1h: float
    
    # Trigger reason
    trigger: str  # 'st_flip' or 'ema_cross'
    volume_at_entry: Optional[float] = None
    volume_ma_at_entry: Optional[float] = None
    
    def __repr__(self):
        return f"Signal({self.signal_type.value} @ {self.timestamp}, price={self.price:.2f}, trigger={self.trigger})"


@dataclass
class ExitSignal:
    """Exit signal container."""
    exit_type: ExitType
    timestamp: pd.Timestamp
    exit_price: float
    entry_price: float
    pnl_points: float


class SignalEngine:
    """
    Signal generation engine implementing the ST Flip + EMA200 + ADX strategy.
    
    Entry Evaluation Order:
    1. ST Flip → check 1H candle alignment with EMA200 → immediate entry or set pending
    2. Pending EMA Cross → check 1H Close cross EMA200 with ADX → deferred entry
    """
    
    def __init__(
        self,
        sl_pct_long: float = 0.55,
        tp_pct_long: float = 3.0,
        sl_pct_short: float = 0.55,
        tp_pct_short: float = 3.0,
        supertrend_atr_long: int = 10,
        supertrend_mult_long: float = 3.0,
        supertrend_atr_short: int = 10,
        supertrend_mult_short: float = 3.0,
        ema_length: int = 200,
        use_adx_long: bool = True,
        use_adx_short: bool = True,
        adx_wait_bars_long: int = 5,
        adx_wait_bars_short: int = 5,
        adx_threshold_long: float = 20.0,
        adx_threshold_short: float = 20.0,
        volume_check: bool = False,
        volume_candle_lookahead: int = 1,
    ):
        """
        Initialize signal engine with per-side strategy parameters (config/strategy.yaml).

        Supertrend ATR/mult apply to columns supertrend_{long,short} on each bar.
        """
        self.sl_pct_long = sl_pct_long
        self.tp_pct_long = tp_pct_long
        self.sl_pct_short = sl_pct_short
        self.tp_pct_short = tp_pct_short
        self.supertrend_atr_long = supertrend_atr_long
        self.supertrend_mult_long = supertrend_mult_long
        self.supertrend_atr_short = supertrend_atr_short
        self.supertrend_mult_short = supertrend_mult_short
        self.ema_length = ema_length
        self.use_adx_long = use_adx_long
        self.use_adx_short = use_adx_short
        self.adx_wait_bars_long = max(1, int(adx_wait_bars_long))
        self.adx_wait_bars_short = max(1, int(adx_wait_bars_short))
        self.adx_threshold_long = float(adx_threshold_long)
        self.adx_threshold_short = float(adx_threshold_short)
        self.volume_check = volume_check
        self.volume_candle_lookahead = max(1, int(volume_candle_lookahead))

        logger.info(
            f"Signal Engine initialized: SL L={sl_pct_long}% S={sl_pct_short}%, "
            f"TP L={tp_pct_long}% S={tp_pct_short}%, "
            f"ST long(ATR={supertrend_atr_long}, Mult={supertrend_mult_long}) "
            f"short(ATR={supertrend_atr_short}, Mult={supertrend_mult_short}), "
            f"ADX long={'on' if use_adx_long else 'off'}(wait={self.adx_wait_bars_long}) "
            f"short={'on' if use_adx_short else 'off'}(wait={self.adx_wait_bars_short}), "
            f"Volume={'on' if volume_check else 'off'} "
            f"(lookahead={self.volume_candle_lookahead})"
        )
    
    def volume_confirmation_window_bars(self) -> int:
        """Number of primary candles in the volume confirmation window (includes trigger bar)."""
        return self.volume_candle_lookahead
    
    @staticmethod
    def single_row_volume_window(bar: pd.Series) -> pd.DataFrame:
        """Wrap one primary bar (with volume, volume_ma) for evaluate_entry_conditions."""
        out = bar.to_frame().T
        out.index = [bar.name]
        return out
    
    @staticmethod
    def _row_volume_confirms(row: pd.Series) -> bool:
        vol = row.get('volume', np.nan)
        ma = row.get('volume_ma', np.nan)
        if pd.isna(vol) or pd.isna(ma) or ma <= 0:
            return False
        return float(vol) > float(ma)

    def _entry_price_from_bar(self, row: pd.Series, trigger: str, is_long: bool) -> float:
        if trigger == 'ema_cross':
            v = row.get('close_1h_cross', np.nan)
            if pd.notna(v):
                return float(v)
        return float(row['close'])
    
    def _signal_on_confirming_row(self, signal: Signal, row: pd.Series) -> Signal:
        is_long = signal.signal_type == SignalType.BUY
        price = self._entry_price_from_bar(row, signal.trigger, is_long)
        is_long = signal.signal_type == SignalType.BUY
        st_key = 'supertrend_long' if is_long else 'supertrend_short'
        dir_key = 'direction_long' if is_long else 'direction_short'
        return Signal(
            signal_type=signal.signal_type,
            timestamp=row.name,
            price=price,
            supertrend_value=row.get(st_key, row.get('supertrend', signal.supertrend_value)),
            supertrend_direction=row.get(dir_key, row.get('direction', signal.supertrend_direction)),
            ema_1h=row.get('ema_1h', signal.ema_1h),
            close_1h=row.get('close_1h', signal.close_1h),
            trigger=signal.trigger,
            volume_at_entry=float(row['volume']) if pd.notna(row.get('volume', np.nan)) else None,
            volume_ma_at_entry=float(row['volume_ma']) if pd.notna(row.get('volume_ma', np.nan)) else None,
        )
    
    def _volume_failure_state_cleanup(
        self, defer_kind: str, signal_type: SignalType
    ) -> Dict[str, Any]:
        """If volume rejects the entry, clear EMA/ADX pending state that would otherwise retry."""
        u: Dict[str, Any] = {}
        if defer_kind == 'ema':
            if signal_type == SignalType.BUY:
                u['clear_pending_long_ema_wait'] = True
            else:
                u['clear_pending_short_ema_wait'] = True
        elif defer_kind == 'adx':
            if signal_type == SignalType.BUY:
                u['clear_adx_wait_long'] = True
            else:
                u['clear_adx_wait_short'] = True
        return u
    
    def _signal_from_bar(
        self,
        row: pd.Series,
        signal_type: SignalType,
        trigger: str
    ) -> Signal:
        is_long = signal_type == SignalType.BUY
        price = self._entry_price_from_bar(row, trigger, is_long)
        st_key = 'supertrend_long' if is_long else 'supertrend_short'
        dir_key = 'direction_long' if is_long else 'direction_short'
        return Signal(
            signal_type=signal_type,
            timestamp=row.name,
            price=price,
            supertrend_value=row.get(st_key, row.get('supertrend', np.nan)),
            supertrend_direction=row.get(dir_key, row.get('direction', 0)),
            ema_1h=row.get('ema_1h', np.nan),
            close_1h=row.get('close_1h', np.nan),
            trigger=trigger,
            volume_at_entry=float(row['volume']) if pd.notna(row.get('volume', np.nan)) else None,
            volume_ma_at_entry=float(row['volume_ma']) if pd.notna(row.get('volume_ma', np.nan)) else None,
        )
    
    def _finalize_entry_volume(
        self,
        signal: Signal,
        success_updates: Dict[str, Any],
        volume_window: Optional[pd.DataFrame],
        allow_volume_defer: bool,
        defer_kind: str,
    ) -> Tuple[Optional[Signal], Dict[str, Any]]:
        """
        Apply 20-bar volume SMA rule over volume_window (primary TF rows).
        defer_kind: 'st' | 'ema' | 'adx' for streaming volume-wait state metadata.
        """
        if not self.volume_check:
            return (signal, success_updates)
        w = self.volume_confirmation_window_bars()
        if volume_window is None or len(volume_window) == 0:
            logger.warning("[VOLUME] volume_check enabled but volume_window is empty; skipping entry")
            return (None, {})
        slice_df = volume_window.iloc[:w]
        for _, row in slice_df.iterrows():
            if self._row_volume_confirms(row):
                adj = self._signal_on_confirming_row(signal, row)
                return (adj, success_updates)
        bars_examined = len(slice_df)
        if bars_examined >= w:
            logger.info("[VOLUME] No bar in window exceeded volume MA; skipping trade")
            return (None, self._volume_failure_state_cleanup(defer_kind, signal.signal_type))
        if allow_volume_defer and w > 1:
            rem = w - bars_examined
            if signal.signal_type == SignalType.BUY:
                return (None, {
                    "set_volume_wait_long": {
                        "remaining": rem, "trigger": signal.trigger, "kind": defer_kind
                    }
                })
            return (None, {
                "set_volume_wait_short": {
                    "remaining": rem, "trigger": signal.trigger, "kind": defer_kind
                }
            })
        logger.info("[VOLUME] Skipping trade (volume not confirmed)")
        return (None, self._volume_failure_state_cleanup(defer_kind, signal.signal_type))
    
    def _pending_volume_long_updates(
        self,
        bar: pd.Series,
        st_bull: bool,
        traded_in_bull_trend: bool,
        volume_wait_bars_left_long: int,
        volume_wait_trigger_long: str,
        volume_wait_kind_long: str,
    ) -> Tuple[Optional[Signal], Dict[str, Any]]:
        u: Dict[str, Any] = {}
        if not st_bull or traded_in_bull_trend:
            u["clear_pending_volume_long"] = True
            return (None, u)
        if volume_wait_bars_left_long <= 0:
            u["clear_pending_volume_long"] = True
            if volume_wait_kind_long == 'ema':
                u["clear_pending_long_ema_wait"] = True
            if volume_wait_kind_long == 'adx':
                u["clear_adx_wait_long"] = True
            return (None, u)
        if self._row_volume_confirms(bar):
            sig = self._signal_from_bar(bar, SignalType.BUY, volume_wait_trigger_long or 'st_flip')
            u["clear_pending_volume_long"] = True
            if volume_wait_kind_long == 'ema':
                u["clear_pending_long_ema_wait"] = True
            if volume_wait_kind_long == 'adx':
                u["clear_adx_wait_long"] = True
            return (sig, u)
        new_l = volume_wait_bars_left_long - 1
        if new_l <= 0:
            u["clear_pending_volume_long"] = True
            if volume_wait_kind_long == 'ema':
                u["clear_pending_long_ema_wait"] = True
            if volume_wait_kind_long == 'adx':
                u["clear_adx_wait_long"] = True
            logger.info("[VOLUME] LONG volume wait expired without confirmation")
            return (None, u)
        u["decrement_volume_wait_long"] = True
        return (None, u)
    
    def _pending_volume_short_updates(
        self,
        bar: pd.Series,
        st_bear: bool,
        traded_in_bear_trend: bool,
        volume_wait_bars_left_short: int,
        volume_wait_trigger_short: str,
        volume_wait_kind_short: str,
    ) -> Tuple[Optional[Signal], Dict[str, Any]]:
        u: Dict[str, Any] = {}
        if not st_bear or traded_in_bear_trend:
            u["clear_pending_volume_short"] = True
            return (None, u)
        if volume_wait_bars_left_short <= 0:
            u["clear_pending_volume_short"] = True
            if volume_wait_kind_short == 'ema':
                u["clear_pending_short_ema_wait"] = True
            if volume_wait_kind_short == 'adx':
                u["clear_adx_wait_short"] = True
            return (None, u)
        if self._row_volume_confirms(bar):
            sig = self._signal_from_bar(bar, SignalType.SELL, volume_wait_trigger_short or 'st_flip')
            u["clear_pending_volume_short"] = True
            if volume_wait_kind_short == 'ema':
                u["clear_pending_short_ema_wait"] = True
            if volume_wait_kind_short == 'adx':
                u["clear_adx_wait_short"] = True
            return (sig, u)
        new_l = volume_wait_bars_left_short - 1
        if new_l <= 0:
            u["clear_pending_volume_short"] = True
            if volume_wait_kind_short == 'ema':
                u["clear_pending_short_ema_wait"] = True
            if volume_wait_kind_short == 'adx':
                u["clear_adx_wait_short"] = True
            logger.info("[VOLUME] SHORT volume wait expired without confirmation")
            return (None, u)
        u["decrement_volume_wait_short"] = True
        return (None, u)
    
    def evaluate_entry_conditions(
        self,
        bar: pd.Series,
        position_size: int,
        traded_in_bull_trend: bool,
        traded_in_bear_trend: bool,
        pending_long_ema_wait: bool = False,
        pending_short_ema_wait: bool = False,
        pending_adx_long: bool = False,
        pending_adx_short: bool = False,
        adx_wait_bars_left_long: int = 0,
        adx_wait_bars_left_short: int = 0,
        adx_wait_trigger_long: str = '',
        adx_wait_trigger_short: str = '',
        volume_window: Optional[pd.DataFrame] = None,
        allow_volume_defer: bool = False,
        pending_volume_long: bool = False,
        pending_volume_short: bool = False,
        volume_wait_bars_left_long: int = 0,
        volume_wait_bars_left_short: int = 0,
        volume_wait_trigger_long: str = '',
        volume_wait_trigger_short: str = '',
        volume_wait_kind_long: str = '',
        volume_wait_kind_short: str = ''
    ) -> Tuple[Optional[Signal], Dict[str, Any]]:
        """
        Evaluate entry conditions for a single bar.
        
        Evaluation order:
        1. ST Flip -> check PREVIOUS HOUR's 1H Close vs EMA200:
           a) Prev Close on correct side of EMA + ADX ok -> immediate entry (st_flip)
           b) Prev Close on correct side of EMA + ADX low -> 5-bar ADX wait
           c) Prev Close on wrong side of EMA -> EMA cross wait
        2. Pending ADX wait (5-bar window) -> check ADX, enter or decrement/expire
        3. Pending EMA cross (deferred entry from a prior unaligned ST flip):
           a) EMA cross + ADX ok -> entry
           b) EMA cross + ADX low -> 5-bar ADX wait
        
        Parameters:
        -----------
        bar : pd.Series
            Current bar data with all indicator columns
        position_size : int
            Current position (0=flat, 1=long, -1=short)
        traded_in_bull_trend : bool
            Already traded in current bullish ST trend
        traded_in_bear_trend : bool
            Already traded in current bearish ST trend
        pending_long_ema_wait : bool
            Waiting for bullish EMA cross after unaligned bullish ST flip
        pending_short_ema_wait : bool
            Waiting for bearish EMA cross after unaligned bearish ST flip
        pending_adx_long : bool
            Waiting for ADX confirmation for a long entry (5-bar window)
        pending_adx_short : bool
            Waiting for ADX confirmation for a short entry (5-bar window)
        adx_wait_bars_left_long : int
            Bars remaining in ADX check window for long
        adx_wait_bars_left_short : int
            Bars remaining in ADX check window for short
        adx_wait_trigger_long : str
            Original trigger ('st_flip' or 'ema_cross') for long ADX wait
        adx_wait_trigger_short : str
            Original trigger ('st_flip' or 'ema_cross') for short ADX wait
        
        Returns:
        --------
        Tuple[Optional[Signal], Dict[str, Any]]
            (Signal or None, updates dict with state changes)
        """
        updates: Dict[str, Any] = {}
        
        if position_size != 0:
            return (None, updates)
        
        timestamp = bar.name
        close = bar['close']
        
        # --- Extract indicator values ---
        if self.volume_check and pending_volume_long:
            return self._pending_volume_long_updates(
                bar, bar.get('st_bull_long', False), traded_in_bull_trend,
                volume_wait_bars_left_long, volume_wait_trigger_long, volume_wait_kind_long
            )
        if self.volume_check and pending_volume_short:
            return self._pending_volume_short_updates(
                bar, bar.get('st_bear_short', False), traded_in_bear_trend,
                volume_wait_bars_left_short, volume_wait_trigger_short, volume_wait_kind_short
            )
        
        st_bull_flip_long = bar.get('st_bull_flip_long', bar.get('st_bull_flip_entry_long', bar.get('st_bull_flip', False)))
        st_bear_flip_short = bar.get('st_bear_flip_short', bar.get('st_bear_flip_entry_short', bar.get('st_bear_flip', False)))
        
        supertrend_value = bar.get('supertrend_long', bar.get('supertrend_entry_long', bar.get('supertrend', np.nan)))
        supertrend_dir = bar.get('direction_long', bar.get('direction_entry_long', bar.get('direction', 0)))
        
        ema_1h = bar.get('ema_1h', np.nan)
        close_1h = bar.get('close_1h', np.nan)
        high_1h = bar.get('high_1h', np.nan)
        low_1h = bar.get('low_1h', np.nan)
        
        # close_1h_cross: confirmed 1H closing price (available from T+1H).
        # Used as entry price for EMA cross signals.
        _close_1h_cross_raw = bar.get('close_1h_cross', np.nan)
        close_1h_cross = _close_1h_cross_raw if (not np.isnan(_close_1h_cross_raw)) else close
        
        # Confirmed 1H values available to the current primary bar:
        # - _close_confirmed / _ema_confirmed: confirmed (non-lookahead) 1H values
        #   Backtest uses close_1h_cross / ema_1h_cross (shifted +1H);
        #   Paper/live falls back to close_1h / ema_1h (already confirmed there).
        _ema_1h_cross_raw = bar.get('ema_1h_cross', np.nan)
        _close_confirmed = _close_1h_cross_raw if not np.isnan(_close_1h_cross_raw) else close_1h
        _ema_confirmed = _ema_1h_cross_raw if not np.isnan(_ema_1h_cross_raw) else ema_1h
        
        ema_bull_cross = bar.get('ema_bull_cross', False)
        ema_bear_cross = bar.get('ema_bear_cross', False)
        try:
            primary_bar_minutes = float(bar.get('primary_bar_minutes', 5) or 5)
        except (TypeError, ValueError):
            primary_bar_minutes = 5
        if primary_bar_minutes <= 0:
            primary_bar_minutes = 5
        timestamp_hour_close = (
            isinstance(timestamp, pd.Timestamp)
            and (
                timestamp + pd.Timedelta(minutes=primary_bar_minutes)
            ).floor("h") != timestamp.floor("h")
        )
        is_1h_close_bar = bool(bar.get('is_1h_close_bar', False) or timestamp_hour_close)
        current_hour_bull_cross = (
            is_1h_close_bar
            and not pd.isna(close_1h)
            and not pd.isna(ema_1h)
            and not pd.isna(_close_confirmed)
            and not pd.isna(_ema_confirmed)
            and close_1h > ema_1h
            and _close_confirmed <= _ema_confirmed
        )
        current_hour_bear_cross = (
            is_1h_close_bar
            and not pd.isna(close_1h)
            and not pd.isna(ema_1h)
            and not pd.isna(_close_confirmed)
            and not pd.isna(_ema_confirmed)
            and close_1h < ema_1h
            and _close_confirmed >= _ema_confirmed
        )
        ema_bull_cross_now = bool(ema_bull_cross or current_hour_bull_cross)
        ema_bear_cross_now = bool(ema_bear_cross or current_hour_bear_cross)
        
        adx_value = float(bar.get('adx', bar.get('adx_long', 0)) or 0)
        adx_above_long = bar.get('adx_above_threshold_long', bar.get('adx_above_threshold', False))
        adx_above_short = bar.get('adx_above_threshold_short', bar.get('adx_above_threshold', False))
        
        # Safety: skip if EMA not available
        if pd.isna(ema_1h) or ema_1h <= 0:
            return (None, updates)
        
        timestamp_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
        
        # Log potential entry situations
        st_bull_l = bar.get('st_bull_long', bar.get('st_bull_entry_long', False))
        st_bear_s = bar.get('st_bear_short', bar.get('st_bear_entry_short', False))
        if (st_bull_flip_long or st_bear_flip_short or pending_long_ema_wait or
                pending_short_ema_wait or pending_adx_long or pending_adx_short):
            st_dir = "BULL_L" if st_bull_l else "BEAR_S" if st_bear_s else "FLAT"
            adx_wait_str = ""
            if pending_adx_long:
                adx_wait_str += f", ADX_WAIT_LONG({adx_wait_bars_left_long}bars)"
            if pending_adx_short:
                adx_wait_str += f", ADX_WAIT_SHORT({adx_wait_bars_left_short}bars)"
            logger.info(
                f"[ENTRY CHECK] {timestamp_str} | ST: {st_dir}, "
                f"EMA: {ema_1h:.2f}, Close1H: {close_1h:.2f}, "
                f"High1H: {high_1h:.2f}, Low1H: {low_1h:.2f}, "
                f"ADX: {adx_value:.1f}, "
                f"TradedBull: {traded_in_bull_trend}, TradedBear: {traded_in_bear_trend}"
                f"{adx_wait_str}"
            )
        
        # =================================================================
        # 1) BULLISH ST FLIP — Immediate Entry Check
        #    Uses PREVIOUS HOUR's confirmed 1H Close vs EMA200:
        #    a) Prev 1H Close > EMA + ADX ok
        #       -> immediate BUY, trigger = st_flip
        #    b) Prev 1H Close > EMA + ADX low
        #       -> 5-bar ADX wait, trigger = st_flip
        #    c) Prev 1H Close <= EMA
        #       -> wait for EMA cross in subsequent hours
        # =================================================================
        if st_bull_flip_long and not traded_in_bull_trend:
            prev_hour_valid = not pd.isna(_close_confirmed) and not pd.isna(_ema_confirmed)
            prev_close_above_ema = (
                prev_hour_valid and
                _close_confirmed > _ema_confirmed
            )
            adx_ok = (not self.use_adx_long) or adx_above_long
            thr = self.adx_threshold_long
            
            if prev_close_above_ema and adx_ok:
                # --- (a) IMMEDIATE BUY ENTRY (prev hour close above EMA) ---
                signal = Signal(
                    signal_type=SignalType.BUY,
                    timestamp=timestamp,
                    price=close,
                    supertrend_value=supertrend_value,
                    supertrend_direction=supertrend_dir,
                    ema_1h=ema_1h,
                    close_1h=close_1h,
                    trigger="st_flip",
                    volume_at_entry=float(bar['volume']) if pd.notna(bar.get('volume', np.nan)) else None,
                    volume_ma_at_entry=float(bar['volume_ma']) if pd.notna(bar.get('volume_ma', np.nan)) else None,
                )
                adx_str = f", ADX({adx_value:.1f}) >= {thr:g} ✓" if self.use_adx_long else ""
                logger.info(
                    f"[CONFIRMED] {timestamp_str} | ST Flip BUY: "
                    f"Prev 1H Close({_close_confirmed:.2f}) > EMA({_ema_confirmed:.2f}) ✓"
                    f"{adx_str}"
                )
                return self._finalize_entry_volume(
                    signal, {}, volume_window, allow_volume_defer, 'st'
                )
            elif prev_close_above_ema and not adx_ok:
                # --- (b) Prev close above EMA but ADX too low -> 5-bar ADX wait ---
                wait_bars = self.adx_wait_bars_long
                logger.info(
                    f"[ADX WAIT] {timestamp_str} | ST Flip BUY aligned: "
                    f"Prev 1H Close({_close_confirmed:.2f}) > EMA({_ema_confirmed:.2f}) ✓, "
                    f"but ADX({adx_value:.1f}) < {thr:g} -> starting {wait_bars}-bar ADX wait"
                )
                updates["set_adx_wait_long"] = {"bars": wait_bars, "trigger": "st_flip"}
                return (None, updates)
            else:
                # --- (c) Prev close <= EMA -> wait for EMA cross ---
                _prev_c = f"{_close_confirmed:.2f}" if prev_hour_valid else "N/A"
                _prev_e = f"{_ema_confirmed:.2f}" if prev_hour_valid else "N/A"
                logger.info(
                    f"[WAITING] {timestamp_str} | ST Flip BUY not aligned: "
                    f"Prev 1H Close({_prev_c}) <= EMA({_prev_e}) "
                    f"-> waiting for EMA cross"
                )
                updates["set_pending_long_ema_wait"] = True
                return (None, updates)
        
        # =================================================================
        # 2) BEARISH ST FLIP — Immediate Entry Check
        #    Uses PREVIOUS HOUR's confirmed 1H Close vs EMA200:
        #    a) Prev 1H Close < EMA + ADX ok
        #       -> immediate SELL, trigger = st_flip
        #    b) Prev 1H Close < EMA + ADX low
        #       -> 5-bar ADX wait, trigger = st_flip
        #    c) Prev 1H Close >= EMA
        #       -> wait for EMA cross in subsequent hours
        # =================================================================
        if st_bear_flip_short and not traded_in_bear_trend:
            prev_hour_valid = not pd.isna(_close_confirmed) and not pd.isna(_ema_confirmed)
            prev_close_below_ema = (
                prev_hour_valid and
                _close_confirmed < _ema_confirmed
            )
            adx_ok = (not self.use_adx_short) or adx_above_short
            thr_s = self.adx_threshold_short
            su_s = bar.get('supertrend_short', bar.get('supertrend_entry_short', bar.get('supertrend', np.nan)))
            dir_s = bar.get('direction_short', bar.get('direction_entry_short', bar.get('direction', 0))
            )
            
            if prev_close_below_ema and adx_ok:
                # --- (a) IMMEDIATE SELL ENTRY (prev hour close below EMA) ---
                signal = Signal(
                    signal_type=SignalType.SELL,
                    timestamp=timestamp,
                    price=close,
                    supertrend_value=su_s,
                    supertrend_direction=dir_s,
                    ema_1h=ema_1h,
                    close_1h=close_1h,
                    trigger="st_flip",
                    volume_at_entry=float(bar['volume']) if pd.notna(bar.get('volume', np.nan)) else None,
                    volume_ma_at_entry=float(bar['volume_ma']) if pd.notna(bar.get('volume_ma', np.nan)) else None,
                )
                adx_str = f", ADX({adx_value:.1f}) >= {thr_s:g} ✓" if self.use_adx_short else ""
                logger.info(
                    f"[CONFIRMED] {timestamp_str} | ST Flip SELL: "
                    f"Prev 1H Close({_close_confirmed:.2f}) < EMA({_ema_confirmed:.2f}) ✓"
                    f"{adx_str}"
                )
                return self._finalize_entry_volume(
                    signal, {}, volume_window, allow_volume_defer, 'st'
                )
            elif prev_close_below_ema and not adx_ok:
                # --- (b) Prev close below EMA but ADX too low -> 5-bar ADX wait ---
                wait_bars = self.adx_wait_bars_short
                logger.info(
                    f"[ADX WAIT] {timestamp_str} | ST Flip SELL aligned: "
                    f"Prev 1H Close({_close_confirmed:.2f}) < EMA({_ema_confirmed:.2f}) ✓, "
                    f"but ADX({adx_value:.1f}) < {thr_s:g} -> starting {wait_bars}-bar ADX wait"
                )
                updates["set_adx_wait_short"] = {"bars": wait_bars, "trigger": "st_flip"}
                return (None, updates)
            else:
                # --- (c) Prev close >= EMA -> wait for EMA cross ---
                _prev_c = f"{_close_confirmed:.2f}" if prev_hour_valid else "N/A"
                _prev_e = f"{_ema_confirmed:.2f}" if prev_hour_valid else "N/A"
                logger.info(
                    f"[WAITING] {timestamp_str} | ST Flip SELL not aligned: "
                    f"Prev 1H Close({_prev_c}) >= EMA({_prev_e}) "
                    f"-> waiting for EMA cross"
                )
                updates["set_pending_short_ema_wait"] = True
                return (None, updates)
        
        # =================================================================
        # 3) PENDING ADX LONG — 5-bar ADX confirmation window
        #    Entered when: ST flip was aligned but ADX < threshold, OR
        #                  EMA cross occurred but ADX < threshold.
        #    On each bar: check ADX, enter if ok, otherwise decrement.
        #    Expires after 5 bars with no ADX confirmation.
        # =================================================================
        if pending_adx_long and st_bull_l and not traded_in_bull_trend:
            adx_ok = (not self.use_adx_long) or adx_above_long
            thr = self.adx_threshold_long
            if adx_ok:
                trigger = adx_wait_trigger_long or 'st_flip'
                signal = Signal(
                    signal_type=SignalType.BUY,
                    timestamp=timestamp,
                    price=close,
                    supertrend_value=supertrend_value,
                    supertrend_direction=supertrend_dir,
                    ema_1h=ema_1h,
                    close_1h=close_1h,
                    trigger=trigger,
                    volume_at_entry=float(bar['volume']) if pd.notna(bar.get('volume', np.nan)) else None,
                    volume_ma_at_entry=float(bar['volume_ma']) if pd.notna(bar.get('volume_ma', np.nan)) else None,
                )
                bars_used = self.adx_wait_bars_long - adx_wait_bars_left_long + 1
                logger.info(
                    f"[CONFIRMED] {timestamp_str} | ADX confirmed {trigger} BUY: "
                    f"ADX({adx_value:.1f}) >= {thr:g} after {bars_used} bar(s) wait"
                )
                return self._finalize_entry_volume(
                    signal,
                    {"clear_adx_wait_long": True},
                    volume_window,
                    allow_volume_defer,
                    'adx',
                )
            else:
                # ADX still below threshold - decrement counter
                new_bars = adx_wait_bars_left_long - 1
                if new_bars <= 0:
                    logger.info(
                        f"[EXPIRED] {timestamp_str} | ADX wait LONG expired: "
                        f"ADX({adx_value:.1f}) never reached {thr:g} in {self.adx_wait_bars_long} bars"
                    )
                    updates["clear_adx_wait_long"] = True
                else:
                    logger.info(
                        f"[ADX WAIT] {timestamp_str} | LONG ADX wait: "
                        f"ADX({adx_value:.1f}) < {thr:g}, {new_bars} bars remaining"
                    )
                    updates["decrement_adx_wait_long"] = True
        
        # =================================================================
        # 4) PENDING ADX SHORT — 5-bar ADX confirmation window
        # =================================================================
        su_s = bar.get('supertrend_short', bar.get('supertrend_entry_short', bar.get('supertrend', np.nan)))
        dir_s = bar.get('direction_short', bar.get('direction_entry_short', bar.get('direction', 0)))
        if pending_adx_short and st_bear_s and not traded_in_bear_trend:
            adx_ok = (not self.use_adx_short) or adx_above_short
            thr_s = self.adx_threshold_short
            if adx_ok:
                trigger = adx_wait_trigger_short or 'st_flip'
                signal = Signal(
                    signal_type=SignalType.SELL,
                    timestamp=timestamp,
                    price=close,
                    supertrend_value=su_s,
                    supertrend_direction=dir_s,
                    ema_1h=ema_1h,
                    close_1h=close_1h,
                    trigger=trigger,
                    volume_at_entry=float(bar['volume']) if pd.notna(bar.get('volume', np.nan)) else None,
                    volume_ma_at_entry=float(bar['volume_ma']) if pd.notna(bar.get('volume_ma', np.nan)) else None,
                )
                bars_used = self.adx_wait_bars_short - adx_wait_bars_left_short + 1
                logger.info(
                    f"[CONFIRMED] {timestamp_str} | ADX confirmed {trigger} SELL: "
                    f"ADX({adx_value:.1f}) >= {thr_s:g} after {bars_used} bar(s) wait"
                )
                return self._finalize_entry_volume(
                    signal,
                    {"clear_adx_wait_short": True},
                    volume_window,
                    allow_volume_defer,
                    'adx',
                )
            else:
                # ADX still below threshold - decrement counter
                new_bars = adx_wait_bars_left_short - 1
                if new_bars <= 0:
                    logger.info(
                        f"[EXPIRED] {timestamp_str} | ADX wait SHORT expired: "
                        f"ADX({adx_value:.1f}) never reached {thr_s:g} in {self.adx_wait_bars_short} bars"
                    )
                    updates["clear_adx_wait_short"] = True
                else:
                    logger.info(
                        f"[ADX WAIT] {timestamp_str} | SHORT ADX wait: "
                        f"ADX({adx_value:.1f}) < {thr_s:g}, {new_bars} bars remaining"
                    )
                    updates["decrement_adx_wait_short"] = True
        
        # =================================================================
        # 5) PENDING BULLISH EMA CROSS — Deferred Entry
        #    Fires only when the backtest-style ema_bull_cross flag is true:
        #    previous confirmed 1H close <= EMA and newly confirmed 1H close > EMA.
        #    Two outcomes:
        #    a) ADX ok -> enter BUY
        #    b) ADX low -> start 5-bar ADX wait, clear EMA wait
        # =================================================================
        if (pending_long_ema_wait and st_bull_l and
                not traded_in_bull_trend and
                ema_bull_cross_now):
            adx_ok = (not self.use_adx_long) or adx_above_long
            thr = self.adx_threshold_long
            if adx_ok:
                entry_price = close_1h if current_hour_bull_cross else close_1h_cross
                entry_close_1h = close_1h if current_hour_bull_cross else _close_confirmed
                entry_ema_1h = ema_1h if current_hour_bull_cross else _ema_confirmed
                signal = Signal(
                    signal_type=SignalType.BUY,
                    timestamp=timestamp,
                    price=entry_price,
                    supertrend_value=supertrend_value,
                    supertrend_direction=supertrend_dir,
                    ema_1h=ema_1h,
                    close_1h=close_1h,
                    trigger="ema_cross",
                    volume_at_entry=float(bar['volume']) if pd.notna(bar.get('volume', np.nan)) else None,
                    volume_ma_at_entry=float(bar['volume_ma']) if pd.notna(bar.get('volume_ma', np.nan)) else None,
                )
                adx_str = f", ADX({adx_value:.1f}) >= {thr:g} ✓" if self.use_adx_long else ""
                logger.info(
                    f"[CONFIRMED] {timestamp_str} | EMA Cross BUY: "
                    f"1H Close({entry_close_1h:.2f}) crossed > EMA({entry_ema_1h:.2f}) ✓, ST=BULL ✓"
                    f"{adx_str}"
                )
                return self._finalize_entry_volume(
                    signal,
                    {"clear_pending_long_ema_wait": True},
                    volume_window,
                    allow_volume_defer,
                    'ema',
                )
            else:
                # EMA cross happened but ADX too low -> 5-bar ADX wait
                wait_bars = self.adx_wait_bars_long
                logger.info(
                    f"[ADX WAIT] {timestamp_str} | EMA Cross BUY: "
                    f"1H Close crossed > EMA ✓, but ADX({adx_value:.1f}) < {thr:g} "
                    f"-> starting {wait_bars}-bar ADX wait"
                )
                updates["set_adx_wait_long"] = {"bars": wait_bars, "trigger": "ema_cross"}
                updates["clear_pending_long_ema_wait"] = True
                return (None, updates)
        
        # =================================================================
        # 6) PENDING BEARISH EMA CROSS — Deferred Entry
        #    Fires only when the backtest-style ema_bear_cross flag is true:
        #    previous confirmed 1H close >= EMA and newly confirmed 1H close < EMA.
        #    Two outcomes:
        #    a) ADX ok -> enter SELL
        #    b) ADX low -> start 5-bar ADX wait, clear EMA wait
        # =================================================================
        if (pending_short_ema_wait and st_bear_s and
                not traded_in_bear_trend and
                ema_bear_cross_now):
            adx_ok = (not self.use_adx_short) or adx_above_short
            thr_s = self.adx_threshold_short
            if adx_ok:
                entry_price = close_1h if current_hour_bear_cross else close_1h_cross
                entry_close_1h = close_1h if current_hour_bear_cross else _close_confirmed
                entry_ema_1h = ema_1h if current_hour_bear_cross else _ema_confirmed
                signal = Signal(
                    signal_type=SignalType.SELL,
                    timestamp=timestamp,
                    price=entry_price,
                    supertrend_value=su_s,
                    supertrend_direction=dir_s,
                    ema_1h=ema_1h,
                    close_1h=close_1h,
                    trigger="ema_cross",
                    volume_at_entry=float(bar['volume']) if pd.notna(bar.get('volume', np.nan)) else None,
                    volume_ma_at_entry=float(bar['volume_ma']) if pd.notna(bar.get('volume_ma', np.nan)) else None,
                )
                adx_str = f", ADX({adx_value:.1f}) >= {thr_s:g} ✓" if self.use_adx_short else ""
                logger.info(
                    f"[CONFIRMED] {timestamp_str} | EMA Cross SELL: "
                    f"1H Close({entry_close_1h:.2f}) crossed < EMA({entry_ema_1h:.2f}) ✓, ST=BEAR ✓"
                    f"{adx_str}"
                )
                return self._finalize_entry_volume(
                    signal,
                    {"clear_pending_short_ema_wait": True},
                    volume_window,
                    allow_volume_defer,
                    'ema',
                )
            else:
                # EMA cross happened but ADX too low -> 5-bar ADX wait
                wait_bars = self.adx_wait_bars_short
                logger.info(
                    f"[ADX WAIT] {timestamp_str} | EMA Cross SELL: "
                    f"1H Close crossed < EMA ✓, but ADX({adx_value:.1f}) < {thr_s:g} "
                    f"-> starting {wait_bars}-bar ADX wait"
                )
                updates["set_adx_wait_short"] = {"bars": wait_bars, "trigger": "ema_cross"}
                updates["clear_pending_short_ema_wait"] = True
                return (None, updates)
        
        return (None, updates)
    
    def calculate_exit_levels(
        self,
        entry_price: float,
        is_long: bool
    ) -> Tuple[float, float]:
        """
        Calculate stop loss and take profit levels.
        
        Pine Script Reference (lines 115-123):
        
        if strategy.position_size > 0  // LONG
            longSL = strategy.position_avg_price * (1 - slPct / 100)
            longTP = strategy.position_avg_price * (1 + tpPct / 100)
        
        if strategy.position_size < 0  // SHORT
            shortSL = strategy.position_avg_price * (1 + slPct / 100)
            shortTP = strategy.position_avg_price * (1 - tpPct / 100)
        
        Parameters:
        -----------
        entry_price : float
            Position entry price
        is_long : bool
            True for long position, False for short
        
        Returns:
        --------
        Tuple[float, float]
            (stop_loss_price, take_profit_price)
        """
        sl_pct = self.sl_pct_long if is_long else self.sl_pct_short
        tp_pct = self.tp_pct_long if is_long else self.tp_pct_short
        sl_mult = 1 - (sl_pct / 100) if is_long else 1 + (sl_pct / 100)
        tp_mult = 1 + (tp_pct / 100) if is_long else 1 - (tp_pct / 100)
        
        stop_loss = entry_price * sl_mult
        take_profit = entry_price * tp_mult
        
        logger.debug(f"Exit levels: Entry={entry_price:.2f}, SL={stop_loss:.2f}, TP={take_profit:.2f}")
        
        return stop_loss, take_profit
    
    def check_exit_conditions(
        self,
        bar: pd.Series,
        position_size: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        entry_time: Optional[pd.Timestamp] = None
    ) -> Optional[ExitSignal]:
        """
        Check if any exit condition is met.
        
        Pine Script Reference:
        - Lines 118, 123: TP/SL via strategy.exit()
        - Lines 131-135: Supertrend flip exit via strategy.close()
        
        Parameters:
        -----------
        bar : pd.Series
            Current bar data
        position_size : int
            Current position (1 long, -1 short)
        entry_price : float
            Position entry price
        stop_loss : float
            Stop loss price
        take_profit : float
            Take profit price
        entry_time : pd.Timestamp, optional
            Entry timestamp - exits only checked after this time
        
        Returns:
        --------
        Optional[ExitSignal]
            Exit signal if triggered, None otherwise
        """
        if position_size == 0:
            return None
        
        timestamp = bar.name
        
        # Only check exit conditions AFTER entry time
        if entry_time is not None and timestamp < entry_time:
            return None
        
        high = bar['high']
        low = bar['low']
        close = bar['close']
        
        is_long = position_size > 0
        
        # =====================================================================
        # CHECK TP/SL FIRST (highest priority - protect capital!)
        # =====================================================================
        
        if is_long:
            if low <= stop_loss:
                pnl = stop_loss - entry_price
                return ExitSignal(
                    exit_type=ExitType.STOP_LOSS,
                    timestamp=timestamp,
                    exit_price=stop_loss,
                    entry_price=entry_price,
                    pnl_points=pnl
                )
            if high >= take_profit:
                pnl = take_profit - entry_price
                return ExitSignal(
                    exit_type=ExitType.TAKE_PROFIT,
                    timestamp=timestamp,
                    exit_price=take_profit,
                    entry_price=entry_price,
                    pnl_points=pnl
                )
        else:  # Short
            if high >= stop_loss:
                pnl = entry_price - stop_loss
                return ExitSignal(
                    exit_type=ExitType.STOP_LOSS,
                    timestamp=timestamp,
                    exit_price=stop_loss,
                    entry_price=entry_price,
                    pnl_points=pnl
                )
            if low <= take_profit:
                pnl = entry_price - take_profit
                return ExitSignal(
                    exit_type=ExitType.TAKE_PROFIT,
                    timestamp=timestamp,
                    exit_price=take_profit,
                    entry_price=entry_price,
                    pnl_points=pnl
                )
        
        # =====================================================================
        # CHECK SUPERTREND FLIP EXIT LAST
        # Pine lines 131-135:
        # if strategy.position_size > 0 and stBearFlip
        #     strategy.close("BUY", comment = "ST Flip Exit")
        # if strategy.position_size < 0 and stBullFlip
        #     strategy.close("SELL", comment = "ST Flip Exit")
        # =====================================================================
        
        st_bear_flip_long = bar.get('st_bear_flip_long_exit', bar.get('st_bear_flip_long', bar.get('st_bear_flip', False)))
        # Short ST-flip exit: dedicated short exit series (short_supertrend_exit in strategy.yaml).
        st_bull_flip_short_exit = bar.get(
            'st_bull_flip_short_exit',
            bar.get('st_bull_flip_short', bar.get('st_bull_flip', False)),
        )
        
        if is_long and st_bear_flip_long:
            pnl = close - entry_price
            return ExitSignal(
                exit_type=ExitType.ST_FLIP,
                timestamp=timestamp,
                exit_price=close,
                entry_price=entry_price,
                pnl_points=pnl
            )
        
        if not is_long and st_bull_flip_short_exit:
            pnl = entry_price - close
            return ExitSignal(
                exit_type=ExitType.ST_FLIP,
                timestamp=timestamp,
                exit_price=close,
                entry_price=entry_price,
                pnl_points=pnl
            )
        
        return None
    
    def process_bar(
        self,
        bar: pd.Series,
        state: 'StrategyState'
    ) -> Tuple[Optional[Signal], Optional[ExitSignal], 'StrategyState']:
        """
        Process a single bar and return any signals.
        
        This is the main entry point called for each bar close.
        
        Parameters:
        -----------
        bar : pd.Series
            Bar data with all indicators
        state : StrategyState
            Current strategy state
        
        Returns:
        --------
        Tuple[Optional[Signal], Optional[ExitSignal], StrategyState]
            Entry signal, exit signal, and updated state
        """
        from .state_manager import StrategyState
        
        exit_signal = None
        entry_signal = None
        
        # First check for exits if in position
        if state.position_size != 0:
            exit_signal = self.check_exit_conditions(
                bar=bar,
                position_size=state.position_size,
                entry_price=state.entry_price,
                stop_loss=state.stop_loss,
                take_profit=state.take_profit,
                entry_time=state.entry_time
            )
        
        # If no position (or just exited), check for entries
        if state.position_size == 0:
            entry_signal, _ = self.evaluate_entry_conditions(
                bar=bar,
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
                pending_volume_long=state.pending_volume_long,
                pending_volume_short=state.pending_volume_short,
                volume_wait_bars_left_long=state.volume_wait_bars_left_long,
                volume_wait_bars_left_short=state.volume_wait_bars_left_short,
                volume_wait_trigger_long=state.volume_wait_trigger_long,
                volume_wait_trigger_short=state.volume_wait_trigger_short,
                volume_wait_kind_long=state.volume_wait_kind_long,
                volume_wait_kind_short=state.volume_wait_kind_short,
            )
        
        return entry_signal, exit_signal, state
