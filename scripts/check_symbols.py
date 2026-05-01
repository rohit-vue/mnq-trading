"""
Check actual symbols in Databento data vs IBKR expected symbols.
"""
import pandas as pd
from pathlib import Path

# Load a sample Databento CSV
# databento_dir = Path("GLBX-20260101-FS6BSKCTYP")
databento_dir = Path("GLBX-20260228-53VA3TKQXT")
sample_file = list(databento_dir.glob("glbx-mdp3-20250301-*.csv"))[0]

print(f"Reading: {sample_file.name}")
df = pd.read_csv(sample_file)

# Filter for MNQ
df = df[df['symbol'].str.startswith('MNQ')]

print(f"\nTotal MNQ rows: {len(df)}")
print(f"\nUnique MNQ symbols in March 2025 data:")
print(df['symbol'].value_counts())

# Check what date range each symbol covers
print("\n\nDate ranges by symbol:")
df['ts_event'] = pd.to_datetime(df['ts_event'])
for sym in df['symbol'].unique():
    sym_df = df[df['symbol'] == sym]
    print(f"  {sym}: {sym_df['ts_event'].min()} to {sym_df['ts_event'].max()} ({len(sym_df):,} rows)")
