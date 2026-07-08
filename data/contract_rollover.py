"""
Volume-based MNQ rollover helpers.

IBKR cannot trade continuous futures directly, so paper/live must trade concrete
quarterly contracts. These helpers choose the active outright contract using a
TradingView-style volume crossover rule: inside the roll window, roll when the
next quarterly contract's completed daily volume exceeds the current contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
import pytz

logger = logging.getLogger(__name__)

QUARTER_MONTHS = [3, 6, 9, 12]
MNQ_MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
MONTH_CODE_TO_MONTH = {v: k for k, v in MNQ_MONTH_CODE.items()}
DEFAULT_TIMEZONE = "US/Eastern"


@dataclass(frozen=True)
class RollDecision:
    should_roll: bool
    reason: str
    current_symbol: str
    next_symbol: Optional[str]
    current_volume: Optional[float] = None
    next_volume: Optional[float] = None
    comparison_dates: Tuple[date, ...] = ()
    days_to_expiry: Optional[int] = None


def rollover_settings(contract_cfg: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Normalized rollover configuration from mnq_contract.yaml."""
    raw = (contract_cfg or {}).get("rollover", {}) if contract_cfg else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "method": str(raw.get("method", "volume")).lower(),
        "roll_window_days": int(raw.get("roll_window_days", raw.get("days_before_expiry", 10))),
        "confirmation_days": max(1, int(raw.get("confirmation_days", 1))),
        "check_after": str(raw.get("check_after", "17:00")),
        "timezone": str(raw.get("timezone", DEFAULT_TIMEZONE)),
        "fallback_days_before_expiry": int(
            raw.get("fallback_days_before_expiry", raw.get("days_before_expiry", 3))
        ),
        "volume_bar_size": str(raw.get("volume_bar_size", "1 day")),
        "compare": str(raw.get("compare", "daily_volume")),
    }


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_until_friday = (4 - first.weekday()) % 7
    first_friday = first + pd.Timedelta(days=days_until_friday).to_pytimedelta()
    return first_friday + pd.Timedelta(weeks=2).to_pytimedelta()


def _contract_year(year_text: str) -> int:
    # Local symbols in this project are like MNQM6 for 2026. Treat one-digit
    # labels as 2020s; two-digit labels keep the usual 2000/1900 pivot.
    if len(year_text) == 1:
        return 2020 + int(year_text)
    two_digit_year = int(year_text)
    return 2000 + two_digit_year if two_digit_year < 80 else 1900 + two_digit_year


def contract_symbol(contract: Any) -> str:
    """Best display symbol for IB contracts or plain strings."""
    if isinstance(contract, str):
        return contract
    for attr in ("localSymbol", "symbol"):
        value = getattr(contract, attr, None)
        if value:
            return str(value)
    return str(contract)


def parse_expiry(contract_or_symbol: Any) -> date:
    """
    Parse MNQ quarterly expiry.

    Prefer IB's lastTradeDateOrContractMonth when present; otherwise parse labels
    such as MNQM6. Month-only IB fields are mapped to the quarter's third Friday.
    """
    ltd = getattr(contract_or_symbol, "lastTradeDateOrContractMonth", None)
    if ltd:
        text = str(ltd)
        if len(text) >= 8 and text[:8].isdigit():
            return datetime.strptime(text[:8], "%Y%m%d").date()
        if len(text) >= 6 and text[:6].isdigit():
            year, month = int(text[:4]), int(text[4:6])
            return _third_friday(year, month)

    sym = contract_symbol(contract_or_symbol).upper()
    match = re.search(r"([FGHJKMNQUVXZ])(\d{1,2})$", sym)
    if not match:
        raise ValueError(f"Cannot parse MNQ expiry from contract: {sym}")
    code, yy = match.groups()
    if code not in MONTH_CODE_TO_MONTH:
        raise ValueError(f"Unsupported MNQ contract month code in {sym}")
    return _third_friday(_contract_year(yy), MONTH_CODE_TO_MONTH[code])


def days_to_expiry(contract: Any, as_of: datetime | date | None = None) -> int:
    if as_of is None:
        as_of_date = datetime.now().date()
    elif isinstance(as_of, datetime):
        as_of_date = as_of.date()
    else:
        as_of_date = as_of
    if as_of_date is None:
        as_of_date = datetime.now().date()
    return (parse_expiry(contract) - as_of_date).days


def sort_contracts_by_expiry(contracts: Iterable[Any]) -> List[Any]:
    return sorted(contracts, key=lambda c: parse_expiry(c))


def current_and_next_contract(
    contracts: Sequence[Any],
    as_of: datetime | date | None = None,
) -> Tuple[Optional[Any], Optional[Any]]:
    """Return nearest non-expired contract and following quarterly contract."""
    if as_of is None:
        as_of_date = datetime.now().date()
    elif isinstance(as_of, datetime):
        as_of_date = as_of.date()
    else:
        as_of_date = as_of
    if as_of_date is None:
        as_of_date = datetime.now().date()
    sorted_contracts = sort_contracts_by_expiry(contracts)
    active = [c for c in sorted_contracts if parse_expiry(c) >= as_of_date]
    if not active:
        return None, None
    current = active[0]
    idx = sorted_contracts.index(current)
    next_contract = sorted_contracts[idx + 1] if idx + 1 < len(sorted_contracts) else None
    return current, next_contract


