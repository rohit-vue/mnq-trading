"""
=============================================================================
MNQ SUPERTREND + EMA TRADING SYSTEM - v2.0
=============================================================================
Interactive trading system for MNQ futures.
Run: python main_v2.py

Features (v2.0):
- Auto-reconnect when TWS disconnects or restarts
- Market orders instead of limit orders
- Clean terminal dashboard with P&L tracking
- All calculations in background, dashboard stays clean

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

# Configure logging to file only (keep terminal clean)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('trading.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

# Primary timeframe from strategy.yaml (e.g. 10m, 15m)
from timeframe_utils import get_primary_bar_size, get_primary_bars_per_hour

# Also add a stream handler for errors only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
logging.getLogger().addHandler(console_handler)

# Suppress verbose logs
logging.getLogger('strategy.signal_engine').setLevel(logging.INFO)  # INFO to see entry condition checks
logging.getLogger('strategy.state_manager').setLevel(logging.WARNING)
# Keep backtest.backtest_engine at INFO level to see flip and entry logs
logging.getLogger('backtest.backtest_engine').setLevel(logging.INFO)
logging.getLogger('ib_async.wrapper').setLevel(logging.WARNING)
logging.getLogger('ib_async.client').setLevel(logging.WARNING)
logging.getLogger('ib_async.ib').setLevel(logging.WARNING)


def print_banner():
    """Print welcome banner."""
    print("\n" + "=" * 60)
    print("     MNQ SUPERTREND + EMA TRADING SYSTEM v2.1")
    print("=" * 60)
    print()


def load_config(config_dir: str = "./config") -> dict:
    """Load all configuration files."""
    config_path = Path(config_dir)
    config = {}
    
    config_files = ['strategy.yaml', 'mnq_contract.yaml', 'risk.yaml', 'ibkr.yaml', 'telegram.yaml']
    
    for filename in config_files:
        filepath = config_path / filename
        if filepath.exists():
            with open(filepath, 'r') as f:
                file_config = yaml.safe_load(f)
                config[filename.replace('.yaml', '')] = file_config
    
    return config


def get_connection_port(ibkr_cfg: dict, mode: str = "paper") -> int:
    """
    Get the correct port based on default_gateway and mode.
    
    Supports:
    - TWS Paper: 7497
    - TWS Live: 7496
    - Gateway Paper: 4002 (for 24/7 operation)
    - Gateway Live: 4001 (for 24/7 operation)
    """
    conn_cfg = ibkr_cfg.get('connection', {})
    ports = conn_cfg.get('ports', {})
    gateway_type = conn_cfg.get('default_gateway', 'tws')
    
    if mode == 'paper':
        if gateway_type == 'gateway':
            return ports.get('gateway_paper', 4002)
        return ports.get('tws_paper', 7497)
    else:  # live
        if gateway_type == 'gateway':
            return ports.get('gateway_live', 4001)
        return ports.get('tws_live', 7496)


def create_telegram_notifier(config: dict) -> 'TelegramNotifier':
    """
    Create and configure TelegramNotifier from config.
    Credentials: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars override
    config/telegram.yaml (see .env.example).
    Returns a configured TelegramNotifier instance.
    """
    import os

    from utils import TelegramNotifier

    tg_cfg = config.get('telegram', {})
    tg_main = tg_cfg.get('telegram', {})
    tg_notify = tg_cfg.get('notifications', {})

    bot_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or str(
        tg_main.get("bot_token", "") or ""
    )
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or str(
        tg_main.get("chat_id", "") or ""
    )

    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        enabled=tg_main.get('enabled', False)
    )
    
    # Configure notification types
    notifier.notify_bot_start = tg_notify.get('bot_start', True)
    notifier.notify_bot_stop = tg_notify.get('bot_stop', True)
    notifier.notify_trade_entry = tg_notify.get('trade_entry', True)
    notifier.notify_trade_exit = tg_notify.get('trade_exit', True)
    notifier.notify_connection = tg_notify.get('connection_status', True)
    notifier.notify_errors = tg_notify.get('errors', True)
    
    pnl_cfg = tg_notify.get('running_pnl', {})
    notifier.notify_running_pnl = pnl_cfg.get('enabled', True)
    notifier.pnl_interval = pnl_cfg.get('interval_seconds', 60)
    
    return notifier


def get_menu_choice() -> str:
    """Display main menu and get user choice."""
    print("What would you like to do?\n")
    print("  [1] Backtest (IBKR) - Test strategy on IBKR historical data")
    print("  [2] Backtest (Databento) - Test strategy on Databento CSV data")
    print("  [3] Paper Trade - Trade on IBKR paper account (with Dashboard)")
    print("  [4] Live Trade - Trade with REAL money (with Dashboard)")
    print("  [0] Exit")
    print()
    
    while True:
        choice = input("Enter your choice (0-4): ").strip()
        if choice in ['0', '1', '2', '3', '4']:
            return choice
        print("Invalid choice. Please enter 0, 1, 2, 3, or 4.")


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


async def startup_reconcile_position(
    ib, contract, position_tracker, state_manager, order_manager,
    dashboard, telegram, contracts_count: int, mode_label: str = "PAPER",
    signal_engine=None
) -> None:
    """
    Check IBKR for an active position and open orders at startup or reconnect.

    Reconstructs bot state (entry price, SL, TP, active_bracket) so the bot
    can continue monitoring an existing position without missing exits.

    Flow:
    1. Read actual IBKR position via position_tracker
    2. Force-fetch fresh open orders from IBKR (reqAllOpenOrdersAsync)
    3. Scan open orders for SL (STP) and TP (LMT) bracket orders
    4. If saved state matches direction → trust saved entry_price, update SL/TP
    5. If no matching saved state → rebuild state from IBKR avg_cost
    6. If no bracket orders found → calculate SL/TP from entry price via signal_engine
    7. Reconstruct order_manager.active_bracket so cancel-on-exit works
    8. Update dashboard + send Telegram alert
    """
    from execution.order_manager import OrderTicket, BracketTickets

    ib_qty = position_tracker.position.quantity
    avg_cost = position_tracker.position.avg_cost

    if ib_qty == 0:
        # No active IBKR position
        if state_manager.state.position_size != 0:
            # Stale state file — IBKR is flat, clear saved state
            msg = (f"Stale state: bot tracked position={state_manager.state.position_size} "
                   f"but IBKR is flat. Clearing state.")
            logger.warning(msg)
            dashboard.print_event("WARNING", msg)
            state_manager.state.position_size = 0
            state_manager.state.entry_price = 0.0
            state_manager.state.stop_loss = 0.0
            state_manager.state.take_profit = 0.0
            state_manager.state.entry_time = None
            state_manager.save_state()
        else:
            dashboard.print_event("INFO", "No active position at startup - waiting for signals")
        return

    # --- Active position found in IBKR ---
    direction = "LONG" if ib_qty > 0 else "SHORT"
    exit_action = "SELL" if ib_qty > 0 else "BUY"

    # Force-fetch fresh open orders from IBKR.
    # ib.openTrades() returns cached data — reqAllOpenOrdersAsync() ensures
    # bracket child orders (SL/TP) are present before we scan.
    stop_price = 0.0
    limit_price = 0.0
    sl_trade_obj = None
    tp_trade_obj = None

    try:
        await ib.reqAllOpenOrdersAsync()
        await asyncio.sleep(0.5)  # Let event loop process the response
    except Exception as e:
        logger.debug(f"reqAllOpenOrders: {e}")

    open_trades = ib.openTrades()
    logger.info(f"Scanning {len(open_trades)} open orders for bracket SL/TP...")

    for t in open_trades:
        tc = t.contract
        # Robust contract match: use conId if both are set, else fall back to symbol
        if contract.conId and tc.conId:
            match = (tc.conId == contract.conId)
        else:
            match = (tc.symbol == contract.symbol)
        if not match:
            continue

        order = t.order
        logger.info(f"  Order: {order.orderType} {order.action} "
                    f"auxPrice={getattr(order, 'auxPrice', 'N/A')} "
                    f"lmtPrice={getattr(order, 'lmtPrice', 'N/A')} "
                    f"parentId={getattr(order, 'parentId', 'N/A')}")

        if order.orderType in ('STP', 'STP LMT') and order.action == exit_action:
            sp = float(getattr(order, 'auxPrice', 0) or 0)
            if sp > 0:
                stop_price = sp
                sl_trade_obj = t
        elif order.orderType == 'LMT' and order.action == exit_action:
            lp = float(getattr(order, 'lmtPrice', 0) or 0)
            if lp > 0:
                limit_price = lp
                tp_trade_obj = t

    logger.info(f"Bracket orders found: SL={stop_price or 'none'}, TP={limit_price or 'none'}")

    # Determine state source
    state_matches = (
        (state_manager.state.position_size > 0 and ib_qty > 0) or
        (state_manager.state.position_size < 0 and ib_qty < 0)
    )

    if state_matches:
        # Saved state matches IBKR direction — trust saved entry_price
        entry_price = state_manager.state.entry_price or avg_cost
        if stop_price > 0:
            state_manager.state.stop_loss = stop_price
        if limit_price > 0:
            state_manager.state.take_profit = limit_price
        state_manager.save_state()
        source = "saved state"
    else:
        # No matching saved state — reconstruct entirely from IBKR
        state_manager.state.position_size = 1 if ib_qty > 0 else -1
        state_manager.state.entry_price = avg_cost
        state_manager.state.entry_time = pd.Timestamp.now()
        state_manager.state.stop_loss = stop_price
        state_manager.state.take_profit = limit_price
        if ib_qty > 0:
            state_manager.state.traded_in_bull_trend = True
        else:
            state_manager.state.traded_in_bear_trend = True
        state_manager.state.trade_count = max(state_manager.state.trade_count, 1)
        state_manager.save_state()
        entry_price = avg_cost
        source = "IBKR data"

    entry_price = state_manager.state.entry_price
    sl = state_manager.state.stop_loss
    tp = state_manager.state.take_profit

    # --- Fallback: no bracket orders found → calculate from entry price ---
    sl_tp_source = "open orders"
    if (sl == 0.0 or tp == 0.0) and signal_engine:
        is_long = ib_qty > 0
        calc_sl, calc_tp = signal_engine.calculate_exit_levels(
            entry_price=entry_price,
            is_long=is_long
        )
        if sl == 0.0:
            sl = calc_sl
            state_manager.state.stop_loss = sl
            logger.info(f"No SL bracket order — calculated SL={sl:.2f} from entry price")
        if tp == 0.0:
            tp = calc_tp
            state_manager.state.take_profit = tp
            logger.info(f"No TP bracket order — calculated TP={tp:.2f} from entry price")
        state_manager.save_state()
        sl_tp_source = "calculated from entry"
    elif sl == 0.0 or tp == 0.0:
        logger.warning(f"SL/TP not found in open orders and no signal_engine for fallback. "
                       f"SL={sl}, TP={tp} — exits may not trigger correctly!")
        sl_tp_source = "MISSING"

    now = datetime.now(pytz.timezone("US/Eastern"))

    # Rebuild active_bracket so cancel_pending_bracket_orders() works on exit
    sl_ticket = OrderTicket(
        order_id=sl_trade_obj.order.orderId if sl_trade_obj else -1,
        action=exit_action,
        order_type="STP",
        quantity=abs(ib_qty),
        stop_price=sl,
        status="working" if sl_trade_obj else "calculated",
        placed_time=now
    )
    tp_ticket = OrderTicket(
        order_id=tp_trade_obj.order.orderId if tp_trade_obj else -1,
        action=exit_action,
        order_type="LMT",
        quantity=abs(ib_qty),
        limit_price=tp,
        status="working" if tp_trade_obj else "calculated",
        placed_time=now
    )
    entry_ticket = OrderTicket(
        order_id=-1,
        action="BUY" if ib_qty > 0 else "SELL",
        order_type="MKT",
        quantity=abs(ib_qty),
        status="filled",
        placed_time=now
    )
    order_manager.active_bracket = BracketTickets(
        entry=entry_ticket,
        take_profit=tp_ticket,
        stop_loss=sl_ticket
    )

    # Update dashboard to show active trade
    trade_id = max(state_manager.state.trade_count, 1)
    dashboard.on_entry(
        trade_id=trade_id,
        direction=direction,
        entry_price=entry_price,
        quantity=abs(ib_qty),
        stop_loss=sl,
        take_profit=tp
    )

    open_orders_count = sum(1 for x in [sl_trade_obj, tp_trade_obj] if x)
    sl_str = f"{sl:.2f}" if sl > 0 else "N/A"
    tp_str = f"{tp:.2f}" if tp > 0 else "N/A"
    msg = (f"Resumed {direction} ({source}): {abs(ib_qty)} contracts "
           f"@ {entry_price:.2f} | SL={sl_str} | TP={tp_str} "
           f"[{sl_tp_source}] | Open orders: {open_orders_count}")
    logger.info(msg)
    dashboard.print_event("INFO", msg)

    await telegram.send_message(
        f"🔄 <b>ACTIVE POSITION DETECTED ({mode_label})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"↕️ Direction: <b>{direction}</b>\n"
        f"📦 Qty: {abs(ib_qty)} contracts\n"
        f"💰 Entry: <code>{entry_price:.2f}</code>\n"
        f"🔴 SL: <code>{sl_str}</code>\n"
        f"🟢 TP: <code>{tp_str}</code>\n"
        f"📋 SL/TP source: {sl_tp_source}\n"
        f"📋 Open bracket orders tracked: {open_orders_count}\n"
        f"✅ Bot is monitoring this position"
    )


async def run_paper_trading_v2(config: dict) -> None:
    """
    Run paper trading mode with:
    - Auto-reconnect
    - Market orders
    - Clean dashboard
    - Telegram notifications
    - IB Gateway support for 24/7 operation
    """
    from ib_async import IB, Future
    from data import RealtimeFeed, MultiTimeframeFeed
    from strategy import SignalEngine, StateManager, SignalType
    from execution import OrderManager, PositionTracker
    from utils import ConnectionManager, ConnectionConfig, TradingDashboard
    
    print("\n" + "=" * 60)
    print("             PAPER TRADING MODE v2.0")
    print("=" * 60)
    
    contracts = get_contracts()
    
    # Get configs
    ibkr_cfg = config.get('ibkr', {})
    strategy_cfg = config.get('strategy', {})
    risk_cfg = config.get('risk', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    adx_cfg = strategy_cfg.get('adx', {})
    from utils.strategy_side_config import (
        resolve_side_configs,
        signal_engine_init_kwargs,
        strategy_info_for_telegram,
    )
    from data.strategy_indicators import live_bar_indicator_slice, bar_flips_for_state_manager

    sides = resolve_side_configs(strategy_cfg)
    
    conn_cfg = ibkr_cfg.get('connection', {})
    recon_cfg = ibkr_cfg.get('reconnection', {})
    
    # Smart port selection: uses IB Gateway for 24/7, TWS otherwise
    port = get_connection_port(ibkr_cfg, mode='paper')
    gateway_type = conn_cfg.get('default_gateway', 'tws').upper()
    
    # Initialize Telegram notifier
    telegram = create_telegram_notifier(config)
    
    # Initialize dashboard
    dashboard = TradingDashboard(symbol="MNQ")
    
    # Create connection config
    connection_config = ConnectionConfig(
        host=conn_cfg.get('host', '127.0.0.1'),
        port=port,
        client_id=conn_cfg.get('client_id', 1),
        max_reconnect_attempts=recon_cfg.get('max_attempts', 0),  # 0 = infinite
        initial_delay=recon_cfg.get('initial_delay_sec', 5),
        max_delay=recon_cfg.get('max_delay_sec', 60),
        backoff_multiplier=recon_cfg.get('backoff_multiplier', 2.0)
    )
    
    # Shared state for reconnection
    shared_state = {
        'feed': None,
        'order_manager': None,
        'position_tracker': None,
        'signal_engine': None,
        'state_manager': None,
        'mtf': None,
        'contract': None,
        'running': True
    }
    
    async def on_reconnect():
        """Handle reconnection - resync with broker and verify positions."""
        dashboard.print_event("INFO", f"Reconnected to {gateway_type} - Resyncing...")
        dashboard.update_connection_status(True)

        # Step 1: Resync positions and orders with IBKR
        if shared_state['order_manager']:
            await shared_state['order_manager'].sync_with_broker()
        if shared_state['position_tracker']:
            await shared_state['position_tracker'].initialize()

        # Step 2: Reconcile position state using the same logic as startup
        if shared_state['state_manager'] and shared_state['position_tracker'] and shared_state['order_manager']:
            await startup_reconcile_position(
                ib=ib,
                contract=shared_state['contract'],
                position_tracker=shared_state['position_tracker'],
                state_manager=shared_state['state_manager'],
                order_manager=shared_state['order_manager'],
                dashboard=dashboard,
                telegram=telegram,
                contracts_count=contracts,
                mode_label="PAPER",
                signal_engine=shared_state.get('signal_engine')
            )

        # Step 3: Restart data feed
        if shared_state['feed']:
            await shared_state['feed'].start(initial_lookback_days=5)

        dashboard.print_event("INFO", "Resync complete - Trading active")
        await telegram.notify_reconnected()

    def on_disconnect():
        """Handle disconnect."""
        dashboard.update_connection_status(False)
        dashboard.print_event("WARNING", f"Disconnected from {gateway_type} - Attempting to reconnect...")
        asyncio.create_task(telegram.notify_disconnected(f"Lost connection to {gateway_type}"))

    async def on_extended_disconnect(attempt_count):
        """Alert user when reconnection has been failing for extended period."""
        msg = (f"ALERT: {attempt_count} failed reconnection attempts (~{attempt_count} min). "
               f"IB Gateway on port {port} may require manual restart.")
        dashboard.print_event("ERROR", msg)
        logger.error(msg)
        await telegram.notify_error(msg)

    # Create connection manager
    conn_manager = ConnectionManager(
        config=connection_config,
        on_reconnect=on_reconnect,
        on_disconnect=on_disconnect,
        on_extended_disconnect=on_extended_disconnect
    )

    try:
        print(f"\nConnecting to IBKR Paper via {gateway_type} on port {port}...")
        if not await conn_manager.connect():
            print("[X] Failed to connect to IBKR")
            await telegram.notify_error(f"Failed to connect to IBKR {gateway_type} on port {port}")
            return

        dashboard.update_connection_status(True)
        print(f"[OK] Connected to IBKR Paper Account via {gateway_type}")
        await telegram.notify_connected(gateway_type)
        
        ib = conn_manager.client
        
        # Get front-month MNQ contract
        base_contract = Future(symbol='MNQ', exchange='CME', currency='USD')
        contract_details = await ib.reqContractDetailsAsync(base_contract)
        
        if not contract_details:
            print("[X] Error: No MNQ contracts found")
            await telegram.notify_error("No MNQ contracts found")
            return
        
        sorted_contracts = sorted(
            contract_details,
            key=lambda x: x.contract.lastTradeDateOrContractMonth
        )
        contract = sorted_contracts[0].contract
        shared_state['contract'] = contract
        print(f"[OK] Trading: {contract.localSymbol}")
        
        # Initialize components
        signal_engine = SignalEngine(
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            **signal_engine_init_kwargs(strategy_cfg),
        )
        shared_state['signal_engine'] = signal_engine
        
        state_manager = StateManager(
            state_file="./data/paper_state.json",
            tick_value=0.50,
            contracts_per_trade=contracts
        )
        shared_state['state_manager'] = state_manager
        
        order_manager = OrderManager(
            ib_client=ib,
            contract=contract,
            default_qty=contracts
        )
        shared_state['order_manager'] = order_manager
        
        position_tracker = PositionTracker(ib_client=ib, contract=contract)
        await position_tracker.initialize()
        shared_state['position_tracker'] = position_tracker

        # --- STARTUP POSITION CHECK ---
        # Check if IBKR already has an active position (e.g., manual order, crash recovery)
        # Reconstruct bot state and track open SL/TP orders before the feed starts
        await startup_reconcile_position(
            ib=ib,
            contract=contract,
            position_tracker=position_tracker,
            state_manager=state_manager,
            order_manager=order_manager,
            dashboard=dashboard,
            telegram=telegram,
            contracts_count=contracts,
            mode_label="PAPER",
            signal_engine=signal_engine
        )

        # Primary timeframe from config
        primary_bar_size = get_primary_bar_size(strategy_cfg)
        primary_bars_per_hour = get_primary_bars_per_hour(strategy_cfg)

        # Create feeds
        primary_feed = RealtimeFeed(
            ib_client=ib,
            contract=contract,
            bar_size=primary_bar_size
        )
        shared_state['feed'] = primary_feed
        mtf = MultiTimeframeFeed(
            primary_feed,
            ema_length=ema_cfg.get('length', 200),
            bars_per_hour=primary_bars_per_hour
        )
        shared_state['mtf'] = mtf

        # Dashboard update task
        async def update_dashboard_loop():
            """Update dashboard periodically."""
            while shared_state['running']:
                try:
                    # Update account info
                    account_summary = position_tracker.get_account_summary()
                    dashboard.update_account(
                        account_value=account_summary.get('net_liquidation', 0),
                        buying_power=account_summary.get('buying_power', 0),
                        daily_pnl=account_summary.get('daily_pnl', 0)
                    )
                    
                    # Print dashboard
                    dashboard.print_dashboard(clear=True)
                    
                    await asyncio.sleep(5)  # Update every 5 seconds
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Dashboard update error: {e}")
                    await asyncio.sleep(5)
        
        # Bar close handler
        # Intrabar update handler for live price tracking
        def on_bar_update(bar):
            """Update current price for dashboard and Telegram P&L."""
            telegram.update_current_price(bar.close)
            dashboard.update_price(bar.close)
        
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
                
                prev_close_1h = df_1h['close'].iloc[-3]
                prev_ema_1h = ema_series.iloc[-3]
                
                was_below_ema = prev_close_1h <= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                was_above_ema = prev_close_1h >= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                
                current_bar['ema_bull_cross'] = current_bar['ema_bull'] and was_below_ema
                current_bar['ema_bear_cross'] = current_bar['ema_bear'] and was_above_ema
            else:
                current_bar['ema_bull_cross'] = False
                current_bar['ema_bear_cross'] = False
            
            # Update dashboard indicators
            st_dir = "BULL" if current_bar.get('direction_long', current_bar.get('direction', 0)) == -1 else "BEAR"
            ema_status = "BULL" if current_bar['ema_bull'] else ("BEAR" if current_bar['ema_bear'] else "NEUTRAL")
            adx_val = current_bar.get('adx', 0)
            
            dashboard.update_price(current_bar['close'], current_bar.name)
            dashboard.update_indicators(st_dir, ema_status, adx_val)
            
            # Log events (these show in dashboard events area)
            events = ""
            if current_bar.get('st_bull_flip', False):
                events = "SuperTrend flipped BULLISH"
                dashboard.print_event("SIGNAL", events)
            elif current_bar.get('st_bear_flip', False):
                events = "SuperTrend flipped BEARISH"
                dashboard.print_event("SIGNAL", events)
            if current_bar.get('ema_bull_cross', False):
                events = "EMA Cross BULLISH"
                dashboard.print_event("SIGNAL", events)
            elif current_bar.get('ema_bear_cross', False):
                events = "EMA Cross BEARISH"
                dashboard.print_event("SIGNAL", events)
            
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
                    take_profit=state.take_profit
                )
                
                if exit_signal:
                    action = "SELL" if state.position_size > 0 else "BUY"
                    direction = "LONG" if state.position_size > 0 else "SHORT"
                    
                    dashboard.print_event("EXIT", f"{exit_signal.exit_type.value.upper()} triggered - Closing position")
                    
                    # Close with MARKET order (bot manages SL/TP, not IBKR)
                    await order_manager.place_market_order(
                        action=action,
                        quantity=abs(state.position_size) * contracts
                    )
                    
                    # Calculate P&L
                    if state.position_size > 0:
                        pnl_points = exit_signal.exit_price - state.entry_price
                    else:
                        pnl_points = state.entry_price - exit_signal.exit_price
                    pnl_dollars = pnl_points * 2 * contracts
                    
                    # Update dashboard
                    dashboard.on_exit(
                        exit_price=exit_signal.exit_price,
                        exit_type=exit_signal.exit_type.value,
                        pnl_dollars=pnl_dollars
                    )
                    
                    # Telegram notification for trade closure
                    await telegram.notify_trade_closed(
                        direction=direction,
                        entry_price=state.entry_price,
                        exit_price=exit_signal.exit_price,
                        exit_reason=exit_signal.exit_type.value,
                        pnl_points=pnl_points,
                        pnl_dollars=pnl_dollars,
                        contracts=contracts,
                        trade_id=state.trade_count,
                        entry_time=state.entry_time
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
                    direction = "LONG" if is_long else "SHORT"
                    
                    dashboard.print_event("ENTRY", f"{direction} @ {entry_signal.price:.2f}")
                    
                    stop_loss, take_profit = signal_engine.calculate_exit_levels(
                        entry_price=entry_signal.price,
                        is_long=is_long
                    )
                    
                    # Place MARKET entry order only (no bracket)
                    # Bot will monitor SL/TP and exit with market order when hit
                    await order_manager.place_market_order(
                        action="BUY" if is_long else "SELL",
                        quantity=contracts
                    )
                    
                    trade_id = state_manager.state.trade_count + 1
                    
                    # Update dashboard
                    dashboard.on_entry(
                        trade_id=trade_id,
                        direction=direction,
                        entry_price=entry_signal.price,
                        quantity=contracts,
                        stop_loss=stop_loss,
                        take_profit=take_profit
                    )
                    
                    state_manager.on_entry(entry_signal, stop_loss, take_profit)
                    
                    # Telegram notification for trade placement
                    await telegram.notify_trade_placed(
                        direction=direction,
                        entry_price=entry_signal.price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        contracts=contracts,
                        trigger=entry_signal.trigger,
                        trade_id=trade_id
                    )
        
        primary_feed.on_bar_close(lambda df, bar: asyncio.create_task(on_bar_close(df, bar)))
        primary_feed.on_bar_update(on_bar_update)
        
        await primary_feed.start(initial_lookback_days=15)
        
        # Start dashboard update task
        dashboard_task = asyncio.create_task(update_dashboard_loop())
        
        # Send bot started notification via Telegram
        await telegram.notify_bot_started(
            mode=f"PAPER ({gateway_type})",
            symbol="MNQ",
            contracts=contracts,
            strategy_info=strategy_info_for_telegram(
                strategy_cfg, ema_cfg.get('length', 200)
            ),
        )
        
        print("\n[OK] Paper trading is ACTIVE")
        print(f"[OK] Connected via {gateway_type} (Port {port})")
        print("[OK] Dashboard will refresh every 5 seconds")
        print("[OK] Telegram notifications enabled" if telegram.enabled else "[--] Telegram notifications disabled")
        print("[OK] Press Ctrl+C to stop\n")
        
        await asyncio.sleep(3)  # Give user time to read

        stop_reason = "Unknown"
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            stop_reason = "User requested shutdown (Ctrl+C)"

        shared_state['running'] = False
        dashboard_task.cancel()

        await telegram.notify_bot_stopped(stop_reason)

        await primary_feed.stop()
        await position_tracker.shutdown()
        await conn_manager.disconnect()
        print("[OK] Paper trading stopped")

    except Exception as e:
        logger.error(f"Paper trading error: {e}")
        import traceback
        traceback.print_exc()
        await telegram.notify_error(f"Paper trading crashed: {str(e)}")
        await conn_manager.disconnect()
        raise
    finally:
        # Always clean up Telegram session to prevent "Unclosed client session" warning
        await telegram.shutdown()


async def run_live_trading_v2(config: dict) -> None:
    """
    Run live trading mode with:
    - Auto-reconnect
    - Market orders  
    - Clean dashboard
    - Telegram notifications
    - IB Gateway support for 24/7 operation
    """
    from ib_async import IB, Future
    from data import RealtimeFeed, MultiTimeframeFeed
    from strategy import SignalEngine, StateManager, SignalType
    from execution import OrderManager, PositionTracker
    from utils import ConnectionManager, ConnectionConfig, TradingDashboard
    
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
    adx_cfg = strategy_cfg.get('adx', {})
    from utils.strategy_side_config import (
        resolve_side_configs,
        signal_engine_init_kwargs,
        strategy_info_for_telegram,
    )
    from data.strategy_indicators import live_bar_indicator_slice, bar_flips_for_state_manager

    sides = resolve_side_configs(strategy_cfg)
    
    conn_cfg = ibkr_cfg.get('connection', {})
    recon_cfg = ibkr_cfg.get('reconnection', {})
    
    # Smart port selection: uses IB Gateway for 24/7, TWS otherwise
    port = get_connection_port(ibkr_cfg, mode='live')
    gateway_type = conn_cfg.get('default_gateway', 'tws').upper()
    
    # Initialize Telegram notifier
    telegram = create_telegram_notifier(config)
    
    # Initialize dashboard
    dashboard = TradingDashboard(symbol="MNQ")
    
    # Create connection config
    connection_config = ConnectionConfig(
        host=conn_cfg.get('host', '127.0.0.1'),
        port=port,
        client_id=conn_cfg.get('client_id', 1),
        max_reconnect_attempts=recon_cfg.get('max_attempts', 0),
        initial_delay=recon_cfg.get('initial_delay_sec', 5),
        max_delay=recon_cfg.get('max_delay_sec', 60),
        backoff_multiplier=recon_cfg.get('backoff_multiplier', 2.0)
    )
    
    # Shared state for reconnection
    shared_state = {
        'feed': None,
        'order_manager': None,
        'position_tracker': None,
        'signal_engine': None,
        'state_manager': None,
        'mtf': None,
        'contract': None,
        'running': True
    }
    
    async def on_reconnect():
        """Handle reconnection - resync with broker and verify positions."""
        dashboard.print_event("INFO", f"Reconnected to {gateway_type} - Resyncing...")
        dashboard.update_connection_status(True)

        # Step 1: Resync positions and orders with IBKR
        if shared_state['order_manager']:
            await shared_state['order_manager'].sync_with_broker()
        if shared_state['position_tracker']:
            await shared_state['position_tracker'].initialize()

        # Step 2: Reconcile position state using the same logic as startup
        if shared_state['state_manager'] and shared_state['position_tracker'] and shared_state['order_manager']:
            await startup_reconcile_position(
                ib=ib,
                contract=shared_state['contract'],
                position_tracker=shared_state['position_tracker'],
                state_manager=shared_state['state_manager'],
                order_manager=shared_state['order_manager'],
                dashboard=dashboard,
                telegram=telegram,
                contracts_count=contracts,
                mode_label="LIVE",
                signal_engine=shared_state.get('signal_engine')
            )

        # Step 3: Restart data feed
        if shared_state['feed']:
            await shared_state['feed'].start(initial_lookback_days=5)

        dashboard.print_event("INFO", "Resync complete - Trading active")
        await telegram.notify_reconnected()

    def on_disconnect():
        """Handle disconnect."""
        dashboard.update_connection_status(False)
        dashboard.print_event("WARNING", f"Disconnected from {gateway_type} - Reconnecting...")
        asyncio.create_task(telegram.notify_disconnected(f"Lost connection to {gateway_type}"))

    async def on_extended_disconnect(attempt_count):
        """Alert user when reconnection has been failing for extended period."""
        msg = (f"ALERT: {attempt_count} failed reconnection attempts (~{attempt_count} min). "
               f"IB Gateway on port {port} may require manual restart. LIVE TRADING HALTED.")
        dashboard.print_event("ERROR", msg)
        logger.error(msg)
        await telegram.notify_error(msg)

    conn_manager = ConnectionManager(
        config=connection_config,
        on_reconnect=on_reconnect,
        on_disconnect=on_disconnect,
        on_extended_disconnect=on_extended_disconnect
    )

    try:
        print(f"\nConnecting to IBKR LIVE via {gateway_type} on port {port}...")
        if not await conn_manager.connect():
            print("[X] Failed to connect to IBKR")
            await telegram.notify_error(f"Failed to connect to IBKR LIVE {gateway_type} on port {port}")
            return
        
        dashboard.update_connection_status(True)
        print(f"[OK] Connected to IBKR LIVE Account via {gateway_type}")
        print("[!] ⚠️  REAL MONEY MODE - Orders will execute on live account!")
        await telegram.notify_connected(f"{gateway_type} (LIVE)")
        
        ib = conn_manager.client
        
        # Get front-month MNQ contract
        base_contract = Future(symbol='MNQ', exchange='CME', currency='USD')
        contract_details = await ib.reqContractDetailsAsync(base_contract)
        
        if not contract_details:
            print("[X] Error: No MNQ contracts found")
            await telegram.notify_error("No MNQ contracts found")
            return
        
        sorted_contracts = sorted(
            contract_details,
            key=lambda x: x.contract.lastTradeDateOrContractMonth
        )
        contract = sorted_contracts[0].contract
        shared_state['contract'] = contract
        print(f"[OK] Trading: {contract.localSymbol}")
        
        # Initialize components (same as paper trading)
        signal_engine = SignalEngine(
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            **signal_engine_init_kwargs(strategy_cfg),
        )
        shared_state['signal_engine'] = signal_engine
        
        state_manager = StateManager(
            state_file="./data/live_state.json",
            tick_value=0.50,
            contracts_per_trade=contracts
        )
        shared_state['state_manager'] = state_manager
        
        order_manager = OrderManager(
            ib_client=ib,
            contract=contract,
            default_qty=contracts
        )
        shared_state['order_manager'] = order_manager
        
        position_tracker = PositionTracker(ib_client=ib, contract=contract)
        await position_tracker.initialize()
        shared_state['position_tracker'] = position_tracker

        # --- STARTUP POSITION CHECK ---
        # Check if IBKR already has an active position (e.g., manual order, crash recovery)
        # Reconstruct bot state and track open SL/TP orders before the feed starts
        await startup_reconcile_position(
            ib=ib,
            contract=contract,
            position_tracker=position_tracker,
            state_manager=state_manager,
            order_manager=order_manager,
            dashboard=dashboard,
            telegram=telegram,
            contracts_count=contracts,
            mode_label="LIVE",
            signal_engine=signal_engine
        )

        # Primary timeframe from config
        primary_bar_size = get_primary_bar_size(strategy_cfg)
        primary_bars_per_hour = get_primary_bars_per_hour(strategy_cfg)

        primary_feed = RealtimeFeed(
            ib_client=ib,
            contract=contract,
            bar_size=primary_bar_size
        )
        shared_state['feed'] = primary_feed
        mtf = MultiTimeframeFeed(
            primary_feed,
            ema_length=ema_cfg.get('length', 200),
            bars_per_hour=primary_bars_per_hour
        )
        shared_state['mtf'] = mtf

        # Dashboard update task
        async def update_dashboard_loop():
            """Update dashboard periodically."""
            while shared_state['running']:
                try:
                    account_summary = position_tracker.get_account_summary()
                    dashboard.update_account(
                        account_value=account_summary.get('net_liquidation', 0),
                        buying_power=account_summary.get('buying_power', 0),
                        daily_pnl=account_summary.get('daily_pnl', 0)
                    )
                    dashboard.print_dashboard(clear=True)
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Dashboard update error: {e}")
                    await asyncio.sleep(5)
        
        # Intrabar update handler for live price tracking
        def on_bar_update(bar):
            """Update current price for dashboard and Telegram P&L."""
            telegram.update_current_price(bar.close)
            dashboard.update_price(bar.close)
        
        # Bar close handler (identical logic to paper trading)
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
                
                prev_close_1h = df_1h['close'].iloc[-3]
                prev_ema_1h = ema_series.iloc[-3]
                
                was_below_ema = prev_close_1h <= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                was_above_ema = prev_close_1h >= prev_ema_1h if not pd.isna(prev_ema_1h) else False
                
                current_bar['ema_bull_cross'] = current_bar['ema_bull'] and was_below_ema
                current_bar['ema_bear_cross'] = current_bar['ema_bear'] and was_above_ema
            else:
                current_bar['ema_bull_cross'] = False
                current_bar['ema_bear_cross'] = False
            
            st_dir = "BULL" if current_bar.get('direction_long', current_bar.get('direction', 0)) == -1 else "BEAR"
            ema_status = "BULL" if current_bar['ema_bull'] else ("BEAR" if current_bar['ema_bear'] else "NEUTRAL")
            adx_val = current_bar.get('adx', 0)
            
            dashboard.update_price(current_bar['close'], current_bar.name)
            dashboard.update_indicators(st_dir, ema_status, adx_val)
            
            if current_bar.get('st_bull_flip', False):
                dashboard.print_event("SIGNAL", "SuperTrend flipped BULLISH")
            elif current_bar.get('st_bear_flip', False):
                dashboard.print_event("SIGNAL", "SuperTrend flipped BEARISH")
            if current_bar.get('ema_bull_cross', False):
                dashboard.print_event("SIGNAL", "EMA Cross BULLISH")
            elif current_bar.get('ema_bear_cross', False):
                dashboard.print_event("SIGNAL", "EMA Cross BEARISH")
            
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
                    take_profit=state.take_profit
                )
                
                if exit_signal:
                    action = "SELL" if state.position_size > 0 else "BUY"
                    direction = "LONG" if state.position_size > 0 else "SHORT"
                    
                    dashboard.print_event("EXIT", f"LIVE {exit_signal.exit_type.value.upper()} - Closing position")
                    
                    # Close with MARKET order (bot manages SL/TP, not IBKR)
                    await order_manager.place_market_order(
                        action=action,
                        quantity=abs(state.position_size) * contracts
                    )
                    
                    if state.position_size > 0:
                        pnl_points = exit_signal.exit_price - state.entry_price
                    else:
                        pnl_points = state.entry_price - exit_signal.exit_price
                    pnl_dollars = pnl_points * 2 * contracts
                    
                    dashboard.on_exit(
                        exit_price=exit_signal.exit_price,
                        exit_type=exit_signal.exit_type.value,
                        pnl_dollars=pnl_dollars
                    )
                    
                    # Telegram notification for trade closure
                    await telegram.notify_trade_closed(
                        direction=direction,
                        entry_price=state.entry_price,
                        exit_price=exit_signal.exit_price,
                        exit_reason=exit_signal.exit_type.value,
                        pnl_points=pnl_points,
                        pnl_dollars=pnl_dollars,
                        contracts=contracts,
                        trade_id=state.trade_count,
                        entry_time=state.entry_time
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
                    direction = "LONG" if is_long else "SHORT"
                    
                    dashboard.print_event("ENTRY", f"LIVE {direction} @ {entry_signal.price:.2f}")
                    
                    stop_loss, take_profit = signal_engine.calculate_exit_levels(
                        entry_price=entry_signal.price,
                        is_long=is_long
                    )
                    
                    # Place MARKET entry order only (no bracket)
                    # Bot will monitor SL/TP and exit with market order when hit
                    await order_manager.place_market_order(
                        action="BUY" if is_long else "SELL",
                        quantity=contracts
                    )
                    
                    trade_id = state_manager.state.trade_count + 1
                    
                    dashboard.on_entry(
                        trade_id=trade_id,
                        direction=direction,
                        entry_price=entry_signal.price,
                        quantity=contracts,
                        stop_loss=stop_loss,
                        take_profit=take_profit
                    )
                    
                    state_manager.on_entry(entry_signal, stop_loss, take_profit)
                    
                    # Telegram notification for trade placement
                    await telegram.notify_trade_placed(
                        direction=direction,
                        entry_price=entry_signal.price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        contracts=contracts,
                        trigger=entry_signal.trigger,
                        trade_id=trade_id
                    )
        
        primary_feed.on_bar_close(lambda df, bar: asyncio.create_task(on_bar_close(df, bar)))
        primary_feed.on_bar_update(on_bar_update)
        
        await primary_feed.start(initial_lookback_days=15)
        
        dashboard_task = asyncio.create_task(update_dashboard_loop())
        
        # Send bot started notification via Telegram
        await telegram.notify_bot_started(
            mode=f"LIVE ({gateway_type})",
            symbol="MNQ",
            contracts=contracts,
            strategy_info=strategy_info_for_telegram(
                strategy_cfg, ema_cfg.get('length', 200)
            ),
        )
        
        print("\n" + "=" * 60)
        print("[OK] 🔴 LIVE TRADING IS ACTIVE - REAL MONEY")
        print("=" * 60)
        print(f"[OK] Connected via {gateway_type} (Port {port})")
        print("[OK] Dashboard will refresh every 5 seconds")
        print("[OK] Telegram notifications enabled" if telegram.enabled else "[--] Telegram notifications disabled")
        print("[OK] Press Ctrl+C to stop\n")
        
        await asyncio.sleep(3)

        stop_reason = "Unknown"
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down LIVE trading...")
            stop_reason = "LIVE trading stopped by user (Ctrl+C)"

        shared_state['running'] = False
        dashboard_task.cancel()

        await telegram.notify_bot_stopped(stop_reason)

        await primary_feed.stop()
        await position_tracker.shutdown()
        await conn_manager.disconnect()
        print("[OK] LIVE trading stopped")

    except Exception as e:
        logger.error(f"LIVE trading error: {e}")
        import traceback
        traceback.print_exc()
        await telegram.notify_error(f"LIVE trading crashed: {str(e)}")
        await conn_manager.disconnect()
        raise
    finally:
        # Always clean up Telegram session to prevent "Unclosed client session" warning
        await telegram.shutdown()


# Import original backtest functions
from main import run_backtest, run_databento_backtest


async def main_async():
    """Main async entry point."""
    print_banner()
    
    # Load config
    config = load_config()
    
    # Show current settings (matches resolve_side_configs + paper/backtest engine)
    strategy_cfg = config.get('strategy', {})
    from utils.strategy_side_config import print_resolved_strategy_banner

    print_resolved_strategy_banner(strategy_cfg)
    print()
    
    ibkr_cfg = config.get('ibkr', {})
    gateway_type = ibkr_cfg.get('connection', {}).get('default_gateway', 'tws').upper()
    
    print("v2.1 Features:")
    print("  ✓ Auto-reconnect when TWS/Gateway disconnects")
    print("  ✓ Market orders for faster execution")
    print("  ✓ Clean terminal dashboard with P&L tracking")
    print(f"  ✓ IB Gateway support ({gateway_type}) for 24/7 operation")
    print("  ✓ Telegram notifications (trades, P&L, connection status)")
    print()
    
    # Get user choice
    choice = get_menu_choice()
    
    if choice == '0':
        print("\nGoodbye!")
        return
    elif choice == '1':
        await run_backtest(config)
    elif choice == '2':
        await run_databento_backtest(config)
    elif choice == '3':
        await run_paper_trading_v2(config)
    elif choice == '4':
        await run_live_trading_v2(config)
    
    print("\n" + "=" * 60)
    print("Session ended. Run 'python main_v2.py' to start again.")
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
