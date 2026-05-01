# Complete IBKR Backtest Flow Explanation
## January 1, 2026 to March 1, 2026 Example

This document explains the complete flow of how the backtest script works from the moment you start it until the CSV report is generated.

---

## PHASE 1: INITIALIZATION AND USER INPUT

### Step 1: Program Starts
When you run `python main_v2.py`, the program begins by:
- Displaying a welcome banner
- Loading all configuration files from the `config` folder:
  - **Strategy settings** (Supertrend parameters, EMA length, stop loss/take profit percentages)
  - **Contract specifications** (MNQ futures details like tick size, tick value)
  - **Risk management** (commission per contract, slippage assumptions)
  - **IBKR connection** (which port to use, host address)

### Step 2: Menu Selection
The program shows you a menu with options. When you select option **"1" (Backtest IBKR)**, it calls the backtest function.

### Step 3: Date Range Input
The program asks you to enter:
- **Start date**: January 1, 2026
- **End date**: March 1, 2026
- **Number of contracts**: How many MNQ contracts to trade per signal (e.g., 1 contract)

The program validates that the start date is before the end date and converts these dates into proper datetime objects with timezone information (US Eastern time).

---

## PHASE 2: CONNECTING TO IBKR AND FETCHING DATA

### Step 4: IBKR Connection
The program connects to Interactive Brokers using the connection settings:
- Connects to **TWS (Trader Workstation)** or **IB Gateway** on your local computer
- Uses port 7497 for paper trading account (or 7496 for live)
- Establishes a connection to request historical data

### Step 5: Contract Selection Strategy
The program determines which MNQ contract to use based on the date range:

**If the date range is 55 days or less:**
- Uses **Continuous Futures (ContFuture)** - this is like TradingView's "MNQ1!" symbol
- This gives you a seamless continuous price series without gaps between contract expirations
- The program requests contract details from IBKR and selects the continuous contract

**If the date range is more than 55 days (like your Jan 1 to Mar 1 example):**
- Uses **Contract Stitching** - fetches data from multiple expired contracts
- For Jan-Mar 2026, it would fetch from:
  - **MNQH6** (March 2026 contract) for recent data
  - **MNQM6** (June 2026 contract) if needed
  - And potentially other contracts depending on the exact dates
- The program calculates which contracts are needed and shows you a list

### Step 6: Historical Data Fetching
The program requests historical bar data from IBKR:

**For each contract needed:**
- Requests **10-minute bars** (OHLCV - Open, High, Low, Close, Volume)
- IBKR has a limit of about 30 days per request, so the program breaks longer periods into chunks
- For each chunk, it sends a request like: "Give me 30 days of 10-minute bars ending on this date"
- The program waits for IBKR to respond with the bar data
- Converts the raw bar data into a pandas DataFrame (a table structure)

**Data stitching process:**
- If multiple contracts are needed, the program:
  1. Fetches data from each contract separately
  2. Combines them in chronological order
  3. Removes any overlapping periods
  4. Creates one continuous price series from January 1 to March 1

### Step 7: Data Validation
The program checks:
- That it received data (not empty)
- That the date range is covered
- That there are no major gaps in the data

---

## PHASE 3: CALCULATING TECHNICAL INDICATORS

### Step 8: Creating 1-Hour Bars
Since the strategy uses a 200-period EMA on the 1-hour timeframe, the program:
- Takes the 10-minute bars
- Groups them into 1-hour bars:
  - **Open** = first 10-minute bar's open price in that hour
  - **High** = highest high among all 10-minute bars in that hour
  - **Low** = lowest low among all 10-minute bars in that hour
  - **Close** = last 10-minute bar's close price in that hour
  - **Volume** = sum of all volumes in that hour

### Step 9: Calculating 1-Hour EMA (200-period)
The program:
- Takes the close prices from the 1-hour bars
- Calculates a 200-period Exponential Moving Average
- This gives a smooth trend line that filters out short-term noise
- Each 1-hour bar gets an EMA value assigned

### Step 10: Mapping 1-Hour Values to 10-Minute Bars
The program needs to know the 1-hour EMA value at each 10-minute bar:
- For each 10-minute bar, it looks up which hour it belongs to
- Assigns that hour's EMA value and close price to the 10-minute bar
- This way, every 10-minute bar knows:
  - The current 1-hour EMA value
  - The current 1-hour close price
  - Whether price is above or below the EMA (bullish or bearish)

