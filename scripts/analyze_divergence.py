"""
Deep dive into trades around the first divergence point (Trade 103).
"""
import pandas as pd
from pathlib import Path

def parse_trade_csv(file_path):
    """Parse the trade CSV and extract the ALL TRADES DETAIL section."""
    trades = []
    in_trades_section = False
    
    with open(file_path, 'r') as f:
        for line in f:
            if 'ALL TRADES DETAIL' in line:
                in_trades_section = True
                continue
            if in_trades_section:
                if line.startswith('-'):
                    continue
                if line.startswith('trade_id'):
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
                                'pnl_dollars': float(parts[8])
                            })
                        except:
                            pass
    
    return pd.DataFrame(trades)

# Load both
ibkr = parse_trade_csv("backtest/results/backtest_20260102_000938.csv")

# Find latest databento fixed file
db_files = sorted(Path("backtest/results").glob("databento_backtest_fixed_*.csv"))
db = parse_trade_csv(db_files[-1])

print("=" * 80)
print("TRADES 100-110 COMPARISON (Around first divergence)")
print("=" * 80)

for i in range(99, 115):
    if i < len(ibkr) and i < len(db):
        ib = ibkr.iloc[i]
        d = db.iloc[i]
        
        match = "MATCH" if abs(ib['pnl_points'] - d['pnl_points']) < 1 else "DIFF"
        
        print(f"\nTrade {i+1} [{match}]:")
        print(f"  IBKR:     {ib['direction']:<6} Entry: {ib['entry_time']:<25} @ {ib['entry_price']:<10.2f}")
        print(f"            Exit:  {ib['exit_time']:<25} @ {ib['exit_price']:<10.2f} ({ib['exit_type']}) = {ib['pnl_points']:.2f}")
        print(f"  Databento: {d['direction']:<6} Entry: {d['entry_time']:<25} @ {d['entry_price']:<10.2f}")
        print(f"            Exit:  {d['exit_time']:<25} @ {d['exit_price']:<10.2f} ({d['exit_type']}) = {d['pnl_points']:.2f}")

# Check if entry times match
print("\n\n" + "=" * 80)
print("ENTRY TIME ANALYSIS")
print("=" * 80)

mismatched_entries = 0
for i in range(min(len(ibkr), len(db))):
    if ibkr.iloc[i]['entry_time'][:16] != db.iloc[i]['entry_time'][:16]:  # Compare up to minute
        mismatched_entries += 1

print(f"Total trades with mismatched entry times: {mismatched_entries} out of {min(len(ibkr), len(db))}")

# What about trades 1-100?
print("\n\nFirst 102 trades matching analysis:")
matching = 0
for i in range(102):
    if i < len(ibkr) and i < len(db):
        if abs(ibkr.iloc[i]['pnl_points'] - db.iloc[i]['pnl_points']) < 0.5:
            matching += 1
        else:
            print(f"  Trade {i+1}: IBKR {ibkr.iloc[i]['pnl_points']:.2f} vs DB {db.iloc[i]['pnl_points']:.2f}")

print(f"\nMatching: {matching}/102")
