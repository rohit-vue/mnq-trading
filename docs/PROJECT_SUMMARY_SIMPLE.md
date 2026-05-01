# Project Summary – In Simple Language

This document explains what the project does, how the trading logic works, and what each main file is for. Technical terms from the code are avoided where possible; meanings are explained in plain words.

---

## What the project does

This is a **trading system** for **Micro E-mini Nasdaq-100 futures (MNQ)**. It:

1. **Follows the trend** using two ideas:
   - A **trend line** on 10-minute bars (the “Supertrend”) to decide if the short-term trend is up or down.
   - A **long-term average price** on 1-hour bars (200-period EMA) to filter: only go long when price is above this average, only go short when price is below it.

2. **Runs in three ways**:
   - **Backtest**: Replays past price data and simulates trades (no real money).
   - **Paper trading**: Sends orders to a broker’s test account (no real money).
   - **Live trading**: Sends real orders (real money).

3. **Uses fixed risk per trade**:
   - **Stop loss**: Exit and take a small loss if price moves against you by a set percentage (e.g. 0.4%).
   - **Take profit**: Exit and lock in a gain if price moves in your favor by a set percentage (e.g. 1.2%).

So in one sentence: **“Trade in the direction of the 10-minute trend, only when the 1-hour trend agrees, with a fixed stop and target.”**

---

## How the strategy works (plain words)

### The two directions

- **Long (buy)**: You expect price to go up. You open a long and close it when price hits your target, your stop, or when the 10-minute trend flips to down.
- **Short (sell)**: You expect price to go down. You open a short and close it when price hits your target, your stop, or when the 10-minute trend flips to up.

### The two triggers for opening a trade

The system can open a trade in two situations (for each direction):

1. **Trend line flip**  
   The 10-minute trend line has just switched from down to up (for long) or from up to down (for short). So the **first** reason to enter is: “the short-term trend just changed direction.”

2. **Price crossing the long-term average**  
   On the 1-hour chart, price has just moved from below the 200-period average to above it (for long), or from above to below (for short). So the **second** reason is: “the 1-hour trend just confirmed by crossing the average.”

In both cases, the system also checks:
- You are not already in a position.
- The 1-hour close is on the correct side of the 200 average (with the special “overlap” rule below).
- You have not already taken a trade in this same trend (see “Re-entry blocking” below).
- For **longs only**, a “trend strength” number (ADX) is used: either it is strong enough right away, or you wait up to 5 bars for it to become strong enough (see “ADX: two cases” below).

---

## Entry cases (when we open a trade)

### Long (buy) entries

All of these must be true to consider a long:

- No open position.
- 10-minute trend line says “up” (bullish).
- 1-hour trend is “confirmed” up (price above the 200 average, with the overlap rule below).
- We have not already taken a long in this current up-trend (re-entry blocking).

Then **either**:

- **Case 1 – Trend line flip**  
  The 10-minute trend line just flipped from down to up.  
  - If the trend-strength number (ADX) is ≥ 20: enter **immediately**.  
  - If ADX &lt; 20: start a **5-bar window**; if ADX becomes ≥ 20 on any of the next 5 bars, enter on that bar; if it never does, that setup is **cancelled** (no entry until the next trend flip).

- **Case 2 – 1-hour cross up**  
  Price on the 1-hour chart just crossed **above** the 200 average.  
  - Entry is **only** allowed if the 5-bar window was **not** already cancelled for this trend.  
  - ADX must be ≥ 20 on that bar (no 5-bar wait for this trigger).

### Short (sell) entries

Same idea, other way:

- No open position.
- 10-minute trend line says “down” (bearish).
- 1-hour trend is “confirmed” down (price below the 200 average).
- We have not already taken a short in this current down-trend.

Then **either**:

- **Case 1 – Trend line flip**  
  The 10-minute trend line just flipped from up to down.  
  Same ADX rule: enter at once if ADX ≥ 20, otherwise 5-bar window; if ADX never reaches 20 in 5 bars, cancel that setup.

- **Case 2 – 1-hour cross down**  
  Price on the 1-hour chart just crossed **below** the 200 average.  
  Same: only if the short setup wasn’t cancelled, and ADX ≥ 20 on that bar.

---

## Edge cases and special rules

### 1. Re-entry blocking (one trade per trend)

After you **close** a trade (for any reason: target, stop, or trend flip), you are **not** allowed to open another trade in the **same** direction until the 10-minute trend line **flips the other way** and then flips back.

- Example: You went long, then closed. You cannot go long again until the trend line has flipped to down and then flipped back to up.
- So: **at most one long per “up” trend and one short per “down” trend.** This avoids opening many times in the same move.