### Step 11: Detecting EMA Crossovers
The program identifies when price crosses the EMA:
- Compares the previous hour's close to the previous hour's EMA
- Compares the current hour's close to the current hour's EMA
- If price was below EMA before and is now above, that's a **bullish cross**
- If price was above EMA before and is now below, that's a **bearish cross**
- These crossovers are marked on the 10-minute bars

### Step 12: Calculating Supertrend Indicator
For each 10-minute bar, the program calculates Supertrend:
- Calculates **ATR (Average True Range)** using the last 10 bars (configurable)
- Multiplies ATR by 3.0 (the multiplier, configurable)
- Calculates upper and lower bands based on the ATR
- Determines if the current bar is **bullish** (price above Supertrend line) or **bearish** (price below)
- Detects **flips** - when Supertrend changes from bullish to bearish or vice versa

### Step 13: Calculating ADX (Average Directional Index)
The program calculates ADX for entry confirmation:
- Calculates **Plus DI** and **Minus DI** (Directional Indicators) over 14 periods
- Calculates **ADX** (the strength of the trend) over 14 periods
- For each bar, checks if ADX is above 20 (the threshold)
- Tracks if ADX has been above 20 for 5 consecutive bars

### Step 14: EMA Confirmation Logic
The program determines if the EMA trend is "confirmed":
- If price is clearly above EMA (by more than 0.1% margin), it's **confirmed bullish**
- If price is clearly below EMA (by more than 0.1% margin), it's **confirmed bearish**
- If price was overlapping the EMA in the previous hour, it waits for the next 1-hour close to confirm direction
- This prevents false signals when price is right at the EMA line

---

## PHASE 4: RUNNING THE BACKTEST ENGINE

### Step 15: Initializing the Backtest Engine
The program creates a backtest engine with:
- **Initial capital**: $100,000 (starting account value)
- **Current equity**: Starts at $100,000
- **Position state**: No position (flat)
- **Trade counter**: Starts at 0
- **State tracking**: Remembers if we've already traded in a bullish or bearish trend

### Step 16: Processing Each Bar (The Main Loop)
The program goes through each 10-minute bar from January 1 to March 1, one by one:

**For each bar, it does the following:**

#### A. Update Supertrend State
- Checks if Supertrend flipped direction on this bar
- Updates internal flags that track whether we've already traded in the current trend
- These flags prevent entering multiple trades in the same Supertrend direction

#### B. Check Exit Conditions (If We Have a Position)
If we're currently in a trade (long or short), the program checks:

1. **Stop Loss Check:**
   - For a **long position**: Checks if the bar's **low** price touched or went below the stop loss level
   - For a **short position**: Checks if the bar's **high** price touched or went above the stop loss level
   - If hit, creates an exit signal with the stop loss price

2. **Take Profit Check:**
   - For a **long position**: Checks if the bar's **high** price touched or went above the take profit level
   - For a **short position**: Checks if the bar's **low** price touched or went below the take profit level
   - If hit, creates an exit signal with the take profit price

3. **Supertrend Flip Exit:**
   - For a **long position**: If Supertrend flips from bullish to bearish, exit the trade
   - For a **short position**: If Supertrend flips from bearish to bullish, exit the trade
   - Uses the bar's close price as the exit price

**If an exit is triggered:**
- Calculates the profit or loss in points (price difference)
- Converts points to dollars (points × $2 per point × number of contracts)
- Subtracts commission costs (entry + exit commissions)
- Applies slippage (assumes we get filled slightly worse than the signal price)
- Records the completed trade with all details
- Updates the account equity
- Resets position state to "flat"

#### C. Check Entry Conditions (If We're Flat)
If we don't have a position, the program checks for entry signals:

**The entry logic follows this priority:**

1. **EMA Cross Entry (Scenario A):**
   - If Supertrend flipped below the EMA (no ADX window was started)
   - AND price now crosses above the EMA on a 1-hour close
   - AND we haven't already traded in this bullish trend
   - → Enter LONG immediately (no ADX required)
   - Uses the confirmed 1-hour close price as entry price

2. **EMA Cross Entry (Scenario A for Shorts):**
   - If Supertrend flipped above the EMA (no ADX window was started)
   - AND price now crosses below the EMA on a 1-hour close
   - AND we haven't already traded in this bearish trend
   - → Enter SHORT immediately (no ADX required)

