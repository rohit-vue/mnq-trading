"""
=============================================================================
POSITION TRACKER
=============================================================================
Tracks positions across IBKR and local state, ensuring consistency.

Features:
- Real-time position monitoring
- P&L tracking
- Margin monitoring
- Daily statistics
=============================================================================
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
import pytz

from ib_async import IB, Contract

logger = logging.getLogger(__name__)


@dataclass
class PositionInfo:
    """Current position information."""
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0
    market_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    account: str = ""


@dataclass
class AccountInfo:
    """Account information for margin/risk checks."""
    net_liquidation: float = 0.0
    buying_power: float = 0.0
    excess_liquidity: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    available_funds: float = 0.0
    daily_pnl: float = 0.0


class PositionTracker:
    """
    Tracks positions and account state in real-time.
    
    Responsibilities:
    - Monitor position changes
    - Calculate unrealized P&L
    - Check margin availability
    - Enforce risk limits
    """
    
    def __init__(
        self,
        ib_client: IB,
        contract: Contract,
        tick_value: float = 0.50,
        tick_size: float = 0.25,
        multiplier: int = 2,
        timezone: str = "US/Eastern"
    ):
        """
        Initialize position tracker.
        
        Parameters:
        -----------
        ib_client : IB
            Connected IBKR client
        contract : Contract
            Trading contract (MNQ)
        tick_value : float
            Value per tick ($0.50 for MNQ)
        tick_size : float
            Minimum price movement ($0.25 for MNQ)
        multiplier : int
            Contract multiplier (2 for MNQ)
        timezone : str
            Display timezone
        """
        self.ib = ib_client
        self.contract = contract
        self.tick_value = tick_value
        self.tick_size = tick_size
        self.multiplier = multiplier
        self.timezone = pytz.timezone(timezone)
        
        # Current position
        self.position = PositionInfo(symbol=contract.symbol)
        
        # Account info
        self.account = AccountInfo()
        
        # Daily tracking
        self.daily_starting_equity: float = 0.0
        self.daily_realized_pnl: float = 0.0
        self.daily_max_equity: float = 0.0
        self.daily_min_equity: float = float('inf')
        
        # Callbacks
        self._on_position_change_callbacks: List[Callable] = []
        self._on_pnl_update_callbacks: List[Callable] = []
        
        # Register IBKR events
        self.ib.positionEvent += self._on_position_update
        self.ib.pnlEvent += self._on_pnl_event
        self.ib.accountValueEvent += self._on_account_value
        
        logger.info(f"Position Tracker initialized for {contract.symbol}")
    
    async def initialize(self) -> None:
        """
        Initialize tracking - fetch current positions and account.
        
        Call this after connection to sync with broker state.
        """
        logger.info("Initializing position tracker...")
        
        # Request account updates (async version)
        # Note: In ib_async, account updates are automatically requested on connect
        # We just need to access the cached data
        await asyncio.sleep(0.5)  # Give time for account updates to sync
        
        # Get current positions (already synced during connection)
        positions = self.ib.positions()
        for pos in positions:
            if pos.contract.symbol == self.contract.symbol:
                self._update_position_from_ibkr(pos)
        
        # Get account summary (already synced during connection)
        account_values = self.ib.accountValues()
        for av in account_values:
            self._parse_account_value(av)
        
        # Set daily baseline
        self.daily_starting_equity = self.account.net_liquidation
        self.daily_max_equity = self.account.net_liquidation
        self.daily_min_equity = self.account.net_liquidation
        
        logger.info(f"Position Tracker initialized: "
                   f"Position={self.position.quantity}, "
                   f"NLV=${self.account.net_liquidation:,.2f}")
    
    def _update_position_from_ibkr(self, ibkr_position) -> None:
        """Update position info from IBKR position object."""
        self.position.quantity = int(ibkr_position.position)
        self.position.avg_cost = ibkr_position.avgCost / self.multiplier  # IBKR returns cost * multiplier
        self.position.account = ibkr_position.account
        
        logger.debug(f"Position updated: {self.position.quantity} @ {self.position.avg_cost:.2f}")
    
    def _on_position_update(self, position) -> None:
        """Handle position updates from IBKR."""
        if position.contract.symbol != self.contract.symbol:
            return
        
        old_qty = self.position.quantity
        self._update_position_from_ibkr(position)
        
        if old_qty != self.position.quantity:
            logger.info(f"Position changed: {old_qty} -> {self.position.quantity}")
            
            # Notify callbacks
            for callback in self._on_position_change_callbacks:
                try:
                    callback(self.position)
                except Exception as e:
                    logger.error(f"Error in position callback: {e}")
    
    def _on_pnl_event(self, pnl) -> None:
        """Handle P&L updates from IBKR."""
        if hasattr(pnl, 'dailyPnL'):
            old_pnl = self.position.unrealized_pnl
            self.position.unrealized_pnl = pnl.unrealizedPnL if hasattr(pnl, 'unrealizedPnL') else 0
            self.position.realized_pnl = pnl.realizedPnL if hasattr(pnl, 'realizedPnL') else 0
            self.account.daily_pnl = pnl.dailyPnL
            
            # Notify callbacks
            for callback in self._on_pnl_update_callbacks:
                try:
                    callback(self.position.unrealized_pnl, self.position.realized_pnl)
                except Exception as e:
                    logger.error(f"Error in P&L callback: {e}")
    
    def _on_account_value(self, account_value) -> None:
        """Handle account value updates."""
        self._parse_account_value(account_value)
    
    def _parse_account_value(self, av) -> None:
        """Parse account value item."""
        key = av.tag
        value = av.value
        
        try:
            float_value = float(value) if value else 0.0
        except (ValueError, TypeError):
            return
        
        if key == "NetLiquidation":
            self.account.net_liquidation = float_value
            # Update daily max/min
            self.daily_max_equity = max(self.daily_max_equity, float_value)
            self.daily_min_equity = min(self.daily_min_equity, float_value)
        elif key == "BuyingPower":
            self.account.buying_power = float_value
        elif key == "ExcessLiquidity":
            self.account.excess_liquidity = float_value
        elif key == "InitMarginReq":
            self.account.initial_margin = float_value
        elif key == "MaintMarginReq":
            self.account.maintenance_margin = float_value
        elif key == "AvailableFunds":
            self.account.available_funds = float_value
    
    def calculate_unrealized_pnl(self, current_price: float) -> float:
        """
        Calculate unrealized P&L based on current price.
        
        Parameters:
        -----------
        current_price : float
            Current market price
        
        Returns:
        --------
        float
            Unrealized P&L in dollars
        """
        if self.position.quantity == 0:
            return 0.0
        
        # Price difference
        if self.position.quantity > 0:  # Long
            price_diff = current_price - self.position.avg_cost
        else:  # Short
            price_diff = self.position.avg_cost - current_price
        
        # Convert to dollars: price_diff * multiplier * abs(quantity)
        pnl = price_diff * self.multiplier * abs(self.position.quantity)
        
        self.position.unrealized_pnl = pnl
        self.position.market_price = current_price
        
        return pnl
    
    def check_margin_for_order(
        self,
        order_qty: int,
        estimated_margin_per_contract: float = 1500
    ) -> bool:
        """
        Check if sufficient margin exists for a new order.
        
        Parameters:
        -----------
        order_qty : int
            Number of contracts to order
        estimated_margin_per_contract : float
            Estimated initial margin per contract
        
        Returns:
        --------
        bool
            True if sufficient margin available
        """
        required_margin = order_qty * estimated_margin_per_contract
        
        # Use excess liquidity for safety
        available = self.account.excess_liquidity
        
        if available < required_margin:
            logger.warning(f"Insufficient margin: Need ${required_margin:,.2f}, "
                         f"Available ${available:,.2f}")
            return False
        
        return True
    
    def check_daily_loss_limit(
        self,
        max_loss_pct: float = 3.0,
        max_loss_amount: Optional[float] = None
    ) -> bool:
        """
        Check if daily loss limit has been hit.
        
        Parameters:
        -----------
        max_loss_pct : float
            Maximum daily loss as % of starting equity
        max_loss_amount : float, optional
            Fixed maximum daily loss
        
        Returns:
        --------
        bool
            True if within limits, False if limit exceeded
        """
        current_pnl = self.account.net_liquidation - self.daily_starting_equity
        
        if max_loss_amount:
            if current_pnl <= -abs(max_loss_amount):
                logger.error(f"Daily loss limit hit: ${current_pnl:,.2f} <= ${-abs(max_loss_amount):,.2f}")
                return False
        
        loss_pct = (current_pnl / self.daily_starting_equity) * 100
        if loss_pct <= -abs(max_loss_pct):
            logger.error(f"Daily loss limit hit: {loss_pct:.2f}% <= {-abs(max_loss_pct):.2f}%")
            return False
        
        return True
    
    def get_daily_drawdown(self) -> float:
        """
        Get current daily drawdown percentage.
        
        Returns:
        --------
        float
            Drawdown from daily high as percentage
        """
        if self.daily_max_equity <= 0:
            return 0.0
        
        current = self.account.net_liquidation
        drawdown = ((self.daily_max_equity - current) / self.daily_max_equity) * 100
        
        return max(0.0, drawdown)
    
    def reset_daily_tracking(self) -> None:
        """Reset daily tracking statistics (call at session start)."""
        logger.info(f"Daily reset - Starting equity: ${self.account.net_liquidation:,.2f}")
        
        self.daily_starting_equity = self.account.net_liquidation
        self.daily_max_equity = self.account.net_liquidation
        self.daily_min_equity = self.account.net_liquidation
        self.daily_realized_pnl = 0.0
    
    def on_position_change(self, callback: Callable) -> None:
        """Register callback for position changes."""
        self._on_position_change_callbacks.append(callback)
    
    def on_pnl_update(self, callback: Callable) -> None:
        """Register callback for P&L updates."""
        self._on_pnl_update_callbacks.append(callback)
    
    def get_position_summary(self) -> Dict[str, Any]:
        """Get current position summary."""
        return {
            'symbol': self.position.symbol,
            'quantity': self.position.quantity,
            'direction': 'LONG' if self.position.quantity > 0 
                        else 'SHORT' if self.position.quantity < 0 
                        else 'FLAT',
            'avg_cost': self.position.avg_cost,
            'market_price': self.position.market_price,
            'unrealized_pnl': self.position.unrealized_pnl,
            'realized_pnl': self.position.realized_pnl
        }
    
    def get_account_summary(self) -> Dict[str, Any]:
        """Get current account summary."""
        return {
            'net_liquidation': self.account.net_liquidation,
            'buying_power': self.account.buying_power,
            'excess_liquidity': self.account.excess_liquidity,
            'initial_margin': self.account.initial_margin,
            'maintenance_margin': self.account.maintenance_margin,
            'daily_pnl': self.account.daily_pnl,
            'daily_drawdown_pct': self.get_daily_drawdown()
        }
    
    async def shutdown(self) -> None:
        """Clean up and stop tracking."""
        # Account updates are cancelled when IB disconnects
        logger.info("Position tracker shutdown")
