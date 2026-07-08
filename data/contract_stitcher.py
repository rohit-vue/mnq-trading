# =============================================================================
# MNQ CONTRACT STITCHER
# =============================================================================
# Fetches data from multiple MNQ contracts (including expired) and stitches
# them together to create continuous historical data.
#
# MNQ contracts expire quarterly:
#   H = March, M = June, U = September, Z = December
# =============================================================================

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Tuple
import asyncio
import logging

logger = logging.getLogger(__name__)


QUARTER_MONTHS = [3, 6, 9, 12]
MNQ_MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
CONTRACT_ROOT = "MNQ"
EXCHANGE = "CME"


def _third_friday(year: int, month: int) -> datetime:
    """Return the 3rd Friday (date at 00:00) for a given month."""
    first_day = datetime(year, month, 1)
    # Monday=0 ... Friday=4
    days_until_friday = (4 - first_day.weekday()) % 7
    first_friday = first_day + timedelta(days=days_until_friday)
    third_friday = first_friday + timedelta(weeks=2)
    return third_friday


def _expiry_boundary_datetime(year: int, month: int) -> datetime:
    """
    CME-style boundary: end of exact contract expiry date
    (3rd Friday of quarter month), so Friday bars are included.
    """
    expiry_day = _third_friday(year, month)
    return datetime(expiry_day.year, expiry_day.month, expiry_day.day, 23, 59, 59)


def _prev_cycle_month(year: int, month: int, cycle_months: List[int]) -> Tuple[int, int]:
    idx = cycle_months.index(month)
    if idx == 0:
        return year - 1, cycle_months[-1]
    return year, cycle_months[idx - 1]


def _contract_symbol(root: str, year: int, month: int, month_code: dict) -> str:
    return f"{root}{month_code[month]}{year % 10}"


def get_contracts_for_date_range(
    start_date: datetime,
    end_date: datetime,
    overlap_days: int = 0,
) -> List[Tuple[str, datetime, datetime, str]]:
    """
    Determine which MNQ quarterly contracts cover the given date range.
    """
    contracts_needed = []
    cycle_months = QUARTER_MONTHS
    month_code = MNQ_MONTH_CODE
    boundary_fn = _expiry_boundary_datetime
    expiry_label_fn = _third_friday
    root = CONTRACT_ROOT

    # Make dates timezone-naive for comparison
    if hasattr(start_date, 'tzinfo') and start_date.tzinfo is not None:
        start_date_naive = start_date.replace(tzinfo=None)
    else:
        start_date_naive = start_date

    if hasattr(end_date, 'tzinfo') and end_date.tzinfo is not None:
        end_date_naive = end_date.replace(tzinfo=None)
    else:
        end_date_naive = end_date

    # Include a buffer to ensure previous/next quarter boundaries exist
    min_year = start_date_naive.year - 1
    max_year = end_date_naive.year + 1

    for year in range(min_year, max_year + 1):
        for month in cycle_months:
            prev_y, prev_m = _prev_cycle_month(year, month, cycle_months)
            contract_start = boundary_fn(prev_y, prev_m)
            contract_end = boundary_fn(year, month)

            # Check overlap against requested range
            if contract_end >= start_date_naive and contract_start <= end_date_naive:
                overlap_start = contract_start - timedelta(days=max(0, int(overlap_days)))
                fetch_start = max(overlap_start, start_date_naive)
                fetch_end = min(contract_end, end_date_naive)
                symbol = _contract_symbol(root, year, month, month_code)
                expiry = expiry_label_fn(year, month).strftime("%Y%m%d")
                contracts_needed.append((symbol, fetch_start, fetch_end, expiry))

    # Sort by start datetime
    contracts_needed.sort(key=lambda x: x[1])
    return contracts_needed