### 2. When price is “on the line” (EMA overlap)

Sometimes the 1-hour closing price is very close to the 200 average (within a small band, e.g. 0.1%). Then the system does **not** treat the trend as clearly up or down on that bar.

- **Rule**: If the **previous** 1-hour candle closed in that “overlap” band, we **wait for the current 1-hour candle to close** before we say “trend is up” or “trend is down.”
- So: **“Confirmed” up/down** means either (a) price is clearly above/below the average by that margin, or (b) we are at the start of a new hour and the **last closed** 1-hour candle was on the right side of the average (and was overlapping before).  
This avoids entering when the 1-hour trend is still ambiguous.

### 3. ADX: two cases (trend strength, longs and shorts)

ADX is a number that says how strong the trend is (above 20 = “trending” enough to allow entry).

- **Case 1 – Strong enough at confirmation**  
  At the moment the trend line flips and the 1-hour trend is confirmed, ADX is already ≥ 20 → we enter **on that bar**.

- **Case 2 – Not strong yet**  
  At that moment ADX &lt; 20 → we start a **5-bar (10-minute) window**.  
  - If on **any** of those 5 bars ADX becomes ≥ 20 → we enter on that bar and clear the window.  
  - If after 5 bars ADX never reached 20 → we **cancel** that setup: no entry on the 6th bar or later for that same trend flip. We wait for the next trend flip to try again.

So: one attempt per trend flip; either we enter within 5 bars when ADX gets strong enough, or we give up until the next flip.

### 4. Bar close only

All decisions (entry and exit) are made **only when a bar closes**. There is no mid-bar entry or exit. This matches “process orders on close” and avoids using future information.

---

## Overlap case in detail (how it works in the code)

This section explains **exactly** how the “price on the line” (overlap) logic is implemented: when the 1-hour close is very close to the 200-period average, the system waits for the 1-hour candle to close before treating the trend as confirmed.

### Why overlap matters

On the 1-hour chart we need to know: is price **above** or **below** the 200 average? If the 1-hour close is almost exactly on the average (within a tiny band), we don’t treat it as clearly above or below. So we **don’t confirm** trend during that hour; we only confirm once we have a **closed** 1-hour candle that is clearly on one side.

### Config

- **`config/strategy.yaml`** → `ema.overlap_margin_pct: 0.1`  
  So “overlapping” = price within **0.1%** of the EMA.  
  Example: EMA = 20,000 → band is 19,980–20,020. If 1H close is in that band, it’s overlapping.

### Where it’s built (data layer)

The same logic lives in:

- **`data/databento_loader.py`** (in `prepare_databento_for_backtest`)
- **`data/historical_loader.py`** (in `prepare_strategy_data`)
- **`main.py`** (in the stitched IBKR backtest path)

All use the same formulas below. Margin in code is `margin = overlap_margin_pct / 100` (e.g. 0.001 for 0.1%).

### Columns and formulas

On **each 10-minute bar** we have:

1. **`close_1h`**  
   The close of the **current** 1-hour bar (the hour this 10m bar belongs to).  
   (In backtest this is the final close of that hour for every 10m bar in that hour.)

2. **`ema_1h`**  
   The 200-period EMA value for that same 1-hour bar.

3. **`close_1h_prev`** = previous bar’s `close_1h`  
   So on the **first 10m bar of a new hour**, this is the **last closed** 1-hour candle’s close.

4. **`ema_1h_prev`** = previous bar’s `ema_1h`  
   The EMA value that belonged to that last closed 1-hour candle.

5. **`is_new_1h_candle`**  
   `True` only on the **first** 10-minute bar of a new hour (the bar right after the 1H candle closed).

6. **Overlap (previous hour)**  
   - `prev_overlap_pct = |close_1h_prev - ema_1h_prev| / ema_1h_prev`  
   - **`prev_ema_overlapping`** = `True` when `prev_overlap_pct <= margin`  
   So: “the **previous** 1-hour candle’s close was within 0.1% of the **previous** 1-hour EMA.”  
   That means the last **closed** hour was ambiguous (price right on the line).

7. **Clear above / below (current hour)**  
   - **`ema_clear_bull`** = `close_1h > ema_1h * (1 + margin)`  
     Price is **clearly** above the average (more than 0.1% above).  
   - **`ema_clear_bear`** = `close_1h < ema_1h * (1 - margin)`  
     Price is **clearly** below (more than 0.1% below).

