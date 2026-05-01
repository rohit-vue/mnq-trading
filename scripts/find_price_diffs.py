"""
Find exactly which dates have large price differences and why.
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
                                'pnl_dollars': float(parts[8])
                            })
                        except:
                            pass
    return pd.DataFrame(trades)

# Load both
ibkr = parse_trade_csv("backtest/results/backtest_20260102_000938.csv")
db_files = sorted(Path("backtest/results").glob("databento_backtest_fixed_*.csv"))
db = parse_trade_csv(db_files[-1])

print("=" * 80)
print("TRADES WITH LARGE P&L DIFFERENCES (>10 points)")
print("=" * 80)

large_diffs = []
for i in range(min(len(ibkr), len(db))):
    ib = ibkr.iloc[i]
    d = db.iloc[i]
    diff = d['pnl_points'] - ib['pnl_points']
    
    if abs(diff) > 10:
        large_diffs.append({
            'trade': i+1,
            'ibkr_entry': ib['entry_time'],
            'ibkr_exit': ib['exit_time'],
            'ibkr_entry_price': ib['entry_price'],
            'ibkr_exit_price': ib['exit_price'],
            'db_entry_price': d['entry_price'],
            'db_exit_price': d['exit_price'],
            'entry_price_diff': d['entry_price'] - ib['entry_price'],
            'exit_price_diff': d['exit_price'] - ib['exit_price'],
            'pnl_diff': diff,
            'ibkr_exit_type': ib['exit_type'],
            'db_exit_type': d['exit_type']
        })

print(f"\nFound {len(large_diffs)} trades with >10 point difference\n")

# Group by month to see which rollover periods are problematic
for item in large_diffs[:30]:  # Show first 30
    month = item['ibkr_entry'][:7] if item['ibkr_entry'] else "Unknown"
    print(f"Trade {item['trade']:3d} ({month}):")
    print(f"  Entry: IBKR {item['ibkr_entry_price']:>10.2f} | DB {item['db_entry_price']:>10.2f} | Diff: {item['entry_price_diff']:+8.2f}")
    print(f"  Exit:  IBKR {item['ibkr_exit_price']:>10.2f} | DB {item['db_exit_price']:>10.2f} | Diff: {item['exit_price_diff']:+8.2f}")
    print(f"  P&L:   IBKR {ibkr.iloc[item['trade']-1]['pnl_points']:>10.2f} | DB {db.iloc[item['trade']-1]['pnl_points']:>10.2f} | Diff: {item['pnl_diff']:+8.2f}")
    print(f"  Exit types: IBKR={item['ibkr_exit_type']}, DB={item['db_exit_type']}")
    print()

# Summary by month
print("\n" + "=" * 80)
print("TOTAL P&L DIFFERENCE BY MONTH")
print("=" * 80)

monthly_diff = {}
for i in range(min(len(ibkr), len(db))):
    month = ibkr.iloc[i]['entry_time'][:7]
    diff = db.iloc[i]['pnl_points'] - ibkr.iloc[i]['pnl_points']
    monthly_diff[month] = monthly_diff.get(month, 0) + diff

for month in sorted(monthly_diff.keys()):
    diff = monthly_diff[month]
    bar = "#" * int(abs(diff) / 10)
    sign = "+" if diff > 0 else ""
    print(f"{month}: {sign}{diff:>8.2f} points  {bar}")

print(f"\nTotal: {sum(monthly_diff.values()):.2f} points")
