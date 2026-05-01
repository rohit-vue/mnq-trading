"""
=============================================================================
BACKTEST METRICS
=============================================================================
Performance metrics calculation for backtest results.

Metrics:
- Win rate
- Profit factor
- Expectancy
- Max drawdown
- Sharpe ratio
- R-multiple distribution
=============================================================================
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

_TRADE_DETAIL_HEADER = (
    "trade_id,direction,entry_time,entry_price,exit_time,exit_price,signal_type,exit_type,"
    "pnl_points,pnl_dollars,contracts,ema_1h_at_entry,volume_at_entry,volume_ma_at_entry,"
    "max_positive_points,max_negative_points,max_positive_pct,max_negative_pct"
)


def _fmt_trade_csv_num(val) -> str:
    """Format optional numeric for ALL TRADES DETAIL CSV."""
    if val is None:
        return ""
    try:
        if isinstance(val, float) and pd.isna(val):
            return ""
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return ""


def _trade_detail_csv_row(trade) -> str:
    """One CSV line for ALL TRADES DETAIL (matches _TRADE_DETAIL_HEADER)."""
    ema_str = _fmt_trade_csv_num(getattr(trade, "ema_1h_at_entry", None))
    entry_px = _fmt_trade_csv_num(getattr(trade, "entry_price", None))
    exit_px = _fmt_trade_csv_num(getattr(trade, "exit_price", None))
    pnl_pts = _fmt_trade_csv_num(getattr(trade, "pnl_points", None))
    pnl_usd = _fmt_trade_csv_num(getattr(trade, "pnl_dollars", None))
    return (
        f"{trade.trade_id},{trade.direction},{trade.entry_time},{entry_px},"
        f"{trade.exit_time},{exit_px},{getattr(trade, 'entry_trigger', '') or ''},{trade.exit_type},"
        f"{pnl_pts},{pnl_usd},{trade.contracts},{ema_str},"
        f"{_fmt_trade_csv_num(getattr(trade, 'volume_at_entry', None))},"
        f"{_fmt_trade_csv_num(getattr(trade, 'volume_ma_at_entry', None))},"
        f"{_fmt_trade_csv_num(getattr(trade, 'max_positive_points', None))},"
        f"{_fmt_trade_csv_num(getattr(trade, 'max_negative_points', None))},"
        f"{_fmt_trade_csv_num(getattr(trade, 'max_positive_pct', None))},"
        f"{_fmt_trade_csv_num(getattr(trade, 'max_negative_pct', None))}"
    )


def _trade_detail_total_row(total_pnl_points: float, net_profit: float) -> str:
    """TOTAL summary row aligned with _TRADE_DETAIL_HEADER (empty non-total fields)."""
    return (
        f"TOTAL,,,,,,,,{total_pnl_points:.2f},{net_profit:.2f},,,,,,,,"
    )


def calculate_metrics(
    trades: List,
    equity_curve: pd.Series,
    initial_capital: float,
    multiplier: int = 2
) -> Dict[str, Any]:
    """
    Calculate comprehensive backtest metrics.
    
    Parameters:
    -----------
    trades : List[Trade]
        List of completed trades
    equity_curve : pd.Series
        Equity values over time
    initial_capital : float
        Starting capital
    multiplier : int
        Contract multiplier
    
    Returns:
    --------
    Dict[str, Any]
        Dictionary of performance metrics
    """
    metrics = {}
    
    if not trades:
        logger.warning("No trades to analyze")
        return _empty_metrics()
    
    # Basic counts
    trade_count = len(trades)
    winners = [t for t in trades if t.pnl_dollars and t.pnl_dollars > 0]
    losers = [t for t in trades if t.pnl_dollars and t.pnl_dollars < 0]
    breakeven = [t for t in trades if t.pnl_dollars and t.pnl_dollars == 0]
    
    metrics['total_trades'] = trade_count
    metrics['winning_trades'] = len(winners)
    metrics['losing_trades'] = len(losers)
    metrics['breakeven_trades'] = len(breakeven)
    
    # Win rate
    metrics['win_rate'] = (len(winners) / trade_count) * 100 if trade_count > 0 else 0
    
    # P&L metrics
    gross_profit = sum(t.pnl_dollars for t in winners) if winners else 0
    gross_loss = abs(sum(t.pnl_dollars for t in losers)) if losers else 0
    net_profit = gross_profit - gross_loss
    
    metrics['gross_profit'] = gross_profit
    metrics['gross_loss'] = gross_loss
    metrics['net_profit'] = net_profit
    
    # Profit factor
    if gross_loss > 0:
        metrics['profit_factor'] = gross_profit / gross_loss
    else:
        metrics['profit_factor'] = float('inf') if gross_profit > 0 else 0
    
    # Average trade metrics
    metrics['avg_win'] = gross_profit / len(winners) if winners else 0
    metrics['avg_loss'] = gross_loss / len(losers) if losers else 0
    metrics['avg_trade'] = net_profit / trade_count if trade_count > 0 else 0
    
    # Largest trades
    metrics['largest_win'] = max(t.pnl_dollars for t in winners) if winners else 0
    metrics['largest_loss'] = min(t.pnl_dollars for t in losers) if losers else 0
    
    # Expectancy (average R-multiple if using fixed risk)
    # Expectancy = (Win% × Avg Win) - (Loss% × Avg Loss)
    win_pct = len(winners) / trade_count if trade_count > 0 else 0
    loss_pct = len(losers) / trade_count if trade_count > 0 else 0
    metrics['expectancy'] = (win_pct * metrics['avg_win']) - (loss_pct * metrics['avg_loss'])
    
    # Consecutive wins/losses
    metrics['max_consecutive_wins'] = _max_consecutive(trades, winning=True)
    metrics['max_consecutive_losses'] = _max_consecutive(trades, winning=False)
    
    # Equity curve metrics
    if len(equity_curve) > 0:
        # Final return
        final_equity = equity_curve.iloc[-1]
        metrics['final_equity'] = final_equity
        metrics['total_return'] = final_equity - initial_capital
        metrics['total_return_pct'] = ((final_equity - initial_capital) / initial_capital) * 100
        
        # Drawdown
        dd_info = _calculate_drawdown(equity_curve)
        metrics['max_drawdown'] = dd_info['max_drawdown']
        metrics['max_drawdown_pct'] = dd_info['max_drawdown_pct']
        metrics['max_drawdown_duration'] = dd_info['max_duration']
        
        # Sharpe ratio (simplified, using daily returns)
        returns = equity_curve.pct_change().dropna()
        if len(returns) > 1:
            daily_returns = returns.resample('D').sum()
            if len(daily_returns) > 0 and daily_returns.std() > 0:
                # Annualized Sharpe (assuming ~252 trading days)
                metrics['sharpe_ratio'] = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
            else:
                metrics['sharpe_ratio'] = 0
        else:
            metrics['sharpe_ratio'] = 0
        
        # Calmar ratio (return / max drawdown)
        if metrics['max_drawdown_pct'] > 0:
            metrics['calmar_ratio'] = metrics['total_return_pct'] / metrics['max_drawdown_pct']
        else:
            metrics['calmar_ratio'] = float('inf') if metrics['total_return_pct'] > 0 else 0
    
    # By direction
    longs = [t for t in trades if t.direction == 'long']
    shorts = [t for t in trades if t.direction == 'short']
    
    # Calculate wins for each direction
    long_winners = [t for t in longs if t.pnl_dollars and t.pnl_dollars > 0]
    short_winners = [t for t in shorts if t.pnl_dollars and t.pnl_dollars > 0]
    
    metrics['long_trades'] = len(longs)
    metrics['short_trades'] = len(shorts)
    metrics['long_wins'] = len(long_winners)
    metrics['short_wins'] = len(short_winners)
    metrics['long_win_rate'] = _win_rate(longs)
    metrics['short_win_rate'] = _win_rate(shorts)
    metrics['long_pnl'] = sum(t.pnl_dollars for t in longs if t.pnl_dollars)
    metrics['short_pnl'] = sum(t.pnl_dollars for t in shorts if t.pnl_dollars)
    
    # Long/Short P&L in points (for consolidated report)
    long_points = [t.pnl_points for t in longs if t.pnl_points is not None]
    short_points = [t.pnl_points for t in shorts if t.pnl_points is not None]
    metrics['long_pnl_points'] = sum(long_points) if long_points else 0
    metrics['short_pnl_points'] = sum(short_points) if short_points else 0
    
    # Long/Short profit factor and max drawdown %
    long_gross_profit = sum(t.pnl_dollars for t in long_winners) if long_winners else 0
    long_gross_loss = abs(sum(t.pnl_dollars for t in longs if t.pnl_dollars and t.pnl_dollars < 0))
    metrics['long_profit_factor'] = (long_gross_profit / long_gross_loss) if long_gross_loss > 0 else (float('inf') if long_gross_profit > 0 else 0)
    short_gross_profit = sum(t.pnl_dollars for t in short_winners) if short_winners else 0
    short_gross_loss = abs(sum(t.pnl_dollars for t in shorts if t.pnl_dollars and t.pnl_dollars < 0))
    metrics['short_profit_factor'] = (short_gross_profit / short_gross_loss) if short_gross_loss > 0 else (float('inf') if short_gross_profit > 0 else 0)
    
    # Long/Short max drawdown % (from equity curve of only that direction's closes)
    metrics['long_max_drawdown_pct'] = _direction_drawdown_pct(equity_curve, trades, 'long', initial_capital)
    metrics['short_max_drawdown_pct'] = _direction_drawdown_pct(equity_curve, trades, 'short', initial_capital)
    
    # By exit type
    tp_exits = [t for t in trades if t.exit_type == 'take_profit']
    sl_exits = [t for t in trades if t.exit_type == 'stop_loss']
    st_exits = [t for t in trades if t.exit_type == 'st_flip']
    
    metrics['tp_exits'] = len(tp_exits)
    metrics['sl_exits'] = len(sl_exits)
    metrics['st_flip_exits'] = len(st_exits)
    
    # Points metrics (total_net_points = sum of all trade pnl_points for backtest timeframe)
    all_points = [t.pnl_points for t in trades if t.pnl_points is not None]
    metrics['total_points'] = sum(all_points) if all_points else 0.0
    profit_points = [p for p in all_points if p > 0]
    loss_points = [p for p in all_points if p < 0]
    metrics['total_profit_points'] = sum(profit_points) if profit_points else 0.0
    metrics['total_loss_points'] = sum(loss_points) if loss_points else 0.0
    if all_points:
        metrics['avg_points_per_trade'] = np.mean(all_points)
        metrics['points_std'] = np.std(all_points)
    
    # Risk/Reward analysis
    if metrics['avg_loss'] > 0:
        metrics['avg_rr_ratio'] = metrics['avg_win'] / metrics['avg_loss']
    else:
        metrics['avg_rr_ratio'] = float('inf')
    
    return metrics


def _empty_metrics() -> Dict[str, Any]:
    """Return empty metrics structure."""
    return {
        'total_trades': 0,
        'winning_trades': 0,
        'losing_trades': 0,
        'win_rate': 0.0,
        'net_profit': 0.0,
        'profit_factor': 0.0,
        'expectancy': 0.0,
        'max_drawdown': 0.0,
        'max_drawdown_pct': 0.0,
        'sharpe_ratio': 0.0
    }


def _win_rate(trades: List) -> float:
    """Calculate win rate for a list of trades."""
    if not trades:
        return 0.0
    winners = [t for t in trades if t.pnl_dollars and t.pnl_dollars > 0]
    return (len(winners) / len(trades)) * 100


def _direction_drawdown_pct(
    equity_curve: pd.Series,
    trades: List,
    direction: str,
    initial_capital: float
) -> float:
    """
    Max drawdown % for a synthetic equity curve that only updates when
    a trade of the given direction closes. Used for long/short breakdown.
    """
    if len(equity_curve) == 0:
        return 0.0
    dir_trades = [t for t in trades if t.direction == direction and t.exit_time is not None and t.pnl_dollars is not None]
    if not dir_trades:
        return 0.0
    dir_trades = sorted(dir_trades, key=lambda t: t.exit_time)
    # Build synthetic equity: start at initial_capital, add P&L at each exit time
    eq = initial_capital
    running = [eq]
    index = [equity_curve.index[0]]
    for t in dir_trades:
        eq += t.pnl_dollars
        running.append(eq)
        index.append(t.exit_time)
    if len(running) < 2:
        return 0.0
    series = pd.Series(running, index=pd.DatetimeIndex(index))
    dd_info = _calculate_drawdown(series)
    return dd_info['max_drawdown_pct']


def _max_consecutive(trades: List, winning: bool = True) -> int:
    """Calculate maximum consecutive wins or losses."""
    max_streak = 0
    current_streak = 0
    
    for trade in trades:
        if trade.pnl_dollars is None:
            continue
            
        is_winner = trade.pnl_dollars > 0
        
        if (winning and is_winner) or (not winning and not is_winner):
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    
    return max_streak


def _calculate_drawdown(equity_curve: pd.Series) -> Dict[str, Any]:
    """
    Calculate maximum drawdown and duration.
    
    Parameters:
    -----------
    equity_curve : pd.Series
        Equity values over time
    
    Returns:
    --------
    Dict
        Drawdown metrics
    """
    if len(equity_curve) == 0:
        return {'max_drawdown': 0, 'max_drawdown_pct': 0, 'max_duration': 0}
    
    # Running maximum
    running_max = equity_curve.expanding().max()
    
    # Drawdown
    drawdown = running_max - equity_curve
    drawdown_pct = (drawdown / running_max) * 100
    
    max_dd = drawdown.max()
    max_dd_pct = drawdown_pct.max()
    
    # Find duration of max drawdown period
    # Find where we're in a drawdown
    in_drawdown = drawdown > 0
    
    max_duration = 0
    current_duration = 0
    
    for i, is_dd in enumerate(in_drawdown):
        if is_dd:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
        else:
            current_duration = 0
    
    return {
        'max_drawdown': max_dd,
        'max_drawdown_pct': max_dd_pct,
        'max_duration': max_duration
    }


def generate_report(
    result: 'BacktestResult',
    output_path: Optional[str] = None,
    console_only: bool = False
) -> str:
    """
    Generate human-readable backtest report in IBKR format.
    
    Parameters:
    -----------
    result : BacktestResult
        Backtest results
    output_path : str, optional
        Path to save report
    console_only : bool
        If True, return only summary (no trades list) for terminal display
    
    Returns:
    --------
    str
        Formatted report text
    """
    metrics = result.metrics
    config = result.config
    trades = result.trades
    
    report = []
    report.append("=" * 50)
    report.append("MNQ SUPERTREND + EMA STRATEGY - BACKTEST REPORT")
    report.append("=" * 50)
    report.append("")
    
    # Performance Summary
    report.append("PERFORMANCE SUMMARY")
    report.append("-" * 30)
    report.append(f"Period,{config.start_date.strftime('%Y-%m-%d')} to {config.end_date.strftime('%Y-%m-%d')}")
    report.append(f"Contract,MNQ (Stitched Continuous)")
    report.append(f"Contracts per Trade,{config.contracts}")
    report.append(f"Initial Capital,${config.initial_capital:.2f}")
    report.append(f"Final Equity,${metrics.get('final_equity', 0):,.2f}")
    report.append(f"Net Profit/Loss,${metrics.get('net_profit', 0):,.2f}")
    
    # Calculate total P&L points and profit/loss points separately
    total_pnl_points = sum(t.pnl_points for t in trades) if trades else 0
    total_profit_pts = sum(t.pnl_points for t in trades if t.pnl_points and t.pnl_points > 0)
    total_loss_pts = sum(t.pnl_points for t in trades if t.pnl_points and t.pnl_points < 0)
    report.append(f"Total P&L Points,{total_pnl_points:.2f}")
    report.append(f"Total Profit Points,{total_profit_pts:.2f}")
    report.append(f"Total Loss Points,{total_loss_pts:.2f}")
    report.append(f"Total Return,{metrics.get('total_return_pct', 0):.2f}%")
    report.append(f"Max Drawdown,{metrics.get('max_drawdown_pct', 0):.2f}%")
    report.append(f"Sharpe Ratio,{metrics.get('sharpe_ratio', 0):.2f}")
    report.append("")
    
    # Trade Statistics
    report.append("TRADE STATISTICS")
    report.append("-" * 30)
    report.append(f"Total Trades,{metrics.get('total_trades', 0)}")
    report.append(f"Winning Trades,{metrics.get('winning_trades', 0)}")
    report.append(f"Losing Trades,{metrics.get('losing_trades', 0)}")
    report.append(f"Win Rate,{metrics.get('win_rate', 0):.1f}%")
    report.append(f"Profit Factor,{metrics.get('profit_factor', 0):.2f}")
    report.append(f"Expectancy per Trade,${metrics.get('expectancy', 0):.2f}")
    report.append(f"Average Win,${metrics.get('avg_win', 0):.2f}")
    report.append(f"Average Loss,${metrics.get('avg_loss', 0):.2f}")
    report.append(f"Largest Win,${metrics.get('largest_win', 0):.2f}")
    report.append(f"Largest Loss,${metrics.get('largest_loss', 0):.2f}")
    report.append("")
    
    # Long vs Short Breakdown (full Milestone report: DD %, PF, Win Rate, P&L points & value)
    report.append("LONG vs SHORT BREAKDOWN")
    report.append("-" * 30)
    report.append(f"Long Trades,{metrics.get('long_trades', 0)}")
    report.append(f"Long Wins,{metrics.get('long_wins', 0)}")
    report.append(f"Long Win Rate,{metrics.get('long_win_rate', 0):.1f}%")
    report.append(f"Long P&L (value),${metrics.get('long_pnl', 0):,.2f}")
    report.append(f"Long P&L (points),{metrics.get('long_pnl_points', 0):.2f}")
    report.append(f"Long Profit Factor,{metrics.get('long_profit_factor', 0):.2f}")
    report.append(f"Long Max Drawdown (%),{metrics.get('long_max_drawdown_pct', 0):.2f}%")
    report.append(f"Short Trades,{metrics.get('short_trades', 0)}")
    report.append(f"Short Wins,{metrics.get('short_wins', 0)}")
    report.append(f"Short Win Rate,{metrics.get('short_win_rate', 0):.1f}%")
    report.append(f"Short P&L (value),${metrics.get('short_pnl', 0):,.2f}")
    report.append(f"Short P&L (points),{metrics.get('short_pnl_points', 0):.2f}")
    report.append(f"Short Profit Factor,{metrics.get('short_profit_factor', 0):.2f}")
    report.append(f"Short Max Drawdown (%),{metrics.get('short_max_drawdown_pct', 0):.2f}%")
    report.append("")
    
    # Exit Type Breakdown
    report.append("EXIT TYPE BREAKDOWN")
    report.append("-" * 30)
    report.append(f"Take Profit Exits,{metrics.get('tp_exits', 0)}")
    report.append(f"Stop Loss Exits,{metrics.get('sl_exits', 0)}")
    report.append(f"Supertrend Flip Exits,{metrics.get('st_flip_exits', 0)}")
    report.append("")
    
    # Strategy Settings
    report.append("STRATEGY SETTINGS")
    report.append("-" * 30)
    report.append(f"Primary Timeframe,{config.primary_timeframe}")
    report.append(f"Supertrend ATR Long Entry,{config.supertrend_atr_long}")
    report.append(f"Supertrend Mult Long Entry,{config.supertrend_mult_long}")
    report.append(f"Supertrend ATR Long Exit,{config.supertrend_atr_long_exit}")
    report.append(f"Supertrend Mult Long Exit,{config.supertrend_mult_long_exit}")
    report.append(f"Supertrend ATR Short Entry,{config.supertrend_atr_short}")
    report.append(f"Supertrend Mult Short Entry,{config.supertrend_mult_short}")
    report.append(f"Supertrend ATR Short Exit,{config.supertrend_atr_short_exit}")
    report.append(f"Supertrend Mult Short Exit,{config.supertrend_mult_short_exit}")
    report.append(f"EMA Length (1H),{config.ema_length}")
    report.append(f"Stop Loss % Long,{config.sl_pct_long}%")
    report.append(f"Stop Loss % Short,{config.sl_pct_short}%")
    report.append(f"Take Profit % Long,{config.tp_pct_long}%")
    report.append(f"Take Profit % Short,{config.tp_pct_short}%")
    report.append("")
    
    # Build summary for console
    summary_text = "\n".join(report)
    
    # Only add trade details if not console_only
    if not console_only:
        # P&L BY TRADE with running total
        report.append("P&L BY TRADE")
        report.append("-" * 30)
        running_pnl = 0.0
        for trade in trades:
            running_pnl += trade.pnl_dollars
            direction = trade.direction.upper()
            exit_type = trade.exit_type
            pnl = trade.pnl_dollars
            report.append(f"Trade {trade.trade_id},{direction},{exit_type},${pnl:,.2f},Running: ${running_pnl:,.2f}")
        report.append("")
        
        # ALL TRADES DETAIL
        report.append("ALL TRADES DETAIL")
        report.append("-" * 30)
        report.append(_TRADE_DETAIL_HEADER)
        for trade in trades:
            report.append(_trade_detail_csv_row(trade))
        # Total net P&L points and dollars for the backtest timeframe
        report.append(_trade_detail_total_row(total_pnl_points, float(metrics.get('net_profit', 0))))
    
    report_text = "\n".join(report)
    
    # Save if path provided (always save full report to file)
    if output_path:
        # Build full report for file
        full_report = report.copy() if not console_only else report.copy()
        if console_only:
            # Add trade details for file even if console_only
            full_report.append("P&L BY TRADE")
            full_report.append("-" * 30)
            running_pnl = 0.0
            for trade in trades:
                running_pnl += trade.pnl_dollars
                direction = trade.direction.upper()
                exit_type = trade.exit_type
                pnl = trade.pnl_dollars
                full_report.append(f"Trade {trade.trade_id},{direction},{exit_type},${pnl:,.2f},Running: ${running_pnl:,.2f}")
            full_report.append("")
            full_report.append("ALL TRADES DETAIL")
            full_report.append("-" * 30)
            full_report.append(_TRADE_DETAIL_HEADER)
            for trade in trades:
                full_report.append(_trade_detail_csv_row(trade))
            full_report.append(_trade_detail_total_row(total_pnl_points, float(metrics.get('net_profit', 0))))
            report_text_file = "\n".join(full_report)
        else:
            report_text_file = report_text
        
        with open(output_path, 'w') as f:
            f.write(report_text_file)
        logger.info(f"Report saved to {output_path}")
    
    # Return summary for console or full report
    return summary_text if console_only else report_text


def export_trades_csv(trades: List, output_path: str) -> None:
    """
    Export trades to CSV file.
    
    Parameters:
    -----------
    trades : List[Trade]
        List of trades
    output_path : str
        Output file path
    """
    data = []
    for trade in trades:
        data.append({
            'trade_id': trade.trade_id,
            'direction': trade.direction,
            'entry_time': trade.entry_time,
            'entry_price': trade.entry_price,
            'exit_time': trade.exit_time,
            'exit_price': trade.exit_price,
            'signal_type': getattr(trade, 'entry_trigger', None) or '',
            'exit_type': trade.exit_type,
            'pnl_points': trade.pnl_points,
            'pnl_dollars': trade.pnl_dollars,
            'contracts': trade.contracts,
            'ema_1h_at_entry': getattr(trade, 'ema_1h_at_entry', None),
            'volume_at_entry': getattr(trade, 'volume_at_entry', None),
            'volume_ma_at_entry': getattr(trade, 'volume_ma_at_entry', None),
            'max_positive_points': getattr(trade, 'max_positive_points', None),
            'max_negative_points': getattr(trade, 'max_negative_points', None),
            'max_positive_pct': getattr(trade, 'max_positive_pct', None),
            'max_negative_pct': getattr(trade, 'max_negative_pct', None),
        })
    
    df = pd.DataFrame(data)
    df.to_csv(output_path, index=False, float_format="%.2f")
    logger.info(f"Trades exported to {output_path}")
