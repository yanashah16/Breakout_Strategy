"""
The two copies of the stock box strategy (2026-11-18 study, nq_wae_backtest/box, 20 stocks, 2016-11 -> 2026-10).

Each copy holds its own position per stock. Together they are the version the user picked:
    10m + 15m copy: +$5.07M, closed-trade DD $130k, net/DD 38.9 (2016-21 20.0 / 2022-26 20.6), 403 entries/stock
    ($1,000 risk per trade in each copy, 1 bps slippage + 0.5 bps commission).
TradingView: nq_wae_backtest/box/pine/box_stocks.pine (10m) and box_stocks_15m.pine (15m).
"""
from __future__ import annotations

STOCKS = ["NVDA", "AAPL", "TSLA", "META", "AVGO", "JPM", "AMZN", "GOOGL", "MSFT", "BRK.B",
          "LLY", "WMT", "ORCL", "V", "NFLX", "MA", "XOM", "COST", "JNJ", "HD"]

_COMMON = {
    "box_bars": 20, "max_box_atr": 10, "break_buffer": 1.4, "atr_len": 14, "direction": "Both",
    "reverse": True, "reverse_bars": 10, "sl_box_pct": -1.5, "tp1_mult": 7, "tp3_mult": 80, "trail_pct": 75,
    "short_tp1_mult": 3, "short_tp3_mult": 10, "short_trail_pct": 50, "qty1": 1, "qty2": 1, "qty3": 4,
    "skip_hours": "15:30-16:00", "min_squeeze": 0.9, "max_trend_er": 0.37, "er_days": 20,
    "atrp_min": 1.95, "atrp_len": 100, "be_mult": 2.5, "be_lock_r": 0.1,
    "size_atr_floor": 1, "tvol_days": 20, "vq_t1": 2, "vq_m1": 1.5, "vq_t2": 3, "vq_m2": 2,
    "stall_age_h": 120, "stall_hours": 6, "stall_pct": 1.0, "stall_max_r": 1,
}

