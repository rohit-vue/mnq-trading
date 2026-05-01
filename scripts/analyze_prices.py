"""
Analyze if the price differences are due to data provider differences,
and check if we can normalize them.
"""
import pandas as pd
from pathlib import Path
from datetime import datetime

# Load 1 minute Databento data for a sample period
# databento_dir = Path("GLBX-20260101-FS6BSKCTYP")
databento_dir = Path("GLBX-20260228-53VA3TKQXT")

# Load March data (where divergence starts at trade 103)
march_file = databento_dir / "glbx-mdp3-20250301-20250331.ohlcv-1m.csv"

print("Loading March 2025 Databento data...")
df = pd.read_csv(march_file)
df = df[df['symbol'].str.startswith('MNQ')]
df['ts_event'] = pd.to_datetime(df['ts_event'])
df = df.sort_values('ts_event')

# Check specific date: 2025-03-21 (MNQH5 expiry day)
print("\n" + "=" * 80)
print("DATA ON MARCH 21, 2025 (MNQH5 EXPIRY DAY)")
print("=" * 80)

march_21 = df[df['ts_event'].dt.date == datetime(2025, 3, 21).date()]
print(f"\nTotal rows on March 21: {len(march_21)}")
print(f"\nSymbols present:")
print(march_21['symbol'].value_counts())

# What time does MNQH5 stop trading?
mnqh5_march21 = march_21[march_21['symbol'] == 'MNQH5']
if len(mnqh5_march21) > 0:
    print(f"\nMNQH5 on March 21:")
    print(f"  First bar: {mnqh5_march21['ts_event'].min()}")
    print(f"  Last bar:  {mnqh5_march21['ts_event'].max()}")
    print(f"  Bars: {len(mnqh5_march21)}")

mnqm5_march21 = march_21[march_21['symbol'] == 'MNQM5']
if len(mnqm5_march21) > 0:
    print(f"\nMNQM5 on March 21:")
    print(f"  First bar: {mnqm5_march21['ts_event'].min()}")
    print(f"  Last bar:  {mnqm5_march21['ts_event'].max()}")
    print(f"  Bars: {len(mnqm5_march21)}")

# Compare prices at specific time (where Trade 103 exits)
# IBKR Trade 103 exits at 2025-03-21 00:00:00-04:00 (which is 04:00 UTC, or 05:00 UTC)
target_time = pd.Timestamp('2025-03-21 04:00:00', tz='UTC')

print(f"\n\nPRICES AT {target_time}:")
for sym in ['MNQH5', 'MNQM5']:
    sym_data = march_21[(march_21['symbol'] == sym) & 
                        (march_21['ts_event'] >= target_time - pd.Timedelta(minutes=10)) &
                        (march_21['ts_event'] <= target_time + pd.Timedelta(minutes=10))]
    if len(sym_data) > 0:
        print(f"\n{sym}:")
        for _, row in sym_data.head(5).iterrows():
            print(f"  {row['ts_event']}: Close={row['close']:.2f}")

# Check if there's a price gap between contracts
print("\n\n" + "=" * 80)
print("PRICE GAP ANALYSIS AT ROLLOVER")
print("=" * 80)

# Find the last MNQH5 bar and first MNQM5 bar on rollover day
rollover_dates = ['2025-03-21', '2025-06-20', '2025-09-19', '2025-12-19']

for rollover in rollover_dates:
    rollover_date = datetime.strptime(rollover[:10], '%Y-%m-%d').date()
    
    # Get the data file for this month
    month = rollover_date.month
    year = rollover_date.year
    file_pattern = f"glbx-mdp3-{year}{month:02d}*.csv"
    files = list(databento_dir.glob(file_pattern))
    
    if not files:
        continue
        
    df_month = pd.read_csv(files[0])
    df_month = df_month[df_month['symbol'].str.startswith('MNQ')]
    df_month['ts_event'] = pd.to_datetime(df_month['ts_event'])
    
    day_data = df_month[df_month['ts_event'].dt.date == rollover_date]
    
    if len(day_data) == 0:
        print(f"\n{rollover}: No data")
        continue
    
    contracts = day_data['symbol'].unique()
    print(f"\n{rollover}: Contracts = {list(contracts)}")
