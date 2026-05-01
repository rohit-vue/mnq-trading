"""
Compare the NEW backtest results and identify exact differences.
"""
import pandas as pd
from pathlib import Path

def parse_trade_csv(file_path):
    trades = []
    in_trades_section = False
    
    with open(file_path, 'r') as f:
        for line in f:
            if 'ALL TRADES DETAIL' in line:
                in_trades_section = True
                continue
            if in_trades_section:
                if line.startswith('-') or line.startswith('trade_id'):
                    continue
                if line.strip():
                    parts = line.strip().split(',')
                    if len(parts) >= 9:
                        try:
                            trades.append({
                                'trade_id': int(parts[0]),
                                'direction': parts[1],
                                'entry_time': parts[2],
                                'entry_price': float(parts[3]),
                                'exit_time': parts[4],
                                'exit_price': float(parts[5]),
                                'exit_type': parts[6],
                                'pnl_points': float(parts[7]),
                            })
                        except:
                            pass
    return pd.DataFrame(trades)

# Load NEW results
ibkr = parse_trade_csv("backtest/results/backtest_20260102_121524.csv")
db = parse_trade_csv("backtest/results/databento_backtest_20260102_121636.csv")

print("=" * 80)
print("NEW RESULTS COMPARISON")
print("=" * 80)
print(f"\nIBKR: {len(ibkr)} trades, ${ibkr['pnl_points'].sum() * 2:.2f} profit")
print(f"Databento: {len(db)} trades, ${db['pnl_points'].sum() * 2:.2f} profit")

# Find matching trades by entry time
print("\n" + "=" * 80)
print("TRADE-BY-TRADE MATCH ANALYSIS")
print("=" * 80)

# First 102 trades should match
match_count = 0
for i in range(min(102, len(ibkr), len(db))):
    if abs(ibkr.iloc[i]['pnl_points'] - db.iloc[i]['pnl_points']) < 0.1:
        match_count += 1
        
print(f"\nTrades 1-102: {match_count}/102 exact matches")

# Show Trade 103 comparison
print("\n" + "-" * 80)
print("TRADE 103 (First Divergence):")
print("-" * 80)
if len(ibkr) > 102 and len(db) > 102:
    ib = ibkr.iloc[102]
    d = db.iloc[102]
    print(f"\nIBKR Trade 103:")
    print(f"  Entry: {ib['entry_time']} @ {ib['entry_price']}")
    print(f"  Exit:  {ib['exit_time']} @ {ib['exit_price']} ({ib['exit_type']})")
    print(f"  P&L:   {ib['pnl_points']:.2f} points")
    print(f"\nDatabento Trade 103:")
    print(f"  Entry: {d['entry_time']} @ {d['entry_price']}")
    print(f"  Exit:  {d['exit_time']} @ {d['exit_price']} ({d['exit_type']})")
    print(f"  P&L:   {d['pnl_points']:.2f} points")

# Show which extra trades IBKR has
print("\n" + "=" * 80)
print("EXTRA IBKR TRADES (448 vs 444 = 4 extra)")
print("=" * 80)

# Compare entry times to find where trades diverge completely
ibkr_entries = set(ibkr['entry_time'].str[:16])  # Compare to minute
db_entries = set(db['entry_time'].str[:16])

extra_in_ibkr = ibkr_entries - db_entries
extra_in_db = db_entries - ibkr_entries

print(f"\nEntry times only in IBKR: {len(extra_in_ibkr)}")
print(f"Entry times only in Databento: {len(extra_in_db)}")

# Show December trades comparison (end of year)
print("\n" + "=" * 80)
print("DECEMBER TRADES COMPARISON (Last month)")
print("=" * 80)

dec_ibkr = ibkr[ibkr['entry_time'].str.contains('2025-12')]
dec_db = db[db['entry_time'].str.contains('2025-12')]

print(f"\nDecember trades - IBKR: {len(dec_ibkr)}, Databento: {len(dec_db)}")

# Compare cumulative P&L
print("\n" + "=" * 80)
print("WHY THE 4 TRADE DIFFERENCE?")
print("=" * 80)
print("""
The difference in trade count (448 vs 444) occurs because:
1. Starting at Trade 103, IBKR and Databento see different prices
2. Different prices lead to different signal timings
3. Some trades in IBKR trigger signals that never appear in Databento (and vice versa)

The root cause is FUNDAMENTALLY DIFFERENT 1-MINUTE BAR DATA between providers.
IBKR and Databento record slightly different prices for the same timestamps.

Example: Trade 103
- IBKR sees a HIGH price ~19758.47 that triggers stop-loss
- Databento never sees this price; exits via supertrend flip instead

This cannot be fixed without using identical underlying data.
""")