COPIES = {
    "box10": {
        "name": "Stock Box Breakout 10m",
        "summary": "Box breakout on a 10-minute chart, ATR stop, three exits; holds winners for days or weeks.",
        "params": {**_COMMON, "timeframe": 10, "ema_len": 200, "leg2_ema_len": 200, "box_atr_len": 150,
                   "short_leg2_ema_len": 30},
        "backtest": "20 stocks alone: +$2.67M, DD $74k, net/DD 36.0, 242 trades per stock in 10 years",
    },
    "box15": {
        "name": "Stock Box Breakout 15m",
        "summary": "The same rules on a 15-minute chart; takes the breakouts the 10m copy misses while it is in a trade.",
        "params": {**_COMMON, "timeframe": 15, "ema_len": 133, "leg2_ema_len": 133, "box_atr_len": 100,
                   "short_leg2_ema_len": 20},
        "backtest": "20 stocks alone: +$2.40M, DD $76k, net/DD 31.5, 178 trades per stock in 10 years",
    },
    # Pole & flag (2026-11-20 study, nq_wae_backtest/patterns/flag_stocks_10m, 19 stocks, no BRK.B)
    "flag10": {
        "name": "Stock Pole & Flag 10m",
        "kind": "flag",
        "summary": "Pole & flag on a 10-minute chart with the 60-minute trend; TP1 at 3 R, runner trails 6 R; holds for days.",
        "params": {"timeframe": 10, "direction": "Both", "atr_len": 14, "trade_hours": "10:00-15:00",
                   "trend_tf": 60, "trend_ema": 50, "trend_slope": 1,
                   "pole_bars": 7, "pole_atr": 2.0, "flag_min_bars": 1, "flag_max_bars": 15, "max_retrace": 0.7,
                   "flag_max_pct": 1.0, "flag_slope_atr": 1.0, "pole_min_datr": 0.35,
                   "short_pole_bars": 8, "short_pole_atr": 4.0, "short_pole_min_datr": 1.0, "short_pole_min_bars": 6,
                   "short_flag_min_bars": 5, "short_flag_max_bars": 15, "short_max_retrace": 0.4,
                   "short_flag_max_pct": 0.5, "short_flag_slope_atr": 5.0, "short_htf_adx_tf": 60,
                   "short_htf_adx_len": 14, "short_htf_adx_min": 0, "short_htf_adx_max": 20,
                   "sl_atr": 0.5, "tp_r": 3.0, "trail_r": 6.0, "be_after_tp1": True,
                   "short_sl_atr": 0.15, "short_tp_r": 1.5, "short_trail_r": 2.0, "qty1": 1, "qty2": 2},
        "backtest": ("19 stocks: +$2.67M, DD $95k, net/DD 28.1 (2016-21 13.8 / 2022-26 14.8), ~400 trades per stock "
                     "in 10 years, win rate 29 %, all 19 stocks positive"),
        "stocks": ["AAPL", "AMZN", "AVGO", "COST", "GOOGL", "HD", "JNJ", "JPM", "LLY", "MA", "META", "MSFT", "NFLX",
                   "NVDA", "ORCL", "TSLA", "V", "WMT", "XOM"],
        "legs": "1 : 2",
        "how_it_enters": ("Looks for a pole: a fast move of at least 2 x ATR within 7 candles that is also at least 35 % "
                          "of the stock's daily range. Then a flag: 1-15 quiet candles that give back at most 70 % of the "
                          "pole and do not drift up by more than 1 ATR. A buy stop sits 1 cent above the flag high for the "
                          "next candle only - only when the 60-minute candles close above a rising EMA 50 and only for "
                          "entries between 10:00 and 15:00. One trade per pole. Shorts are rare and strict: a sell-off "
                          "of at least 1 daily range over 6+ candles while the 60-minute ADX is still at most 20."),
        "how_it_exits": ("Stop 0.5 x ATR below the flag low. Leg 1 (1/3) takes profit at 3 R; then the runner's stop moves "
                         "to the entry and trails 6 R behind the best price (shorts: stop 0.15 ATR, target 1.5 R, trail "
                         "2 R). No fixed final target and no closing at the end of the day - positions are held "
                         "overnight, winners often for days or weeks."),
    },
}
for _c in COPIES.values():
    _c.setdefault("kind", "box")
    _c.setdefault("stocks", STOCKS)
    _c.setdefault("legs", "1 : 1 : 4")

HOW_IT_ENTERS = ("Draws a box around the last 20 candles. When a candle closes more than 1.4 x ATR above the box "
                 "(below for a short) it enters at that close - only with the 200-bar EMA trend, only on stocks that "
                 "move enough (daily ATR >= 1.95 % of the price), only when volatility is expanding and the daily "
                 "trend is not already stretched. No new trades after 15:30. A breakout that fails within 10 candles "
                 "turns around at its stop.")
HOW_IT_EXITS = ("Stop 1.5 x ATR. Leg 1 (1/6) takes profit at 7 R. Leg 2 (1/6) leaves when a candle closes back "
                "through the trend EMA after target 1. Leg 3 (4/6) trails, giving back 75 % of its best profit "
                "(shorts: target 1 at 3 R, final target 10 R, trail 50 %). At 2.5 R before target 1 the stop moves to "
                "entry + 0.1 R. A trade older than 120 hours, less than 1 R in profit, whose last 6 hours stayed "
                "inside 1 % closes. Strong-volume breakouts trade legs 2 + 3 at 1.5x / 2x. Positions are held overnight.")


for _k in ("box10", "box15"):
    COPIES[_k].setdefault("how_it_enters", HOW_IT_ENTERS)
    COPIES[_k].setdefault("how_it_exits", HOW_IT_EXITS)
ALL_STOCKS = list(dict.fromkeys(s for c in COPIES.values() for s in c["stocks"]))


def ib_symbol(sym: str) -> str:
    """IBKR spells class shares with a space: BRK.B -> BRK B."""
    return sym.replace(".", " ")
