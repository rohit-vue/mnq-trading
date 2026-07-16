"""
=============================================================================
MNQ SUPERTREND + EMA TRADING SYSTEM - v2.0
=============================================================================
Interactive trading system for MNQ futures.
Run: python main_v2.py

Features (v2.0):
- Auto-reconnect when TWS disconnects or restarts
- Market orders instead of limit orders
- Clean terminal dashboard with P&L tracking
- All calculations in background, dashboard stays clean

Modes:
1. Backtest - Test strategy on historical IBKR data
2. Paper Trade - Trade on IBKR paper account
3. Live Trade - Trade on IBKR live account (real money)
=============================================================================
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Optional
from pathlib import Path
import yaml
import pytz
import pandas as pd
import numpy as np

from utils.load_env import load_project_dotenv
from data.contract_rollover import (
    contract_symbol as rollover_contract_symbol,
    evaluate_roll_decision,
    fetch_daily_volumes,
    rollover_settings,
    select_active_mnq_contract,
    should_run_roll_check,
)

load_project_dotenv()

# Configure logging to file only (keep terminal clean)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('trading.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

# Primary timeframe from strategy.yaml (e.g. 10m, 15m)
from timeframe_utils import get_primary_bar_size, get_primary_bars_per_hour

# Also add a stream handler for errors only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)
console_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
logging.getLogger().addHandler(console_handler)

# Suppress verbose logs
logging.getLogger('strategy.signal_engine').setLevel(logging.INFO)  # INFO to see entry condition checks
logging.getLogger('strategy.state_manager').setLevel(logging.WARNING)
# Keep backtest.backtest_engine at INFO level to see flip and entry logs
logging.getLogger('backtest.backtest_engine').setLevel(logging.INFO)
logging.getLogger('ib_async.wrapper').setLevel(logging.WARNING)
logging.getLogger('ib_async.client').setLevel(logging.WARNING)
logging.getLogger('ib_async.ib').setLevel(logging.WARNING)


def contract_label_from_ib(contract: Any) -> str:
    """Human-readable contract id for trading.log lines."""
    ls = getattr(contract, "localSymbol", None)
    if ls:
        return str(ls)
    sym = getattr(contract, "symbol", None)
    return str(sym) if sym else "UNKNOWN"


async def ensure_market_data(ib, contract: Any, ibkr_cfg: dict) -> Any:
    """
    Subscribe to MNQ quotes so keepUpToDate bars stream and IBKR allows orders (avoids Error 354).

    Prefers LIVE (type 1). Falls back to delayed types only when accept_delayed is true
    and live ticks do not arrive.
    """
    data_cfg = ibkr_cfg.get("data", {})
    delayed_cfg = data_cfg.get("delayed_data", {})
    accept_delayed = delayed_cfg.get("accept", False)
    # Prefer live; optionally fall back to delayed / delayed-frozen.
    types_to_try = [1, 3, 4] if accept_delayed else [1]
    mdt_labels = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
    ticker = None

    for mkt_type in types_to_try:
        label = mdt_labels.get(mkt_type, str(mkt_type))
        logger.info("Requesting market data type=%s (%s)", mkt_type, label)
        ib.reqMarketDataType(mkt_type)
        await asyncio.sleep(0.3)
        if ticker is not None:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
            await asyncio.sleep(0.2)
        ticker = ib.reqMktData(contract, "", False, False)
        for _ in range(20):
            await asyncio.sleep(0.5)
            last = ticker.last
            if last is not None and last == last:  # not NaN
                reported = getattr(ticker, "marketDataType", mkt_type)
                logger.info(
                    "Market data active: last=%s bid=%s ask=%s (requested=%s reported=%s)",
                    last,
                    ticker.bid,
                    ticker.ask,
                    label,
                    mdt_labels.get(int(reported or mkt_type), reported),
                )
                return ticker
            close = ticker.close
            if close is not None and close == close:
                logger.info("Market data active (close=%s, requested=%s)", close, label)
                return ticker
        logger.warning("No ticks yet for market data type=%s", label)

    if delayed_cfg.get("warn_user", True):
        msg = (
            "No MNQ market data ticks received. In TWS/Gateway enable market data "
            "(live CME subscription preferred; delayed as fallback) "
            "(Global Configuration -> Market Data). "
            "Until quotes flow, orders may be rejected (IB Error 354) and bars may not update."
        )
        logger.warning(msg)
        print(f"\n[!] {msg}\n")
    return ticker


def fast_tick_bar_close_config(strategy_cfg: dict) -> dict:
    raw = strategy_cfg.get("execution", {}).get("fast_tick_bar_close", {})
    if isinstance(raw, bool):
        return {
            "enabled": raw,
            "grace_sec": 0.25,
            "reconcile_official": True,
            "stale_resubscribe": {
                "enabled": True,
                "stale_sec": 90.0,
                "cooldown_sec": 120.0,
                "min_price_gap_points": 4.0,
                "recent_stream_sec": 30.0,
            },
        }
    raw = raw or {}
    stale_raw = raw.get("stale_resubscribe", {})
    if isinstance(stale_raw, bool):
        stale_raw = {"enabled": stale_raw}
    stale_raw = stale_raw or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "grace_sec": float(raw.get("grace_sec", 0.25)),
        "reconcile_official": bool(raw.get("reconcile_official", True)),
        "stale_resubscribe": {
            "enabled": bool(stale_raw.get("enabled", True)),
            "stale_sec": float(stale_raw.get("stale_sec", 90.0)),
            "cooldown_sec": float(stale_raw.get("cooldown_sec", 120.0)),
            "min_price_gap_points": float(stale_raw.get("min_price_gap_points", 4.0)),
            "recent_stream_sec": float(stale_raw.get("recent_stream_sec", 30.0)),
        },
    }


def read_ticker_last_trade(ticker: Any) -> Optional[float]:
    """Raw last-trade price used by the tick-built OHLC path."""
    if ticker is None:
        return None
    val = getattr(ticker, "last", None)
    if callable(val):
        try:
            val = val()
        except Exception:
            return None
    if val is None or val != val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _record_market_data_tick(shared_state: Optional[dict], ticker: Any) -> None:
    if shared_state is None:
        return
    now = datetime.now(pytz.UTC)
    shared_state["market_data_last_update_wall"] = now
    last = read_ticker_last_trade(ticker)
    if last is None:
        return
    prev = shared_state.get("market_data_last_trade")
    if prev is None or abs(float(prev) - last) > 1e-9:
        shared_state["market_data_last_price_change_wall"] = now
        shared_state["market_data_last_trade"] = last


def detach_tick_bar_builder(ticker: Any, handler: Any) -> None:
    """Remove a prior reqMktData update handler before replacing a ticker."""
    if ticker is None or handler is None:
        return
    try:
        ticker.updateEvent -= handler
    except (ValueError, AttributeError):
        pass


def attach_tick_bar_builder(ticker: Any, builder: Any, shared_state: Optional[dict] = None) -> Any:
    """Feed reqMktData ticker updates into the local tick bar builder."""
    if ticker is None:
        return None

    def on_ticker_update(_ticker=ticker) -> None:
        _record_market_data_tick(shared_state, _ticker)
        builder.update_from_ticker(_ticker)

    ticker.updateEvent += on_ticker_update
    # Seed from the current snapshot if one is already populated.
    _record_market_data_tick(shared_state, ticker)
    builder.update_from_ticker(ticker)
    return on_ticker_update


async def run_fast_tick_bar_close_loop(
    *,
    mode_label: str,
    contract_label: str,
    feed: Any,
    builder: Any,
    shared_state: dict,
    grace_sec: float,
) -> None:
    """Emit tick-built bars at primary-timeframe boundaries."""
    logger.info(
        "FAST_TICK_BAR_CLOSE enabled | mode=%s | contract=%s | bar_size=%s | grace_sec=%.3f",
        mode_label,
        contract_label,
        getattr(builder, "bar_size", "n/a"),
        grace_sec,
    )
    while shared_state.get("running", False):
        try:
            await asyncio.sleep(builder.seconds_until_next_emit(grace_sec=grace_sec))
            if not shared_state.get("running", False):
                break
            active_feed = shared_state.get("feed", feed)
            if active_feed is None:
                continue
            tick_bar = builder.finalize_expected_closed()
            if tick_bar is None:
                logger.debug("FAST_TICK_BAR_CLOSE skipped: no local bar to freeze")
                continue
            emitted = active_feed.emit_external_bar(tick_bar.to_bar(), source="tick")
            if emitted:
                logger.info(
                    "TICK_BAR_CLOSE | mode=%s | contract=%s | bar_end=%s | "
                    "O=%.4f H=%.4f L=%.4f C=%.4f | volume_proxy=%.0f | ticks=%s",
                    mode_label,
                    shared_state.get("contract_label", contract_label),
                    tick_bar.start.isoformat(),
                    tick_bar.open,
                    tick_bar.high,
                    tick_bar.low,
                    tick_bar.close,
                    tick_bar.volume,
                    tick_bar.bar_count,
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("FAST_TICK_BAR_CLOSE error: %s", exc)
            await asyncio.sleep(1)


def resolve_ib_client_id(conn_cfg: dict) -> int:
    """
    IBKR allows one socket per (host, port, client_id). Error 326 means ID is already in use.
    Override with env IB_CLIENT_ID to avoid clashes with another bot, TWS chart, or IDE session.
    """
    raw = os.environ.get("IB_CLIENT_ID", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        return int(conn_cfg.get("client_id", 1))
    except (TypeError, ValueError):
        return 1


def startup_progress(msg: str) -> None:
    """Print startup step to console (logs go to trading.log only)."""
    print(f"  [i] {msg}")
    sys.stdout.flush()


def notify_telegram_background(telegram: Any, coro) -> None:
    """Fire-and-forget Telegram send so IBKR setup is not blocked on network I/O."""
    if telegram is not None and hasattr(telegram, "schedule"):
        telegram.schedule(coro)


def seed_dashboard_prices_from_feed(dashboard: Any, feed: Any) -> None:
    """Set dashboard price/time from buffered OHLC so PRICE_POLL is non-zero before the next tick."""
    if feed is None:
        return
    df = feed.get_dataframe()
    if df is None or len(df) == 0:
        return
    row = df.iloc[-1]
    try:
        dashboard.update_price(float(row["close"]), row.name)
    except Exception:
        try:
            dashboard.update_price(float(row["close"]))
        except Exception:
            pass


def log_price_poll_snapshot(
    mode: str,
    contract_label: str,
    dashboard: Any,
    *,
    feed: Any = None,
    market_ticker: Any = None,
    ib_connected: Optional[bool] = None,
) -> None:
    """
    Log once per dashboard refresh (~5s).

    - ib_last: streaming quote from IB reqMktData (live or delayed).
    - forming_*: current in-progress primary bar from the feed buffer (iloc[-1]).
    - signal_*: last completed primary bar used for strategy (iloc[-2]).
    - EMA200_1H / close_1H: from last closed bar alignment (updates each bar close).
    """
    n_bars = 0
    forming_c = float("nan")
    forming_ts = "n/a"
    signal_c = float("nan")
    signal_ts = "n/a"

    if feed is not None:
        df = feed.get_dataframe()
        if df is not None and len(df) > 0:
            n_bars = len(df)
            fr = df.iloc[-1]
            forming_c = float(fr["close"])
            ts = fr.name
            forming_ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if df is not None and len(df) >= 2:
            sr = df.iloc[-2]
            signal_c = float(sr["close"])
            ts = sr.name
            signal_ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        else:
            closed = feed.get_latest_closed_bar()
            if closed:
                signal_c = float(closed["close"])
                dt = closed["datetime"]
                signal_ts = dt.isoformat() if hasattr(dt, "isoformat") else str(dt)

    quote = read_ib_ticker_quote(market_ticker)
    ib_last = quote["last"] if quote else float("nan")
    ib_bid = quote.get("bid") if quote else None
    ib_ask = quote.get("ask") if quote else None
    bid_s = f"{ib_bid:.2f}" if ib_bid is not None else "n/a"
    ask_s = f"{ib_ask:.2f}" if ib_ask is not None else "n/a"

    ema_1h = float(dashboard.ema_1h) if getattr(dashboard, "ema_1h", 0) else float("nan")
    close_1h = float(dashboard.close_1h) if getattr(dashboard, "close_1h", 0) else float("nan")
    ema_1h_s = f"{ema_1h:.2f}" if ema_1h == ema_1h else "n/a"
    close_1h_s = f"{close_1h:.2f}" if close_1h == close_1h else "n/a"

    conn_ib = ib_connected if ib_connected is not None else dashboard.is_connected
    bar_age_min = feed.minutes_since_last_bar() if feed is not None else None
    bar_age_s = f"{bar_age_min:.0f}" if bar_age_min is not None else "n/a"

    logger.info(
        "PRICE_POLL | mode=%s | contract=%s | ib_last=%s | ib_bid=%s | ib_ask=%s | "
        "forming_close=%.4f | forming_ts=%s | signal_close=%.4f | signal_ts=%s | "
        "EMA200_1H=%s | close_1H=%s | EMA_side=%s | ST=%s | ADX=%.2f | "
        "bar_age_min=%s | bars=%s | ib_connected=%s",
        mode,
        contract_label,
        f"{ib_last:.4f}" if ib_last == ib_last else "n/a",
        bid_s,
        ask_s,
        forming_c,
        forming_ts,
        signal_c,
        signal_ts,
        ema_1h_s,
        close_1h_s,
        dashboard.ema_status,
        dashboard.st_direction,
        float(dashboard.adx_value),
        bar_age_s,
        n_bars,
        conn_ib,
    )


def log_bar_close_snapshot(mode: str, contract_label: str, bar: Any) -> None:
    """Log each primary bar close processed by the strategy (aligned row)."""
    ts = bar.name
    ts_text = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

    def _f(key: str) -> float:
        try:
            if key not in bar.index:
                return float("nan")
            return float(bar[key])
        except Exception:
            return float("nan")

    o, h, lo, c = _f("open"), _f("high"), _f("low"), _f("close")
    vol = bar["volume"] if "volume" in bar.index else np.nan
    vol_s = f"{float(vol):.0f}" if pd.notna(vol) else "n/a"
    ema_1h = _f("ema_1h")
    close_1h = _f("close_1h")
    ema_1h_s = f"{ema_1h:.2f}" if pd.notna(ema_1h) else "n/a"
    close_1h_s = f"{close_1h:.2f}" if pd.notna(close_1h) else "n/a"
    logger.info(
        "BAR_CLOSE | mode=%s | contract=%s | bar_end=%s | O=%.4f H=%.4f L=%.4f C=%.4f | "
        "volume=%s | EMA200_1H=%s | close_1H=%s",
        mode,
        contract_label,
        ts_text,
        o,
        h,
        lo,
        c,
        vol_s,
        ema_1h_s,
        close_1h_s,
    )


def _bar_value(bar: Any, key: str, default: Any = np.nan) -> Any:
    """Safe column access for an aligned bar Series."""
    try:
        if key in bar.index:
            return bar[key]
    except Exception:
        pass
    return default


def _bar_bool(bar: Any, *keys: str) -> bool:
    """True if any of the given bar columns is truthy (NaN-safe)."""
    for key in keys:
        val = _bar_value(bar, key, False)
        try:
            if pd.isna(val):
                continue
        except (TypeError, ValueError):
            pass
        if bool(val):
            return True
    return False


def _entry_no_trade_reason(bar: Any, state: Any, signal_engine: Any, entry_updates: dict) -> str:
    """
    Human-readable reason why no entry was taken on this bar.

    Mirrors SignalEngine.evaluate_entry_conditions decision branches so the log
    explains exactly what the engine did (or why it did nothing).
    """
    bull_flip = _bar_bool(bar, "st_bull_flip_long", "st_bull_flip_entry_long", "st_bull_flip")
    bear_flip = _bar_bool(bar, "st_bear_flip_short", "st_bear_flip_entry_short", "st_bear_flip")

    if entry_updates.get("set_adx_wait_long"):
        n = entry_updates["set_adx_wait_long"].get("bars", "?")
        return f"LONG setup aligned but ADX < {signal_engine.adx_threshold_long:g} -> ADX wait armed ({n} bars)"
    if entry_updates.get("set_adx_wait_short"):
        n = entry_updates["set_adx_wait_short"].get("bars", "?")
        return f"SHORT setup aligned but ADX < {signal_engine.adx_threshold_short:g} -> ADX wait armed ({n} bars)"
    if entry_updates.get("set_pending_long_ema_wait"):
        return "LONG ST flip but prev 1H close <= EMA -> waiting for EMA cross up"
    if entry_updates.get("set_pending_short_ema_wait"):
        return "SHORT ST flip but prev 1H close >= EMA -> waiting for EMA cross down"
    if entry_updates.get("decrement_adx_wait_long") or entry_updates.get("decrement_adx_wait_short"):
        return "ADX wait continues (ADX still below threshold)"
    if entry_updates.get("clear_adx_wait_long") or entry_updates.get("clear_adx_wait_short"):
        return "ADX wait expired (no ADX confirmation within window)"
    if entry_updates.get("set_volume_wait_long") or entry_updates.get("set_volume_wait_short"):
        return "setup confirmed but volume below MA -> volume wait armed"
    if bull_flip and state.traded_in_bull_trend:
        return "LONG ST flip but already traded this bull trend (blocked until next flip)"
    if bear_flip and state.traded_in_bear_trend:
        return "SHORT ST flip but already traded this bear trend (blocked until next flip)"
    if state.pending_adx_long:
        return f"LONG ADX wait active ({state.adx_wait_bars_left_long} bars left)"
    if state.pending_adx_short:
        return f"SHORT ADX wait active ({state.adx_wait_bars_left_short} bars left)"
    if state.pending_long_ema_wait:
        return "LONG EMA-cross wait active (need 1H close > EMA + ADX)"
    if state.pending_short_ema_wait:
        return "SHORT EMA-cross wait active (need 1H close < EMA + ADX)"
    return "no trigger this bar (no ST flip and no pending setup)"


def log_entry_decision(
    mode: str,
    contract_label: str,
    bar: Any,
    state: Any,
    signal_engine: Any,
    entry_signal: Any,
    entry_updates: dict,
) -> None:
    """Full visibility into the entry decision on every flat bar close."""
    ts = bar.name
    ts_text = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

    direction = _bar_value(bar, "direction_long", _bar_value(bar, "direction", 0))
    st_dir = "BULL" if direction == -1 else "BEAR"
    bull_flip = _bar_bool(bar, "st_bull_flip_long", "st_bull_flip_entry_long", "st_bull_flip")
    bear_flip = _bar_bool(bar, "st_bear_flip_short", "st_bear_flip_entry_short", "st_bear_flip")

    close_1h_cross = _bar_value(bar, "close_1h_cross", np.nan)
    ema_1h_cross = _bar_value(bar, "ema_1h_cross", np.nan)
    prev_close = close_1h_cross if not pd.isna(close_1h_cross) else _bar_value(bar, "close_1h", np.nan)
    prev_ema = ema_1h_cross if not pd.isna(ema_1h_cross) else _bar_value(bar, "ema_1h", np.nan)
    adx = float(_bar_value(bar, "adx", 0) or 0)

    if entry_signal is not None:
        decision = (
            f"ENTRY {entry_signal.signal_type.value} @ {entry_signal.price:.2f} "
            f"(trigger={entry_signal.trigger})"
        )
    else:
        decision = "NO ENTRY: " + _entry_no_trade_reason(bar, state, signal_engine, entry_updates)

    def _fmt(v: Any) -> str:
        try:
            return f"{float(v):.2f}" if not pd.isna(v) else "n/a"
        except (TypeError, ValueError):
            return "n/a"

    logger.info(
        "ENTRY_EVAL | mode=%s | contract=%s | bar_end=%s | ST=%s | bull_flip=%s | bear_flip=%s | "
        "prev1H_close=%s | EMA1H=%s | ADX=%.1f (thrL=%.0f thrS=%.0f useL=%s useS=%s) | "
        "tradedBull=%s | tradedBear=%s | pendEMA_L=%s | pendEMA_S=%s | pendADX_L=%s | pendADX_S=%s | %s",
        mode,
        contract_label,
        ts_text,
        st_dir,
        bull_flip,
        bear_flip,
        _fmt(prev_close),
        _fmt(prev_ema),
        adx,
        signal_engine.adx_threshold_long,
        signal_engine.adx_threshold_short,
        signal_engine.use_adx_long,
        signal_engine.use_adx_short,
        state.traded_in_bull_trend,
        state.traded_in_bear_trend,
        state.pending_long_ema_wait,
        state.pending_short_ema_wait,
        state.pending_adx_long,
        state.pending_adx_short,
        decision,
    )


def log_position_hold(mode: str, contract_label: str, bar: Any, state: Any) -> None:
    """Per-bar status while a position is open (distance to SL/TP)."""
    ts = bar.name
    ts_text = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    close = float(_bar_value(bar, "close", np.nan) or 0)
    direction = "LONG" if state.position_size > 0 else "SHORT"
    if state.position_size > 0:
        sl_dist = close - state.stop_loss
        tp_dist = state.take_profit - close
    else:
        sl_dist = state.stop_loss - close
        tp_dist = close - state.take_profit
    logger.info(
        "POSITION_HOLD | mode=%s | contract=%s | bar_end=%s | dir=%s | entry=%.2f | price=%.2f | "
        "SL=%.2f (%.2f pts away) | TP=%.2f (%.2f pts away)",
        mode,
        contract_label,
        ts_text,
        direction,
        state.entry_price,
        close,
        state.stop_loss,
        sl_dist,
        state.take_profit,
        tp_dist,
    )


def read_ib_ticker_price(ticker: Any) -> Optional[float]:
    """Best available MNQ price from an IB market data ticker."""
    quote = read_ib_ticker_quote(ticker)
    return quote.get("last") if quote else None


def read_ib_ticker_quote(ticker: Any) -> Optional[dict]:
    """Last/bid/ask from IB reqMktData ticker (live or delayed)."""
    if ticker is None:
        return None

    def _f(val: Any) -> Optional[float]:
        if val is None or val != val:
            return None
        if callable(val):
            try:
                val = val()
            except Exception:
                return None
            if val is None or val != val:
                return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    last = _f(getattr(ticker, "last", None))
    close = _f(getattr(ticker, "close", None))
    bid = _f(getattr(ticker, "bid", None))
    ask = _f(getattr(ticker, "ask", None))
    mkt = _f(getattr(ticker, "marketPrice", None))

    price = last or mkt or close
    if price is None and bid is not None and ask is not None:
        price = (bid + ask) / 2.0

    if price is None and bid is None and ask is None:
        return None

    return {"last": price, "bid": bid, "ask": ask}


def resolve_market_price(
    shared_state: dict,
    feed: Any = None,
) -> tuple[Optional[float], str]:
    """
    Best current MNQ price for dashboard / Telegram P&L.

    Prefer the feed's forming primary bar (updates on IB historical refetch for
    delayed data). Fall back to reqMktData last/bid/ask when the buffer is empty.
    """
    feed = feed or shared_state.get("feed")
    if feed is not None:
        df = feed.get_dataframe()
        if df is not None and len(df) > 0:
            return float(df.iloc[-1]["close"]), "forming_bar"

    ticker = shared_state.get("market_data_ticker")
    quote = read_ib_ticker_quote(ticker)
    if quote and quote.get("last") is not None:
        return quote["last"], "ib_ticker"

    return None, "none"


def primary_bar_stream_idle_minutes(bar_size: str) -> float:
    """
    Wall-clock minutes without IB stream activity before restart.

    Uses stream idle time, NOT bar timestamp vs wall clock (delayed IB data is
    always ~15–20 min behind and must not trigger restart loops).
    """
    size = (bar_size or "10 mins").lower()
    if "5 min" in size:
        return 30.0
    if "15 min" in size:
        return 50.0
    if "30 min" in size:
        return 90.0
    if "1 hour" in size or "60" in size:
        return 180.0
    return 45.0  # default 10m


async def refresh_feed_if_stale(
    feed: Any,
    bar_size: str,
    *,
    mode_label: str,
    only_when_market_open: bool = True,
) -> bool:
    """Restart IB keepUpToDate subscription when the stream stops advancing.

    Note: callers run feed.poll_refetch_and_emit() first (boundary-aligned
    failover in the dashboard maintenance loop), then this restart path.
    """
    if feed is None:
        return False

    stale_limit = primary_bar_stream_idle_minutes(bar_size)
    idle = feed.minutes_since_stream_activity()
    if idle is None or idle < stale_limit:
        return False
    if only_when_market_open and not feed.is_market_hours(check_rth=False):
        return False
    if not feed.can_restart(cooldown_minutes=5.0):
        logger.debug(
            "Stream idle %.0f min (limit %.0f) but restart cooldown active — skipping",
            idle,
            stale_limit,
        )
        return False
    logger.warning(
        "Feed stream idle %.0f min (limit %.0f) — restarting %s subscription",
        idle,
        stale_limit,
        mode_label,
    )
    await feed.restart(initial_lookback_days=5)
    return True


async def recover_stale_market_data(
    *,
    ib: Any,
    mode_label: str,
    contract_label: str,
    feed: Any,
    ticker: Any,
    shared_state: dict,
    strategy_cfg: dict,
    ibkr_cfg: dict,
) -> Any:
    """Resubscribe reqMktData when live last-trade ticks freeze but bars keep moving."""
    fast_cfg = fast_tick_bar_close_config(strategy_cfg)
    stale_cfg = fast_cfg.get("stale_resubscribe", {})
    if not fast_cfg.get("enabled") or not stale_cfg.get("enabled", True):
        return ticker

    builder = shared_state.get("tick_bar_builder")
    contract = shared_state.get("contract")
    if builder is None or contract is None or ticker is None or feed is None:
        return ticker
    if hasattr(feed, "is_market_hours") and not feed.is_market_hours(check_rth=False):
        return ticker

    now = datetime.now(pytz.UTC)
    last_change = shared_state.get("market_data_last_price_change_wall")
    if last_change is None:
        last_change = shared_state.get("market_data_last_update_wall")
    if last_change is None:
        return ticker
    if getattr(last_change, "tzinfo", None) is None:
        last_change = pytz.UTC.localize(last_change)

    stale_sec = float(stale_cfg.get("stale_sec", 90.0))
    stale_age = (now - last_change).total_seconds()
    if stale_age < stale_sec:
        return ticker

    stream_idle_min = None
    if hasattr(feed, "minutes_since_stream_activity"):
        stream_idle_min = feed.minutes_since_stream_activity()
    recent_stream_sec = float(stale_cfg.get("recent_stream_sec", 30.0))
    if stream_idle_min is None or (stream_idle_min * 60.0) > recent_stream_sec:
        return ticker

    last_trade = read_ticker_last_trade(ticker)
    feed_close = None
    df = feed.get_dataframe() if hasattr(feed, "get_dataframe") else None
    if df is not None and len(df) > 0:
        try:
            feed_close = float(df.iloc[-1]["close"])
        except (TypeError, ValueError, KeyError):
            feed_close = None

    price_gap = None
    if last_trade is not None and feed_close is not None:
        price_gap = abs(feed_close - last_trade)
        min_gap = float(stale_cfg.get("min_price_gap_points", 4.0))
        if price_gap < min_gap:
            return ticker
    elif stale_age < stale_sec * 2.0:
        # If we cannot compare prices, wait for stronger evidence before cycling.
        return ticker

    cooldown_sec = float(stale_cfg.get("cooldown_sec", 120.0))
    last_resub = shared_state.get("market_data_last_resubscribe_wall")
    if last_resub is not None:
        if getattr(last_resub, "tzinfo", None) is None:
            last_resub = pytz.UTC.localize(last_resub)
        if (now - last_resub).total_seconds() < cooldown_sec:
            return ticker

    shared_state["market_data_last_resubscribe_wall"] = now
    logger.warning(
        "MARKET_DATA_STALE_RESUBSCRIBE | mode=%s | contract=%s | stale_sec=%.0f | "
        "last=%s | feed_close=%s | gap=%s | stream_idle_sec=%.1f",
        mode_label,
        contract_label,
        stale_age,
        f"{last_trade:.2f}" if last_trade is not None else "n/a",
        f"{feed_close:.2f}" if feed_close is not None else "n/a",
        f"{price_gap:.2f}" if price_gap is not None else "n/a",
        stream_idle_min * 60.0,
    )

    old_handler = shared_state.get("tick_bar_update_handler")
    detach_tick_bar_builder(ticker, old_handler)
    try:
        ib.cancelMktData(contract)
    except Exception as exc:
        logger.debug("Could not cancel stale market data for %s: %s", contract_label, exc)

    try:
        await asyncio.sleep(0.2)
        new_ticker = await ensure_market_data(ib, contract, ibkr_cfg)
        if new_ticker is None:
            return ticker
        shared_state["market_data_ticker"] = new_ticker
        shared_state["tick_bar_update_handler"] = attach_tick_bar_builder(
            new_ticker,
            builder,
            shared_state=shared_state,
        )
        logger.info("MARKET_DATA_RESUBSCRIBED | mode=%s | contract=%s", mode_label, contract_label)
        return new_ticker
    except Exception as exc:
        logger.exception("MARKET_DATA_RESUBSCRIBE_FAILED | mode=%s | contract=%s | error=%s", mode_label, contract_label, exc)
        return ticker


def reset_roll_state_flags(state_manager: Any) -> None:
    """Reset per-contract trend/wait state when the active futures month changes."""
    state_manager.state.traded_in_bull_trend = False
    state_manager.state.traded_in_bear_trend = False
    state_manager.clear_pending_long_ema_wait()
    state_manager.clear_pending_short_ema_wait()
    state_manager.clear_adx_wait_long()
    state_manager.clear_adx_wait_short()
    state_manager.clear_volume_wait_long()
    state_manager.clear_volume_wait_short()
    state_manager.save_state()


async def maybe_roll_mnq_contract(
    *,
    ib: Any,
    contract_cfg: dict,
    ibkr_cfg: dict,
    telegram: Any,
    dashboard: Any,
    shared_state: dict,
    state_manager: Any,
    contracts_count: int,
    mode_label: str,
) -> Optional[str]:
    """Daily volume-based futures rollover check for paper/live trading."""
    contract = shared_state.get("contract")
    feed = shared_state.get("feed")
    order_manager = shared_state.get("order_manager")
    position_tracker = shared_state.get("position_tracker")
    if contract is None or feed is None or order_manager is None or position_tracker is None:
        return None

    cfg = rollover_settings(contract_cfg)
    if not cfg["enabled"] or cfg["method"] != "volume":
        return None
    if not should_run_roll_check(shared_state, contract_cfg, datetime.now(pytz.UTC)):
        return None

    from ib_async import Future

    base_contract = Future(symbol="MNQ", exchange="CME", currency="USD")
    details = await ib.reqContractDetailsAsync(base_contract)
    if not details:
        logger.warning("CONTRACT_ROLL_CHECK | mode=%s | no MNQ contract details", mode_label)
        return None

    contracts = sorted([d.contract for d in details], key=lambda c: getattr(c, "lastTradeDateOrContractMonth", ""))
    try:
        current_idx = next(
            i for i, c in enumerate(contracts)
            if rollover_contract_symbol(c) == rollover_contract_symbol(contract)
        )
    except StopIteration:
        logger.warning(
            "CONTRACT_ROLL_CHECK | mode=%s | current contract %s not in IB details",
            mode_label,
            rollover_contract_symbol(contract),
        )
        return None

    next_contract = contracts[current_idx + 1] if current_idx + 1 < len(contracts) else None
    if next_contract is None:
        logger.info(
            "CONTRACT_ROLL_CHECK | mode=%s | current=%s | no next contract",
            mode_label,
            rollover_contract_symbol(contract),
        )
        return None

    lookback_days = max(cfg["roll_window_days"], cfg["confirmation_days"] + 2)
    current_vols = await fetch_daily_volumes(
        ib,
        contract,
        lookback_days=lookback_days,
        bar_size=cfg["volume_bar_size"],
        timezone=cfg["timezone"],
    )
    next_vols = await fetch_daily_volumes(
        ib,
        next_contract,
        lookback_days=lookback_days,
        bar_size=cfg["volume_bar_size"],
        timezone=cfg["timezone"],
    )
    decision = evaluate_roll_decision(contract, next_contract, current_vols, next_vols, contract_cfg)
    logger.info(
        "CONTRACT_ROLL_CHECK | mode=%s | current=%s | next=%s | dte=%s | "
        "current_vol=%s | next_vol=%s | dates=%s | decision=%s | reason=%s",
        mode_label,
        decision.current_symbol,
        decision.next_symbol,
        decision.days_to_expiry,
        decision.current_volume,
        decision.next_volume,
        ",".join(d.isoformat() for d in decision.comparison_dates),
        decision.should_roll,
        decision.reason,
    )

    if not decision.should_roll:
        return None

    old_contract = contract
    new_contract = next_contract
    old_label = rollover_contract_symbol(old_contract)
    new_label = rollover_contract_symbol(new_contract)
    state = state_manager.state
    if state.position_size != 0:
        action = "SELL" if state.position_size > 0 else "BUY"
        await order_manager.place_market_order(
            action=action,
            quantity=abs(state.position_size) * contracts_count,
        )
        logger.info(
            "CONTRACT_ROLL_EXIT | mode=%s | old=%s | action=%s | qty=%s | reason=%s",
            mode_label,
            old_label,
            action,
            abs(state.position_size) * contracts_count,
            decision.reason,
        )

    reset_roll_state_flags(state_manager)

    qualified = await ib.qualifyContractsAsync(new_contract)
    if qualified:
        new_contract = qualified[0]
    old_ticker = shared_state.get("market_data_ticker")
    old_handler = shared_state.get("tick_bar_update_handler")
    detach_tick_bar_builder(old_ticker, old_handler)
    if old_ticker is not None:
        try:
            ib.cancelMktData(old_contract)
        except Exception as exc:
            logger.debug("Could not cancel old market data for %s: %s", old_label, exc)

    await feed.stop()
    feed.contract = new_contract
    order_manager.contract = new_contract
    position_tracker.contract = new_contract
    position_tracker.position.symbol = getattr(new_contract, "symbol", "MNQ")
    shared_state["contract"] = new_contract
    shared_state["contract_label"] = new_label
    shared_state["market_data_ticker"] = await ensure_market_data(ib, new_contract, ibkr_cfg)
    builder = shared_state.get("tick_bar_builder")
    if builder is not None:
        shared_state["tick_bar_update_handler"] = attach_tick_bar_builder(
            shared_state.get("market_data_ticker"),
            builder,
            shared_state=shared_state,
        )
    await position_tracker.initialize()
    await feed.start(initial_lookback_days=15)
    seed_dashboard_prices_from_feed(dashboard, feed)
    dashboard.print_event("ROLLOVER", f"{old_label} -> {new_label} ({decision.reason})")
    logger.info(
        "CONTRACT_ROLL | mode=%s | old=%s | new=%s | reason=%s | dte=%s | "
        "current_vol=%s | next_vol=%s",
        mode_label,
        old_label,
        new_label,
        decision.reason,
        decision.days_to_expiry,
        decision.current_volume,
        decision.next_volume,
    )
    if hasattr(telegram, "notify_contract_roll"):
        await telegram.notify_contract_roll(
            old_symbol=old_label,
            new_symbol=new_label,
            reason=decision.reason,
            current_volume=decision.current_volume,
            next_volume=decision.next_volume,
            mode=mode_label,
        )
    return new_label


def refresh_dashboard_indicators_from_feed(
    feed: Any,
    mtf: Any,
    dashboard: Any,
    strategy_cfg: dict,
    sides: dict,
    ema_cfg: dict,
) -> bool:
    """
    Recompute ST / EMA / ADX on the last closed bar for dashboard display.
    Does not place orders — used when the feed was stale or between bar closes.
    """
    from data.bar_index import ensure_datetime_index
    from data.live_bar_alignment import enrich_10m_with_1h_like_backtest
    from data.strategy_indicators import live_bar_indicator_slice

    raw = feed.get_dataframe() if feed is not None else None
    if raw is None or len(raw) < 60:
        return False
    feed_tz = getattr(feed, "timezone", "US/Eastern")
    df = ensure_datetime_index(raw, tz=feed_tz, datetime_col=None)
    try:
        inds = live_bar_indicator_slice(
            df,
            sides["long_supertrend_entry"],
            sides["short_supertrend_entry"],
            sides["long_adx"],
            sides["short_adx"],
            long_supertrend_exit=sides["long_supertrend_exit"],
            short_supertrend_exit=sides["short_supertrend_exit"],
            row_i=-2,
        )
        df_1h = mtf.aggregate_1h_from_10m(df)
        df_aligned = enrich_10m_with_1h_like_backtest(df, df_1h, ema_cfg.get("length", 200))
        current_bar = df_aligned.iloc[-2].copy()
        for k, v in inds.items():
            current_bar[k] = v
        st_dir = (
            "BULL"
            if current_bar.get("direction_long", current_bar.get("direction", 0)) == -1
            else "BEAR"
        )
        ema_status = (
            "BULL"
            if current_bar.get("ema_bull")
            else ("BEAR" if current_bar.get("ema_bear") else "NEUTRAL")
        )
        adx_val = float(current_bar.get("adx", 0) or 0)
        dashboard.update_indicators(
            st_dir,
            ema_status,
            adx_val,
            ema_1h=_bar_value(current_bar, "ema_1h"),
            close_1h=_bar_value(current_bar, "close_1h"),
            signal_bar_time=current_bar.name,
        )
        return True
    except Exception as e:
        logger.debug("Dashboard indicator refresh failed: %s", e)
        return False


async def maybe_send_hourly_market_status(
    telegram: Any,
    dashboard: Any,
    feed: Any,
    state_manager: Any,
    *,
    mode_label: str,
    symbol: str,
    last_sent: Optional[datetime],
    interval_sec: int,
    shared_state: Optional[dict] = None,
) -> Optional[datetime]:
    """Send Telegram market snapshot once per interval."""
    if not telegram.enabled or not telegram.notify_market_hourly:
        return last_sent
    now = datetime.now(pytz.timezone("US/Eastern"))
    if last_sent is not None:
        elapsed = (now - last_sent).total_seconds()
        if elapsed < interval_sec:
            return last_sent
    state = state_manager.state if state_manager else None
    pos = int(state.position_size) if state else 0
    entry = float(state.entry_price) if state and state.entry_price else 0.0
    lb = dashboard.last_bar_time
    lb_text = lb.isoformat() if lb is not None and hasattr(lb, "isoformat") else str(lb or "n/a")
    # Stream idle (not bar timestamp lag) — delayed quotes always look "old" by clock.
    stale_min = feed.minutes_since_stream_activity() if feed else None
    n_bars = len(feed.get_dataframe()) if feed and feed.get_dataframe() is not None else 0
    market_open = feed.is_market_hours(check_rth=False) if feed else True
    idle_warn = primary_bar_stream_idle_minutes(
        getattr(feed, "bar_size", "10 mins") if feed else "10 mins"
    ) * 0.8
    mkt_price, _ = resolve_market_price(shared_state or {"feed": feed}, feed)
    if mkt_price is None:
        mkt_price = float(dashboard.current_price)
    await telegram.notify_market_status(
        mode=mode_label,
        symbol=symbol,
        price=float(mkt_price),
        st_direction=dashboard.st_direction,
        ema_status=dashboard.ema_status,
        adx=float(dashboard.adx_value),
        last_bar_ts=lb_text,
        bar_stale_min=stale_min,
        stream_idle_warn_min=idle_warn,
        position_size=pos,
        entry_price=entry,
        feed_bars=n_bars,
        market_open=market_open,
    )
    return now


async def handle_supertrend_flip_telegram(
    telegram: Any,
    current_bar: Any,
    *,
    mode_label: str,
    contract_label: str,
) -> None:
    """Notify Telegram when a completed bar flips SuperTrend."""
    if current_bar.get("st_bull_flip", False):
        direction = "BULLISH"
    elif current_bar.get("st_bear_flip", False):
        direction = "BEARISH"
    else:
        return
    ts = current_bar.name
    ts_text = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    ema_status = (
        "BULL"
        if current_bar.get("ema_bull")
        else ("BEAR" if current_bar.get("ema_bear") else "NEUTRAL")
    )
    adx_val = float(current_bar.get("adx", 0) or 0)
    await telegram.notify_supertrend_flip(
        direction=direction,
        price=float(current_bar["close"]),
        bar_time=ts_text,
        ema_status=ema_status,
        adx=adx_val,
        mode=mode_label,
        symbol=contract_label,
    )


async def run_dashboard_maintenance(
    *,
    ib: Any,
    mode_label: str,
    contract_label: str,
    dashboard: Any,
    feed: Any,
    mtf: Any,
    telegram: Any,
    state_manager: Any,
    strategy_cfg: dict,
    sides: dict,
    ema_cfg: dict,
    primary_bar_size: str,
    market_ticker: Any,
    shared_state: dict,
    contract_cfg: dict,
    ibkr_cfg: dict,
    contracts_count: int,
) -> None:
    """Ticker price, stale-feed recovery, indicator refresh, hourly Telegram status."""
    rolled_label = await maybe_roll_mnq_contract(
        ib=ib,
        contract_cfg=contract_cfg,
        ibkr_cfg=ibkr_cfg,
        telegram=telegram,
        dashboard=dashboard,
        shared_state=shared_state,
        state_manager=state_manager,
        contracts_count=contracts_count,
        mode_label=mode_label,
    )
    if rolled_label:
        contract_label = rolled_label
        feed = shared_state.get("feed", feed)
        mtf = shared_state.get("mtf", mtf)
        market_ticker = shared_state.get("market_data_ticker", market_ticker)

    ticker = market_ticker or shared_state.get("market_data_ticker")

    if feed is not None:
        # Stream-first: keepUpToDate emits bar closes. Boundary-aligned refetch
        # (candle close + ~1s) fires only when the stream missed that close.
        poll_emitted = await feed.poll_refetch_and_emit()
        if poll_emitted:
            logger.info(
                "Maintenance refetch emitted %s bar close(s) on %s feed",
                poll_emitted,
                mode_label,
            )

    ticker = await recover_stale_market_data(
        ib=ib,
        mode_label=mode_label,
        contract_label=contract_label,
        feed=feed,
        ticker=ticker,
        shared_state=shared_state,
        strategy_cfg=strategy_cfg,
        ibkr_cfg=ibkr_cfg,
    )

    price, price_src = resolve_market_price(shared_state, feed)
    if price is None:
        price = read_ib_ticker_price(ticker)
        price_src = "ib_ticker" if price is not None else "none"
    if price is not None:
        telegram.update_current_price(price, source=price_src)
        dashboard.update_price(price)

    refreshed = await refresh_feed_if_stale(
        feed,
        primary_bar_size,
        mode_label=mode_label,
    )
    if refreshed:
        seed_dashboard_prices_from_feed(dashboard, feed)

    refresh_dashboard_indicators_from_feed(
        feed, mtf, dashboard, strategy_cfg, sides, ema_cfg
    )

    key = f"last_hourly_market_{mode_label}"
    shared_state[key] = await maybe_send_hourly_market_status(
        telegram,
        dashboard,
        feed,
        state_manager,
        mode_label=mode_label,
        symbol=contract_label,
        last_sent=shared_state.get(key),
        interval_sec=telegram.market_status_interval,
        shared_state=shared_state,
    )


def print_banner():
    """Print welcome banner."""
    print("\n" + "=" * 60)
    print("     MNQ SUPERTREND + EMA TRADING SYSTEM v2.1")
    print("=" * 60)
    print()


def load_config(config_dir: str = "./config") -> dict:
    """Load all configuration files."""
    config_path = Path(config_dir)
    config = {}
    
    config_files = ['strategy.yaml', 'mnq_contract.yaml', 'risk.yaml', 'ibkr.yaml', 'telegram.yaml']
    
    for filename in config_files:
        filepath = config_path / filename
        if filepath.exists():
            with open(filepath, 'r') as f:
                file_config = yaml.safe_load(f)
                config[filename.replace('.yaml', '')] = file_config
    
    return config


def get_connection_port(ibkr_cfg: dict, mode: str = "paper") -> int:
    """
    Get the correct port based on default_gateway and mode.
    
    Supports:
    - TWS Paper: 7497
    - TWS Live: 7496
    - Gateway Paper: 4002 (for 24/7 operation)
    - Gateway Live: 4001 (for 24/7 operation)
    """
    conn_cfg = ibkr_cfg.get('connection', {})
    ports = conn_cfg.get('ports', {})
    gateway_type = conn_cfg.get('default_gateway', 'tws')
    
    if mode == 'paper':
        if gateway_type == 'gateway':
            return ports.get('gateway_paper', 4002)
        return ports.get('tws_paper', 7497)
    else:  # live
        if gateway_type == 'gateway':
            return ports.get('gateway_live', 4001)
        return ports.get('tws_live', 7496)


def create_telegram_notifier(config: dict) -> 'TelegramNotifier':
    """
    Create and configure TelegramNotifier from config.
    Credentials: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars override
    config/telegram.yaml (see .env.example).
    Returns a configured TelegramNotifier instance.
    """
    import os

    from utils import TelegramNotifier

    tg_cfg = config.get('telegram', {})
    tg_main = tg_cfg.get('telegram', {})
    tg_notify = tg_cfg.get('notifications', {})

    bot_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or str(
        tg_main.get("bot_token", "") or ""
    )
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or str(
        tg_main.get("chat_id", "") or ""
    )

    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        enabled=tg_main.get('enabled', False)
    )
    
    # Configure notification types
    notifier.notify_bot_start = tg_notify.get('bot_start', True)
    notifier.notify_bot_stop = tg_notify.get('bot_stop', True)
    notifier.notify_trade_entry = tg_notify.get('trade_entry', True)
    notifier.notify_trade_exit = tg_notify.get('trade_exit', True)
    notifier.notify_connection = tg_notify.get('connection_status', True)
    notifier.notify_errors = tg_notify.get('errors', True)
    
    pnl_cfg = tg_notify.get('running_pnl', {})
    notifier.notify_running_pnl = pnl_cfg.get('enabled', True)
    notifier.pnl_interval = pnl_cfg.get('interval_seconds', 3600)
    notifier.notify_market_hourly = tg_notify.get('market_status_hourly', True)
    notifier.notify_supertrend_flip_enabled = tg_notify.get('supertrend_flip', True)
    notifier.notify_contract_roll_enabled = tg_notify.get('contract_roll', True)
    notifier.market_status_interval = int(tg_notify.get('market_status_interval_seconds', 3600))
    
    return notifier


def get_menu_choice() -> str:
    """Display main menu and get user choice."""
    print("What would you like to do?\n")
    print("  [1] Backtest - Test strategy on historical data (Databento/IBKR)")
    print("  [2] Paper Trade - Trade on IBKR paper account (with Dashboard)")
    print("  [3] Live Trade - Trade with REAL money (with Dashboard)")
    print("  [0] Exit")
    print()
    
    while True:
        choice = input("Enter your choice (0-3): ").strip()
        if choice in ['0', '1', '2', '3']:
            return choice
        print("Invalid choice. Please enter 0, 1, 2, or 3.")


def get_contracts() -> int:
    """Get number of contracts from user."""
    while True:
        try:
            contracts = input("\nNumber of contracts to trade [1]: ").strip()
            if not contracts:
                return 1
            contracts = int(contracts)
            if contracts > 0:
                return contracts
            print("Must be at least 1 contract.")
        except ValueError:
            print("Please enter a valid number.")


async def startup_reconcile_position(
    ib, contract, position_tracker, state_manager, order_manager,
    dashboard, telegram, contracts_count: int, mode_label: str = "PAPER",
    signal_engine=None
) -> None:
    """
    Check IBKR for an active position and open orders at startup or reconnect.

    Reconstructs bot state (entry price, SL, TP, active_bracket) so the bot
    can continue monitoring an existing position without missing exits.

    Flow:
    1. Read actual IBKR position via position_tracker
    2. Force-fetch fresh open orders from IBKR (reqAllOpenOrdersAsync)
    3. Scan open orders for SL (STP) and TP (LMT) bracket orders
    4. If saved state matches direction → trust saved entry_price, update SL/TP
    5. If no matching saved state → rebuild state from IBKR avg_cost
    6. If no bracket orders found → calculate SL/TP from entry price via signal_engine
    7. Reconstruct order_manager.active_bracket so cancel-on-exit works
    8. Update dashboard + send Telegram alert
    """
    from execution.order_manager import OrderTicket, BracketTickets

    ib_qty = position_tracker.position.quantity
    avg_cost = position_tracker.position.avg_cost

    if ib_qty == 0:
        # No active IBKR position
        if state_manager.state.position_size != 0:
            # Stale state file — IBKR is flat, clear saved state
            msg = (f"Stale state: bot tracked position={state_manager.state.position_size} "
                   f"but IBKR is flat. Clearing state.")
            logger.warning(msg)
            dashboard.print_event("WARNING", msg)
            state_manager.state.position_size = 0
            state_manager.state.entry_price = 0.0
            state_manager.state.stop_loss = 0.0
            state_manager.state.take_profit = 0.0
            state_manager.state.entry_time = None
            state_manager.save_state()
        else:
            dashboard.print_event("INFO", "No active position at startup - waiting for signals")
        return

    # --- Active position found in IBKR ---
    direction = "LONG" if ib_qty > 0 else "SHORT"
    exit_action = "SELL" if ib_qty > 0 else "BUY"

    # Force-fetch fresh open orders from IBKR.
    # ib.openTrades() returns cached data — reqAllOpenOrdersAsync() ensures
    # bracket child orders (SL/TP) are present before we scan.
    stop_price = 0.0
    limit_price = 0.0
    sl_trade_obj = None
    tp_trade_obj = None

    try:
        await ib.reqAllOpenOrdersAsync()
        await asyncio.sleep(0.5)  # Let event loop process the response
    except Exception as e:
        logger.debug(f"reqAllOpenOrders: {e}")

    open_trades = ib.openTrades()
    logger.info(f"Scanning {len(open_trades)} open orders for bracket SL/TP...")

    for t in open_trades:
        tc = t.contract
        # Robust contract match: use conId if both are set, else fall back to symbol
        if contract.conId and tc.conId:
            match = (tc.conId == contract.conId)
        else:
            match = (tc.symbol == contract.symbol)
        if not match:
            continue

        order = t.order
        logger.info(f"  Order: {order.orderType} {order.action} "
                    f"auxPrice={getattr(order, 'auxPrice', 'N/A')} "
                    f"lmtPrice={getattr(order, 'lmtPrice', 'N/A')} "
                    f"parentId={getattr(order, 'parentId', 'N/A')}")

        if order.orderType in ('STP', 'STP LMT') and order.action == exit_action:
            sp = float(getattr(order, 'auxPrice', 0) or 0)
            if sp > 0:
                stop_price = sp
                sl_trade_obj = t
        elif order.orderType == 'LMT' and order.action == exit_action:
            lp = float(getattr(order, 'lmtPrice', 0) or 0)
            if lp > 0:
                limit_price = lp
                tp_trade_obj = t

    logger.info(f"Bracket orders found: SL={stop_price or 'none'}, TP={limit_price or 'none'}")

    # Determine state source
    state_matches = (
        (state_manager.state.position_size > 0 and ib_qty > 0) or
        (state_manager.state.position_size < 0 and ib_qty < 0)
    )

    if state_matches:
        # Saved state matches IBKR direction — trust saved entry_price
        entry_price = state_manager.state.entry_price or avg_cost
        if stop_price > 0:
            state_manager.state.stop_loss = stop_price
        if limit_price > 0:
            state_manager.state.take_profit = limit_price
        state_manager.save_state()
        source = "saved state"
    else:
        # No matching saved state — reconstruct entirely from IBKR
        state_manager.state.position_size = 1 if ib_qty > 0 else -1
        state_manager.state.entry_price = avg_cost
        state_manager.state.entry_time = pd.Timestamp.now()
        state_manager.state.stop_loss = stop_price
        state_manager.state.take_profit = limit_price
        if ib_qty > 0:
            state_manager.state.traded_in_bull_trend = True
        else:
            state_manager.state.traded_in_bear_trend = True
        state_manager.state.trade_count = max(state_manager.state.trade_count, 1)
        state_manager.save_state()
        entry_price = avg_cost
        source = "IBKR data"

    entry_price = state_manager.state.entry_price
    sl = state_manager.state.stop_loss
    tp = state_manager.state.take_profit

    # --- Fallback: no bracket orders found → calculate from entry price ---
    sl_tp_source = "open orders"
    if (sl == 0.0 or tp == 0.0) and signal_engine:
        is_long = ib_qty > 0
        calc_sl, calc_tp = signal_engine.calculate_exit_levels(
            entry_price=entry_price,
            is_long=is_long
        )
        if sl == 0.0:
            sl = calc_sl
            state_manager.state.stop_loss = sl
            logger.info(f"No SL bracket order — calculated SL={sl:.2f} from entry price")
        if tp == 0.0:
            tp = calc_tp
            state_manager.state.take_profit = tp
            logger.info(f"No TP bracket order — calculated TP={tp:.2f} from entry price")
        state_manager.save_state()
        sl_tp_source = "calculated from entry"
    elif sl == 0.0 or tp == 0.0:
        logger.warning(f"SL/TP not found in open orders and no signal_engine for fallback. "
                       f"SL={sl}, TP={tp} — exits may not trigger correctly!")
        sl_tp_source = "MISSING"

    now = datetime.now(pytz.timezone("US/Eastern"))

    # Rebuild active_bracket so cancel_pending_bracket_orders() works on exit
    sl_ticket = OrderTicket(
        order_id=sl_trade_obj.order.orderId if sl_trade_obj else -1,
        action=exit_action,
        order_type="STP",
        quantity=abs(ib_qty),
        stop_price=sl,
        status="working" if sl_trade_obj else "calculated",
        placed_time=now
    )
    tp_ticket = OrderTicket(
        order_id=tp_trade_obj.order.orderId if tp_trade_obj else -1,
        action=exit_action,
        order_type="LMT",
        quantity=abs(ib_qty),
        limit_price=tp,
        status="working" if tp_trade_obj else "calculated",
        placed_time=now
    )
    entry_ticket = OrderTicket(
        order_id=-1,
        action="BUY" if ib_qty > 0 else "SELL",
        order_type="MKT",
        quantity=abs(ib_qty),
        status="filled",
        placed_time=now
    )
    order_manager.active_bracket = BracketTickets(
        entry=entry_ticket,
        take_profit=tp_ticket,
        stop_loss=sl_ticket
    )

    # Update dashboard to show active trade
    trade_id = max(state_manager.state.trade_count, 1)
    dashboard.on_entry(
        trade_id=trade_id,
        direction=direction,
        entry_price=entry_price,
        quantity=abs(ib_qty),
        stop_loss=sl,
        take_profit=tp
    )

    open_orders_count = sum(1 for x in [sl_trade_obj, tp_trade_obj] if x)
    sl_str = f"{sl:.2f}" if sl > 0 else "N/A"
    tp_str = f"{tp:.2f}" if tp > 0 else "N/A"
    msg = (f"Resumed {direction} ({source}): {abs(ib_qty)} contracts "
           f"@ {entry_price:.2f} | SL={sl_str} | TP={tp_str} "
           f"[{sl_tp_source}] | Open orders: {open_orders_count}")
    logger.info(msg)
    dashboard.print_event("INFO", msg)

    await telegram.send_message(
        f"🔄 <b>ACTIVE POSITION DETECTED ({mode_label})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"↕️ Direction: <b>{direction}</b>\n"
        f"📦 Qty: {abs(ib_qty)} contracts\n"
        f"💰 Entry: <code>{entry_price:.2f}</code>\n"
        f"🔴 SL: <code>{sl_str}</code>\n"
        f"🟢 TP: <code>{tp_str}</code>\n"
        f"📋 SL/TP source: {sl_tp_source}\n"
        f"📋 Open bracket orders tracked: {open_orders_count}\n"
        f"✅ Bot is monitoring this position"
    )


async def run_paper_trading_v2(config: dict, contracts: Optional[int] = None) -> None:
    """
    Run paper trading mode with:
    - Auto-reconnect
    - Market orders
    - Clean dashboard
    - Telegram notifications
    - IB Gateway support for 24/7 operation
    """
    from ib_async import IB, Future
    from data import RealtimeFeed, MultiTimeframeFeed
    from strategy import SignalEngine, StateManager, SignalType
    from execution import OrderManager, PositionTracker
    from utils import ConnectionManager, ConnectionConfig, TradingDashboard
    
    print("\n" + "=" * 60)
    print("             PAPER TRADING MODE v2.0")
    print("=" * 60)
    
    if contracts is None:
        contracts = get_contracts()
    else:
        print(f"\nContracts: {contracts} (from command line)")
    
    # Get configs
    ibkr_cfg = config.get('ibkr', {})
    strategy_cfg = config.get('strategy', {})
    contract_cfg = config.get('mnq_contract', {})
    risk_cfg = config.get('risk', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    adx_cfg = strategy_cfg.get('adx', {})
    from utils.strategy_side_config import (
        resolve_side_configs,
        signal_engine_init_kwargs,
        strategy_info_for_telegram,
    )
    from data.strategy_indicators import live_bar_indicator_slice, bar_flips_for_state_manager

    sides = resolve_side_configs(strategy_cfg)

    if strategy_cfg.get("execution", {}).get("independent_books"):
        print(
            "[!] strategy.yaml has execution.independent_books=true (backtest merges separate "
            "long/short runs). Paper uses one combined book — trade list will not match merged "
            "backtest 1:1.\n"
        )
    
    conn_cfg = ibkr_cfg.get('connection', {})
    recon_cfg = ibkr_cfg.get('reconnection', {})
    
    # Smart port selection: uses IB Gateway for 24/7, TWS otherwise
    port = get_connection_port(ibkr_cfg, mode='paper')
    gateway_type = conn_cfg.get('default_gateway', 'tws').upper()
    
    # Initialize Telegram notifier
    telegram = create_telegram_notifier(config)
    
    # Initialize dashboard
    dashboard = TradingDashboard(symbol="MNQ")
    
    # Create connection config
    connection_config = ConnectionConfig(
        host=conn_cfg.get('host', '127.0.0.1'),
        port=port,
        client_id=resolve_ib_client_id(conn_cfg),
        max_reconnect_attempts=recon_cfg.get('max_attempts', 0),  # 0 = infinite
        initial_delay=recon_cfg.get('initial_delay_sec', 5),
        max_delay=recon_cfg.get('max_delay_sec', 60),
        backoff_multiplier=recon_cfg.get('backoff_multiplier', 2.0)
    )
    
    # Shared state for reconnection
    shared_state = {
        'feed': None,
        'order_manager': None,
        'position_tracker': None,
        'signal_engine': None,
        'state_manager': None,
        'mtf': None,
        'contract': None,
        'running': True
    }
    telegram.set_market_price_provider(
        lambda: resolve_market_price(shared_state, shared_state.get("feed"))
    )
    
    async def on_reconnect():
        """Handle reconnection - resync with broker and verify positions."""
        dashboard.print_event("INFO", f"Reconnected to {gateway_type} - Resyncing...")
        dashboard.update_connection_status(True)

        # Step 1: Resync positions and orders with IBKR
        if shared_state['order_manager']:
            await shared_state['order_manager'].sync_with_broker()
        if shared_state['position_tracker']:
            await shared_state['position_tracker'].initialize()

        # Step 2: Reconcile position state using the same logic as startup
        if shared_state['state_manager'] and shared_state['position_tracker'] and shared_state['order_manager']:
            await startup_reconcile_position(
                ib=ib,
                contract=shared_state['contract'],
                position_tracker=shared_state['position_tracker'],
                state_manager=shared_state['state_manager'],
                order_manager=shared_state['order_manager'],
                dashboard=dashboard,
                telegram=telegram,
                contracts_count=contracts,
                mode_label="PAPER",
                signal_engine=shared_state.get('signal_engine')
            )

        # Step 3: Restart data feed
        if shared_state['feed']:
            await shared_state['feed'].start(initial_lookback_days=5)
            seed_dashboard_prices_from_feed(dashboard, shared_state['feed'])

        dashboard.print_event("INFO", "Resync complete - Trading active")
        await telegram.notify_reconnected(mode="PAPER", gateway_type=gateway_type)

    def on_disconnect():
        """Handle disconnect."""
        dashboard.update_connection_status(False)
        dashboard.print_event("WARNING", f"Disconnected from {gateway_type} - Attempting to reconnect...")
        telegram.schedule(
            telegram.notify_disconnected(
                "Lost connection to IBKR — data feed paused until reconnect",
                mode="PAPER",
                port=port,
                gateway_type=gateway_type,
            )
        )

    async def on_extended_disconnect(attempt_count):
        """Alert user when reconnection has been failing for extended period."""
        msg = (f"ALERT: {attempt_count} failed reconnection attempts (~{attempt_count} min). "
               f"IB Gateway on port {port} may require manual restart.")
        dashboard.print_event("ERROR", msg)
        logger.error(msg)
        await telegram.notify_error(msg)

    # Create connection manager
    conn_manager = ConnectionManager(
        config=connection_config,
        on_reconnect=on_reconnect,
        on_disconnect=on_disconnect,
        on_extended_disconnect=on_extended_disconnect
    )

    stop_notified = False
    try:
        print(
            f"\n[i] IB API client_id={connection_config.client_id} "
            f"(unique per session; override with IB_CLIENT_ID env or config/ibkr.yaml)"
        )
        print(f"\nConnecting to IBKR Paper via {gateway_type} on port {port}...")
        if not await conn_manager.connect():
            print("[X] Failed to connect to IBKR")
            await telegram.notify_error(f"Failed to connect to IBKR {gateway_type} on port {port}")
            return

        dashboard.update_connection_status(True)
        print(f"[OK] Connected to IBKR Paper Account via {gateway_type}")
        notify_telegram_background(
            telegram,
            telegram.notify_connected(gateway_type),
        )
        
        ib = conn_manager.client
        
        # Select active MNQ outright contract. Inside the 10-day roll window this
        # follows the volume rollover rule from config/mnq_contract.yaml.
        startup_progress("Resolving MNQ contract with IBKR...")
        try:
            contract, roll_decision = await asyncio.wait_for(
                select_active_mnq_contract(ib, contract_cfg),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            print("[X] Timed out waiting for MNQ contract details from IBKR (90s)")
            print("    Check TWS is fully logged in and API connections are enabled.")
            await telegram.notify_error("MNQ contract resolution timed out (90s)")
            return
        except Exception as exc:
            print("[X] Error: No MNQ contracts found")
            await telegram.notify_error(f"No MNQ contracts found: {exc}")
            return
        shared_state['contract'] = contract
        shared_state['contract_label'] = contract_label_from_ib(contract)
        print(f"[OK] Trading: {contract.localSymbol}")
        logger.info(
            "CONTRACT_SELECT | mode=PAPER | selected=%s | reason=%s | rolled=%s | dte=%s",
            contract_label_from_ib(contract),
            roll_decision.reason,
            roll_decision.should_roll,
            roll_decision.days_to_expiry,
        )
        contract_label = contract_label_from_ib(contract)

        startup_progress("Subscribing to market data...")
        shared_state["market_data_ticker"] = await ensure_market_data(ib, contract, ibkr_cfg)
        
        # Initialize components
        signal_engine = SignalEngine(
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            **signal_engine_init_kwargs(strategy_cfg),
        )
        shared_state['signal_engine'] = signal_engine
        
        state_manager = StateManager(
            state_file="./data/paper_state.json",
            tick_value=0.50,
            contracts_per_trade=contracts
        )
        shared_state['state_manager'] = state_manager
        
        order_manager = OrderManager(
            ib_client=ib,
            contract=contract,
            default_qty=contracts
        )
        shared_state['order_manager'] = order_manager
        
        position_tracker = PositionTracker(ib_client=ib, contract=contract)
        await position_tracker.initialize()
        shared_state['position_tracker'] = position_tracker

        # --- STARTUP POSITION CHECK ---
        # Check if IBKR already has an active position (e.g., manual order, crash recovery)
        # Reconstruct bot state and track open SL/TP orders before the feed starts
        await startup_reconcile_position(
            ib=ib,
            contract=contract,
            position_tracker=position_tracker,
            state_manager=state_manager,
            order_manager=order_manager,
            dashboard=dashboard,
            telegram=telegram,
            contracts_count=contracts,
            mode_label="PAPER",
            signal_engine=signal_engine
        )

        # Primary timeframe from config
        primary_bar_size = get_primary_bar_size(strategy_cfg)
        primary_bars_per_hour = get_primary_bars_per_hour(strategy_cfg)

        # Create feeds
        primary_feed = RealtimeFeed(
            ib_client=ib,
            contract=contract,
            bar_size=primary_bar_size
        )
        primary_feed.set_ema_warmup_context(
            contract_cfg,
            ema_length=ema_cfg.get('length', 200),
        )
        shared_state['feed'] = primary_feed
        mtf = MultiTimeframeFeed(
            primary_feed,
            ema_length=ema_cfg.get('length', 200),
            bars_per_hour=primary_bars_per_hour
        )
        shared_state['mtf'] = mtf

        fast_tick_cfg = fast_tick_bar_close_config(strategy_cfg)
        tick_bar_builder = None
        tick_bar_task = None
        if fast_tick_cfg["enabled"]:
            from data.tick_bar_builder import TickBarBuilder

            tick_bar_builder = TickBarBuilder(bar_size=primary_bar_size)
            primary_feed.reconcile_synthetic_official = fast_tick_cfg["reconcile_official"]
            shared_state["tick_bar_update_handler"] = attach_tick_bar_builder(
                shared_state.get("market_data_ticker"),
                tick_bar_builder,
                shared_state=shared_state,
            )
            shared_state["tick_bar_builder"] = tick_bar_builder

        # Dashboard update task
        async def update_dashboard_loop():
            """Update dashboard periodically."""
            while shared_state['running']:
                try:
                    # Update account info
                    account_summary = position_tracker.get_account_summary()
                    dashboard.update_account(
                        account_value=account_summary.get('net_liquidation', 0),
                        buying_power=account_summary.get('buying_power', 0),
                        daily_pnl=account_summary.get('daily_pnl', 0)
                    )
                    ib_connected = ib.isConnected()
                    dashboard.update_connection_status(ib_connected)
                    await run_dashboard_maintenance(
                        ib=ib,
                        mode_label="PAPER",
                        contract_label=shared_state.get("contract_label", contract_label),
                        dashboard=dashboard,
                        feed=shared_state.get("feed", primary_feed),
                        mtf=shared_state.get("mtf", mtf),
                        telegram=telegram,
                        state_manager=state_manager,
                        strategy_cfg=strategy_cfg,
                        sides=sides,
                        ema_cfg=ema_cfg,
                        primary_bar_size=primary_bar_size,
                        market_ticker=shared_state.get("market_data_ticker"),
                        shared_state=shared_state,
                        contract_cfg=contract_cfg,
                        ibkr_cfg=ibkr_cfg,
                        contracts_count=contracts,
                    )
                    log_price_poll_snapshot(
                        "PAPER",
                        shared_state.get("contract_label", contract_label),
                        dashboard,
                        feed=shared_state.get("feed", primary_feed),
                        market_ticker=shared_state.get("market_data_ticker"),
                        ib_connected=ib_connected,
                    )
                    
                    # Print dashboard
                    dashboard.print_dashboard(clear=True)
                    
                    await asyncio.sleep(5)  # Update every 5 seconds
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("Dashboard update error: %s", e)
                    await asyncio.sleep(5)
        
        # Bar close handler
        # Intrabar update handler for live price tracking
        def on_bar_update(bar):
            """Intrabar update from keepUpToDate bar stream (IB reqMktData handled in maintenance)."""
            pass

        async def on_bar_close(df, bar):
            try:
                if df is None or len(df) < 60:
                    return
                active_contract_label = shared_state.get("contract_label", contract_label)

                from data.live_bar_close import prepare_bar_close_row

                current_bar, prep_timings = prepare_bar_close_row(
                    df=df,
                    mtf=mtf,
                    sides=sides,
                    strategy_cfg=strategy_cfg,
                    ema_cfg=ema_cfg,
                    mode_label="PAPER",
                )
                logger.info(
                    "BAR_CLOSE_TIMING | mode=PAPER | contract=%s | bar_end=%s | "
                    "indicators_ms=%.0f | mtf_ms=%.0f | row_ms=%.0f | prepare_ms=%.0f",
                    active_contract_label,
                    current_bar.name,
                    prep_timings["indicators_ms"],
                    prep_timings["mtf_ms"],
                    prep_timings["row_ms"],
                    prep_timings["prepare_ms"],
                )

                # Update dashboard indicators
                st_dir = "BULL" if current_bar.get('direction_long', current_bar.get('direction', 0)) == -1 else "BEAR"
                ema_status = "BULL" if current_bar['ema_bull'] else ("BEAR" if current_bar['ema_bear'] else "NEUTRAL")
                adx_val = current_bar.get('adx', 0)

                dashboard.update_indicators(
                    st_dir,
                    ema_status,
                    adx_val,
                    ema_1h=_bar_value(current_bar, "ema_1h"),
                    close_1h=_bar_value(current_bar, "close_1h"),
                    signal_bar_time=current_bar.name,
                )
                log_bar_close_snapshot("PAPER", active_contract_label, current_bar)

                # Log events (these show in dashboard events area)
                events = ""
                if current_bar.get('st_bull_flip', False):
                    events = "SuperTrend flipped BULLISH"
                    dashboard.print_event("SIGNAL", events)
                elif current_bar.get('st_bear_flip', False):
                    events = "SuperTrend flipped BEARISH"
                    dashboard.print_event("SIGNAL", events)
                if current_bar.get('st_bull_flip', False) or current_bar.get('st_bear_flip', False):
                    # Telegram is non-critical; keep network I/O off the signal/order path.
                    notify_telegram_background(
                        telegram,
                        handle_supertrend_flip_telegram(
                            telegram,
                            current_bar,
                            mode_label="PAPER",
                            contract_label=active_contract_label,
                        ),
                    )
                if current_bar.get('ema_bull_cross', False):
                    events = "EMA Cross BULLISH"
                    dashboard.print_event("SIGNAL", events)
                elif current_bar.get('ema_bear_cross', False):
                    events = "EMA Cross BEARISH"
                    dashboard.print_event("SIGNAL", events)

                bf, br, st_direction = bar_flips_for_state_manager(current_bar)
                state_manager.update_supertrend_state(
                    st_bull_flip=bf,
                    st_bear_flip=br,
                    current_direction=st_direction
                )

                state = state_manager.state

                # Check exits (match backtest: respect entry_time on same bar)
                if state.position_size != 0:
                    log_position_hold("PAPER", active_contract_label, current_bar, state)
                    exit_signal = signal_engine.check_exit_conditions(
                        bar=current_bar,
                        position_size=state.position_size,
                        entry_price=state.entry_price,
                        stop_loss=state.stop_loss,
                        take_profit=state.take_profit,
                        entry_time=state.entry_time,
                    )

                    if exit_signal:
                        action = "SELL" if state.position_size > 0 else "BUY"
                        direction = "LONG" if state.position_size > 0 else "SHORT"

                        dashboard.print_event("EXIT", f"{exit_signal.exit_type.value.upper()} triggered - Closing position")

                        # Close with MARKET order (bot manages SL/TP, not IBKR)
                        await order_manager.place_market_order(
                            action=action,
                            quantity=abs(state.position_size) * contracts
                        )

                        # Calculate P&L
                        if state.position_size > 0:
                            pnl_points = exit_signal.exit_price - state.entry_price
                        else:
                            pnl_points = state.entry_price - exit_signal.exit_price
                        pnl_dollars = pnl_points * 2 * contracts

                        logger.info(
                            "ORDER_EXIT | mode=PAPER | contract=%s | direction=%s | exit_type=%s | "
                            "exit_price=%.4f | entry_price=%.4f | pnl_pts=%.2f | pnl_usd=%.2f | contracts=%s",
                            active_contract_label,
                            direction,
                            exit_signal.exit_type.value,
                            exit_signal.exit_price,
                            state.entry_price,
                            pnl_points,
                            pnl_dollars,
                            contracts,
                        )

                        # Update dashboard
                        dashboard.on_exit(
                            exit_price=exit_signal.exit_price,
                            exit_type=exit_signal.exit_type.value,
                            pnl_dollars=pnl_dollars
                        )

                        notify_telegram_background(
                            telegram,
                            telegram.notify_trade_closed(
                                direction=direction,
                                entry_price=state.entry_price,
                                exit_price=exit_signal.exit_price,
                                exit_reason=exit_signal.exit_type.value,
                                pnl_points=pnl_points,
                                pnl_dollars=pnl_dollars,
                                contracts=contracts,
                                trade_id=state.trade_count,
                                entry_time=state.entry_time,
                            ),
                        )

                        state_manager.on_exit(exit_signal)

                # Check entries (allow_volume_defer=False matches BacktestEngine)
                if state.position_size == 0:
                    vol_win = (
                        SignalEngine.single_row_volume_window(current_bar)
                        if signal_engine.volume_check
                        else None
                    )
                    entry_signal, entry_updates = signal_engine.evaluate_entry_conditions(
                        bar=current_bar,
                        position_size=0,
                        traded_in_bull_trend=state.traded_in_bull_trend,
                        traded_in_bear_trend=state.traded_in_bear_trend,
                        pending_long_ema_wait=state.pending_long_ema_wait,
                        pending_short_ema_wait=state.pending_short_ema_wait,
                        pending_adx_long=state.pending_adx_long,
                        pending_adx_short=state.pending_adx_short,
                        adx_wait_bars_left_long=state.adx_wait_bars_left_long,
                        adx_wait_bars_left_short=state.adx_wait_bars_left_short,
                        adx_wait_trigger_long=state.adx_wait_trigger_long,
                        adx_wait_trigger_short=state.adx_wait_trigger_short,
                        volume_window=vol_win,
                        allow_volume_defer=False,
                        pending_volume_long=state.pending_volume_long,
                        pending_volume_short=state.pending_volume_short,
                        volume_wait_bars_left_long=state.volume_wait_bars_left_long,
                        volume_wait_bars_left_short=state.volume_wait_bars_left_short,
                        volume_wait_trigger_long=state.volume_wait_trigger_long,
                        volume_wait_trigger_short=state.volume_wait_trigger_short,
                        volume_wait_kind_long=state.volume_wait_kind_long,
                        volume_wait_kind_short=state.volume_wait_kind_short,
                    )
                    if entry_updates.get("set_pending_long_ema_wait"):
                        state_manager.set_pending_long_ema_wait()
                    if entry_updates.get("clear_pending_long_ema_wait"):
                        state_manager.clear_pending_long_ema_wait()
                    if entry_updates.get("set_pending_short_ema_wait"):
                        state_manager.set_pending_short_ema_wait()
                    if entry_updates.get("clear_pending_short_ema_wait"):
                        state_manager.clear_pending_short_ema_wait()
                    # ADX wait updates
                    if entry_updates.get("set_adx_wait_long"):
                        data = entry_updates["set_adx_wait_long"]
                        state_manager.set_adx_wait_long(data["bars"], data["trigger"])
                    if entry_updates.get("clear_adx_wait_long"):
                        state_manager.clear_adx_wait_long()
                    if entry_updates.get("decrement_adx_wait_long"):
                        state_manager.decrement_adx_wait_long()
                    if entry_updates.get("set_adx_wait_short"):
                        data = entry_updates["set_adx_wait_short"]
                        state_manager.set_adx_wait_short(data["bars"], data["trigger"])
                    if entry_updates.get("clear_adx_wait_short"):
                        state_manager.clear_adx_wait_short()
                    if entry_updates.get("decrement_adx_wait_short"):
                        state_manager.decrement_adx_wait_short()
                    if entry_updates.get("set_volume_wait_long"):
                        d = entry_updates["set_volume_wait_long"]
                        state_manager.set_volume_wait_long(d["remaining"], d["trigger"], d["kind"])
                    if entry_updates.get("set_volume_wait_short"):
                        d = entry_updates["set_volume_wait_short"]
                        state_manager.set_volume_wait_short(d["remaining"], d["trigger"], d["kind"])
                    if entry_updates.get("clear_pending_volume_long"):
                        state_manager.clear_volume_wait_long()
                    if entry_updates.get("clear_pending_volume_short"):
                        state_manager.clear_volume_wait_short()
                    if entry_updates.get("decrement_volume_wait_long"):
                        state_manager.decrement_volume_wait_long()
                    if entry_updates.get("decrement_volume_wait_short"):
                        state_manager.decrement_volume_wait_short()
                    log_entry_decision(
                        "PAPER", active_contract_label, current_bar, state,
                        signal_engine, entry_signal, entry_updates,
                    )
                    if entry_signal:
                        is_long = entry_signal.signal_type == SignalType.BUY
                        direction = "LONG" if is_long else "SHORT"

                        dashboard.print_event("ENTRY", f"{direction} @ {entry_signal.price:.2f}")

                        stop_loss, take_profit = signal_engine.calculate_exit_levels(
                            entry_price=entry_signal.price,
                            is_long=is_long
                        )

                        # Place MARKET entry order only (no bracket)
                        # Bot will monitor SL/TP and exit with market order when hit
                        entry_trade = await order_manager.place_market_order(
                            action="BUY" if is_long else "SELL",
                            quantity=contracts
                        )
                        actual_fill_price = await order_manager.wait_for_fill_price(entry_trade)

                        trade_id = state_manager.state.trade_count + 1

                        # Update dashboard
                        dashboard.on_entry(
                            trade_id=trade_id,
                            direction=direction,
                            entry_price=entry_signal.price,
                            quantity=contracts,
                            stop_loss=stop_loss,
                            take_profit=take_profit
                        )

                        state_manager.on_entry(entry_signal, stop_loss, take_profit)

                        logger.info(
                            "ORDER_ENTRY | mode=PAPER | contract=%s | direction=%s | entry_price=%.4f | "
                            "actual_fill_price=%s | qty=%s | SL=%.4f | TP=%.4f | trigger=%s | trade_id=%s",
                            active_contract_label,
                            direction,
                            entry_signal.price,
                            f"{actual_fill_price:.4f}" if actual_fill_price is not None else "n/a",
                            contracts,
                            stop_loss,
                            take_profit,
                            entry_signal.trigger,
                            trade_id,
                        )

                        notify_telegram_background(
                            telegram,
                            telegram.notify_trade_placed(
                                direction=direction,
                                entry_price=entry_signal.price,
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                                contracts=contracts,
                                trigger=entry_signal.trigger,
                                trade_id=trade_id,
                                actual_fill_price=actual_fill_price,
                            ),
                        )
            except Exception as e:
                logger.exception("Paper on_bar_close failed: %s", e)

        primary_feed.on_bar_close(lambda df, bar: asyncio.create_task(on_bar_close(df, bar)))
        primary_feed.on_bar_update(on_bar_update)
        
        startup_progress(
            "Loading historical bars + EMA warmup from IBKR (often 30-90 seconds)..."
        )
        await primary_feed.start(initial_lookback_days=15)
        seed_dashboard_prices_from_feed(dashboard, primary_feed)
        _df0 = primary_feed.get_dataframe()
        logger.info(
            "Realtime feed ready: %s buffered bars; dashboard seeded from last OHLC row",
            len(_df0) if _df0 is not None else 0,
        )
        
        if tick_bar_builder is not None:
            tick_bar_task = asyncio.create_task(
                run_fast_tick_bar_close_loop(
                    mode_label="PAPER",
                    contract_label=shared_state.get("contract_label", contract_label),
                    feed=primary_feed,
                    builder=tick_bar_builder,
                    shared_state=shared_state,
                    grace_sec=fast_tick_cfg["grace_sec"],
                )
            )

        # Start dashboard update task
        dashboard_task = asyncio.create_task(update_dashboard_loop())
        
        # Send bot started notification via Telegram
        await telegram.notify_bot_started(
            mode=f"PAPER ({gateway_type})",
            symbol="MNQ",
            contracts=contracts,
            strategy_info=strategy_info_for_telegram(
                strategy_cfg, ema_cfg.get('length', 200)
            ),
        )
        
        print("\n[OK] Paper trading is ACTIVE")
        print(f"[OK] Connected via {gateway_type} (Port {port})")
        print("[OK] Dashboard will refresh every 5 seconds")
        print("[OK] Telegram notifications enabled" if telegram.enabled else "[--] Telegram notifications disabled")
        print("[OK] Press Ctrl+C to stop\n")
        
        await asyncio.sleep(3)  # Give user time to read

        stop_reason = "Unknown"
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            stop_reason = "User requested shutdown (Ctrl+C)"

        shared_state['running'] = False
        try:
            state_manager.save_state()
            logger.info(
                "STATE_SAVED_ON_SHUTDOWN | mode=PAPER | pendEMA_L=%s | pendEMA_S=%s | "
                "tradedBull=%s | tradedBear=%s | position=%s",
                state_manager.state.pending_long_ema_wait,
                state_manager.state.pending_short_ema_wait,
                state_manager.state.traded_in_bull_trend,
                state_manager.state.traded_in_bear_trend,
                state_manager.state.position_size,
            )
        except Exception as save_exc:
            logger.error("Failed to save paper state on shutdown: %s", save_exc)
        if tick_bar_task is not None:
            tick_bar_task.cancel()
        dashboard_task.cancel()

        await telegram.notify_bot_stopped(stop_reason)
        stop_notified = True

        await primary_feed.stop()
        await position_tracker.shutdown()
        await conn_manager.disconnect()
        print("[OK] Paper trading stopped")

    except Exception as e:
        logger.error(f"Paper trading error: {e}")
        import traceback
        traceback.print_exc()
        await telegram.notify_error(f"Paper trading crashed: {str(e)}")
        if not stop_notified:
            await telegram.notify_bot_stopped(f"Crashed: {e}")
        await conn_manager.disconnect()
        raise
    finally:
        await telegram.shutdown()


async def run_live_trading_v2(config: dict, contracts: Optional[int] = None) -> None:
    """
    Run live trading mode with:
    - Auto-reconnect
    - Market orders  
    - Clean dashboard
    - Telegram notifications
    - IB Gateway support for 24/7 operation
    """
    from ib_async import IB, Future
    from data import RealtimeFeed, MultiTimeframeFeed
    from strategy import SignalEngine, StateManager, SignalType
    from execution import OrderManager, PositionTracker
    from utils import ConnectionManager, ConnectionConfig, TradingDashboard
    
    print("\n" + "=" * 60)
    print("          [!]  LIVE TRADING MODE - REAL MONEY  [!]")
    print("=" * 60)
    
    print("\n[!]  WARNING: This will trade with REAL MONEY!")
    print("[!]  Make sure you understand the risks involved.\n")
    
    confirm = input("Type 'CONFIRM LIVE TRADING' to proceed: ").strip()
    
    if confirm != 'CONFIRM LIVE TRADING':
        print("Live trading cancelled.")
        return
    
    print("\n[!]  Live trading starting in 5 seconds...")
    print("    Press Ctrl+C to abort\n")
    
    await asyncio.sleep(5)
    
    if contracts is None:
        contracts = get_contracts()
    else:
        print(f"\nContracts: {contracts} (from command line)")
    
    # Get configs
    ibkr_cfg = config.get('ibkr', {})
    strategy_cfg = config.get('strategy', {})
    contract_cfg = config.get('mnq_contract', {})
    risk_cfg = config.get('risk', {})
    
    supertrend_cfg = strategy_cfg.get('supertrend', {})
    ema_cfg = strategy_cfg.get('ema', {})
    risk_params = strategy_cfg.get('risk', {})
    adx_cfg = strategy_cfg.get('adx', {})
    from utils.strategy_side_config import (
        resolve_side_configs,
        signal_engine_init_kwargs,
        strategy_info_for_telegram,
    )
    from data.strategy_indicators import live_bar_indicator_slice, bar_flips_for_state_manager

    sides = resolve_side_configs(strategy_cfg)

    if strategy_cfg.get("execution", {}).get("independent_books"):
        print(
            "[!] strategy.yaml has execution.independent_books=true (backtest merges separate "
            "long/short runs). Live uses one combined book — trade list will not match merged "
            "backtest 1:1.\n"
        )
    
    conn_cfg = ibkr_cfg.get('connection', {})
    recon_cfg = ibkr_cfg.get('reconnection', {})
    
    # Smart port selection: uses IB Gateway for 24/7, TWS otherwise
    port = get_connection_port(ibkr_cfg, mode='live')
    gateway_type = conn_cfg.get('default_gateway', 'tws').upper()
    
    # Initialize Telegram notifier
    telegram = create_telegram_notifier(config)
    
    # Initialize dashboard
    dashboard = TradingDashboard(symbol="MNQ")
    
    # Create connection config
    connection_config = ConnectionConfig(
        host=conn_cfg.get('host', '127.0.0.1'),
        port=port,
        client_id=resolve_ib_client_id(conn_cfg),
        max_reconnect_attempts=recon_cfg.get('max_attempts', 0),
        initial_delay=recon_cfg.get('initial_delay_sec', 5),
        max_delay=recon_cfg.get('max_delay_sec', 60),
        backoff_multiplier=recon_cfg.get('backoff_multiplier', 2.0)
    )
    
    # Shared state for reconnection
    shared_state = {
        'feed': None,
        'order_manager': None,
        'position_tracker': None,
        'signal_engine': None,
        'state_manager': None,
        'mtf': None,
        'contract': None,
        'running': True
    }
    telegram.set_market_price_provider(
        lambda: resolve_market_price(shared_state, shared_state.get("feed"))
    )
    
    async def on_reconnect():
        """Handle reconnection - resync with broker and verify positions."""
        dashboard.print_event("INFO", f"Reconnected to {gateway_type} - Resyncing...")
        dashboard.update_connection_status(True)

        # Step 1: Resync positions and orders with IBKR
        if shared_state['order_manager']:
            await shared_state['order_manager'].sync_with_broker()
        if shared_state['position_tracker']:
            await shared_state['position_tracker'].initialize()

        # Step 2: Reconcile position state using the same logic as startup
        if shared_state['state_manager'] and shared_state['position_tracker'] and shared_state['order_manager']:
            await startup_reconcile_position(
                ib=ib,
                contract=shared_state['contract'],
                position_tracker=shared_state['position_tracker'],
                state_manager=shared_state['state_manager'],
                order_manager=shared_state['order_manager'],
                dashboard=dashboard,
                telegram=telegram,
                contracts_count=contracts,
                mode_label="LIVE",
                signal_engine=shared_state.get('signal_engine')
            )

        # Step 3: Restart data feed
        if shared_state['feed']:
            await shared_state['feed'].start(initial_lookback_days=5)
            seed_dashboard_prices_from_feed(dashboard, shared_state['feed'])

        dashboard.print_event("INFO", "Resync complete - Trading active")
        await telegram.notify_reconnected(mode="LIVE", gateway_type=gateway_type)

    def on_disconnect():
        """Handle disconnect."""
        dashboard.update_connection_status(False)
        dashboard.print_event("WARNING", f"Disconnected from {gateway_type} - Reconnecting...")
        telegram.schedule(
            telegram.notify_disconnected(
                "Lost connection to IBKR — LIVE trading paused until reconnect",
                mode="LIVE",
                port=port,
                gateway_type=gateway_type,
            )
        )

    async def on_extended_disconnect(attempt_count):
        """Alert user when reconnection has been failing for extended period."""
        msg = (f"ALERT: {attempt_count} failed reconnection attempts (~{attempt_count} min). "
               f"IB Gateway on port {port} may require manual restart. LIVE TRADING HALTED.")
        dashboard.print_event("ERROR", msg)
        logger.error(msg)
        await telegram.notify_error(msg)

    conn_manager = ConnectionManager(
        config=connection_config,
        on_reconnect=on_reconnect,
        on_disconnect=on_disconnect,
        on_extended_disconnect=on_extended_disconnect
    )

    stop_notified = False
    try:
        print(
            f"\n[i] IB API client_id={connection_config.client_id} "
            f"(unique per session; override with IB_CLIENT_ID env or config/ibkr.yaml)"
        )
        print(f"\nConnecting to IBKR LIVE via {gateway_type} on port {port}...")
        if not await conn_manager.connect():
            print("[X] Failed to connect to IBKR")
            await telegram.notify_error(f"Failed to connect to IBKR LIVE {gateway_type} on port {port}")
            return
        
        dashboard.update_connection_status(True)
        print(f"[OK] Connected to IBKR LIVE Account via {gateway_type}")
        print("[!] REAL MONEY MODE - Orders will execute on live account!")
        notify_telegram_background(
            telegram,
            telegram.notify_connected(f"{gateway_type} (LIVE)"),
        )
        
        ib = conn_manager.client
        
        # Select active MNQ outright contract. Inside the 10-day roll window this
        # follows the volume rollover rule from config/mnq_contract.yaml.
        try:
            contract, roll_decision = await select_active_mnq_contract(ib, contract_cfg)
        except Exception as exc:
            print("[X] Error: No MNQ contracts found")
            await telegram.notify_error(f"No MNQ contracts found: {exc}")
            return
        shared_state['contract'] = contract
        shared_state['contract_label'] = contract_label_from_ib(contract)
        print(f"[OK] Trading: {contract.localSymbol}")
        logger.info(
            "CONTRACT_SELECT | mode=LIVE | selected=%s | reason=%s | rolled=%s | dte=%s",
            contract_label_from_ib(contract),
            roll_decision.reason,
            roll_decision.should_roll,
            roll_decision.days_to_expiry,
        )
        contract_label = contract_label_from_ib(contract)

        shared_state["market_data_ticker"] = await ensure_market_data(ib, contract, ibkr_cfg)
        
        # Initialize components (same as paper trading)
        signal_engine = SignalEngine(
            volume_check=strategy_cfg.get('volume_check', False),
            volume_candle_lookahead=strategy_cfg.get('volume_candle_lookahead', 1),
            **signal_engine_init_kwargs(strategy_cfg),
        )
        shared_state['signal_engine'] = signal_engine
        
        state_manager = StateManager(
            state_file="./data/live_state.json",
            tick_value=0.50,
            contracts_per_trade=contracts
        )
        shared_state['state_manager'] = state_manager
        
        order_manager = OrderManager(
            ib_client=ib,
            contract=contract,
            default_qty=contracts
        )
        shared_state['order_manager'] = order_manager
        
        position_tracker = PositionTracker(ib_client=ib, contract=contract)
        await position_tracker.initialize()
        shared_state['position_tracker'] = position_tracker

        # --- STARTUP POSITION CHECK ---
        # Check if IBKR already has an active position (e.g., manual order, crash recovery)
        # Reconstruct bot state and track open SL/TP orders before the feed starts
        await startup_reconcile_position(
            ib=ib,
            contract=contract,
            position_tracker=position_tracker,
            state_manager=state_manager,
            order_manager=order_manager,
            dashboard=dashboard,
            telegram=telegram,
            contracts_count=contracts,
            mode_label="LIVE",
            signal_engine=signal_engine
        )

        # Primary timeframe from config
        primary_bar_size = get_primary_bar_size(strategy_cfg)
        primary_bars_per_hour = get_primary_bars_per_hour(strategy_cfg)

        primary_feed = RealtimeFeed(
            ib_client=ib,
            contract=contract,
            bar_size=primary_bar_size
        )
        primary_feed.set_ema_warmup_context(
            contract_cfg,
            ema_length=ema_cfg.get('length', 200),
        )
        shared_state['feed'] = primary_feed
        mtf = MultiTimeframeFeed(
            primary_feed,
            ema_length=ema_cfg.get('length', 200),
            bars_per_hour=primary_bars_per_hour
        )
        shared_state['mtf'] = mtf

        fast_tick_cfg = fast_tick_bar_close_config(strategy_cfg)
        tick_bar_builder = None
        tick_bar_task = None
        if fast_tick_cfg["enabled"]:
            from data.tick_bar_builder import TickBarBuilder

            tick_bar_builder = TickBarBuilder(bar_size=primary_bar_size)
            primary_feed.reconcile_synthetic_official = fast_tick_cfg["reconcile_official"]
            shared_state["tick_bar_update_handler"] = attach_tick_bar_builder(
                shared_state.get("market_data_ticker"),
                tick_bar_builder,
                shared_state=shared_state,
            )
            shared_state["tick_bar_builder"] = tick_bar_builder

        # Dashboard update task
        async def update_dashboard_loop():
            """Update dashboard periodically."""
            while shared_state['running']:
                try:
                    account_summary = position_tracker.get_account_summary()
                    dashboard.update_account(
                        account_value=account_summary.get('net_liquidation', 0),
                        buying_power=account_summary.get('buying_power', 0),
                        daily_pnl=account_summary.get('daily_pnl', 0)
                    )
                    ib_connected = ib.isConnected()
                    dashboard.update_connection_status(ib_connected)
                    await run_dashboard_maintenance(
                        ib=ib,
                        mode_label="LIVE",
                        contract_label=shared_state.get("contract_label", contract_label),
                        dashboard=dashboard,
                        feed=shared_state.get("feed", primary_feed),
                        mtf=shared_state.get("mtf", mtf),
                        telegram=telegram,
                        state_manager=state_manager,
                        strategy_cfg=strategy_cfg,
                        sides=sides,
                        ema_cfg=ema_cfg,
                        primary_bar_size=primary_bar_size,
                        market_ticker=shared_state.get("market_data_ticker"),
                        shared_state=shared_state,
                        contract_cfg=contract_cfg,
                        ibkr_cfg=ibkr_cfg,
                        contracts_count=contracts,
                    )
                    log_price_poll_snapshot(
                        "LIVE",
                        shared_state.get("contract_label", contract_label),
                        dashboard,
                        feed=shared_state.get("feed", primary_feed),
                        market_ticker=shared_state.get("market_data_ticker"),
                        ib_connected=ib_connected,
                    )
                    dashboard.print_dashboard(clear=True)
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("Dashboard update error: %s", e)
                    await asyncio.sleep(5)
        
        # Intrabar update handler for live price tracking
        def on_bar_update(bar):
            """Intrabar update from keepUpToDate bar stream (IB reqMktData handled in maintenance)."""
            pass
        
        # Bar close handler (same bar/predicate alignment as paper + backtest)
        async def on_bar_close(df, bar):
            try:
                if df is None or len(df) < 60:
                    return
                active_contract_label = shared_state.get("contract_label", contract_label)

                from data.live_bar_close import prepare_bar_close_row

                current_bar, prep_timings = prepare_bar_close_row(
                    df=df,
                    mtf=mtf,
                    sides=sides,
                    strategy_cfg=strategy_cfg,
                    ema_cfg=ema_cfg,
                    mode_label="LIVE",
                )
                logger.info(
                    "BAR_CLOSE_TIMING | mode=LIVE | contract=%s | bar_end=%s | "
                    "indicators_ms=%.0f | mtf_ms=%.0f | row_ms=%.0f | prepare_ms=%.0f",
                    active_contract_label,
                    current_bar.name,
                    prep_timings["indicators_ms"],
                    prep_timings["mtf_ms"],
                    prep_timings["row_ms"],
                    prep_timings["prepare_ms"],
                )

                st_dir = "BULL" if current_bar.get('direction_long', current_bar.get('direction', 0)) == -1 else "BEAR"
                ema_status = "BULL" if current_bar['ema_bull'] else ("BEAR" if current_bar['ema_bear'] else "NEUTRAL")
                adx_val = current_bar.get('adx', 0)

                dashboard.update_indicators(
                    st_dir,
                    ema_status,
                    adx_val,
                    ema_1h=_bar_value(current_bar, "ema_1h"),
                    close_1h=_bar_value(current_bar, "close_1h"),
                    signal_bar_time=current_bar.name,
                )
                log_bar_close_snapshot("LIVE", active_contract_label, current_bar)

                if current_bar.get('st_bull_flip', False):
                    dashboard.print_event("SIGNAL", "SuperTrend flipped BULLISH")
                elif current_bar.get('st_bear_flip', False):
                    dashboard.print_event("SIGNAL", "SuperTrend flipped BEARISH")
                if current_bar.get('st_bull_flip', False) or current_bar.get('st_bear_flip', False):
                    # Telegram is non-critical; keep network I/O off the signal/order path.
                    notify_telegram_background(
                        telegram,
                        handle_supertrend_flip_telegram(
                            telegram,
                            current_bar,
                            mode_label="LIVE",
                            contract_label=active_contract_label,
                        ),
                    )
                if current_bar.get('ema_bull_cross', False):
                    dashboard.print_event("SIGNAL", "EMA Cross BULLISH")
                elif current_bar.get('ema_bear_cross', False):
                    dashboard.print_event("SIGNAL", "EMA Cross BEARISH")

                bf, br, st_direction = bar_flips_for_state_manager(current_bar)
                state_manager.update_supertrend_state(
                    st_bull_flip=bf,
                    st_bear_flip=br,
                    current_direction=st_direction
                )

                state = state_manager.state

                if state.position_size != 0:
                    log_position_hold("LIVE", active_contract_label, current_bar, state)
                    exit_signal = signal_engine.check_exit_conditions(
                        bar=current_bar,
                        position_size=state.position_size,
                        entry_price=state.entry_price,
                        stop_loss=state.stop_loss,
                        take_profit=state.take_profit,
                        entry_time=state.entry_time,
                    )

                    if exit_signal:
                        action = "SELL" if state.position_size > 0 else "BUY"
                        direction = "LONG" if state.position_size > 0 else "SHORT"

                        dashboard.print_event("EXIT", f"LIVE {exit_signal.exit_type.value.upper()} - Closing position")

                        await order_manager.place_market_order(
                            action=action,
                            quantity=abs(state.position_size) * contracts
                        )

                        if state.position_size > 0:
                            pnl_points = exit_signal.exit_price - state.entry_price
                        else:
                            pnl_points = state.entry_price - exit_signal.exit_price
                        pnl_dollars = pnl_points * 2 * contracts

                        logger.info(
                            "ORDER_EXIT | mode=LIVE | contract=%s | direction=%s | exit_type=%s | "
                            "exit_price=%.4f | entry_price=%.4f | pnl_pts=%.2f | pnl_usd=%.2f | contracts=%s",
                            active_contract_label,
                            direction,
                            exit_signal.exit_type.value,
                            exit_signal.exit_price,
                            state.entry_price,
                            pnl_points,
                            pnl_dollars,
                            contracts,
                        )

                        dashboard.on_exit(
                            exit_price=exit_signal.exit_price,
                            exit_type=exit_signal.exit_type.value,
                            pnl_dollars=pnl_dollars
                        )

                        notify_telegram_background(
                            telegram,
                            telegram.notify_trade_closed(
                                direction=direction,
                                entry_price=state.entry_price,
                                exit_price=exit_signal.exit_price,
                                exit_reason=exit_signal.exit_type.value,
                                pnl_points=pnl_points,
                                pnl_dollars=pnl_dollars,
                                contracts=contracts,
                                trade_id=state.trade_count,
                                entry_time=state.entry_time,
                            ),
                        )

                        state_manager.on_exit(exit_signal)

                if state.position_size == 0:
                    vol_win = (
                        SignalEngine.single_row_volume_window(current_bar)
                        if signal_engine.volume_check
                        else None
                    )
                    entry_signal, entry_updates = signal_engine.evaluate_entry_conditions(
                        bar=current_bar,
                        position_size=0,
                        traded_in_bull_trend=state.traded_in_bull_trend,
                        traded_in_bear_trend=state.traded_in_bear_trend,
                        pending_long_ema_wait=state.pending_long_ema_wait,
                        pending_short_ema_wait=state.pending_short_ema_wait,
                        pending_adx_long=state.pending_adx_long,
                        pending_adx_short=state.pending_adx_short,
                        adx_wait_bars_left_long=state.adx_wait_bars_left_long,
                        adx_wait_bars_left_short=state.adx_wait_bars_left_short,
                        adx_wait_trigger_long=state.adx_wait_trigger_long,
                        adx_wait_trigger_short=state.adx_wait_trigger_short,
                        volume_window=vol_win,
                        allow_volume_defer=False,
                        pending_volume_long=state.pending_volume_long,
                        pending_volume_short=state.pending_volume_short,
                        volume_wait_bars_left_long=state.volume_wait_bars_left_long,
                        volume_wait_bars_left_short=state.volume_wait_bars_left_short,
                        volume_wait_trigger_long=state.volume_wait_trigger_long,
                        volume_wait_trigger_short=state.volume_wait_trigger_short,
                        volume_wait_kind_long=state.volume_wait_kind_long,
                        volume_wait_kind_short=state.volume_wait_kind_short,
                    )
                    if entry_updates.get("set_pending_long_ema_wait"):
                        state_manager.set_pending_long_ema_wait()
                    if entry_updates.get("clear_pending_long_ema_wait"):
                        state_manager.clear_pending_long_ema_wait()
                    if entry_updates.get("set_pending_short_ema_wait"):
                        state_manager.set_pending_short_ema_wait()
                    if entry_updates.get("clear_pending_short_ema_wait"):
                        state_manager.clear_pending_short_ema_wait()
                    if entry_updates.get("set_adx_wait_long"):
                        data = entry_updates["set_adx_wait_long"]
                        state_manager.set_adx_wait_long(data["bars"], data["trigger"])
                    if entry_updates.get("clear_adx_wait_long"):
                        state_manager.clear_adx_wait_long()
                    if entry_updates.get("decrement_adx_wait_long"):
                        state_manager.decrement_adx_wait_long()
                    if entry_updates.get("set_adx_wait_short"):
                        data = entry_updates["set_adx_wait_short"]
                        state_manager.set_adx_wait_short(data["bars"], data["trigger"])
                    if entry_updates.get("clear_adx_wait_short"):
                        state_manager.clear_adx_wait_short()
                    if entry_updates.get("decrement_adx_wait_short"):
                        state_manager.decrement_adx_wait_short()
                    if entry_updates.get("set_volume_wait_long"):
                        d = entry_updates["set_volume_wait_long"]
                        state_manager.set_volume_wait_long(d["remaining"], d["trigger"], d["kind"])
                    if entry_updates.get("set_volume_wait_short"):
                        d = entry_updates["set_volume_wait_short"]
                        state_manager.set_volume_wait_short(d["remaining"], d["trigger"], d["kind"])
                    if entry_updates.get("clear_pending_volume_long"):
                        state_manager.clear_volume_wait_long()
                    if entry_updates.get("clear_pending_volume_short"):
                        state_manager.clear_volume_wait_short()
                    if entry_updates.get("decrement_volume_wait_long"):
                        state_manager.decrement_volume_wait_long()
                    if entry_updates.get("decrement_volume_wait_short"):
                        state_manager.decrement_volume_wait_short()
                    log_entry_decision(
                        "LIVE", active_contract_label, current_bar, state,
                        signal_engine, entry_signal, entry_updates,
                    )
                    if entry_signal:
                        is_long = entry_signal.signal_type == SignalType.BUY
                        direction = "LONG" if is_long else "SHORT"

                        dashboard.print_event("ENTRY", f"LIVE {direction} @ {entry_signal.price:.2f}")

                        stop_loss, take_profit = signal_engine.calculate_exit_levels(
                            entry_price=entry_signal.price,
                            is_long=is_long
                        )

                        entry_trade = await order_manager.place_market_order(
                            action="BUY" if is_long else "SELL",
                            quantity=contracts
                        )
                        actual_fill_price = await order_manager.wait_for_fill_price(entry_trade)

                        trade_id = state_manager.state.trade_count + 1

                        dashboard.on_entry(
                            trade_id=trade_id,
                            direction=direction,
                            entry_price=entry_signal.price,
                            quantity=contracts,
                            stop_loss=stop_loss,
                            take_profit=take_profit
                        )

                        state_manager.on_entry(entry_signal, stop_loss, take_profit)

                        logger.info(
                            "ORDER_ENTRY | mode=LIVE | contract=%s | direction=%s | entry_price=%.4f | "
                            "actual_fill_price=%s | qty=%s | SL=%.4f | TP=%.4f | trigger=%s | trade_id=%s",
                            active_contract_label,
                            direction,
                            entry_signal.price,
                            f"{actual_fill_price:.4f}" if actual_fill_price is not None else "n/a",
                            contracts,
                            stop_loss,
                            take_profit,
                            entry_signal.trigger,
                            trade_id,
                        )

                        notify_telegram_background(
                            telegram,
                            telegram.notify_trade_placed(
                                direction=direction,
                                entry_price=entry_signal.price,
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                                contracts=contracts,
                                trigger=entry_signal.trigger,
                                trade_id=trade_id,
                                actual_fill_price=actual_fill_price,
                            ),
                        )
            except Exception as e:
                logger.exception("Live on_bar_close failed: %s", e)

        primary_feed.on_bar_close(lambda df, bar: asyncio.create_task(on_bar_close(df, bar)))
        primary_feed.on_bar_update(on_bar_update)
        
        await primary_feed.start(initial_lookback_days=15)
        seed_dashboard_prices_from_feed(dashboard, primary_feed)
        _df_live = primary_feed.get_dataframe()
        logger.info(
            "Realtime feed ready: %s buffered bars; dashboard seeded from last OHLC row",
            len(_df_live) if _df_live is not None else 0,
        )
        
        if tick_bar_builder is not None:
            tick_bar_task = asyncio.create_task(
                run_fast_tick_bar_close_loop(
                    mode_label="LIVE",
                    contract_label=shared_state.get("contract_label", contract_label),
                    feed=primary_feed,
                    builder=tick_bar_builder,
                    shared_state=shared_state,
                    grace_sec=fast_tick_cfg["grace_sec"],
                )
            )

        dashboard_task = asyncio.create_task(update_dashboard_loop())
        
        # Send bot started notification via Telegram
        await telegram.notify_bot_started(
            mode=f"LIVE ({gateway_type})",
            symbol="MNQ",
            contracts=contracts,
            strategy_info=strategy_info_for_telegram(
                strategy_cfg, ema_cfg.get('length', 200)
            ),
        )
        
        print("\n" + "=" * 60)
        print("[OK] LIVE TRADING IS ACTIVE - REAL MONEY")
        print("=" * 60)
        print(f"[OK] Connected via {gateway_type} (Port {port})")
        print("[OK] Dashboard will refresh every 5 seconds")
        print("[OK] Telegram notifications enabled" if telegram.enabled else "[--] Telegram notifications disabled")
        print("[OK] Press Ctrl+C to stop\n")
        
        await asyncio.sleep(3)

        stop_reason = "Unknown"
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down LIVE trading...")
            stop_reason = "LIVE trading stopped by user (Ctrl+C)"

        shared_state['running'] = False
        try:
            state_manager.save_state()
            logger.info(
                "STATE_SAVED_ON_SHUTDOWN | mode=LIVE | pendEMA_L=%s | pendEMA_S=%s | "
                "tradedBull=%s | tradedBear=%s | position=%s",
                state_manager.state.pending_long_ema_wait,
                state_manager.state.pending_short_ema_wait,
                state_manager.state.traded_in_bull_trend,
                state_manager.state.traded_in_bear_trend,
                state_manager.state.position_size,
            )
        except Exception as save_exc:
            logger.error("Failed to save live state on shutdown: %s", save_exc)
        if tick_bar_task is not None:
            tick_bar_task.cancel()
        dashboard_task.cancel()

        await telegram.notify_bot_stopped(stop_reason)
        stop_notified = True

        await primary_feed.stop()
        await position_tracker.shutdown()
        await conn_manager.disconnect()
        print("[OK] LIVE trading stopped")

    except Exception as e:
        logger.error(f"LIVE trading error: {e}")
        import traceback
        traceback.print_exc()
        await telegram.notify_error(f"LIVE trading crashed: {str(e)}")
        if not stop_notified:
            await telegram.notify_bot_stopped(f"Crashed: {e}")
        await conn_manager.disconnect()
        raise
    finally:
        await telegram.shutdown()


# Import shared backtest selector
from main import run_backtest_selection


def parse_cli_args(argv: Optional[list] = None):
    """Optional non-interactive mode for VPS/systemd (skips menu + contract prompt)."""
    parser = argparse.ArgumentParser(
        description="MNQ SuperTrend + EMA trading system",
    )
    parser.add_argument(
        "--mode",
        choices=("paper", "live", "backtest"),
        help="Run without menu: paper, live, or backtest",
    )
    parser.add_argument(
        "--contracts",
        type=int,
        default=None,
        metavar="N",
        help="Contract size (default 1 for paper when --mode is set)",
    )
    return parser.parse_args(argv)


async def main_async(cli_args: Optional[argparse.Namespace] = None):
    """Main async entry point."""
    if cli_args is None:
        cli_args = parse_cli_args()

    print_banner()
    
    # Load config
    config = load_config()
    
    # Show current settings (matches resolve_side_configs + paper/backtest engine)
    strategy_cfg = config.get('strategy', {})
    from utils.strategy_side_config import print_resolved_strategy_banner

    print_resolved_strategy_banner(strategy_cfg)
    print()
    
    ibkr_cfg = config.get('ibkr', {})
    gateway_type = ibkr_cfg.get('connection', {}).get('default_gateway', 'tws').upper()
    
    print("v2.1 Features:")
    print("  [OK] Auto-reconnect when TWS/Gateway disconnects")
    print("  [OK] Market orders for faster execution")
    print("  [OK] Clean terminal dashboard with P&L tracking")
    print(f"  [OK] IB Gateway support ({gateway_type}) for 24/7 operation")
    print("  [OK] Telegram notifications (trades, P&L, connection status)")
    print()

    contracts = cli_args.contracts
    if cli_args.mode:
        mode_map = {"paper": "2", "live": "3", "backtest": "1"}
        choice = mode_map[cli_args.mode]
        if contracts is None and choice in ("2", "3"):
            contracts = 1
    else:
        choice = get_menu_choice()
    
    if choice == '0':
        print("\nGoodbye!")
        return
    elif choice == '1':
        await run_backtest_selection(config)
    elif choice == '2':
        await run_paper_trading_v2(config, contracts=contracts)
    elif choice == '3':
        await run_live_trading_v2(config, contracts=contracts)
    
    print("\n" + "=" * 60)
    print("Session ended. Run 'python main_v2.py' to start again.")
    print("=" * 60)


def main():
    """Main entry point."""
    cli_args = parse_cli_args()
    try:
        asyncio.run(main_async(cli_args))
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        logger.error(f"Error: {e}")
        raise


if __name__ == '__main__':
    main()