3. **Pending ADX Window (5-Candle Rule):**
   - If Supertrend flipped with EMA confirmed, but ADX was below 20
   - The program starts a "pending" state and watches the next 5 consecutive 10-minute bars
   - If ADX reaches 20 or above on any of those 5 bars → Enter the trade
   - If all 5 bars have ADX below 20 → Cancel the setup, wait for next Supertrend flip

4. **Supertrend Flip with ADX Already Above 20:**
   - If Supertrend flips bullish, EMA is confirmed, and ADX is already ≥ 20
   - AND we haven't already traded in this bullish trend
   - → Enter LONG immediately
   - Uses the bar's close price as entry price

5. **Supertrend Flip with ADX Already Above 20 (Shorts):**
   - Same logic but for bearish flips → Enter SHORT

**If an entry is triggered:**
- Applies slippage to the entry price (assumes we get filled slightly worse)
- Calculates stop loss and take profit levels:
  - **Long**: Stop Loss = Entry Price × (1 - 0.4%) = Entry Price × 0.996
  - **Long**: Take Profit = Entry Price × (1 + 1.2%) = Entry Price × 1.012
  - **Short**: Stop Loss = Entry Price × (1 + 0.4%) = Entry Price × 1.004
  - **Short**: Take Profit = Entry Price × (1 - 1.2%) = Entry Price × 0.988
- Records the entry in the state manager
- Sets flags to prevent re-entering in the same trend
- Logs the entry signal for later analysis

#### D. Track Equity Curve
Even if no trade happens on this bar, the program:
- Calculates unrealized profit/loss if we have an open position
- Updates the equity curve (account value over time)
- Records this equity value with the timestamp

### Step 17: Final Position Close
After processing all bars, if there's still an open position:
- The program closes it at the final bar's close price
- Calculates the final profit/loss
- Records it as a completed trade

---

## PHASE 5: CALCULATING PERFORMANCE METRICS

### Step 18: Trade Analysis
The program analyzes all completed trades:

**Basic Statistics:**
- **Total trades**: Count of all completed trades
- **Winning trades**: Count of trades with positive profit
- **Losing trades**: Count of trades with negative profit
- **Win rate**: (Winning trades / Total trades) × 100%

**Profit/Loss Metrics:**
- **Gross profit**: Sum of all winning trades' profits
- **Gross loss**: Sum of all losing trades' losses (as positive number)
- **Net profit**: Gross profit - Gross loss
- **Profit factor**: Gross profit / Gross loss (higher is better)

**Average Metrics:**
- **Average win**: Gross profit / Number of winning trades
- **Average loss**: Gross loss / Number of losing trades
- **Expectancy**: (Win rate × Average win) - (Loss rate × Average loss)

**Extreme Values:**
- **Largest win**: Highest profit from a single trade
- **Largest loss**: Largest loss from a single trade

### Step 19: Equity Curve Analysis
The program analyzes how the account value changed over time:

**Return Metrics:**
- **Final equity**: Account value at the end of the backtest
- **Total return**: Final equity - Initial capital ($100,000)
- **Total return %**: (Total return / Initial capital) × 100%

**Drawdown Analysis:**
- **Maximum drawdown**: Largest peak-to-trough decline in equity
- **Maximum drawdown %**: (Maximum drawdown / Peak equity) × 100%
- **Drawdown duration**: How long the account stayed in drawdown

**Risk-Adjusted Metrics:**
- **Sharpe ratio**: Measures return per unit of risk (higher is better)
- **Calmar ratio**: Total return % / Maximum drawdown % (higher is better)

### Step 20: Directional Breakdown
The program separates trades by direction:

**Long Trades:**
- Count, win rate, total profit/loss in dollars and points
- Profit factor for long trades only
- Maximum drawdown for long trades only

**Short Trades:**
- Same metrics but for short trades

### Step 21: Exit Type Breakdown
The program categorizes exits:
- **Take Profit exits**: How many trades hit the profit target
- **Stop Loss exits**: How many trades hit the stop loss
- **Supertrend Flip exits**: How many trades exited due to trend reversal

---

## PHASE 6: GENERATING THE CSV REPORT

### Step 22: Creating the Output File
The program:
- Creates a filename with timestamp: `backtest_20260306_024338.csv`
- Saves it in the `backtest/results` folder
- Opens the file for writing