async def fetch_stitched_data(
    ib_client,
    start_date: datetime,
    end_date: datetime,
    bar_size: str = "10 mins",
    exchange: str = EXCHANGE,
    contract_cfg: dict | None = None,
) -> pd.DataFrame:
    """
    Fetch historical data from multiple MNQ contracts and stitch together.
    
    Parameters:
    -----------
    ib_client : IB
        Connected ib_async IB client
    start_date : datetime
        Start of the date range
    end_date : datetime
        End of the date range
    bar_size : str
        Bar size (default "10 mins")
    
    Returns:
    --------
    pd.DataFrame
        Stitched OHLCV data with datetime index
    """
    from ib_async import Future
    
    from data.contract_rollover import assign_contract_per_day, rollover_settings

    # Get list of contracts needed. Include overlap before each new contract's
    # calendar start so the next month has volume during the current roll window.
    roll_cfg = rollover_settings(contract_cfg)
    contracts_needed = get_contracts_for_date_range(
        start_date,
        end_date,
        overlap_days=roll_cfg["roll_window_days"] if roll_cfg["method"] == "volume" else 0,
    )
    root = CONTRACT_ROOT
    
    if not contracts_needed:
        logger.error(f"No contracts found for date range {start_date} to {end_date}")
        return pd.DataFrame()
    
    logger.info(f"Need {len(contracts_needed)} contracts to cover date range:")
    for symbol, fetch_start, fetch_end, expiry in contracts_needed:
        logger.info(f"  {symbol}: {fetch_start.strftime('%Y-%m-%d')} to {fetch_end.strftime('%Y-%m-%d')}")
    
    all_data = []
    
    for symbol, fetch_start, fetch_end, expiry in contracts_needed:
        logger.info(f"\nFetching data for {symbol}...")
        
        # Create contract with includeExpired for expired contracts
        is_expired = datetime.strptime(expiry, '%Y%m%d') < datetime.now()
        
        contract = Future(
            symbol=CONTRACT_ROOT,
            exchange=exchange,
            currency='USD',
            lastTradeDateOrContractMonth=expiry,
            includeExpired=is_expired
        )
        
        # Qualify the contract
        try:
            qualified = await ib_client.qualifyContractsAsync(contract)
            if not qualified:
                logger.warning(f"Could not qualify {symbol}, skipping...")
                continue
            contract = qualified[0]
            logger.info(f"  Qualified: {contract.localSymbol}")
        except Exception as e:
            logger.warning(f"Error qualifying {symbol}: {e}")
            continue
        
        # Calculate days to fetch
        days = (fetch_end - fetch_start).days + 1
        
        # Fetch in chunks of 30 days (monthly)
        MAX_DAYS = 30
        chunk_data = []
        chunk_end = fetch_end
        remaining_days = days
        
        while remaining_days > 0:
            chunk_days = min(remaining_days, MAX_DAYS)
            
            logger.info(f"  Fetching {chunk_days} days ending {chunk_end.strftime('%Y-%m-%d')}...")
            
            try:
                bars = await ib_client.reqHistoricalDataAsync(
                    contract=contract,
                    endDateTime=chunk_end,
                    durationStr=f"{chunk_days} D",
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2
                )
                
                if bars:
                    df = _bars_to_dataframe(bars)
                    chunk_data.append(df)
                    logger.info(f"    Received {len(df)} bars")
                else:
                    logger.warning(f"    No bars returned")
                    
            except Exception as e:
                logger.error(f"    Error fetching data: {e}")
            
            # Move to next chunk
            chunk_end = chunk_end - timedelta(days=chunk_days)
            remaining_days -= chunk_days
            
            # Small delay
            await asyncio.sleep(1)
        
        # Combine chunks for this contract
        if chunk_data:
            contract_df = pd.concat(chunk_data)
            contract_df = contract_df[~contract_df.index.duplicated(keep='first')]
            contract_df = contract_df.sort_index()
            
            # Filter to exact date range for this contract
            # Convert to timezone-aware timestamps for comparison
            import pytz
            tz = pytz.timezone('US/Eastern')
            fetch_start_tz = tz.localize(fetch_start) if fetch_start.tzinfo is None else fetch_start
            fetch_end_tz = tz.localize(fetch_end) if fetch_end.tzinfo is None else fetch_end
            
            contract_df = contract_df[contract_df.index >= fetch_start_tz]
            contract_df = contract_df[contract_df.index <= fetch_end_tz]
            contract_df["symbol"] = symbol
            
            all_data.append(contract_df)
            logger.info(f"  Total for {symbol}: {len(contract_df)} bars")
    
    # Stitch all contracts together
    if all_data:
        stitched = pd.concat(all_data)
        stitched = stitched.sort_index()

        # Select one concrete contract per day using the shared volume rollover
        # rule, then remove any remaining same-timestamp duplicates.
        assign_df = stitched.reset_index().rename(columns={"datetime": "timestamp"})
        if "timestamp" not in assign_df.columns:
            assign_df = assign_df.rename(columns={assign_df.columns[0]: "timestamp"})
        daily_assignments = assign_contract_per_day(
            assign_df,
            contracts_needed,
            contract_cfg,
            timestamp_col="timestamp",
            symbol_col="symbol",
            volume_col="volume",
        )
        stitched["_roll_date"] = [idx.date() for idx in stitched.index]
        stitched["_front_symbol"] = stitched["_roll_date"].map(daily_assignments)
        stitched = stitched[stitched["symbol"] == stitched["_front_symbol"]].copy()
        stitched = stitched.sort_values(["symbol"]).sort_index()
        stitched = stitched[~stitched.index.duplicated(keep='first')]
        stitched = stitched.drop(columns=["_roll_date", "_front_symbol"], errors="ignore")
        stitched = stitched.sort_index()
        
        logger.info(f"\nStitched data: {len(stitched)} total bars")
        logger.info(f"Date range: {stitched.index[0]} to {stitched.index[-1]}")

        from data.bar_index import ensure_datetime_index

        return ensure_datetime_index(stitched, tz="US/Eastern", datetime_col=None)
    else:
        return pd.DataFrame()


def _bars_to_dataframe(bars) -> pd.DataFrame:
    """Convert IBKR BarData objects to DataFrame."""
    from data.bar_index import bars_to_ohlcv_dataframe

    return bars_to_ohlcv_dataframe(bars, tz="US/Eastern")