def _as_eastern_date(value: Any, timezone: str = DEFAULT_TIMEZONE) -> date:
    tz = pytz.timezone(timezone)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.date()
    return ts.tz_convert(tz).date()


def daily_volumes_by_date(
    bars_or_df: Any,
    *,
    timestamp_col: str = "timestamp",
    volume_col: str = "volume",
    timezone: str = DEFAULT_TIMEZONE,
) -> Dict[date, float]:
    """Map US/Eastern date to total volume."""
    if bars_or_df is None:
        return {}
    if isinstance(bars_or_df, pd.DataFrame):
        if bars_or_df.empty:
            return {}
        df = bars_or_df.copy()
        if timestamp_col in df.columns:
            dates = df[timestamp_col].apply(lambda v: _as_eastern_date(v, timezone))
        else:
            dates = pd.Series(df.index, index=df.index).apply(lambda v: _as_eastern_date(v, timezone))
        grouped = df.assign(_roll_date=dates).groupby("_roll_date")[volume_col].sum()
        return {k: float(v) for k, v in grouped.items()}

    totals: Dict[date, float] = {}
    for bar in bars_or_df:
        bar_date = _as_eastern_date(getattr(bar, "date", None), timezone)
        totals[bar_date] = totals.get(bar_date, 0.0) + float(getattr(bar, "volume", 0) or 0)
    return totals


def should_roll_volume(
    current_volumes: Mapping[date, float],
    next_volumes: Mapping[date, float],
    confirmation_days: int = 1,
) -> Tuple[bool, Tuple[date, ...], Optional[float], Optional[float]]:
    """True when next contract volume beats current for the latest N common days."""
    confirmation_days = max(1, int(confirmation_days))
    common_dates = sorted(set(current_volumes) & set(next_volumes))
    if len(common_dates) < confirmation_days:
        return False, tuple(common_dates), None, None
    check_dates = tuple(common_dates[-confirmation_days:])
    wins = [float(next_volumes[d]) > float(current_volumes[d]) for d in check_dates]
    last_date = check_dates[-1]
    return (
        all(wins),
        check_dates,
        float(current_volumes[last_date]),
        float(next_volumes[last_date]),
    )


def evaluate_roll_decision(
    current_contract: Any,
    next_contract: Optional[Any],
    current_volumes: Mapping[date, float],
    next_volumes: Mapping[date, float],
    contract_cfg: Mapping[str, Any] | None,
    *,
    as_of: datetime | date | None = None,
) -> RollDecision:
    cfg = rollover_settings(contract_cfg)
    current_sym = contract_symbol(current_contract)
    next_sym = contract_symbol(next_contract) if next_contract is not None else None
    dte = days_to_expiry(current_contract, as_of)

    if not cfg["enabled"] or cfg["method"] != "volume":
        return RollDecision(False, "rollover_disabled", current_sym, next_sym, days_to_expiry=dte)
    if next_contract is None:
        return RollDecision(False, "no_next_contract", current_sym, None, days_to_expiry=dte)
    if dte > cfg["roll_window_days"]:
        return RollDecision(False, "outside_roll_window", current_sym, next_sym, days_to_expiry=dte)

    should_roll, check_dates, current_vol, next_vol = should_roll_volume(
        current_volumes,
        next_volumes,
        cfg["confirmation_days"],
    )
    if should_roll:
        return RollDecision(
            True,
            "volume",
            current_sym,
            next_sym,
            current_vol,
            next_vol,
            check_dates,
            dte,
        )

    if dte <= cfg["fallback_days_before_expiry"]:
        return RollDecision(
            True,
            "fallback",
            current_sym,
            next_sym,
            current_vol,
            next_vol,
            check_dates,
            dte,
        )

    reason = "volume_not_crossed" if check_dates else "missing_volume"
    return RollDecision(False, reason, current_sym, next_sym, current_vol, next_vol, check_dates, dte)


async def fetch_daily_volumes(
    ib: Any,
    contract: Any,
    *,
    lookback_days: int = 10,
    bar_size: str = "1 day",
    timezone: str = DEFAULT_TIMEZONE,
) -> Dict[date, float]:
    bars = await ib.reqHistoricalDataAsync(
        contract=contract,
        endDateTime="",
        durationStr=f"{max(1, int(lookback_days))} D",
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
    )
    return daily_volumes_by_date(bars, timezone=timezone)


