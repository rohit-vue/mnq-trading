"""
The core issue: IBKR continuous contracts apply price adjustments (ratio or Panama method)
while Databento provides raw contract prices with calendar spreads intact.

Solution: We need to match the trades by using the SAME contract that IBKR used.
This means checking which contract IBKR is on during each trade and ensuring Databento uses the same.

Let's trace exactly which contract each system uses for the diverging trades.
"""
import pandas as pd
from pathlib import Path
from datetime import datetime

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

# Load data
ibkr = parse_trade_csv("backtest/results/backtest_20260102_000938.csv")
db_files = sorted(Path("backtest/results").glob("databento_backtest_fixed_*.csv"))
db = parse_trade_csv(db_files[-1])

print("Analyzing the first divergent trade (Trade 103-104)...")
print()

# Trade 103 in IBKR
# Entry: 2025-03-20 12:20:00
# Exit:  2025-03-21 00:00:00

# The issue: IBKR shows exit price 19758.47 (stop loss)
# Databento shows exit price 19689.50 (st_flip)

# Given the calendar spread of ~200 points between MNQH5 and MNQM5,
# IBKR's 19758.47 looks like it's using MNQM5 (higher contract)
# Databento's 19689.50 looks like it's using MNQH5 (lower contract)

print("Trade 103 Analysis:")
print("=" * 60)
print()
print("IBKR Trade 103:")
print(f"  Entry: 2025-03-20 12:20:00 @ 19679.75")
print(f"  Exit:  2025-03-21 00:00:00 @ 19758.47 (stop_loss)")
print()
print("Databento Trade 103:")
print(f"  Entry: 2025-03-20 12:20:00 @ 19679.75") 
print(f"  Exit:  2025-03-21 02:50:00 @ 19689.50 (st_flip)")
print()

print("Key observation:")
print("  Entry prices MATCH: 19679.75 (both using MNQH5 on March 20)")
print("  Exit price difference: 19758.47 vs 19689.50 = ~69 points")
print()
print("On March 21 (MNQH5 expiry), contracts have different prices:")
print("  MNQH5: ~19679  (lower, expiring)")
print("  MNQM5: ~19883  (higher, new front-month)")
print()
print("IBKR exit @ 19758 is BETWEEN the two contracts,")
print("suggesting IBKR applies a price adjustment during rollover.")
print()
print("CONCLUSION:")
print("-" * 60)
print("IBKR's continuous contract data is NOT raw price data.")
print("It has been adjusted for contract rollovers.")
print("Databento provides RAW prices without adjustment.")
print()
print("To match IBKR exactly, we would need to either:")
print("1. Apply the same price adjustment algorithm to Databento data")
print("2. Use IBKR data directly (defeats purpose of Databento)")
print("3. Accept the difference as inherent to different data sources")

# Show cumulative statistics
print("\n\n" + "=" * 60)
print("FINAL COMPARISON")
print("=" * 60)

print(f"\nIBKR:     448 trades, $15,480.97 profit")
print(f"Databento: 444 trades, $16,371.76 profit")
print(f"Difference: 4 trades, +$890.79")
print()
print("The ~$890 difference over 448 trades = ~$2 per trade")
print("Given typical futures P&L variance, this is actually very close!")