8. **Confirmed (what the strategy uses for entries)**  
   - **`ema_confirmed_bull`** =  
     - **either** `ema_clear_bull`  
     - **or** (we are on the first 10m bar of a new hour **and** the previous 1H close was above the previous 1H EMA **and** the previous hour was overlapping):  
       `is_new_1h_candle & (close_1h_prev > ema_1h_prev) & prev_ema_overlapping`  
   - **`ema_confirmed_bear`** =  
     - **either** `ema_clear_bear`  
     - **or** (first bar of new hour **and** previous 1H close below previous 1H EMA **and** previous hour overlapping):  
       `is_new_1h_candle & (close_1h_prev < ema_1h_prev) & prev_ema_overlapping`

So:

- If price is **clearly** above or below the EMA by more than the margin → we confirm immediately (`ema_clear_bull` / `ema_clear_bear`).
- If the **previous** hour was overlapping (close near EMA), we **do not** confirm during that hour. We only confirm on the **first 10m bar of the next hour**, when we look at the **closed** previous candle: if it closed above EMA we set `ema_confirmed_bull`, if below we set `ema_confirmed_bear`. That’s the “wait for 1H close” behavior.

### How the signal engine uses it

In **`strategy/signal_engine.py`**, all **long** entry conditions use **`ema_confirmed_bull`** (not `ema_bull`), and all **short** entry conditions use **`ema_confirmed_bear`** (not `ema_bear`):

- `ema_confirmed_bull = bar.get('ema_confirmed_bull', bar.get('ema_bull', False))`
- `ema_confirmed_bear = bar.get('ema_confirmed_bear', bar.get('ema_bear', False))`

So if the data has the “confirmed” columns (from databento_loader, historical_loader, or main.py), the strategy waits for the 1H close when price was overlapping; if those columns are missing (e.g. old data), it falls back to simple above/below (`ema_bull` / `ema_bear`).

### Example timeline (overlap case)

1. **10:00–10:50** (one 1H candle running)  
   That hour’s close is very close to the 200 EMA (within 0.1%) → that hour is “overlapping.”  
   During this hour, `ema_clear_bull` and `ema_clear_bear` are both False (price not clearly above or below by margin).  
   So `ema_confirmed_bull` and `ema_confirmed_bear` are False **until** we get a closed candle.

2. **11:00** (first 10m bar of the new hour)  
   - `is_new_1h_candle` = True.  
   - `close_1h_prev` = close of the 10:00–11:00 candle (e.g. 20,005).  
   - `ema_1h_prev` = EMA at that hour (e.g. 20,002).  
   - That previous close was within 0.1% of the EMA → `prev_ema_overlapping` = True.  
   - Previous close > previous EMA → 20,005 > 20,002.  
   So: `ema_confirmed_bull` = True (first bar of new hour + previous closed above + previous was overlapping).  
   The strategy is **allowed** to consider a long only from this bar onward (other conditions permitting). Before this bar, during the overlapping hour, no long was confirmed.

### Summary

| Concept | Meaning |
|--------|---------|
| **Overlap** | Previous 1H close within `overlap_margin_pct` (e.g. 0.1%) of previous 1H EMA. |
| **Clear bull/bear** | Current 1H close more than margin% above or below current 1H EMA. |
| **Confirmed** | Either clear, or (first 10m bar of new hour and last closed 1H was on the right side of EMA and last hour was overlapping). |
| **Effect** | When price was “on the line,” the system does not confirm trend until the 1H candle has closed; then it uses that closed candle’s close vs EMA to set `ema_confirmed_bull` or `ema_confirmed_bear`. |

---

## Exit cases (when we close a trade)

Exits are checked in this order (and the first one that triggers is used):

1. **Stop loss (highest priority)**  
   - Long: if the bar’s **low** reaches or goes below the stop price → exit at the stop (loss).  
   - Short: if the bar’s **high** reaches or goes above the stop price → exit at the stop (loss).

2. **Take profit**  
   - Long: if the bar’s **high** reaches or goes above the target price → exit at the target (profit).  
   - Short: if the bar’s **low** reaches or goes below the target price → exit at the target (profit).

3. **Trend line flip (only if neither stop nor target was hit)**  
   - Long: if the 10-minute trend line **flips to down** on this bar → exit at the bar’s close.  
   - Short: if the 10-minute trend line **flips to up** on this bar → exit at the bar’s close.

So: **first** protect capital (stop) and lock profit (target); **then** exit on trend reversal.

---

## Main files and what they do

### Entry point and menus

| File | Role |
|------|------|
| **main.py** | Main program. Shows menu: Backtest (IBKR or Databento), Paper, Live. Loads config, runs the chosen mode, and ties together data, strategy, and execution. |
| **main_v2.py** | Alternative entry point with the same strategy logic; can be used for different workflows or testing. |

### Configuration

