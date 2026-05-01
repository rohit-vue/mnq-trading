"""
Resolve long vs short strategy blocks from strategy.yaml with legacy fallback.

If long_supertrend / short_supertrend (and risk / adx) are omitted, values from
supertrend, risk, and adx are used for both sides.
"""

from typing import Any, Dict, List


def resolve_side_configs(strategy_root: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Parameters
    ----------
    strategy_root : dict
        Full parsed content of config/strategy.yaml.

    Returns
    -------
    dict with long/short supertrend entry+exit configs, plus risk/adx per side.
    """
    legacy_st = strategy_root.get("supertrend") or {}
    legacy_risk = strategy_root.get("risk") or {}
    legacy_adx = strategy_root.get("adx") or {}

    # Entry/exit ST fallbacks:
    # 1) explicit *_entry / *_exit
    # 2) side-level long_supertrend / short_supertrend
    # 3) legacy shared supertrend
    long_st_base = strategy_root.get("long_supertrend") or legacy_st
    short_st_base = strategy_root.get("short_supertrend") or legacy_st
    long_st_entry = strategy_root.get("long_supertrend_entry") or long_st_base
    long_st_exit = strategy_root.get("long_supertrend_exit") or long_st_entry
    short_st_entry = strategy_root.get("short_supertrend_entry") or short_st_base
    short_st_exit = strategy_root.get("short_supertrend_exit") or short_st_entry
    long_risk = strategy_root.get("long_risk") or legacy_risk
    short_risk = strategy_root.get("short_risk") or legacy_risk
    long_adx = strategy_root.get("long_adx") or legacy_adx
    short_adx = strategy_root.get("short_adx") or legacy_adx

    return {
        # Keep original keys as aliases for entry to preserve existing callers.
        "long_supertrend": long_st_entry,
        "short_supertrend": short_st_entry,
        "long_supertrend_entry": long_st_entry,
        "long_supertrend_exit": long_st_exit,
        "short_supertrend_entry": short_st_entry,
        "short_supertrend_exit": short_st_exit,
        "long_risk": long_risk,
        "short_risk": short_risk,
        "long_adx": long_adx,
        "short_adx": short_adx,
    }


def redundant_supertrend_warnings(strategy_root: Dict[str, Any]) -> List[str]:
    """
    When both side aliases (long_supertrend / short_supertrend) and explicit *_entry
    blocks exist with different atr_length or multiplier, only *_entry / *_exit drive
    SignalEngine and indicators — the duplicate alias blocks are ignored. Warn so YAML
    stays unambiguous.
    """
    out: List[str] = []

    def _diff(name_alias: str, name_entry: str, label: str) -> None:
        alias = strategy_root.get(name_alias)
        ent = strategy_root.get(name_entry)
        if not alias or not ent:
            return
        for key in ("atr_length", "multiplier"):
            av = alias.get(key)
            ev = ent.get(key)
            if av is not None and ev is not None and av != ev:
                out.append(
                    f"{label}: `{name_entry}` overrides `{name_alias}` for signals "
                    f"({key} entry={ev}, alias={av}; alias ignored)."
                )
                return

    _diff("long_supertrend", "long_supertrend_entry", "Long ST")
    _diff("short_supertrend", "short_supertrend_entry", "Short ST")
    return out


def print_resolved_strategy_banner(strategy_root: Dict[str, Any]) -> None:
    """
    Print SuperTrend entry+exit, risk, ADX, EMA exactly as resolve_side_configs +
    signal_engine_init_kwargs use (same sources as backtest bar prep).
    """
    s = resolve_side_configs(strategy_root)
    le, lx = s["long_supertrend_entry"], s["long_supertrend_exit"]
    se, sx = s["short_supertrend_entry"], s["short_supertrend_exit"]
    lr, sr = s["long_risk"], s["short_risk"]
    la, sa = s["long_adx"], s["short_adx"]
    ema_cfg = strategy_root.get("ema") or {}
    ema_len = int(ema_cfg.get("length", 200))

    print("Current Strategy Settings (same resolution as backtest / paper engine):")
    print(
        f"  Long ST entry:  ATR={le.get('atr_length', 10)}, "
        f"Mult={le.get('multiplier', 3)}"
    )
    print(
        f"  Long ST exit:   ATR={lx.get('atr_length', 10)}, "
        f"Mult={lx.get('multiplier', 3)}"
    )
    print(
        f"  Short ST entry: ATR={se.get('atr_length', 10)}, "
        f"Mult={se.get('multiplier', 3)}"
    )
    print(
        f"  Short ST exit:  ATR={sx.get('atr_length', 10)}, "
        f"Mult={sx.get('multiplier', 3)}"
    )
    print(f"  EMA (1H):       length={ema_len}")
    print(
        f"  Long SL/TP:     {lr.get('stop_loss_pct', 0.4)}% / "
        f"{lr.get('take_profit_pct', 1.2)}%"
    )
    print(
        f"  Short SL/TP:    {sr.get('stop_loss_pct', 0.4)}% / "
        f"{sr.get('take_profit_pct', 1.2)}%"
    )
    print(
        f"  ADX long:       thresh={la.get('threshold', 20)}, "
        f"use={la.get('use_adx', True)}, wait={la.get('consecutive_candles', 5)} bars"
    )
    print(
        f"  ADX short:      thresh={sa.get('threshold', 20)}, "
        f"use={sa.get('use_adx', True)}, wait={sa.get('consecutive_candles', 5)} bars"
    )
    vol = strategy_root.get("volume_check", False)
    vma = strategy_root.get("volume_ma_period", 20)
    vlook = strategy_root.get("volume_candle_lookahead", 1)
    print(f"  Volume filter:  enabled={vol}, MA period={vma}, lookahead bars={vlook}")

    for msg in redundant_supertrend_warnings(strategy_root):
        print(f"  [!] {msg}")


def strategy_info_for_telegram(
    strategy_root: Dict[str, Any],
    ema_length: int,
) -> Dict[str, Any]:
    """Flatten resolved YAML for TelegramNotifier.notify_bot_started."""
    s = resolve_side_configs(strategy_root)
    le, lx = s["long_supertrend_entry"], s["long_supertrend_exit"]
    se, sx = s["short_supertrend_entry"], s["short_supertrend_exit"]
    lr, sr = s["long_risk"], s["short_risk"]
    la, sa = s["long_adx"], s["short_adx"]
    return {
        "ema_length": ema_length,
        "st_atr_long_entry": le.get("atr_length", 10),
        "st_mult_long_entry": le.get("multiplier", 3.0),
        "st_atr_long_exit": lx.get("atr_length", 10),
        "st_mult_long_exit": lx.get("multiplier", 3.0),
        "st_atr_short_entry": se.get("atr_length", 10),
        "st_mult_short_entry": se.get("multiplier", 3.0),
        "st_atr_short_exit": sx.get("atr_length", 10),
        "st_mult_short_exit": sx.get("multiplier", 3.0),
        "sl_pct_long": lr.get("stop_loss_pct", 0.4),
        "tp_pct_long": lr.get("take_profit_pct", 1.2),
        "sl_pct_short": sr.get("stop_loss_pct", 0.4),
        "tp_pct_short": sr.get("take_profit_pct", 1.2),
        "adx_threshold_long": la.get("threshold", 20),
        "adx_threshold_short": sa.get("threshold", 20),
        "adx_wait_long": la.get("consecutive_candles", 5),
        "adx_wait_short": sa.get("consecutive_candles", 5),
        "use_adx_long": la.get("use_adx", True),
        "use_adx_short": sa.get("use_adx", True),
        "volume_check": strategy_root.get("volume_check", False),
        "volume_ma_period": strategy_root.get("volume_ma_period", 20),
        "volume_candle_lookahead": strategy_root.get("volume_candle_lookahead", 1),
    }


# Used in BacktestConfig only; SignalEngine does not accept these (exit series come from bar columns).
_BACKTEST_ONLY_ST_EXIT_KEYS = frozenset(
    {
        "supertrend_atr_long_exit",
        "supertrend_mult_long_exit",
        "supertrend_atr_short_exit",
        "supertrend_mult_short_exit",
    }
)


def signal_engine_kwargs(strategy_root: Dict[str, Any]) -> Dict[str, Any]:
    """Keyword args for backtest.BacktestConfig (**kwargs) — includes exit ST fields."""
    s = resolve_side_configs(strategy_root)
    lr, sr = s["long_risk"], s["short_risk"]
    la, sa = s["long_adx"], s["short_adx"]
    lst_entry = s["long_supertrend_entry"]
    lst_exit = s["long_supertrend_exit"]
    sst_entry = s["short_supertrend_entry"]
    sst_exit = s["short_supertrend_exit"]
    return {
        "sl_pct_long": float(lr.get("stop_loss_pct", 0.4)),
        "tp_pct_long": float(lr.get("take_profit_pct", 1.2)),
        "sl_pct_short": float(sr.get("stop_loss_pct", 0.4)),
        "tp_pct_short": float(sr.get("take_profit_pct", 1.2)),
        # Entry ST values (used by SignalEngine and reported in backtest)
        "supertrend_atr_long": int(lst_entry.get("atr_length", 10)),
        "supertrend_mult_long": float(lst_entry.get("multiplier", 3.0)),
        "supertrend_atr_short": int(sst_entry.get("atr_length", 10)),
        "supertrend_mult_short": float(sst_entry.get("multiplier", 3.0)),
        # Exit ST values (reporting + explicit config traceability)
        "supertrend_atr_long_exit": int(lst_exit.get("atr_length", 10)),
        "supertrend_mult_long_exit": float(lst_exit.get("multiplier", 3.0)),
        "supertrend_atr_short_exit": int(sst_exit.get("atr_length", 10)),
        "supertrend_mult_short_exit": float(sst_exit.get("multiplier", 3.0)),
        "use_adx_long": bool(la.get("use_adx", True)),
        "use_adx_short": bool(sa.get("use_adx", True)),
        "adx_wait_bars_long": max(1, int(la.get("consecutive_candles", 5))),
        "adx_wait_bars_short": max(1, int(sa.get("consecutive_candles", 5))),
        "adx_threshold_long": float(la.get("threshold", 20.0)),
        "adx_threshold_short": float(sa.get("threshold", 20.0)),
    }


def signal_engine_init_kwargs(strategy_root: Dict[str, Any]) -> Dict[str, Any]:
    """Keyword args for strategy.signal_engine.SignalEngine — excludes backtest-only exit ST keys."""
    return {
        k: v
        for k, v in signal_engine_kwargs(strategy_root).items()
        if k not in _BACKTEST_ONLY_ST_EXIT_KEYS
    }
