# MNQ Trading Bot v2.0 - Changes Summary

## Overview
This document explains the changes made to implement the requested features:

1. ✅ **Trade Execution Fix** - Ensures trades are placed using existing RSI, EMA, ADX logic
2. ✅ **Auto-Reconnect** - Bot automatically reconnects when TWS disconnects or restarts
3. ✅ **Market Orders** - Changed from limit orders to market orders for faster execution
4. ✅ **Terminal Dashboard** - Clean dashboard showing trades, P&L, and status

---

## Files Changed/Added

### New Files

#### `utils/connection_manager.py`
- **ConnectionConfig** dataclass with connection settings
- **ConnectionManager** class that wraps ib_insync IB client
- Features:
  - Auto-reconnect on disconnect with exponential backoff
  - Configurable max attempts, initial delay, max delay
  - Callbacks for connect/disconnect/reconnect events
  - Uses settings from `config/ibkr.yaml` reconnection section

#### `utils/dashboard.py`
- **TradingDashboard** class for clean terminal display
- Shows:
  - Connection status (🟢/🔴)
  - Current market status (price, SuperTrend, EMA, ADX)
  - Active position with unrealized P&L
  - Realized P&L summary
  - Account info (Net Liquidation, Buying Power)
  - Recent trades history (last 5)
- Auto-refreshes every 5 seconds
- Events print without clearing dashboard

#### `utils/__init__.py`
- Package init file

#### `main_v2.py`
- New main entry point with v2 features
- Uses ConnectionManager for auto-reconnect
- Uses TradingDashboard for clean display
- Logging goes to `trading.log` file (terminal stays clean)
- All original trading logic (RSI, EMA, ADX) preserved

---

## How Auto-Reconnect Works

1. When TWS disconnects, `ConnectionManager._on_disconnect()` is called
2. It starts a reconnection loop with exponential backoff
3. Delay starts at 5 seconds, doubles each attempt (max 60 seconds)
4. On successful reconnect:
   - Resyncs positions with broker
   - Resyncs open orders
   - Restarts data feed
   - Continues trading

Configuration in `config/ibkr.yaml`:
```yaml
reconnection:
  enabled: true
  max_attempts: 5        # 0 = infinite
  initial_delay_sec: 5
  max_delay_sec: 60
  backoff_multiplier: 2.0
```

---

## Market Orders

The `place_bracket_order()` method already supported market orders via `entry_type="MKT"` parameter.

In the code:
```python
await order_manager.place_bracket_order(
    action="BUY" if is_long else "SELL",
    quantity=contracts,
    take_profit_price=take_profit,
    stop_loss_price=stop_loss,
    entry_type="MKT"  # MARKET order for entry
)
```

The bracket structure:
- **Entry**: Market Order (executes immediately at market price)
- **Take Profit**: Limit Order (exits at target profit level)
- **Stop Loss**: Stop Order (exits at stop price)

---

## Dashboard Layout

```
======================================================================
                    MNQ TRADING DASHBOARD
======================================================================

  Status: 🟢 CONNECTED    Time: 2026-01-16 00:03:00 PKT

----------------------------------------------------------------------
  📊 MARKET STATUS
----------------------------------------------------------------------
  Symbol: MNQ
  Price:  21,500.00
  Last Bar: 00:00

  SuperTrend: BULL      EMA: BULL      ADX:  25.3

----------------------------------------------------------------------
  📈 ACTIVE POSITION
----------------------------------------------------------------------
  Direction:  LONG
  Entry:      21,480.00  Qty: 1
  Current:    21,500.00
  Stop Loss:  21,394.08  Take Profit: 21,737.76
  Unrealized: +$40.00

----------------------------------------------------------------------
  💰 P&L SUMMARY
----------------------------------------------------------------------
  Realized P&L:  +$120.00
  Daily P&L:     +$160.00
  Today Trades:  2    Total: 5    Win Rate: 60.0%

----------------------------------------------------------------------
  📋 RECENT TRADES (Last 5)
----------------------------------------------------------------------
  #5 LONG  | 21,480.00 → 21,530.00 |      +$100.00 | TP
  #4 SHORT | 21,550.00 → 21,600.00 |      -$100.00 | SL
  ...

======================================================================
  Press Ctrl+C to stop
======================================================================
```

---

## How to Run

### Paper Trading (with new features)
```bash
python main_v2.py
# Select option 3
```

### Live Trading (with new features)
```bash
python main_v2.py
# Select option 4
# Type 'CONFIRM LIVE TRADING'
```

### Original Version (without new features)
```bash
python main.py
```

---

## Trading Logic (UNCHANGED)

The core trading logic remains exactly the same:

### Entry Conditions (BUY)

**Case 1 (SuperTrend Flip):**
- Price already ABOVE EMA 200 (1H)
- SuperTrend (10M) flips to BUY
- ADX(14) >= 20 for 5 consecutive candles

**Case 2 (EMA Cross):**
- SuperTrend is already in BUY mode
- 1H candle CLOSES ABOVE EMA 200
- ADX(14) >= 20 at the time of EMA cross

### Entry Conditions (SELL)
- SuperTrend bearish
- 1H close < 1H EMA
- Haven't traded this trend
- Trigger: ST flip OR EMA cross

### Exit Conditions
1. **Take Profit**: Hit TP price level
2. **Stop Loss**: Hit SL price level
3. **SuperTrend Flip**: Exit on opposite flip

---

## Notes

- All logging now goes to `trading.log` file
- Terminal is kept clean for dashboard
- Errors still print to terminal
- State is saved to `data/paper_state.json` or `data/live_state.json`