### Step 23: Writing the Report Header
The program writes:
- A header line with equals signs for formatting
- Title: "MNQ SUPERTREND + EMA STRATEGY - BACKTEST REPORT"
- Another separator line

### Step 24: Writing Performance Summary Section
The program writes a section with:
- **Period**: "2026-01-01 to 2026-03-01"
- **Contract**: Which contract was used (Continuous or Stitched)
- **Contracts per Trade**: Number of contracts traded (e.g., 1)
- **Initial Capital**: $100,000.00
- **Final Equity**: The ending account value
- **Net Profit/Loss**: Total profit or loss in dollars
- **Total P&L Points**: Total profit/loss in price points
- **Total Profit Points**: Sum of all positive point gains
- **Total Loss Points**: Sum of all negative point losses
- **Total Return**: Percentage return
- **Max Drawdown**: Maximum drawdown percentage
- **Sharpe Ratio**: Risk-adjusted return metric

### Step 25: Writing Trade Statistics Section
The program writes:
- **Total Trades**: Count of all trades
- **Winning Trades**: Count of profitable trades
- **Losing Trades**: Count of losing trades
- **Win Rate**: Percentage of winning trades
- **Profit Factor**: Ratio of gross profit to gross loss
- **Expectancy per Trade**: Average expected profit per trade
- **Average Win**: Average profit from winning trades
- **Average Loss**: Average loss from losing trades
- **Largest Win**: Biggest single profit
- **Largest Loss**: Biggest single loss

### Step 26: Writing Long vs Short Breakdown Section
The program writes separate metrics for:
- **Long trades**: Count, wins, win rate, total P&L (dollars and points), profit factor, max drawdown %
- **Short trades**: Same metrics for short trades

### Step 27: Writing Exit Type Breakdown Section
The program writes:
- **Take Profit Exits**: Count of TP exits
- **Stop Loss Exits**: Count of SL exits
- **Supertrend Flip Exits**: Count of ST flip exits

### Step 28: Writing Strategy Settings Section
The program writes the configuration used:
- **Supertrend ATR Length**: 10 (periods)
- **Supertrend Multiplier**: 3
- **EMA Length (1H)**: 200 (periods)
- **Stop Loss %**: 0.4%
- **Take Profit %**: 1.2%

### Step 29: Writing P&L By Trade Section
The program writes a running total of profits:
- For each trade in order:
  - Trade number, direction (LONG/SHORT), exit type, profit/loss in dollars
  - Running total: Cumulative profit/loss up to that point
- Example:
  - Trade 1, LONG, take_profit, $240.00, Running: $240.00
  - Trade 2, SHORT, stop_loss, -$80.00, Running: $160.00
  - Trade 3, LONG, st_flip, $120.00, Running: $280.00

### Step 30: Writing All Trades Detail Section
The program writes a detailed table with columns:
- **trade_id**: Sequential trade number
- **direction**: "long" or "short"
- **entry_time**: Timestamp when trade was entered
- **entry_price**: Price at which position was opened
- **exit_time**: Timestamp when trade was closed
- **exit_price**: Price at which position was closed
- **signal_type**: What triggered the entry ("st_flip", "ema_cross", "st_flip_adx_window", etc.)
- **exit_type**: How the trade exited ("take_profit", "stop_loss", "st_flip")
- **pnl_points**: Profit/loss in price points
- **pnl_dollars**: Profit/loss in dollars
- **contracts**: Number of contracts traded

Each row represents one complete trade with all its details.

### Step 31: Writing Total Summary Row
At the end of the trades table, the program writes:
- A "TOTAL" row with:
  - Total net P&L points (sum of all pnl_points)
  - Total net P&L dollars (sum of all pnl_dollars)
  - Empty cells for other columns

### Step 32: Closing the File
The program:
- Closes the CSV file
- Prints a message to the console showing where the file was saved
- Displays a summary in the terminal

---

## PHASE 7: DISPLAYING RESULTS

### Step 33: Console Summary
The program prints a formatted summary to your terminal showing:
- Period, contract, number of contracts
- P&L summary (initial, final, net profit, return %, max drawdown)
- Trade statistics (total trades, win rate, profit factor, expectancy)
- Long vs Short breakdown
- File location

### Step 34: Program Completion
The program:
- Disconnects from IBKR
- Cleans up any temporary data
- Returns to the main menu or exits

---

## KEY CONCEPTS EXPLAINED

