"""
=============================================================================
TRADING DASHBOARD
=============================================================================
Clean terminal dashboard showing:
- Active trades
- P&L (realized and unrealized)
- Position information
- Strategy state
- Connection status

All calculations happen in the background. Dashboard is clean and informative.
=============================================================================
"""

import os
import sys
from datetime import datetime
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
import pytz


@dataclass
class TradeInfo:
    """Active trade information."""
    trade_id: int
    direction: str  # 'LONG' or 'SHORT'
    entry_price: float
    entry_time: datetime
    quantity: int
    stop_loss: float
    take_profit: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0


class TradingDashboard:
    """
    Clean terminal dashboard for trading bot.
    
    Displays key information in a clean, organized format.
    All calculations happen in the background.
    """
    
    def __init__(
        self,
        symbol: str = "MNQ",
        timezone: str = "US/Eastern"
    ):
        """Initialize dashboard."""
        self.symbol = symbol
        self.timezone = pytz.timezone(timezone)
        
        # State
        self.is_connected = False
        self.active_trade: Optional[TradeInfo] = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.total_trades = 0
        self.winning_trades = 0
        self.realized_pnl = 0.0
        self.account_value = 0.0
        self.buying_power = 0.0
        
        # Strategy state
        self.st_direction = "NEUTRAL"  # BULL or BEAR
        self.ema_status = "NEUTRAL"  # BULL or BEAR
        self.adx_value = 0.0
        self.ema_1h: float = 0.0
        self.close_1h: float = 0.0
        self.current_price = 0.0
        self.last_bar_time: Optional[datetime] = None
        
        # Trade history (last 5 trades)
        self.recent_trades: List[Dict[str, Any]] = []
    
    def update_connection_status(self, is_connected: bool) -> None:
        """Update connection status."""
        self.is_connected = is_connected
    
    def update_price(self, price: float, bar_time: Optional[datetime] = None) -> None:
        """Update current price."""
        self.current_price = price
        if bar_time:
            self.last_bar_time = bar_time
        
        # Update unrealized P&L if in position
        if self.active_trade:
            if self.active_trade.direction == "LONG":
                points_diff = price - self.active_trade.entry_price
            else:
                points_diff = self.active_trade.entry_price - price
            
            # MNQ: $2 per point per contract
            self.active_trade.unrealized_pnl = points_diff * 2 * self.active_trade.quantity
            self.active_trade.current_price = price
    
    def update_indicators(
        self,
        st_direction: str,
        ema_status: str,
        adx_value: float,
        *,
        ema_1h: Optional[float] = None,
        close_1h: Optional[float] = None,
        signal_bar_time: Optional[datetime] = None,
    ) -> None:
        """Update indicator values (and optional 1H EMA context for logging/UI)."""
        self.st_direction = st_direction
        self.ema_status = ema_status
        self.adx_value = adx_value
        if ema_1h is not None and ema_1h == ema_1h:
            self.ema_1h = float(ema_1h)
        if close_1h is not None and close_1h == close_1h:
            self.close_1h = float(close_1h)
        if signal_bar_time is not None:
            self.last_bar_time = signal_bar_time
    
    def update_account(
        self,
        account_value: float,
        buying_power: float,
        daily_pnl: float
    ) -> None:
        """Update account information."""
        self.account_value = account_value
        self.buying_power = buying_power
        self.daily_pnl = daily_pnl
    
    def on_entry(
        self,
        trade_id: int,
        direction: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float
    ) -> None:
        """Record trade entry."""
        self.active_trade = TradeInfo(
            trade_id=trade_id,
            direction=direction,
            entry_price=entry_price,
            entry_time=datetime.now(self.timezone),
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            current_price=entry_price
        )
        self.daily_trades += 1
        self.total_trades += 1
    
    def on_exit(
        self,
        exit_price: float,
        exit_type: str,
        pnl_dollars: float
    ) -> None:
        """Record trade exit."""
        if self.active_trade:
            # Add to recent trades
            trade_record = {
                'id': self.active_trade.trade_id,
                'direction': self.active_trade.direction,
                'entry': self.active_trade.entry_price,
                'exit': exit_price,
                'pnl': pnl_dollars,
                'exit_type': exit_type,
                'time': datetime.now(self.timezone)
            }
            
            self.recent_trades.insert(0, trade_record)
            if len(self.recent_trades) > 5:
                self.recent_trades = self.recent_trades[:5]
            
            # Update stats
            self.realized_pnl += pnl_dollars
            if pnl_dollars > 0:
                self.winning_trades += 1
            
            self.active_trade = None
    
    def clear_screen(self) -> None:
        """Clear terminal screen."""
        if sys.platform == 'win32':
            os.system('cls')
        else:
            os.system('clear')
    
    def render(self) -> str:
        """
        Render dashboard to string.
        
        Returns the complete dashboard as a formatted string.
        """
        now = datetime.now(self.timezone)
        
        lines = []
        
        # Header
        lines.append("")
        lines.append("=" * 70)
        lines.append("                    MNQ TRADING DASHBOARD")
        lines.append("=" * 70)
        lines.append("")
        
        # Status bar
        conn_status = "[OK] CONNECTED" if self.is_connected else "[X] DISCONNECTED"
        lines.append(f"  Status: {conn_status}    Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        lines.append("")
        
        # Current Market Status
        lines.append("-" * 70)
        lines.append("  MARKET STATUS")
        lines.append("-" * 70)
        lines.append(f"  Symbol: {self.symbol}")
        lines.append(f"  Price:  {self.current_price:,.2f}")
        
        if self.last_bar_time:
            lines.append(f"  Last Bar: {self.last_bar_time.strftime('%H:%M')}")
        
        lines.append("")
        ema_line = f"  SuperTrend: {self.st_direction:8}  EMA: {self.ema_status:8}  ADX: {self.adx_value:5.1f}"
        if self.ema_1h > 0:
            ema_line += f"  |  EMA200(1H): {self.ema_1h:,.2f}"
        if self.close_1h > 0:
            ema_line += f"  Close(1H): {self.close_1h:,.2f}"
        lines.append(ema_line)
        lines.append("")
        
        # Active Position
        lines.append("-" * 70)
        lines.append("  ACTIVE POSITION")
        lines.append("-" * 70)
        
        if self.active_trade:
            trade = self.active_trade
            pnl_color = "+" if trade.unrealized_pnl >= 0 else ""
            
            lines.append(f"  Direction:  {trade.direction}")
            lines.append(f"  Entry:      {trade.entry_price:,.2f}  Qty: {trade.quantity}")
            lines.append(f"  Current:    {trade.current_price:,.2f}")
            lines.append(f"  Stop Loss:  {trade.stop_loss:,.2f}  Take Profit: {trade.take_profit:,.2f}")
            lines.append(f"  Unrealized: {pnl_color}${trade.unrealized_pnl:,.2f}")
        else:
            lines.append("  No active position - FLAT")
        
        lines.append("")
        
        # P&L Summary
        lines.append("-" * 70)
        lines.append("  P&L SUMMARY")
        lines.append("-" * 70)
        
        realized_color = "+" if self.realized_pnl >= 0 else ""
        daily_color = "+" if self.daily_pnl >= 0 else ""
        
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        
        lines.append(f"  Realized P&L:  {realized_color}${self.realized_pnl:,.2f}")
        lines.append(f"  Daily P&L:     {daily_color}${self.daily_pnl:,.2f}")
        lines.append(f"  Today Trades:  {self.daily_trades}    Total: {self.total_trades}    Win Rate: {win_rate:.1f}%")
        lines.append("")
        
        # Account Info
        if self.account_value > 0:
            lines.append("-" * 70)
            lines.append("  ACCOUNT")
            lines.append("-" * 70)
            lines.append(f"  Net Liquidation: ${self.account_value:,.2f}")
            lines.append(f"  Buying Power:    ${self.buying_power:,.2f}")
            lines.append("")
        
        # Recent Trades
        if self.recent_trades:
            lines.append("-" * 70)
            lines.append("  RECENT TRADES (Last 5)")
            lines.append("-" * 70)
            
            for trade in self.recent_trades:
                pnl_str = f"+${trade['pnl']:,.2f}" if trade['pnl'] >= 0 else f"-${abs(trade['pnl']):,.2f}"
                exit_str = trade['exit_type'].upper()
                lines.append(f"  #{trade['id']} {trade['direction']:5} | {trade['entry']:,.2f} → {trade['exit']:,.2f} | {pnl_str:>12} | {exit_str}")
            
            lines.append("")
        
        # Footer
        lines.append("=" * 70)
        lines.append("  Press Ctrl+C to stop")
        lines.append("=" * 70)
        lines.append("")
        
        return "\n".join(lines)
    
    def print_dashboard(self, clear: bool = True) -> None:
        """
        Print dashboard to terminal.
        
        Parameters:
        -----------
        clear : bool
            If True, clear screen before printing
        """
        if clear:
            self.clear_screen()
        
        print(self.render())
    
    def print_event(self, event_type: str, message: str) -> None:
        """
        Print an event message without clearing dashboard.
        
        Parameters:
        -----------
        event_type : str
            Type of event (ENTRY, EXIT, SIGNAL, etc.)
        message : str
            Event message
        """
        now = datetime.now(self.timezone).strftime('%H:%M:%S')
        
        if event_type == "ENTRY":
            icon = "[+]"
        elif event_type == "EXIT":
            icon = "[-]"
        elif event_type == "SIGNAL":
            icon = "[*]"
        elif event_type == "ERROR":
            icon = "[X]"
        elif event_type == "WARNING":
            icon = "[!]"
        else:
            icon = "[i]"
        
        print(f"  {icon} [{now}] {event_type}: {message}")