async def select_active_mnq_contract(
    ib: Any,
    contract_cfg: Mapping[str, Any] | None,
    *,
    as_of: datetime | None = None,
    exchange: str = "CME",
) -> Tuple[Any, RollDecision]:
    """Select the active MNQ outright contract using volume rollover when needed."""
    from ib_async import Future

    base = Future(symbol="MNQ", exchange=exchange, currency="USD")
    details = await ib.reqContractDetailsAsync(base)
    if not details:
        raise RuntimeError("No MNQ contract details from IBKR")

    contracts = [d.contract for d in details]
    current, next_contract = current_and_next_contract(contracts, as_of=as_of)
    if current is None:
        raise RuntimeError("No active MNQ contracts from IBKR")

    cfg = rollover_settings(contract_cfg)
    if not cfg["enabled"] or cfg["method"] != "volume" or next_contract is None:
        decision = RollDecision(False, "rollover_disabled", contract_symbol(current), None)
        return current, decision

    if days_to_expiry(current, as_of) <= cfg["roll_window_days"]:
        current_vols = await fetch_daily_volumes(
            ib,
            current,
            lookback_days=max(cfg["roll_window_days"], cfg["confirmation_days"] + 2),
            bar_size=cfg["volume_bar_size"],
            timezone=cfg["timezone"],
        )
        next_vols = await fetch_daily_volumes(
            ib,
            next_contract,
            lookback_days=max(cfg["roll_window_days"], cfg["confirmation_days"] + 2),
            bar_size=cfg["volume_bar_size"],
            timezone=cfg["timezone"],
        )
    else:
        current_vols, next_vols = {}, {}

    decision = evaluate_roll_decision(current, next_contract, current_vols, next_vols, contract_cfg, as_of=as_of)
    chosen = next_contract if decision.should_roll and next_contract is not None else current
    qualified = await ib.qualifyContractsAsync(chosen)
    return (qualified[0] if qualified else chosen), decision


def _date_to_roll_context(
    contracts_for_range: Sequence[Tuple[str, datetime, datetime, str]],
    day: date,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    for idx, (symbol, _start, end_dt, _expiry) in enumerate(contracts_for_range):
        expiry = end_dt.date()
        if day <= expiry:
            next_symbol = contracts_for_range[idx + 1][0] if idx + 1 < len(contracts_for_range) else None
            return symbol, next_symbol, (expiry - day).days
    return None, None, None


def assign_contract_per_day(
    df: pd.DataFrame,
    contracts_for_range: Sequence[Tuple[str, datetime, datetime, str]],
    contract_cfg: Mapping[str, Any] | None,
    *,
    timestamp_col: str = "timestamp",
    symbol_col: str = "symbol",
    volume_col: str = "volume",
) -> Dict[date, str]:
    """
    Pick the contract symbol to use for each day in a multi-contract dataset.

    Outside the roll window it returns the calendar front month. Inside the
    window it rolls one-way when the next contract's daily volume exceeds current
    volume, with the configured fallback near expiry.
    """
    if df.empty:
        return {}

    cfg = rollover_settings(contract_cfg)
    work = df[[timestamp_col, symbol_col, volume_col]].copy()
    work["_roll_date"] = work[timestamp_col].apply(lambda v: pd.Timestamp(v).date())
    daily = work.groupby(["_roll_date", symbol_col])[volume_col].sum()

    assignments: Dict[date, str] = {}
    active_symbol: Optional[str] = None
    for day in sorted(work["_roll_date"].unique()):
        calendar_current, calendar_next, dte = _date_to_roll_context(contracts_for_range, day)
        if calendar_current is None:
            continue
        if active_symbol is None:
            active_symbol = calendar_current
        if calendar_current != active_symbol and active_symbol not in set(work[symbol_col]):
            active_symbol = calendar_current

        current = active_symbol
        # Once rolled early, the "next" comparison should continue along the chain.
        symbols = [c[0] for c in contracts_for_range]
        if current in symbols:
            idx = symbols.index(current)
            next_symbol = symbols[idx + 1] if idx + 1 < len(symbols) else None
            _, _next_unused, dte = _date_to_roll_context(contracts_for_range[idx:], day)
            if dte is None:
                dte = 999
        else:
            next_symbol = calendar_next
            dte = dte if dte is not None else 999

        if cfg["enabled"] and cfg["method"] == "volume" and next_symbol and dte <= cfg["roll_window_days"]:
            current_vol = float(daily.get((day, current), 0.0))
            next_vol = float(daily.get((day, next_symbol), 0.0))
            if next_vol > current_vol or dte <= cfg["fallback_days_before_expiry"]:
                active_symbol = next_symbol
                logger.info(
                    "Volume rollover assignment: %s -> %s on %s (vol %.0f vs %.0f, dte=%s)",
                    current,
                    next_symbol,
                    day,
                    current_vol,
                    next_vol,
                    dte,
                )
        assignments[day] = active_symbol

    return assignments


def should_run_roll_check(shared_state: Dict[str, Any], cfg: Mapping[str, Any], now: datetime) -> bool:
    """Once per configured day after check_after in the rollover timezone."""
    settings = rollover_settings({"rollover": cfg} if "method" in cfg else cfg)
    tz = pytz.timezone(settings["timezone"])
    local_now = now.astimezone(tz) if now.tzinfo else tz.localize(now)
    hour, minute = [int(part) for part in settings["check_after"].split(":", 1)]
    if local_now.time() < time(hour, minute):
        return False
    key = "last_roll_check_date"
    today_key = local_now.date().isoformat()
    if shared_state.get(key) == today_key:
        return False
    shared_state[key] = today_key
    return True
