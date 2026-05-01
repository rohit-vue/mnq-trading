"""
Compare IBKR and Databento backtest results trade by trade.
Identify where trades diverge to understand data differences.
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
                    # Header line
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
                        except (ValueError, IndexError):
                            pass
    
    return pd.DataFrame(trades)

# Load both files
ibkr_path = Path("backtest/results/backtest_20260102_000938.csv")
databento_path = Path("backtest/results/databento_backtest_fixed_20260102_005951.csv")

# Check for other databento files
databento_files = list(Path("backtest/results").glob("databento_backtest_fixed_*.csv"))
if databento_files:
    databento_path = sorted(databento_files)[-1]  # Get latest
    print(f"Using Databento file: {databento_path}")

print("Loading IBKR trades...")
ibkr_trades = parse_trade_csv(ibkr_path)
print(f"  Loaded {len(ibkr_trades)} trades")

print("Loading Databento trades...")
db_trades = parse_trade_csv(databento_path)
print(f"  Loaded {len(db_trades)} trades")

print("\n" + "=" * 70)
print("SUMMARY COMPARISON")
print("=" * 70)

ibkr_total = ibkr_trades['pnl_points'].sum()
db_total = db_trades['pnl_points'].sum()

print(f"\n{'Metric':<30} {'IBKR':<15} {'Databento':<15} {'Diff':<15}")
print("-" * 70)
print(f"{'Total Trades':<30} {len(ibkr_trades):<15} {len(db_trades):<15} {len(db_trades) - len(ibkr_trades):<15}")
print(f"{'Total P&L Points':<30} {ibkr_total:<15.2f} {db_total:<15.2f} {db_total - ibkr_total:<15.2f}")
print(f"{'Total P&L Dollars':<30} ${ibkr_trades['pnl_dollars'].sum():<14.2f} ${db_trades['pnl_dollars'].sum():<14.2f} ${db_trades['pnl_dollars'].sum() - ibkr_trades['pnl_dollars'].sum():<14.2f}")

# Find where trades start to diverge
print("\n" + "=" * 70)
print("TRADE-BY-TRADE COMPARISON (First divergence)")
print("=" * 70)

min_trades = min(len(ibkr_trades), len(db_trades))
divergence_found = False
total_diff = 0

for i in range(min_trades):
    ibkr = ibkr_trades.iloc[i]
    db = db_trades.iloc[i]
    
    # Check if trades match
    pnl_diff = db['pnl_points'] - ibkr['pnl_points']
    total_diff += pnl_diff
    
    # Direction mismatch or significant P&L difference
    if ibkr['direction'] != db['direction'] or abs(pnl_diff) > 1:
        if not divergence_found:
            print(f"\n*** FIRST DIVERGENCE at Trade {i+1} ***")
            divergence_found = True
        
        if i < 120:  # Print first divergences
            print(f"\nTrade {i+1}:")
            print(f"  IBKR:     {ibkr['direction']:<6} {ibkr['exit_type']:<12} Entry: {ibkr['entry_price']:<10.2f} Exit: {ibkr['exit_price']:<10.2f} P&L: {ibkr['pnl_points']:<10.2f}")
            print(f"  Databento: {db['direction']:<6} {db['exit_type']:<12} Entry: {db['entry_price']:<10.2f} Exit: {db['exit_price']:<10.2f} P&L: {db['pnl_points']:<10.2f}")
            print(f"  Diff: {pnl_diff:.2f} points (Running total: {total_diff:.2f})")

print(f"\n\nTotal P&L difference after comparing {min_trades} trades: {total_diff:.2f} points")

# Count matching vs diverging trades
matching = 0
diverging = 0
for i in range(min_trades):
    if abs(db_trades.iloc[i]['pnl_points'] - ibkr_trades.iloc[i]['pnl_points']) < 1:
        matching += 1
    else:
        diverging += 1

print(f"\nMatching trades (within 1 point): {matching}")
print(f"Diverging trades: {diverging}")

# Analyze exit price differences
print("\n" + "=" * 70)
print("EXIT PRICE ANALYSIS")
print("=" * 70)

# Compare exit prices where trades match in direction
exit_price_diffs = []
for i in range(min_trades):
    if ibkr_trades.iloc[i]['direction'] == db_trades.iloc[i]['direction']:
        diff = db_trades.iloc[i]['exit_price'] - ibkr_trades.iloc[i]['exit_price']
        if abs(diff) > 0.01:
            exit_price_diffs.append({
                'trade': i+1,
                'diff': diff,
                'ibkr_exit': ibkr_trades.iloc[i]['exit_price'],
                'db_exit': db_trades.iloc[i]['exit_price'],
                'exit_type': ibkr_trades.iloc[i]['exit_type']
            })

if exit_price_diffs:
    print(f"\nFound {len(exit_price_diffs)} trades with different exit prices")
    print("\nTop 10 largest exit price differences:")
    exit_price_diffs.sort(key=lambda x: abs(x['diff']), reverse=True)
    for ep in exit_price_diffs[:10]:
        print(f"  Trade {ep['trade']}: {ep['diff']:+.2f} points (IBKR: {ep['ibkr_exit']:.2f}, DB: {ep['db_exit']:.2f}) - {ep['exit_type']}")
