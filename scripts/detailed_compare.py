"""
Check first 150 trades to see exactly where they start to diverge.
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

# Load both
ibkr = parse_trade_csv("backtest/results/backtest_20260102_000938.csv")
db_files = sorted(Path("backtest/results").glob("databento_backtest_fixed_*.csv"))
db = parse_trade_csv(db_files[-1])

print("=" * 100)
print("DETAILED TRADE COMPARISON (Trades 1-150)")
print("=" * 100)

cumulative_diff = 0
divergent = []

for i in range(min(150, len(ibkr), len(db))):
    ib = ibkr.iloc[i]
    d = db.iloc[i]
    
    entry_diff = abs(d['entry_price'] - ib['entry_price'])
    exit_diff = abs(d['exit_price'] - ib['exit_price'])
    pnl_diff = d['pnl_points'] - ib['pnl_points']
    cumulative_diff += pnl_diff
    
    # Only show trades with differences
    if entry_diff > 0.5 or exit_diff > 0.5 or abs(pnl_diff) > 0.5:
        divergent.append(i+1)
        print(f"\nTrade {i+1}:")
        print(f"  Entry:   IBKR {ib['entry_time'][:19]} @ {ib['entry_price']:.2f}")
        print(f"  Entry:   DB   {d['entry_time'][:19]} @ {d['entry_price']:.2f}  (diff: {entry_diff:+.2f})")
        print(f"  Exit:    IBKR {ib['exit_time'][:19]} @ {ib['exit_price']:.2f} ({ib['exit_type']})")
        print(f"  Exit:    DB   {d['exit_time'][:19]} @ {d['exit_price']:.2f} ({d['exit_type']})")
        print(f"  P&L:     IBKR {ib['pnl_points']:+.2f} | DB {d['pnl_points']:+.2f} | Diff: {pnl_diff:+.2f}")
        print(f"  Running diff: {cumulative_diff:+.2f}")

print(f"\n\nSummary for trades 1-150:")
print(f"  Perfectly matching trades: {150 - len(divergent)}")
print(f"  Divergent trades: {len(divergent)}")
print(f"  Trade numbers with differences: {divergent}")