| File | Role |
|------|------|
| **config/strategy.yaml** | Strategy settings: trend-line length and multiplier, 200-period average, stop/target percentages, overlap band (e.g. 0.1%), ADX usage and threshold (e.g. 20), 5-bar window, bar-close behavior. |
| **config/risk.yaml** | Risk-related settings: position size (e.g. fixed 1 contract), slippage and commission for backtests. |
| **config/mnq_contract.yaml** | MNQ contract details (tick size, multiplier, symbol, etc.) for the broker. |
| **config/ibkr.yaml** | Broker connection settings (ports, host) for paper and live. |

### Data

| File | Role |
|------|------|
| **data/databento_loader.py** | Loads and prepares data from Databento CSV files for backtest: builds 10m and 1H bars, computes trend line, 1H average, overlap and “confirmed” up/down, ADX, and all columns needed for entry/exit. |
| **data/historical_loader.py** | Fetches and prepares historical data from the broker (IBKR) and builds the same indicator and condition columns for backtest. |

### Indicators (numbers the strategy uses)

| File | Role |
|------|------|
| **indicators/supertrend.py** | Computes the trend line (Supertrend) and its direction (up/down) and “flip” flags from 10-minute high, low, close. |
| **indicators/ema.py** | Computes the 200-period average (EMA) on 1-hour close and provides the trend filter. |
| **indicators/** (ADX) | ADX (and +/- DI) calculation for the “trend strength” check (e.g. ADX ≥ 20). |

### Strategy logic (when to enter and exit)

| File | Role |
|------|------|
| **strategy/signal_engine.py** | **Heart of the strategy.** Evaluates, bar by bar: (1) All long/short entry conditions (trend flip vs EMA cross, overlap rule, ADX Case 1 vs 5-bar window, re-entry blocking, invalidated setup). (2) Exit conditions: stop, target, trend-line flip. Returns entry signal (or none) and any state updates (e.g. start/clear 5-bar window, mark setup invalidated). |
| **strategy/state_manager.py** | Holds **state** across bars: in a position or not, entry price, stop/target, “already traded in this trend” flags, pending 5-bar window (long/short), “setup invalidated” flags. Updates on entry, exit, and trend flips (e.g. reset “traded in trend” only on flip). Used by both backtest and live/paper. |

### Backtest

| File | Role |
|------|------|
| **backtest/backtest_engine.py** | Runs the simulation: for each bar in order, (1) update state from trend flips, (2) check exit (stop, target, flip), (3) if flat, check entry and apply 5-bar window updates, (4) record trades and equity. Applies slippage and commission. At the end, closes any open position and computes results. |
| **backtest/metrics.py** | Computes performance stats from the list of trades and equity curve: win rate, profit factor, max drawdown, Sharpe-like metrics, etc. |

### Execution (paper and live)

| Folder / file | Role |
|---------------|------|
| **execution/** | Order placement and position tracking with the broker (e.g. IBKR): send orders, track fills, manage stop/target orders. |

### Utilities and other

| File | Role |
|------|------|
| **run_databento_test.py** | Script to run a backtest using Databento CSV data and the same strategy (with overlap and ADX rules). |
| **requirements.txt** | Python package list (pandas, numpy, yaml, broker API, etc.). |

---

## Flow in one paragraph

**Data** (from CSV or broker) is turned into 10-minute and 1-hour bars; then the **indicators** (trend line, 200 average, ADX) and **conditions** (flips, crosses, overlap, confirmed up/down) are added. The **backtest engine** (or live/paper loop) walks bar by bar: it **updates state** (e.g. reset “traded in trend” on trend flips, manage 5-bar ADX window), **checks exits** (stop, target, trend flip), then **checks entries** (trend flip or EMA cross, with overlap and ADX rules). The **signal engine** implements all entry and exit logic; the **state manager** remembers position and “one trade per trend” and 5-bar window state. Results are trades and an equity curve; in backtest, **metrics** summarize performance.

---

## Quick reference: entry vs exit

| | Long | Short |
|---|------|--------|
| **Enter when** | 10m trend up, 1H confirmed up, not already traded this up-trend, and either (a) trend line just flipped up (with ADX rule), or (b) 1H just crossed above 200 average (ADX ≥ 20, setup not cancelled). | Same, mirrored: 10m trend down, 1H confirmed down, and either trend flip down or 1H cross below 200 average. |
| **Exit when** | Stop hit (low ≤ stop), or target hit (high ≥ target), or 10m trend line flips down. | Stop hit (high ≥ stop), or target hit (low ≤ target), or 10m trend line flips up. |

This document is a high-level summary. For exact parameters (e.g. ATR length, multiplier, stop %, overlap %), see **config/strategy.yaml**. For implementation details, see **strategy/signal_engine.py** and **strategy/state_manager.py**.
