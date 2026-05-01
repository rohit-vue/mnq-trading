# Strategy Rules Implementation – Summary for Review

This document summarizes the changes made to implement the **EMA conditional close** and **ADX Case 1 / Case 2 (5-candle window)** rules. The same logic is used for **Databento backtest**, **IBKR backtest**, **paper**, and **live** (main.py and main_v2.py).

---

## 1. EMA 200 (1H) – Conditional Close Confirmation

### Rule (as specified)
- **Not** a global “1H must be closed” rule.
- Applied only when:
  - Supertrend has given a valid flip (or EMA is confirmed), and
  - ADX ≥ 20 within the allowed window, and
  - The **current 1H candle is still running**, and
  - The **running 1H price is overlapping / very close to EMA 200** (direction not clearly confirmed).
- In that case: **wait** for the current 1H candle to close; then confirm direction (above = bullish, below = bearish) and allow or reject entry.
- When price is **clearly** above or below EMA 200: **no** wait; entry is allowed as soon as ST + ADX conditions are met.

### Implementation

- **Config** (`config/strategy.yaml`):
  - `ema.overlap_margin_pct: 0.1`  
  - Price is treated as “overlapping” EMA when within **0.1%** of the EMA value.

- **Data layer** (all places that build 10m + 1H EMA):
  - **New columns:**
    - `close_1h_prev`, `ema_1h_prev`: previous bar’s 1H close and EMA (so on the first 10m bar of a new hour we have the **closed** 1H candle’s close).
    - `prev_ema_overlapping`: `True` when the **previous** 1H close was within `overlap_margin_pct` of the previous 1H EMA.
    - `ema_clear_bull`: current 1H close > EMA × (1 + margin).
    - `ema_clear_bear`: current 1H close < EMA × (1 - margin).
    - **`ema_confirmed_bull`**:  
      - `ema_clear_bull` **or**  
      - (we are on the first 10m bar of a new hour **and** previous 1H close > previous 1H EMA **and** previous bar was overlapping).  
      So when price was overlapping, we only confirm after the 1H close.
    - **`ema_confirmed_bear`**: same idea for bearish.

- **Signal engine**
  - Entry conditions use **`ema_confirmed_bull`** / **`ema_confirmed_bear`** instead of `ema_bull` / `ema_bear`.
  - Fallback: if `ema_confirmed_*` is missing (e.g. old data), it uses `ema_bull` / `ema_bear`.

- **Files touched**
  - `config/strategy.yaml` – added `overlap_margin_pct`.
  - `data/databento_loader.py` – `prepare_databento_for_backtest()`: new parameter `ema_overlap_margin_pct`, and new columns above.
  - `data/historical_loader.py` – `prepare_strategy_data()`: same parameter and same columns.
  - `main.py` – Databento backtest: passes `ema_overlap_margin_pct`; IBKR stitched path: same EMA overlap/confirmed logic added.
  - `run_databento_test.py` – passes `ema_overlap_margin_pct` from config.

---

## 2. ADX – Case 1 vs Case 2 (5-candle window)

### Rule (as specified)
- **Case 1:** If at the moment **ST flip + EMA are confirmed** we already have **ADX ≥ 20** → enter **immediately** (no 5-candle wait).
- **Case 2:** If at that moment **ADX < 20**:
  - Start a **5-candle monitoring window** (including the confirmation bar).
  - If ADX becomes ≥ 20 on **any** of those 5 bars → enter on that bar.
  - If ADX stays < 20 for **all** 5 bars → **invalidate** that setup permanently (no entry on 6th bar or later for that flip).
- One entry attempt per ST flip; no entry after the 5th candle.

### Implementation

- **State** (`strategy/state_manager.py`, `StrategyState`):
  - `pending_long_setup: bool` – we are in the 5-candle window for a long (ST flip + EMA confirmed, ADX was < 20).
  - `pending_long_since_time: Optional[pd.Timestamp]` – bar timestamp when the window started (confirmation bar).
  - **Cleared when:**
    - ST flips **bearish** (`update_supertrend_state`),
    - We **enter long** (`on_entry`),
    - We **invalidate** (bars_since ≥ 5).
  - **Set when:** ST flip + EMA confirmed but ADX < 20 → `set_pending_long(bar_timestamp)`.
  - New methods: `set_pending_long(since_time)`, `clear_pending_long()`; serialization in `to_dict` / `from_dict`.

