"""
=============================================================================
TELEGRAM NOTIFIER
=============================================================================
Sends real-time trade notifications to Telegram.

Notifications:
- Bot start/stop
- Trade entry (with time, direction, price, SL/TP)
- Trade exit (with reason, P&L)
- Running P&L updates while in a trade
- Connection status changes
- Errors
=============================================================================
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any
import pytz
import aiohttp

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends trading notifications to Telegram.
    
    Uses Telegram Bot API directly via aiohttp (no heavy dependency).
    """
    
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        enabled: bool = True,
        timezone: str = "US/Eastern"
    ):
        """
        Initialize Telegram notifier.
        
        Parameters:
        -----------
        bot_token : str
            Telegram bot token from @BotFather
        chat_id : str
            Telegram chat ID to send messages to
        enabled : bool
            Whether notifications are enabled
        timezone : str
            Timezone for timestamps
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.timezone = pytz.timezone(timezone)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        
        # Notification settings (can be updated from config)
        self.notify_bot_start = True
        self.notify_bot_stop = True
        self.notify_trade_entry = True
        self.notify_trade_exit = True
        self.notify_running_pnl = True
        self.notify_connection = True
        self.notify_errors = True
        self.notify_market_hourly = True
        self.notify_supertrend_flip = True
        self.market_status_interval = 3600  # seconds (1 hour)
        self.pnl_interval = 60  # seconds
        
        # Running P&L task
        self._pnl_task: Optional[asyncio.Task] = None
        self._current_trade_info: Optional[Dict[str, Any]] = None
        self._running = False
        
        # Session for HTTP requests
        self._session: Optional[aiohttp.ClientSession] = None
        
        logger.info(f"Telegram Notifier initialized (enabled={enabled})")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Send a message to Telegram.
        
        Parameters:
        -----------
        text : str
            Message text (supports HTML formatting)
        parse_mode : str
            Parse mode (HTML or Markdown)
        
        Returns:
        --------
        bool
            True if sent successfully
        """
        if not self.enabled:
            return False
        
        if not self.bot_token or self.bot_token == "YOUR_BOT_TOKEN_HERE":
            logger.warning("Telegram bot token not configured")
            return False
        
        if not self.chat_id or self.chat_id == "YOUR_CHAT_ID_HERE":
            logger.warning("Telegram chat ID not configured")
            return False
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }
            
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    logger.debug("Telegram message sent successfully")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"Telegram API error {resp.status}: {error_text}")
                    return False
                    
        except asyncio.TimeoutError:
            logger.error("Telegram message send timeout")
            return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    # =========================================================================
    # BOT LIFECYCLE NOTIFICATIONS
    # =========================================================================
    
    async def notify_bot_started(
        self,
        mode: str,
        symbol: str,
        contracts: int,
        strategy_info: Dict[str, Any]
    ) -> None:
        """Send bot start notification."""
        if not self.notify_bot_start:
            return
        
        now = datetime.now(self.timezone)
        
        if strategy_info.get("st_atr_long_entry") is not None:
            strat_lines = (
                f"  • Long ST entry: ATR={strategy_info.get('st_atr_long_entry')}, "
                f"Mult={strategy_info.get('st_mult_long_entry')}\n"
                f"  • Long ST exit: ATR={strategy_info.get('st_atr_long_exit')}, "
                f"Mult={strategy_info.get('st_mult_long_exit')}\n"
                f"  • Short ST entry: ATR={strategy_info.get('st_atr_short_entry')}, "
                f"Mult={strategy_info.get('st_mult_short_entry')}\n"
                f"  • Short ST exit: ATR={strategy_info.get('st_atr_short_exit')}, "
                f"Mult={strategy_info.get('st_mult_short_exit')}\n"
                f"  • EMA: {strategy_info.get('ema_length', 200)} (1H)\n"
                f"  • Long SL/TP: {strategy_info.get('sl_pct_long', 0.4)}% / "
                f"{strategy_info.get('tp_pct_long', 1.2)}%\n"
                f"  • Short SL/TP: {strategy_info.get('sl_pct_short', 0.4)}% / "
                f"{strategy_info.get('tp_pct_short', 1.2)}%\n"
                f"  • ADX long: thresh={strategy_info.get('adx_threshold_long', 20)}, "
                f"use={strategy_info.get('use_adx_long', True)}, "
                f"wait={strategy_info.get('adx_wait_long', 5)} bars\n"
                f"  • ADX short: thresh={strategy_info.get('adx_threshold_short', 20)}, "
                f"use={strategy_info.get('use_adx_short', True)}, "
                f"wait={strategy_info.get('adx_wait_short', 5)} bars\n"
                f"  • Volume: check={strategy_info.get('volume_check', False)}, "
                f"MA={strategy_info.get('volume_ma_period', 20)}, "
                f"lookahead={strategy_info.get('volume_candle_lookahead', 1)}\n"
            )
        elif strategy_info.get("st_atr_long") is not None:
            strat_lines = (
                f"  • Long ST: ATR={strategy_info.get('st_atr_long')}, "
                f"Mult={strategy_info.get('st_mult_long')}\n"
                f"  • Short ST: ATR={strategy_info.get('st_atr_short')}, "
                f"Mult={strategy_info.get('st_mult_short')}\n"
                f"  • EMA: {strategy_info.get('ema_length', 200)} (1H)\n"
                f"  • Long SL/TP: {strategy_info.get('sl_pct_long', 0.4)}% / "
                f"{strategy_info.get('tp_pct_long', 1.2)}%\n"
                f"  • Short SL/TP: {strategy_info.get('sl_pct_short', 0.4)}% / "
                f"{strategy_info.get('tp_pct_short', 1.2)}%\n"
                f"  • ADX long: {strategy_info.get('adx_threshold_long', 20)}, "
                f"short: {strategy_info.get('adx_threshold_short', 20)}\n"
            )
        else:
            strat_lines = (
                f"  • SuperTrend: ATR={strategy_info.get('st_atr', 10)}, "
                f"Mult={strategy_info.get('st_mult', 3)}\n"
                f"  • EMA: {strategy_info.get('ema_length', 200)} (1H)\n"
                f"  • Stop Loss: {strategy_info.get('sl_pct', 0.4)}%\n"
                f"  • Take Profit: {strategy_info.get('tp_pct', 1.2)}%\n"
                f"  • ADX Threshold: {strategy_info.get('adx_threshold', 20)}\n"
            )

        msg = (
            "🟢 <b>BOT STARTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Time: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n"
            f"📊 Mode: <b>{mode}</b>\n"
            f"💹 Symbol: <b>{symbol}</b>\n"
            f"📦 Contracts: <b>{contracts}</b>\n"
            "\n"
            "⚙️ <b>Strategy Settings:</b>\n"
            f"{strat_lines}"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Bot is now monitoring for signals..."
        )
        
        await self.send_message(msg)
    
    async def notify_bot_stopped(self, reason: str = "Manual shutdown") -> None:
        """Send bot stop notification."""
        if not self.notify_bot_stop:
            return
        
        now = datetime.now(self.timezone)
        
        msg = (
            "🔴 <b>BOT STOPPED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Time: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n"
            f"📝 Reason: {reason}\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        
        await self.send_message(msg)
    
    # =========================================================================
    # TRADE NOTIFICATIONS
    # =========================================================================
    
    async def notify_trade_placed(
        self,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        contracts: int,
        trigger: str = "",
        trade_id: int = 0
    ) -> None:
        """
        Send trade entry notification.
        
        Parameters:
        -----------
        direction : str
            'LONG' or 'SHORT'
        entry_price : float
            Entry price
        stop_loss : float
            Stop loss price
        take_profit : float
            Take profit price
        contracts : int
            Number of contracts
        trigger : str
            What triggered the entry
        trade_id : int
            Trade number
        """
        if not self.notify_trade_entry:
            return
        
        now = datetime.now(self.timezone)
        
        direction_emoji = "📈" if direction == "LONG" else "📉"
        
        # Calculate risk/reward
        if direction == "LONG":
            risk_pts = entry_price - stop_loss
            reward_pts = take_profit - entry_price
        else:
            risk_pts = stop_loss - entry_price
            reward_pts = entry_price - take_profit
        
        risk_dollars = risk_pts * 2 * contracts
        reward_dollars = reward_pts * 2 * contracts
        
        msg = (
            f"{direction_emoji} <b>TRADE PLACED - #{trade_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Time: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n"
            f"↕️ Direction: <b>{direction}</b>\n"
            f"💰 Entry Price: <code>{entry_price:,.2f}</code>\n"
            f"📦 Contracts: <b>{contracts}</b>\n"
            "\n"
            "🎯 <b>Exit Levels:</b>\n"
            f"  🟢 Take Profit: <code>{take_profit:,.2f}</code> "
            f"(+{reward_pts:,.2f} pts / +${reward_dollars:,.2f})\n"
            f"  🔴 Stop Loss:   <code>{stop_loss:,.2f}</code> "
            f"(-{risk_pts:,.2f} pts / -${risk_dollars:,.2f})\n"
        )
        
        if trigger:
            msg += f"\n🔔 Trigger: <i>{trigger}</i>\n"
        
        msg += "━━━━━━━━━━━━━━━━━━━━━"
        
        await self.send_message(msg)
        
        # Store trade info for running P&L
        self._current_trade_info = {
            'trade_id': trade_id,
            'direction': direction,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'contracts': contracts,
            'entry_time': now
        }
        
        # Start running P&L updates
        if self.notify_running_pnl:
            self._start_pnl_updates()
    
    async def notify_trade_closed(
        self,
        direction: str,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        pnl_points: float,
        pnl_dollars: float,
        contracts: int,
        trade_id: int = 0,
        entry_time: Optional[datetime] = None
    ) -> None:
        """
        Send trade exit notification.
        
        Parameters:
        -----------
        direction : str
            'LONG' or 'SHORT'
        entry_price : float
            Entry price
        exit_price : float
            Exit price
        exit_reason : str
            Reason for exit (take_profit, stop_loss, st_flip)
        pnl_points : float
            P&L in points
        pnl_dollars : float
            P&L in dollars
        contracts : int
            Number of contracts
        trade_id : int
            Trade number
        entry_time : datetime, optional
            When the trade was entered
        """
        if not self.notify_trade_exit:
            return
        
        now = datetime.now(self.timezone)
        
        # Stop running P&L updates
        self._stop_pnl_updates()
        self._current_trade_info = None
        
        # Determine result emoji and text
        if pnl_dollars > 0:
            result_emoji = "✅"
            result_text = "WIN"
            pnl_sign = "+"
        elif pnl_dollars < 0:
            result_emoji = "❌"
            result_text = "LOSS"
            pnl_sign = ""
        else:
            result_emoji = "➡️"
            result_text = "BREAKEVEN"
            pnl_sign = ""
        
        # Exit reason mapping
        reason_map = {
            "take_profit": "🟢 Take Profit Hit",
            "stop_loss": "🔴 Stop Loss Hit",
            "st_flip": "🔄 SuperTrend Flip Exit",
        }
        reason_display = reason_map.get(exit_reason, f"📝 {exit_reason}")
        
        # Calculate duration
        duration_str = ""
        if entry_time:
            duration = now - entry_time
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            duration_str = f"⏱️ Duration: {hours}h {minutes}m\n"
        
        msg = (
            f"{result_emoji} <b>TRADE CLOSED - #{trade_id} ({result_text})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Time: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n"
            f"↕️ Direction: <b>{direction}</b>\n"
            f"💰 Entry: <code>{entry_price:,.2f}</code>\n"
            f"💰 Exit:  <code>{exit_price:,.2f}</code>\n"
            f"📦 Contracts: <b>{contracts}</b>\n"
            f"{duration_str}"
            "\n"
            f"📝 <b>Exit Reason:</b> {reason_display}\n"
            "\n"
            f"💵 <b>P&L:</b> {pnl_sign}{pnl_points:,.2f} pts "
            f"({pnl_sign}${pnl_dollars:,.2f})\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        
        await self.send_message(msg)
    
    # =========================================================================
    # RUNNING P&L NOTIFICATIONS
    # =========================================================================
    
    def _start_pnl_updates(self) -> None:
        """Start periodic P&L update task."""
        if self._pnl_task and not self._pnl_task.done():
            self._pnl_task.cancel()
        
        self._running = True
        self._pnl_task = asyncio.create_task(self._pnl_update_loop())
    
    def _stop_pnl_updates(self) -> None:
        """Stop periodic P&L update task."""
        self._running = False
        if self._pnl_task and not self._pnl_task.done():
            self._pnl_task.cancel()
        self._pnl_task = None
    
    async def _pnl_update_loop(self) -> None:
        """Periodically send P&L updates while in a trade."""
        try:
            # Wait initial interval before first update
            await asyncio.sleep(self.pnl_interval)
            
            while self._running and self._current_trade_info:
                await self._send_pnl_update()
                await asyncio.sleep(self.pnl_interval)
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"P&L update loop error: {e}")
    
    async def _send_pnl_update(self) -> None:
        """Send a comprehensive P&L update every hour."""
        if not self._current_trade_info:
            return
        
        trade = self._current_trade_info
        current_price = trade.get('current_price', trade['entry_price'])
        
        direction = trade['direction']
        entry_price = trade['entry_price']
        contracts = trade['contracts']
        entry_time = trade.get('entry_time')
        
        # Calculate P&L
        if direction == "LONG":
            pnl_points = current_price - entry_price
        else:
            pnl_points = entry_price - current_price
        
        pnl_dollars = pnl_points * 2 * contracts
        pnl_pct = (pnl_points / entry_price) * 100 if entry_price > 0 else 0
        
        # Determine emoji
        if pnl_dollars > 0:
            pnl_emoji = "🟢"
            pnl_sign = "+"
        elif pnl_dollars < 0:
            pnl_emoji = "🔴"
            pnl_sign = ""
        else:
            pnl_emoji = "➡️"
            pnl_sign = ""
        
        now = datetime.now(self.timezone)
        
        # Calculate trade duration
        duration_str = ""
        if entry_time:
            if isinstance(entry_time, datetime):
                # Make entry_time timezone-aware if it's naive
                if entry_time.tzinfo is None:
                    entry_time_aware = self.timezone.localize(entry_time)
                else:
                    entry_time_aware = entry_time
                duration = now - entry_time_aware
            else:
                duration = now - entry_time
            
            total_seconds = int(duration.total_seconds())
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            
            if days > 0:
                duration_str = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"
        
        # Distance to SL and TP
        tp = trade['take_profit']
        sl = trade['stop_loss']
        
        if direction == "LONG":
            dist_tp_pts = tp - current_price
            dist_sl_pts = current_price - sl
        else:
            dist_tp_pts = current_price - tp
            dist_sl_pts = sl - current_price
        
        dist_tp_dollars = dist_tp_pts * 2 * contracts
        dist_sl_dollars = dist_sl_pts * 2 * contracts
        
        # Progress bar: how close to TP vs SL
        total_range = dist_tp_pts + dist_sl_pts
        if total_range > 0:
            progress = dist_sl_pts / total_range  # 0 = at SL, 1 = at TP
            filled = int(progress * 10)
            bar = "🔴" + "▓" * filled + "░" * (10 - filled) + "🟢"
        else:
            bar = "━━━━━━━━━━"
        
        # Entry time display
        entry_time_str = ""
        if entry_time:
            if isinstance(entry_time, datetime):
                if entry_time.tzinfo is None:
                    entry_time_aware = self.timezone.localize(entry_time)
                else:
                    entry_time_aware = entry_time
                entry_time_str = entry_time_aware.strftime('%Y-%m-%d %H:%M %Z')
            else:
                entry_time_str = str(entry_time)
        
        msg = (
            f"{pnl_emoji} <b>HOURLY P&L UPDATE - #{trade['trade_id']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 Time: <code>{now.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>\n"
            f"↕️ Direction: <b>{direction}</b>\n"
            f"📦 Contracts: <b>{contracts}</b>\n"
            "\n"
            f"💰 Entry: <code>{entry_price:,.2f}</code>"
        )
        
        if entry_time_str:
            msg += f" ({entry_time_str})"
        
        msg += (
            f"\n💹 Current: <code>{current_price:,.2f}</code>\n"
            "\n"
            f"💵 <b>P&L:</b> {pnl_sign}{pnl_points:,.2f} pts "
            f"({pnl_sign}${pnl_dollars:,.2f}) "
            f"({pnl_sign}{pnl_pct:.3f}%)\n"
        )
        
        if duration_str:
            msg += f"⏱️ In trade for: <b>{duration_str}</b>\n"
        
        msg += (
            "\n"
            f"  {bar}\n"
            f"🟢 TP: <code>{tp:,.2f}</code> ({dist_tp_pts:,.2f} pts / ${dist_tp_dollars:,.2f} away)\n"
            f"🔴 SL: <code>{sl:,.2f}</code> ({dist_sl_pts:,.2f} pts / ${dist_sl_dollars:,.2f} away)\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        
        await self.send_message(msg)
    
    async def notify_market_status(
        self,
        mode: str,
        symbol: str,
        price: float,
        st_direction: str,
        ema_status: str,
        adx: float,
        last_bar_ts: str,
        bar_stale_min: Optional[float] = None,
        position_size: int = 0,
        entry_price: float = 0.0,
        feed_bars: int = 0,
        market_open: bool = True,
    ) -> None:
        """Send periodic market status snapshot (hourly by default)."""
        if not self.notify_market_hourly:
            return

        now = datetime.now(self.timezone)
        pos_line = "FLAT"
        if position_size > 0:
            pos_line = f"LONG {abs(position_size)} @ {entry_price:,.2f}"
        elif position_size < 0:
            pos_line = f"SHORT {abs(position_size)} @ {entry_price:,.2f}"

        stale_line = ""
        if bar_stale_min is not None:
            stale_emoji = "⚠️" if bar_stale_min > 25 else "✅"
            stale_line = f"\n{stale_emoji} Last bar age: <b>{bar_stale_min:.0f} min</b>"

        session = "OPEN" if market_open else "CLOSED (maintenance/weekend)"

        msg = (
            f"📊 <b>MARKET STATUS — {symbol}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"🤖 Mode: <b>{mode}</b>\n"
            f"🕐 Session: <b>{session}</b>\n"
            f"💹 Price: <code>{price:,.2f}</code>\n"
            f"📈 SuperTrend: <b>{st_direction}</b>\n"
            f"📉 EMA filter: <b>{ema_status}</b>\n"
            f"📐 ADX: <b>{adx:.1f}</b>\n"
            f"📍 Position: <b>{pos_line}</b>\n"
            f"🕯 Last bar: <code>{last_bar_ts}</code>\n"
            f"📚 Buffered bars: <b>{feed_bars}</b>"
            f"{stale_line}\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(msg)

    async def notify_supertrend_flip(
        self,
        direction: str,
        price: float,
        bar_time: str,
        ema_status: str,
        adx: float,
        mode: str,
        symbol: str = "MNQ",
    ) -> None:
        """Send alert when SuperTrend flips on a completed primary bar."""
        if not self.notify_supertrend_flip:
            return

        emoji = "🟢" if direction.upper() == "BULLISH" else "🔴"
        now = datetime.now(self.timezone)
        msg = (
            f"{emoji} <b>SUPERTREND FLIP — {direction.upper()}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"🤖 Mode: <b>{mode}</b> | {symbol}\n"
            f"🕯 Bar close: <code>{bar_time}</code>\n"
            f"💹 Close: <code>{price:,.2f}</code>\n"
            f"📉 EMA filter: <b>{ema_status}</b>\n"
            f"📐 ADX: <b>{adx:.1f}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(msg)

    def update_current_price(self, price: float) -> None:
        """
        Update the current price for P&L calculations.
        Call this on each bar update or price tick.
        
        Parameters:
        -----------
        price : float
            Current market price
        """
        if self._current_trade_info:
            self._current_trade_info['current_price'] = price
    
    # =========================================================================
    # CONNECTION NOTIFICATIONS
    # =========================================================================
    
    async def notify_connected(self, gateway_type: str = "TWS") -> None:
        """Send connection established notification."""
        if not self.notify_connection:
            return
        
        now = datetime.now(self.timezone)
        msg = (
            f"🔗 <b>Connected to {gateway_type}</b>\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        await self.send_message(msg)
    
    async def notify_disconnected(self, reason: str = "") -> None:
        """Send disconnection notification."""
        if not self.notify_connection:
            return
        
        now = datetime.now(self.timezone)
        msg = (
            "⚠️ <b>DISCONNECTED from Broker</b>\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"📝 {reason}\n"
            "🔄 Attempting to reconnect..."
        )
        await self.send_message(msg)
    
    async def notify_reconnected(self) -> None:
        """Send reconnection notification."""
        if not self.notify_connection:
            return
        
        now = datetime.now(self.timezone)
        msg = (
            "✅ <b>RECONNECTED to Broker</b>\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            "📊 Trading resumed"
        )
        await self.send_message(msg)
    
    # =========================================================================
    # ERROR NOTIFICATIONS
    # =========================================================================
    
    async def notify_error(self, error_msg: str) -> None:
        """Send error notification."""
        if not self.notify_errors:
            return
        
        now = datetime.now(self.timezone)
        msg = (
            "❌ <b>ERROR</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"📝 {error_msg}\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(msg)
    
    # =========================================================================
    # CLEANUP
    # =========================================================================
    
    async def shutdown(self) -> None:
        """Clean up resources. Safe to call multiple times."""
        self._stop_pnl_updates()

        if self._session and not self._session.closed:
            try:
                await self._session.close()
                # Allow event loop to process the close
                await asyncio.sleep(0.25)
            except Exception as e:
                logger.debug(f"Error closing Telegram session: {e}")
        self._session = None

        logger.info("Telegram Notifier shutdown")
