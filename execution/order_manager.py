"""
=============================================================================
ORDER MANAGER
=============================================================================
Handles order placement, modification, and tracking via IBKR API.

Features:
- Market and limit order placement
- Bracket orders (entry with TP/SL)
- Order status tracking
- Cancel-safe reconnection handling
- Margin and pacing compliance
=============================================================================
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import pytz

from ib_async import (
    IB, Contract, Order, Trade as IBTrade,
    MarketOrder, LimitOrder, StopOrder, BracketOrder,
    OrderStatus
)

logger = logging.getLogger(__name__)


class OrderType(Enum):
    """Order type enum."""
    MARKET = "MKT"
    LIMIT = "LMT"
    STOP = "STP"
    STOP_LIMIT = "STP LMT"


class OrderAction(Enum):
    """Order action enum."""
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderTicket:
    """Order ticket tracking."""
    order_id: int
    client_order_id: Optional[str] = None
    action: str = ""
    order_type: str = ""
    quantity: int = 0
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = "pending"
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    placed_time: Optional[datetime] = None
    filled_time: Optional[datetime] = None
    parent_id: Optional[int] = None


@dataclass
class BracketTickets:
    """Bracket order tickets (entry + TP + SL)."""
    entry: OrderTicket
    take_profit: OrderTicket
    stop_loss: OrderTicket


class OrderManager:
    """
    Manages order lifecycle for the trading strategy.
    
    Responsibilities:
    - Place entry orders (market or limit)
    - Attach bracket orders (TP/SL)
    - Track order status
    - Handle order modifications
    - Ensure IBKR pacing compliance
    """
    
    def __init__(
        self,
        ib_client: IB,
        contract: Contract,
        default_qty: int = 1,
        max_orders_per_second: int = 45,
        timezone: str = "US/Eastern"
    ):
        """
        Initialize order manager.
        
        Parameters:
        -----------
        ib_client : IB
            Connected ib-insync client
        contract : Contract
            Trading contract (MNQ futures)
        default_qty : int
            Default order quantity
        max_orders_per_second : int
            IBKR pacing limit (50 max, using 45 for safety)
        timezone : str
            Timezone for timestamps
        """
        self.ib = ib_client
        self.contract = contract
        self.default_qty = default_qty
        self.max_orders_per_sec = max_orders_per_second
        self.timezone = pytz.timezone(timezone)
        
        # Order tracking
        self.pending_orders: Dict[int, OrderTicket] = {}
        self.filled_orders: Dict[int, OrderTicket] = {}
        self.cancelled_orders: Dict[int, OrderTicket] = {}
        
        # Active bracket
        self.active_bracket: Optional[BracketTickets] = None
        
        # Pacing control
        self._order_timestamps: List[datetime] = []
        
        # Register callbacks
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.execDetailsEvent += self._on_execution
        self.ib.errorEvent += self._on_error
        
        logger.info(f"Order Manager initialized for {contract.symbol}")
    
    async def place_market_order(
        self,
        action: str,
        quantity: Optional[int] = None,
        transmit: bool = True
    ) -> Optional[IBTrade]:
        """
        Place a market order.
        
        Parameters:
        -----------
        action : str
            "BUY" or "SELL"
        quantity : int, optional
            Order quantity (default: self.default_qty)
        transmit : bool
            If False, order won't be sent until transmitted
        
        Returns:
        --------
        Optional[Trade]
            IBKR Trade object if successful
        """
        qty = quantity or self.default_qty
        
        # Check pacing
        await self._check_pacing()
        
        order = MarketOrder(
            action=action,
            totalQuantity=qty,
            transmit=transmit
        )
        
        logger.info(f"Placing MARKET {action} order for {qty} {self.contract.symbol}")
        
        try:
            trade = self.ib.placeOrder(self.contract, order)
            
            # Track order
            ticket = OrderTicket(
                order_id=order.orderId,
                action=action,
                order_type="MKT",
                quantity=qty,
                status="submitted",
                placed_time=datetime.now(self.timezone)
            )
            self.pending_orders[order.orderId] = ticket
            
            logger.info(f"Market order placed: ID={order.orderId}")
            return trade
            
        except Exception as e:
            logger.error(f"Failed to place market order: {e}")
            return None
    
    async def place_bracket_order(
        self,
        action: str,
        quantity: int,
        take_profit_price: float,
        stop_loss_price: float,
        entry_type: str = "MKT",
        entry_limit_price: Optional[float] = None
    ) -> Optional[BracketTickets]:
        """
        Place a bracket order (entry with TP and SL).
        
        Pine Script Reference (lines 115-123):
            strategy.exit("BUY EXIT", "BUY", stop = longSL, limit = longTP)
        
        Parameters:
        -----------
        action : str
            "BUY" or "SELL" for entry
        quantity : int
            Order quantity
        take_profit_price : float
            Take profit price
        stop_loss_price : float
            Stop loss price
        entry_type : str
            "MKT" for market, "LMT" for limit
        entry_limit_price : float, optional
            Limit price if entry_type is "LMT"
        
        Returns:
        --------
        Optional[BracketTickets]
            Bracket order tickets if successful
        """
        await self._check_pacing()
        
        # Determine exit action (opposite of entry)
        exit_action = "SELL" if action == "BUY" else "BUY"
        
        # Create parent (entry) order
        if entry_type == "MKT":
            parent = MarketOrder(
                action=action,
                totalQuantity=quantity,
                transmit=False
            )
        else:
            if entry_limit_price is None:
                raise ValueError("entry_limit_price required for LMT order")
            parent = LimitOrder(
                action=action,
                totalQuantity=quantity,
                lmtPrice=entry_limit_price,
                transmit=False
            )
        
        # Create take profit (limit) order
        take_profit = LimitOrder(
            action=exit_action,
            totalQuantity=quantity,
            lmtPrice=take_profit_price,
            transmit=False
        )
        
        # Create stop loss order
        stop_loss = StopOrder(
            action=exit_action,
            totalQuantity=quantity,
            stopPrice=stop_loss_price,
            transmit=True  # Last order transmits all
        )
        
        logger.info(f"Placing BRACKET {action}: Entry={entry_type}, TP={take_profit_price:.2f}, SL={stop_loss_price:.2f}")
        
        try:
            # Place parent order
            parent_trade = self.ib.placeOrder(self.contract, parent)
            parent_id = parent.orderId
            
            # Link child orders to parent
            await asyncio.sleep(0.01)  # Small delay
            
            take_profit.parentId = parent_id
            stop_loss.parentId = parent_id
            
            tp_trade = self.ib.placeOrder(self.contract, take_profit)
            sl_trade = self.ib.placeOrder(self.contract, stop_loss)
            
            # Create tickets
            now = datetime.now(self.timezone)
            
            entry_ticket = OrderTicket(
                order_id=parent_id,
                action=action,
                order_type=entry_type,
                quantity=quantity,
                limit_price=entry_limit_price,
                status="submitted",
                placed_time=now
            )
            
            tp_ticket = OrderTicket(
                order_id=take_profit.orderId,
                action=exit_action,
                order_type="LMT",
                quantity=quantity,
                limit_price=take_profit_price,
                status="submitted",
                placed_time=now,
                parent_id=parent_id
            )
            
            sl_ticket = OrderTicket(
                order_id=stop_loss.orderId,
                action=exit_action,
                order_type="STP",
                quantity=quantity,
                stop_price=stop_loss_price,
                status="submitted",
                placed_time=now,
                parent_id=parent_id
            )
            
            # Track orders
            self.pending_orders[parent_id] = entry_ticket
            self.pending_orders[take_profit.orderId] = tp_ticket
            self.pending_orders[stop_loss.orderId] = sl_ticket
            
            # Store active bracket
            self.active_bracket = BracketTickets(
                entry=entry_ticket,
                take_profit=tp_ticket,
                stop_loss=sl_ticket
            )
            
            logger.info(f"Bracket orders placed: Entry={parent_id}, TP={take_profit.orderId}, SL={stop_loss.orderId}")
            
            return self.active_bracket
            
        except Exception as e:
            logger.error(f"Failed to place bracket order: {e}")
            return None
    
    async def close_position(
        self,
        action: str,
        quantity: int,
        reason: str = "manual"
    ) -> Optional[IBTrade]:
        """
        Close position with market order.
        
        Used for ST flip exit (Pine lines 131-135).
        
        Parameters:
        -----------
        action : str
            "BUY" or "SELL" (opposite of current position)
        quantity : int
            Quantity to close
        reason : str
            Reason for close (for logging)
        
        Returns:
        --------
        Optional[Trade]
            IBKR Trade object if successful
        """
        # Cancel any pending TP/SL orders first
        await self.cancel_pending_bracket_orders()
        
        logger.info(f"Closing position: {action} {quantity}, reason={reason}")
        
        return await self.place_market_order(
            action=action,
            quantity=quantity,
            transmit=True
        )
    
    async def cancel_pending_bracket_orders(self) -> None:
        """Cancel pending TP/SL orders when exiting via ST flip."""
        if not self.active_bracket:
            return
        
        orders_to_cancel = []
        
        # Cancel TP if pending
        tp_ticket = self.active_bracket.take_profit
        if tp_ticket.status in ("submitted", "working"):
            orders_to_cancel.append(tp_ticket.order_id)
        
        # Cancel SL if pending
        sl_ticket = self.active_bracket.stop_loss
        if sl_ticket.status in ("submitted", "working"):
            orders_to_cancel.append(sl_ticket.order_id)
        
        for order_id in orders_to_cancel:
            try:
                # Find order in IB
                for trade in self.ib.openTrades():
                    if trade.order.orderId == order_id:
                        self.ib.cancelOrder(trade.order)
                        logger.info(f"Cancelled order {order_id}")
                        break
            except Exception as e:
                logger.error(f"Failed to cancel order {order_id}: {e}")
        
        self.active_bracket = None
    
    async def modify_stop_loss(self, new_price: float) -> bool:
        """
        Modify the stop loss price.
        
        Note: Pine Script doesn't use trailing stops, but this
        could be useful for future enhancements.
        
        Parameters:
        -----------
        new_price : float
            New stop loss price
        
        Returns:
        --------
        bool
            True if modification successful
        """
        if not self.active_bracket:
            logger.warning("No active bracket to modify")
            return False
        
        sl_order_id = self.active_bracket.stop_loss.order_id
        
        try:
            for trade in self.ib.openTrades():
                if trade.order.orderId == sl_order_id:
                    trade.order.auxPrice = new_price
                    self.ib.placeOrder(self.contract, trade.order)
                    
                    # Update ticket
                    self.active_bracket.stop_loss.stop_price = new_price
                    
                    logger.info(f"Modified SL to {new_price:.2f}")
                    return True
            
            logger.warning(f"SL order {sl_order_id} not found in open trades")
            return False
            
        except Exception as e:
            logger.error(f"Failed to modify SL: {e}")
            return False
    
    def _on_order_status(
        self,
        trade: IBTrade
    ) -> None:
        """Handle order status updates from IBKR."""
        order_id = trade.order.orderId
        status = trade.orderStatus.status
        
        logger.debug(f"Order {order_id} status: {status}")
        
        if order_id in self.pending_orders:
            ticket = self.pending_orders[order_id]
            ticket.status = status.lower()
            
            if status in ("Filled", "Cancelled", "ApiCancelled"):
                # Move to appropriate dict
                if status == "Filled":
                    ticket.filled_qty = int(trade.orderStatus.filled)
                    ticket.avg_fill_price = trade.orderStatus.avgFillPrice
                    ticket.filled_time = datetime.now(self.timezone)
                    self.filled_orders[order_id] = ticket
                    logger.info(f"Order {order_id} FILLED @ {ticket.avg_fill_price:.2f}")
                else:
                    self.cancelled_orders[order_id] = ticket
                    logger.info(f"Order {order_id} {status}")
                
                del self.pending_orders[order_id]
    
    def _on_execution(self, trade: IBTrade, fill) -> None:
        """Handle execution reports."""
        logger.info(f"Execution: {fill.contract.symbol} {fill.execution.side} "
                   f"{fill.execution.shares} @ {fill.execution.avgPrice:.2f}")
    
    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """Handle order errors."""
        # Connection-level errors are handled by ConnectionManager — skip here
        # to avoid duplicate log spam (1100 alone was generating 3 lines per event)
        if errorCode in (1100, 1101, 1102, 2110, 10182):
            return
        if errorCode in (202, 203):  # Order cancelled, Security not found
            logger.warning(f"Order error {errorCode}: {errorString}")
        elif errorCode >= 2000:  # Warnings
            logger.debug(f"Order warning {errorCode}: {errorString}")
        else:
            logger.error(f"Order error {errorCode} for {reqId}: {errorString}")
    
    async def _check_pacing(self) -> None:
        """Ensure compliance with IBKR pacing limits."""
        now = datetime.now(self.timezone)
        
        # Remove timestamps older than 1 second
        self._order_timestamps = [
            ts for ts in self._order_timestamps
            if (now - ts).total_seconds() < 1.0
        ]
        
        # If at limit, wait
        if len(self._order_timestamps) >= self.max_orders_per_sec:
            wait_time = 1.0 - (now - self._order_timestamps[0]).total_seconds()
            if wait_time > 0:
                logger.debug(f"Pacing: waiting {wait_time:.3f}s")
                await asyncio.sleep(wait_time)
        
        self._order_timestamps.append(now)
    
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get list of open orders."""
        return [
            {
                'order_id': t.order_id,
                'action': t.action,
                'type': t.order_type,
                'qty': t.quantity,
                'status': t.status
            }
            for t in self.pending_orders.values()
        ]
    
    def get_position_info(self) -> Dict[str, Any]:
        """Get current position information from IBKR."""
        positions = self.ib.positions()
        
        for pos in positions:
            if pos.contract.symbol == self.contract.symbol:
                return {
                    'symbol': pos.contract.symbol,
                    'position': pos.position,
                    'avg_cost': pos.avgCost,
                    'account': pos.account
                }
        
        return {'position': 0}
    
    async def sync_with_broker(self) -> None:
        """
        Synchronize state with broker on reconnection.
        
        Fetches open orders and positions to restore state.
        """
        logger.info("Syncing with broker...")
        
        # Get open orders
        open_orders = self.ib.openOrders()
        for order in open_orders:
            if order.orderId not in self.pending_orders:
                # Found order not in our tracking
                logger.warning(f"Found untracked order: {order.orderId}")
        
        # Get positions
        positions = self.ib.positions()
        for pos in positions:
            if pos.contract.symbol == self.contract.symbol:
                logger.info(f"Current position: {pos.position} @ {pos.avgCost:.2f}")
        
        logger.info("Broker sync complete")
