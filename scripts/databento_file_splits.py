import pandas as pd
from pathlib import Path

# =========================
# CONFIG
# =========================
INPUT_CSV = "glbx-mdp3-20180101-20260506.ohlcv-1m.csv"
OUTPUT_DIR = Path("monthly_splits")
CHUNK_SIZE = 5_000_000  # safe for large files

OUTPUT_DIR.mkdir(exist_ok=True)

# =========================
# HELPERS
# =========================
def month_key(ts):
    """Return YYYY-MM string from timestamp"""
    return ts.strftime("%Y-%m")


def month_bounds(ts):
    """Return first_day, last_day strings for a timestamp's month"""
    first = ts.replace(day=1)
    last = (first + pd.offsets.MonthEnd(1))
    return first.strftime("%Y%m%d"), last.strftime("%Y%m%d")


# =========================
# MAIN LOGIC
# =========================
buffers = {}  # { "YYYY-MM": DataFrame }

for chunk in pd.read_csv(
    INPUT_CSV,
    parse_dates=["ts_event"],
    chunksize=CHUNK_SIZE,
):
    # Ensure UTC
    chunk["ts_event"] = pd.to_datetime(chunk["ts_event"], utc=True)

    for month, group in chunk.groupby(chunk["ts_event"].dt.to_period("M")):
        month_str = str(month)

        if month_str not in buffers:
            buffers[month_str] = []

        buffers[month_str].append(group)

    # Flush completed months safely
    completed_months = list(buffers.keys())[:-1]

    for month_str in completed_months:
        df = pd.concat(buffers.pop(month_str))

        first_day, last_day = month_bounds(df["ts_event"].iloc[0])

        out_file = OUTPUT_DIR / f"glbx-mdp3-{first_day}-{last_day}.ohlcv-1m.csv"

        df.sort_values("ts_event", inplace=True)
        df.to_csv(out_file, index=False)

        print(f"✅ Written {out_file} ({len(df):,} rows)")

# =========================
# FLUSH FINAL MONTH
# =========================
for month_str, parts in buffers.items():
    df = pd.concat(parts)
    first_day, last_day = month_bounds(df["ts_event"].iloc[0])

    out_file = OUTPUT_DIR / f"glbx-mdp3-{first_day}-{last_day}.ohlcv-1m.csv"
    df.sort_values("ts_event", inplace=True)
    df.to_csv(out_file, index=False)

    print(f"✅ Written {out_file} ({len(df):,} rows)")