- **Signal engine** (`strategy/signal_engine.py`):
  - `evaluate_entry_conditions(..., pending_long_since_time=...)` now returns **`(Optional[Signal], Dict)`**.
  - **If `pending_long_since_time` is set:**
    - `bars_since = (current_bar_time - pending_long_since_time) / 10` (10‑minute bars).
    - If `bars_since >= 5` → return `(None, {"clear_pending_long": True})` (invalidate).
    - If `bars_since < 5` and ADX ≥ 20 → return `(BUY signal, {"clear_pending_long": True})` (enter and clear).
    - Otherwise → return `(None, {})` (keep waiting).
  - **BUY Case 1 (ST flip):**  
    If ST flip + `ema_confirmed_bull` + not traded_in_bull_trend:
    - If ADX ≥ 20 → return `(BUY signal, {})` (Case 1 – immediate entry).
    - Else → return `(None, {"set_pending_long": True, "pending_since": timestamp})` (Case 2 – start window).
  - **BUY Case 2 (EMA cross):**  
    Still **immediate** only: ST bull + `ema_confirmed_bull` + `ema_bull_cross` + ADX ≥ 20 → enter; no 5-candle window for EMA-cross trigger.
  - **SHORT:** Unchanged (no ADX window); uses `ema_confirmed_bear`.

- **Callers** apply the returned dict:
  - If `clear_pending_long` → `state_manager.clear_pending_long()`.
  - If `set_pending_long` → `state_manager.set_pending_long(updates["pending_since"])`.

- **Files touched**
  - `strategy/state_manager.py` – state fields, `set_pending_long`, `clear_pending_long`, `_clear_pending_long`, reset on ST bear flip and on long entry.
  - `strategy/signal_engine.py` – new signature and return type, pending-window logic, use of `ema_confirmed_bull`/`ema_confirmed_bear`.
  - `backtest/backtest_engine.py` – passes `pending_long_since_time`, unpacks `(signal, updates)`, applies updates.
  - `main.py` – paper and live: same (pass pending, apply updates).
  - `main_v2.py` – same for both entry call sites.

---

## 3. Quick reference

| Rule | Where it lives |
|------|----------------|
| EMA overlap margin (0.1%) | `config/strategy.yaml` → `ema.overlap_margin_pct` |
| EMA confirmed (overlap → wait for 1H close) | Data: `ema_confirmed_bull` / `ema_confirmed_bear` in databento_loader, historical_loader, main (stitched). Signal: use these in entry conditions. |
| ADX Case 1 (ADX ≥ 20 at confirmation → enter now) | `signal_engine.evaluate_entry_conditions`: ST flip + ema_confirmed_bull + ADX ≥ 20 → return signal. |
| ADX Case 2 (ADX < 20 → 5-candle window, enter when ADX ≥ 20 or invalidate) | State: `pending_long_setup`, `pending_long_since_time`. Signal: return `set_pending_long` / `clear_pending_long`; callers apply via state_manager. |
| One entry per ST flip; no entry after 5th candle | `bars_since >= 5` → `clear_pending_long` and no entry. |

---

## 4. How to test

- **Databento:**  
  `python run_databento_test.py`  
  Uses same strategy (with EMA confirmed + ADX Case 1/2) and same config.

- **IBKR backtest:**  
  Run main → Backtest → choose dates.  
  ContFuture path uses `prepare_strategy_data` (with new columns); stitched path uses the new EMA block in main.py.

- **Paper / Live:**  
  Entry flow now uses `(signal, updates)` and applies pending state; 5-candle window and invalidation behave as above.

---

## 5. Optional: adjust EMA overlap

To change when price is considered “overlapping” the EMA, edit in `config/strategy.yaml`:

```yaml
ema:
  overlap_margin_pct: 0.1   # 0.1% default; increase to be stricter (e.g. 0.2)
```

No code change required; all data and signal logic read this from config.