### What is "State Management"?
The program keeps track of:
- **Current position**: Are we long, short, or flat?
- **Entry price**: What price did we enter at?
- **Stop loss and take profit levels**: Where will we exit?
- **Trend flags**: Have we already traded in the current Supertrend direction? (Prevents multiple entries)
- **Pending states**: Are we waiting for ADX to reach 20 within 5 bars?

### What is "Slippage"?
When you place a market order in real trading, you might not get filled at exactly the signal price. Slippage simulates this:
- For entries: If signal is at $15,000, you might get filled at $15,000.25 (1 tick worse)
- For exits: Same concept - you get filled slightly worse than the signal price
- The backtest assumes 1 tick of slippage per side (entry + exit)

### What is "Commission"?
Every trade has costs:
- Entry commission: $0.62 per contract
- Exit commission: $0.62 per contract
- Total: $1.24 per contract per round trip
- These costs are subtracted from each trade's profit

### What is "Equity Curve"?
A line graph showing how your account value changes over time:
- Starts at $100,000
- Goes up when trades are profitable
- Goes down when trades lose money
- Shows drawdowns (declines from peaks)
- Used to calculate maximum drawdown and other risk metrics

### What is "Contract Stitching"?
Futures contracts expire. To get continuous data:
- Fetch data from multiple contracts (MNQH6, MNQM6, etc.)
- Combine them in chronological order
- Remove overlaps
- Create one seamless price series

---

## EXAMPLE WALKTHROUGH

Let's say on **January 15, 2026 at 10:50 AM**:

1. **Bar closes** at price $15,000
2. **Supertrend** flips from bearish to bullish
3. **EMA (1H)** shows price is above the 200-period EMA (confirmed bullish)
4. **ADX** is 25 (above 20 threshold)
5. **State check**: We haven't traded in this bullish trend yet

**Result**: Entry signal generated for LONG position

6. **Entry price**: $15,000 + slippage = $15,000.25 (assuming 1 tick slippage)
7. **Stop loss**: $15,000.25 × 0.996 = $14,940.25 (0.4% below entry)
8. **Take profit**: $15,000.25 × 1.012 = $15,180.25 (1.2% above entry)
9. **Position opened**: 1 contract long at $15,000.25

Then on **January 16, 2026 at 2:30 PM**:

1. **Bar closes** at price $15,190
2. **Bar's high** was $15,185 (didn't quite hit take profit)
3. **Supertrend** is still bullish
4. **No exit triggered** - position stays open

Then on **January 16, 2026 at 2:40 PM**:

1. **Bar closes** at price $15,195
2. **Bar's high** was $15,182 (still didn't hit take profit)
3. **Supertrend** is still bullish
4. **No exit triggered** - position stays open

Then on **January 16, 2026 at 2:50 PM**:

1. **Bar closes** at price $15,200
2. **Bar's high** was $15,185
3. **Wait - check the bar's high**: $15,185 ≥ $15,180.25 (take profit level)
4. **Take profit hit!** Exit signal generated

5. **Exit price**: $15,180.25 (the take profit level)
6. **Profit calculation**:
   - Points: $15,180.25 - $15,000.25 = 180.00 points
   - Dollars: 180.00 × $2 per point × 1 contract = $360.00
   - Commission: $1.24 (entry + exit)
   - Net profit: $360.00 - $1.24 = $358.76
7. **Trade recorded**: Trade #1, LONG, take_profit exit, $358.76 profit
8. **Equity updated**: $100,000 + $358.76 = $100,358.76
9. **Position closed**: Back to flat

This process repeats for every bar from January 1 to March 1, checking for entries and exits, calculating profits and losses, and building up the complete trade history that gets written to the CSV file.

---

## SUMMARY

The entire backtest process:
1. **Connects** to IBKR
2. **Fetches** historical price data for your date range
3. **Calculates** all technical indicators (Supertrend, EMA, ADX)
4. **Simulates** trading by processing each bar:
   - Checks for exit conditions if in a position
   - Checks for entry conditions if flat
   - Tracks profits, losses, and account equity
5. **Calculates** performance metrics (win rate, profit factor, drawdown, etc.)
6. **Generates** a comprehensive CSV report with:
   - Summary statistics
   - Trade-by-trade breakdown
   - Performance metrics
   - Strategy settings used

The CSV file contains everything you need to analyze the strategy's performance over the January 1 to March 1, 2026 period.
