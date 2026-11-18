"""Trend-continuation chart patterns: 1) pullback in trend  2) pole & flag  3) trend-line break.

    python patterns/patterns_backtest.py patterns/pullback_params.yaml
    python patterns/patterns_backtest.py patterns/flag_params.yaml
    python patterns/patterns_backtest.py trendline/trendline_params.yaml   (trendline files live in bt/trendline/)

data: instrument can be ES, NQ (any root in data/<ROOT>_1min.parquet) or a list [ES, NQ] = one run per market.
Any value under `strategy:` written as a list -> every combination is tested
(results/patterns_sweep_<setup>_<MKT>_<time>.csv; prints top 5 by net profit and top 5 by net / max DD).
Single run -> summary, yearly table, trades CSV and `plot: N` PNG charts of the last N patterns that were traded
(results/patterns_<setup>_<MKT>_<time>/), so you can SEE how well each pattern is detected.

Everything is decided on CLOSED chart bars (no look-ahead); fills are walked on the 1-minute bars.

Common rules (both directions, `direction` Long only | Short only | Both):
  * trend filter (higher timeframe): trend_tf-minute candles, last COMPLETED candle at the chart bar's close:
    long only when its close > EMA(trend_ema) and that EMA is rising vs trend_slope candles ago (short: mirror).
    trend_ema null = no higher-timeframe filter.
  * one position at a time, 1 contract x qty; no entries on bars opening 16:45-18:00 New York or from flat_time;
  * exits: stop (pattern based, see below); 2 legs: leg 1 = qty1 contracts, takes profit at TP1 = tp_r x R
    (R = entry - stop); leg 2 = qty2 contracts (0 = single-target trade), the runner:
      be_after_tp1 true -> once TP1 fills, the runner's stop moves to the entry + tp1_lock_r x R (0 = the entry)
                    + tp1_lock_atr x ATR (e.g. tp1_lock_r 0 + tp1_lock_atr 0.2 = BE + 0.2 ATR);
      trail true -> the runner's stop trails trail_r R behind the best price since the entry, starting when TP1
                    fills (trail_start_r null) or once the trade is trail_start_r R in profit (0 = from the entry);
                    it only moves in the trade's favour;
      tp2_r -> fixed target for the runner (null = none: it leaves on its stop / trail / flat time / max_bars).
    be_r: whole position to break-even once be_r R in profit (null = off); max_bars: close at the close of the
    max_bars-th chart bar (null = off); flat_time: close everything at that New York time (null = hold).
    Same 1m bar: stops first, then targets; break-even / trail moves count from the next minute.
    Stats: trades / win rate / avg R per entry (legs summed); profit factor on legs.
  * min_risk_atr: skip the signal if its risk (entry level - stop, incl. sl_atr) < x ATR: a stop that close sits
    inside the noise (null = off). min_trend_bars (pullback only): trade only when the fast EMA has been above
    the slow EMA (short: below) for >= N bars - no pullbacks in a brand-new trend (null = off).
    max_room_r (pullback only): skip if the swing extreme is more than x R beyond the entry level
    (R = entry level - stop incl. sl_atr): the pullback was too deep for the stop (null = off).
  * extended session = entries on bars opening outside 09:30-16:00 New York. eth_sl_atr / eth_tp_r / eth_trail_r
    replace sl_atr / tp_r / trail_r for those trades (null = same as RTH).
  * st_tf: extra higher-timeframe Supertrend(st_atr, st_mult) on st_tf-minute candles (last completed):
    green = no shorts, red = no longs (on top of trend_ema). null = off.
  * htf_adx_tf: ADX(htf_adx_len) of htf_adx_tf-minute candles (last completed) must be >= htf_adx_min for any
    entry (trend strength, no direction); htf_adx_max = at most x (a young trend). null = off.
  * flag: adx_max = chart ADX(14) at most x at the signal bar (null = off).
  * flag: ETH exits (eth_tp_r ...) use rth_hours too (entry bar outside rth_hours = ETH).
  * flag + direction Both: short_<key> = a different entry rule for SHORTS only (pattern, pole, ADX, slopes,
    Supertrend, higher-TF ADX). null = same as longs, off = that filter off for shorts.
    short_sl_atr / short_tp_r / short_trail_r = short-side exits (null = same as longs; they override eth_* for shorts).
  * reverse: true = trade each pattern the OTHER way (flag: bull flag -> sell stop 1 tick below the flag low,
    stop at the flag high + sl_atr ATR; bear flag mirrored). Same filters; [false, true] in a sweep tests both.
  * vud_min / vud_max: volume direction filter on the signal bar: share of the last vud_bars bars' volume in
    candles of the signal's direction (long: close > open) must be > vud_min and <= vud_max (null = off).
    reverse: checked on the ORIGINAL signal (trendline: the breakout's direction).
  * trend_on_trade: with reverse true the trend_ema filter normally checks the ORIGINAL signal's side, so it keeps
    trades AGAINST the trend; trend_on_trade true checks the TRADE's side instead (with-trend fades). Default false.
  * atr_reg_min / atr_reg_max: volatility regime at the signal bar = ATR / SMA(ATR, atr_reg_bars) must be within (null = off).
  * max_stop_atr: the stop is at most x ATR from the entry level (incl. sl_atr); a wider pattern stop is pulled in
    to x ATR, the trade is still taken (null = off). Stops R - and everything in R - blowing up on wild days.
  * risk_min_pts: skip a trade whose stop is closer than x POINTS to the entry level (after reverse / max_stop_atr,
    before sl_atr; null = off). Skips quiet / locked markets where an ATR stop is tiny (risk_usd would buy huge size).
  * costs: commission per contract per fill; slippage_ticks on stop / market fills (not on the target limit).
  * risk_usd: size every trade so the whole position risks this many $ at its stop (contracts = risk_usd / (R x $ per
    point), split qty1 : qty2, fractional allowed); null = fixed qty1 / qty2 contracts. Avg R is the same either way.
  * data: stock: true = a US stock from data/<SYM>_5min.parquet ($1 per share-point, 1 cent tick; qty = shares).

1) pullback  (Raschke "Holy Grail" style - trend, pull back to the fast EMA, buy the turn)
   trend on the chart: EMA(ema_fast) > EMA(ema_slow) (long), optional ADX(14) >= adx_min;
   impulse: the highest high of the last swing_bars bars was >= push_atr x ATR above the fast EMA;
   pullback: that swing high is >= min_pull_bars bars old, the price came down >= pull_atr x ATR from it,
   this bar's low touched the fast EMA zone (low <= EMA fast + touch_atr x ATR) and it CLOSED above EMA(ema_slow);
   entry: buy stop 1 tick above this bar's high, for the next bar only (re-armed every bar the setup still holds);
   one trade per swing high (after a stop-out the same pullback is not traded again);
   stop: lowest low since the swing high - sl_atr x ATR.
2) flag  (pole & flag / bull flag)
   pole top = the highest high of the last flag_max_bars bars, flag_min_bars..flag_max_bars bars ago;
   pole = from the lowest low of the pole_bars bars up to the top; height >= pole_atr x ATR (fast, big move);
   flag = the bars after the top: no new high, deepest low retraces <= max_retrace of the pole,
   flag height (high - low) <= flag_max_pct of the pole, and drifting sideways / down: its last close is not above
   its first close + flag_slope_atr x ATR;
   entry: buy stop 1 tick above the flag high, next bar only (re-armed while the flag holds); one trade per pole;
   stop: flag low - sl_atr x ATR; target: tp_mode r = tp_r x R | pole = measured move (pole height from the entry).
3) trendline  (break of the counter-trend line of a pullback)
   pivots: pivot high = a high above the pv_left bars before and pv_right bars after it, usable only pv_right bars
   later (when TradingView's ta.pivothigh would print it). Long line = the last two confirmed pivot highs, the
   second LOWER than the first (falling line in an uptrend), min_span..max_span bars apart; no close above the line
   between the two pivots; the line is live for max_age bars after the 2nd pivot;
   entry: the first close above the line + break_atr x ATR, at that close;
   stop: lowest low since the 1st pivot - sl_atr x ATR. Short: rising line through two pivot lows.
   line_tol_atr: a close may stand up to x ATR beyond the line (between the pivots / before the break) and the
   line still counts (0 = strict, larger = looser lines). reverse: true = the break up is SOLD at that close,
   stop = highest high since the 1st pivot + sl_atr ATR (break down mirrored) = the failed-breakout trade.
   slope_min / slope_max: keep only lines whose angle is within these degrees (null = no limit), both directions.
   Angle = atan(|price change between the 2 pivots| / bars between them), price in points, not scaled by ATR.
   fail_reverse_bars N (reverse: false only, ignored with reverse: true): trade the break normally; if that trade fails within N bars, reverse at
   the close of the bar it failed on. fail_mode stop = its initial stop was hit; line = stop hit OR a close back through
   the line. Reverse stop = far side of the pattern (incl. the bars since the signal) + sl_atr ATR, capped by max_stop_atr.
   tp_sl_bars N (any setup): if TP1 has not been reached N bars after the entry, close the whole trade at that close.
4) MULTI-TOUCH LINE BREAK (setup: multitouch): at every confirmed pivot (pivot_bars each side) every pair of the last max_pivots
   pivots is a candidate line (rising through lows / falling through highs, at most slope_max_atr ATR per bar). A candidate needs no
   close beyond it (touch_atr ATR tolerance) from its 1st pivot to now, and the one with the most pivots within touch_atr ATR wins,
   at least min_touches. The active line is replaced at each new pivot that finds a valid line (the line moves with the market).
   Signal: first close more than break_atr ATR through it. Rising line broken = short at that close, falling line broken = long.
   Stop: the highest high (short) / lowest low (long) since the line's last touch; the trade is skipped if that is outside
   risk_min_atr..risk_max_atr ATR from the entry.
5) ZONE LINE (setup: zoneline, after LuxAlgo "Accurate Trend Lines"): a line needs min_touches significant pivots (pivot_bars each side, a swing of
   prominence_atr ATR on both sides), at least min_gap_bars apart, within zone_atr ATR of it, at most slope_max_atr ATR per bar, no close beyond the zone.
   Bounce (long at a rising line): low enters the zone, close above the line, up candle; with bounce_need_trend the last pivot high is above the one
   before; with approach_max_atr the fall over the last approach_bars bars is at most that many ATR (a smooth approach). Stop below the bar / zone.
   Break + retest (short at a rising line): close through the zone; with break_need_lh the last pivot high is below the one before. Then within
   retest_bars the high returns to the zone, and within confirm_bars a close below the retest bar's low enters (a new swing). A close back through
   the zone cancels it. Stop above the retest high. Falling lines are the mirror. entry_mode bounce | retest | both.
   multitouch + zoneline: line_on wick = pivots and lines on the highs / lows, close = on the closes. slope_min_atr .. slope_max_atr = allowed steepness in ATR per bar.
   stop_mode swing = the structure stop above, atr = stop_atr ATR from the entry, bar = the signal bar's far end. reverse: true = take the other side at the same close;
   the stop is then the opposite structure (swing), or the mirror of atr / bar.
"""
import itertools
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "box"))
sys.path.insert(0, str(ROOT / "others"))
from box_backtest import SPECS, adx_ind, ema, load_big, load_cfg, supertrend_dir   # noqa: E402
from orb_backtest import NY, RESULTS, atr, bar_flags, chart_bars, hhmm, values   # noqa: E402

SETUPS = ("pullback", "flag", "trendline", "multitouch", "zoneline")
COMMON = {"setup": "pullback", "timeframe": 5, "direction": "Both", "atr_len": 14,
          "trend_tf": 60, "trend_ema": 50, "trend_slope": 3,
          "sl_atr": 0.1, "tp_r": 2.0, "tp_mode": "r", "be_r": None, "max_bars": None, "flat_time": None,
          "qty1": 1, "qty2": 0, "tp2_r": None, "be_after_tp1": True, "tp1_lock_r": 0.0, "tp1_lock_atr": 0.0, "trail": False, "trail_r": 1.0,
          "trail_start_r": None,
          "vol_len": 20, "tvol_days": 20, "vol_min": None, "vol_max": None, "tvol_min": None, "tvol_max": None,
          "study_bars": 12,
          "trade_hours": None, "max_day_trades": None,
          "min_risk_atr": None, "min_trend_bars": None, "max_room_r": None,
          "eth_sl_atr": None, "eth_tp_r": None, "eth_trail_r": None,
          "st_tf": None, "st_atr": 10, "st_mult": 3.0,
          "htf_adx_tf": None, "htf_adx_len": 14, "htf_adx_min": 20.0, "htf_adx_max": None,
          "risk_usd": None, "short_sl_atr": None, "short_tp_r": None, "short_trail_r": None,
          "reverse": False, "max_stop_atr": None, "vud_bars": 6, "vud_min": None, "vud_max": None,
          "tp_sl_bars": None, "fail_reverse_bars": None, "fail_mode": "stop", "risk_min_pts": None,
          "trend_on_trade": False, "atr_reg_bars": 5760, "atr_reg_min": None, "atr_reg_max": None}
SETUP_KEYS = {
    "pullback": {"ema_fast": 20, "ema_slow": 50, "adx_min": None, "swing_bars": 20, "push_atr": 1.0,
                 "min_pull_bars": 2, "pull_atr": 1.0, "touch_atr": 0.0,
                 # pro rules (null = off = the basic version)
                 "ema_slope_bars": None, "new_high_bars": None, "leg_bars": 30, "retr_min": None, "retr_max": None,
                 "pb_max": None, "sig_close": None, "max_risk_atr": None},
    "flag": {"pole_bars": 10, "pole_atr": 3.0, "flag_min_bars": 3, "flag_max_bars": 15, "max_retrace": 0.5,
             "flag_max_pct": 0.5, "flag_slope_atr": 0.0,
             # pole quality + trend strength filters (null = off), checked at the signal bar close
             "pole_min_bars": None, "pole_min_datr": None, "pole_max_bar": None, "adx_min": None,
             "fast_tf": 15, "fast_ema": 50, "fast_slope_bars": 3, "fast_slope_atr": None, "trend_slope_atr": None,
             "adx_max": None},
    "trendline": {"pv_left": 5, "pv_right": 3, "min_span": 5, "max_span": 60, "max_age": 30, "break_atr": 0.0,
                  "line_tol_atr": 0.0, "slope_min": None, "slope_max": None},
    "multitouch": {"line_on": "wick", "pivot_bars": 5, "prominence_atr": 0.0, "max_pivots": 16, "min_touches": 3, "min_gap_bars": 0, "min_span_bars": None,
                   "touch_atr": 0.35, "slope_min_atr": 0.0, "slope_max_atr": 1.0, "break_atr": 0.0,
                   "stop_mode": "swing", "stop_atr": 1.5, "risk_min_atr": 0.3, "risk_max_atr": 4.0},
    "zoneline": {"line_on": "wick", "pivot_bars": 5, "prominence_atr": 1.0, "max_pivots": 16, "min_touches": 3, "min_gap_bars": 10, "zone_atr": 0.25,
                 "slope_min_atr": 0.0, "slope_max_atr": 0.5, "stop_mode": "swing", "stop_atr": 1.5, "entry_mode": "both", "bounce_need_trend": True, "approach_bars": 5, "approach_max_atr": 2.5,
                 "break_need_lh": True, "retest_bars": 30, "confirm_bars": 6, "risk_min_atr": 0.3, "risk_max_atr": 4.0},
}
# flag: extended-hours (ETH) overrides. eth_<key> null = same as RTH, "off" = that filter off in ETH.
# RTH / ETH is decided by the SIGNAL bar's open time (rth_hours, New York).
FLAG_ETH = list(SETUP_KEYS["flag"]) + ["trend_tf", "trend_ema", "trend_slope", "min_risk_atr"]
# flag + direction Both: short-side entry overrides. short_<key> null = same as longs, "off" = that filter off for shorts.
FLAG_SHORT = FLAG_ETH + ["st_tf", "st_atr", "st_mult", "htf_adx_tf", "htf_adx_len", "htf_adx_min", "htf_adx_max"]
FLAG_EXTRA = {"rth_hours": "09:30-16:00", "sessions": "Both", **{f"eth_{k}": None for k in FLAG_ETH},
              **{f"short_{k}": None for k in FLAG_SHORT}}
# RAM: how many results of each big kind a Market keeps (flag sweeps loop pole_bars / flag_max_bars slowest,
# so 2 sides x 2 is enough; a flag result is rebuilt from "flraw" in milliseconds)
MEMO_CAP = {"pb": 8, "tl": 8, "mt": 12, "zl": 12, "fl": 4, "flraw": 2}
REASON = {1: "Stop", 2: "TP1", 3: "BE stop", 4: "Time", 5: "Flat time", 6: "Open", 7: "Trail stop", 8: "TP2", 9: "Failed"}


# ----------------------------------------------------------------------------- trend filter
def htf_bars(m1, tf):
    """higher-timeframe candles for the filters. data: htf_anchor "18:00" = candles start at that New York time
    (TradingView's CME 2h / 4h candles start at 18:00); null = UTC clock (the old behaviour)."""
    anchor = m1.attrs.get("htf_anchor")
    if not anchor:
        return chart_bars(m1, tf)
    ny = m1.t.dt.tz_convert(NY)
    off = ((ny.dt.tz_localize(None) - m1.t.dt.tz_localize(None)) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
    loc = ((m1.t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1)).to_numpy(np.int64) + off
    bucket = loc - ((loc - hhmm(anchor)) % tf) - off       # candle open in UTC minutes
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    bstart = np.r_[starts, len(m1)].astype(np.int64)
    o = m1.open.to_numpy()[starts]
    h = np.maximum.reduceat(m1.high.to_numpy(), starts)
    l = np.minimum.reduceat(m1.low.to_numpy(), starts)
    c = m1.close.to_numpy()[bstart[1:] - 1]
    t = pd.to_datetime(bucket[starts], unit="m", utc=True).tz_convert(NY)
    return t, o, h, l, c, bstart


def htf_trend(m1, t, tf, htf, n, slope):
    """+1 / -1 / 0 per chart bar from the last completed htf-minute candle (no look-ahead)."""
    if not n:
        return np.full(len(t), 2, np.int8)          # 2 = no filter (both sides allowed)
    ht, ho, hh, hl, hc, _ = htf_bars(m1, int(htf))
    e = ema(hc, int(n))
    slope = int(slope or 0)                      # null / 0 = only close vs EMA, no "EMA rising" check
    up = hc > e
    dn = hc < e
    if slope > 0:
        up &= e > np.roll(e, slope)
        dn &= e < np.roll(e, slope)
    up[:slope + int(n)] = dn[:slope + int(n)] = False
    hend = (ht + pd.Timedelta(minutes=int(htf))).asi8
    cend = (t + pd.Timedelta(minutes=tf)).asi8
    k = np.searchsorted(hend, cend, side="right") - 1
    out = np.zeros(len(t), np.int8)
    good = k >= 0
    out[good] = np.where(up[k[good]], 1, np.where(dn[k[good]], -1, 0))
    return out


@njit(cache=True)
def trend_age(EF, ES_):
    """bars the fast EMA has been above (aL) / below (aS) the slow EMA, counting the bar itself; 0 = not."""
    n = len(EF)
    aL = np.zeros(n, np.int64); aS = np.zeros(n, np.int64)
    for i in range(1, n):
        aL[i] = aL[i - 1] + 1 if EF[i] > ES_[i] else 0
        aS[i] = aS[i - 1] + 1 if EF[i] < ES_[i] else 0
    return aL, aS


def htf_slope(m1, t, tf, htf, n, bars):
    """(EMA(n) now - EMA(n) `bars` candles ago) / ATR(14), both on htf-minute candles, last COMPLETED candle."""
    ht, ho, hh, hl, hc, _ = htf_bars(m1, int(htf))
    e = ema(hc, int(n))
    ha = atr(hh, hl, hc, 14)
    b = max(1, int(bars or 1))
    sl = np.full(len(e), np.nan)
    sl[b:] = (e[b:] - e[:-b]) / ha[b:]
    sl[:b + int(n)] = np.nan
    hend = (ht + pd.Timedelta(minutes=int(htf))).asi8
    cend = (t + pd.Timedelta(minutes=tf)).asi8
    k = np.searchsorted(hend, cend, side="right") - 1
    out = np.full(len(t), np.nan)
    out[k >= 0] = sl[k[k >= 0]]
    return out


def daily_atr(t, h, l, c, n=14):
    """ATR(n) of CME sessions, value of the PREVIOUS completed session for every bar (no look-ahead)."""
    s = session_ids(t)
    g = pd.DataFrame({"s": s, "h": h, "l": l, "c": c}).groupby("s").agg(h=("h", "max"), l=("l", "min"),
                                                                         c=("c", "last"))
    pc = g.c.shift()
    tr = np.maximum(g.h - g.l, np.maximum((g.h - pc).abs(), (g.l - pc).abs()))
    return tr.rolling(n).mean().shift().reindex(s).to_numpy()


@njit(cache=True)
def pole_bigbar(h, l, a1, a2, pole):
    """largest bar range inside the pole / pole height (1 = the pole is one spike bar)"""
    out = np.full(len(a1), np.nan)
    for i in range(len(a1)):
        if a1[i] >= 0 and pole[i] > 0:
            m = 0.0
            for k in range(a1[i], a2[i] + 1):
                m = max(m, h[k] - l[k])
            out[i] = m / pole[i]
    return out


# ----------------------------------------------------------------------------- detectors
# Each returns per chart bar i (decided at i's close), for longs and shorts:
#   lvl  : stop-entry level for bar i+1 (kind 1) or the close of i (kind 2); nan = no signal
#   stp  : protective stop price;  a1, a2 : anchor bars (for charts); tgt: measured-move target (nan = use R)
@njit(cache=True)
def det_pullback(h, l, c, A, EF, ES_, ADX, adx_min, swing, push, min_pull, pull, touch, tick,
                 slope_bars, new_high, leg_bars, retr_min, retr_max, pb_max, sig_close, max_risk):
    """LONG logic only; shorts call it with mirrored prices (-low as high, -high as low, -close, -EMAs)."""
    n = len(c)
    lvl = np.full(n, np.nan); stp = np.full(n, np.nan); a1 = np.full(n, -1); a2 = np.full(n, -1)
    cnt = 0; lastanc = -1                        # pullbacks counted since the fast EMA crossed above the slow one
    warm = max(swing, leg_bars, new_high, slope_bars) + 2
    for i in range(1, n):
        if EF[i] > ES_[i] and not (EF[i - 1] > ES_[i - 1]):
            cnt = 0; lastanc = -1
        if i < warm:
            continue
        a = A[i]
        if np.isnan(a) or np.isnan(ES_[i]) or a <= 0:
            continue
        if not (EF[i] > ES_[i]) or not (c[i] > ES_[i]):
            continue
        if slope_bars > 0 and not (EF[i] > EF[i - slope_bars]):
            continue
        if adx_min > 0 and not (ADX[i] >= adx_min):
            continue
        j = i - swing + 1                        # swing high = highest high of the last swing bars (latest if tied)
        for k in range(i - swing + 1, i + 1):
            if h[k] >= h[j]:
                j = k
        if i - j < min_pull or h[j] < EF[j] + push * A[j]:
            continue
        if new_high > 0:                         # the swing high broke out: >= every high of the new_high bars before
            prior = h[j - 1]
            for k in range(j - new_high, j):
                prior = max(prior, h[k])
            if h[j] < prior:
                continue
        lo = l[j]; lb = j                        # pullback low since the swing high
        for k in range(j, i + 1):
            if l[k] < lo:
                lo = l[k]; lb = k
        if h[j] - lo < pull * a:
            continue
        if leg_bars > 0 and (retr_min > 0 or retr_max > 0):
            leg = l[j]                           # impulse leg low: lowest low of the leg_bars bars up to the swing
            for k in range(j - leg_bars, j + 1):
                leg = min(leg, l[k])
            rng = h[j] - leg
            if rng <= 0:
                continue
            r = (h[j] - lo) / rng
            if (retr_min > 0 and r < retr_min) or (retr_max > 0 and r > retr_max):
                continue
        if l[i] > EF[i] + touch * a:
            continue
        if j != lastanc:                         # a new qualified pullback in this trend
            lastanc = j; cnt += 1
        if pb_max > 0 and cnt > pb_max:
            continue
        if sig_close > 0:                        # signal bar closes in the upper part of its range
            rb = h[i] - l[i]
            if rb <= 0 or c[i] - l[i] < sig_close * rb:
                continue
        lv = h[i] + tick
        if max_risk > 0 and lv - lo > max_risk * a:
            continue
        lvl[i] = lv; stp[i] = lo; a1[i] = j; a2[i] = lb
    return lvl, stp, a1, a2


@njit(cache=True)
def det_flag(o, h, l, c, A, pole_bars, pole_atr, fmin, fmax, max_ret, max_pct, slope_atr, tick, side):
    n = len(c)
    lvl = np.full(n, np.nan); stp = np.full(n, np.nan); a1 = np.full(n, -1); a2 = np.full(n, -1)
    tgt = np.full(n, np.nan)
    for i in range(fmax + pole_bars + 1, n):
        a = A[i]
        if np.isnan(a) or a <= 0:
            continue
        p = i - fmax                             # pole top: extreme of the last fmax bars (+ this one)
        for k in range(i - fmax, i + 1):
            if (side == 1 and h[k] >= h[p]) or (side == -1 and l[k] <= l[p]):
                p = k
        f = i - p
        if f < fmin or f > fmax:
            continue
        if side == 1:
            top = h[p]
            b = p - pole_bars; lo = l[b]
            for k in range(p - pole_bars, p + 1):
                if l[k] < lo:
                    lo = l[k]; b = k
            pole = top - lo
            if pole < pole_atr * A[p]:
                continue
            fh = h[p + 1]; fl = l[p + 1]
            for k in range(p + 1, i + 1):
                fh = max(fh, h[k]); fl = min(fl, l[k])
            if top - fl > max_ret * pole or fh - fl > max_pct * pole or fh >= top:
                continue
            if c[i] > c[p + 1] + slope_atr * a:
                continue
            lvl[i] = fh + tick; stp[i] = fl; tgt[i] = pole
        else:
            bot = l[p]
            b = p - pole_bars; hi = h[b]
            for k in range(p - pole_bars, p + 1):
                if h[k] > hi:
                    hi = h[k]; b = k
            pole = hi - bot
            if pole < pole_atr * A[p]:
                continue
            fh = h[p + 1]; fl = l[p + 1]
            for k in range(p + 1, i + 1):
                fh = max(fh, h[k]); fl = min(fl, l[k])
            if fh - bot > max_ret * pole or fh - fl > max_pct * pole or fl <= bot:
                continue
            if c[i] < c[p + 1] - slope_atr * a:
                continue
            lvl[i] = fl - tick; stp[i] = fh; tgt[i] = pole
        a1[i] = b; a2[i] = p
    return lvl, stp, a1, a2, tgt


@njit(cache=True)
def flag_raw(h, l, pole_bars, fmax):
    """The slow part of det_flag (long side; shorts: pass -l, -h), which depends only on pole_bars + flag_max_bars.
    Per bar i: pole top p (last highest high of the last fmax bars + this one), pole start b (first lowest low of
    the pole_bars bars before p), pole size, flag high / low (bars p+1..i). p = -1: no flag bar after the top."""
    n = len(h)
    P = np.full(n, -1, np.int64); B = np.full(n, -1, np.int64)
    pole = np.full(n, np.nan); FH = np.full(n, np.nan); FL = np.full(n, np.nan)
    for i in range(fmax + pole_bars + 1, n):
        p = i - fmax
        for k in range(i - fmax, i + 1):
            if h[k] >= h[p]:
                p = k
        if p >= i:
            continue
        b = p - pole_bars; lo = l[b]
        for k in range(p - pole_bars, p + 1):
            if l[k] < lo:
                lo = l[k]; b = k
        fh = h[p + 1]; fl = l[p + 1]
        for k in range(p + 1, i + 1):
            fh = max(fh, h[k]); fl = min(fl, l[k])
        P[i] = p; B[i] = b; pole[i] = h[p] - lo; FH[i] = fh; FL[i] = fl
    return P, B, pole, FH, FL


@njit(cache=True)
def flag_apply(h, c, A, P, pole, FH, FL, pole_atr, fmin, max_ret, max_pct, slope_atr, tick):
    """det_flag's thresholds on flag_raw's output -> entry level per bar (nan = no signal). Long side; shorts:
    mirrored prices, level negated. Stop / anchors / pole size come straight from flag_raw (read only where the
    level is set, so their values on other bars do not matter)."""
    n = len(c)
    lvl = np.full(n, np.nan)
    for i in range(n):
        p = P[i]
        if p < 0:
            continue
        a = A[i]
        if np.isnan(a) or a <= 0:
            continue
        if i - p < fmin:
            continue
        pl = pole[i]
        if pl < pole_atr * A[p]:
            continue
        top = h[p]; fh = FH[i]; fl = FL[i]
        if top - fl > max_ret * pl or fh - fl > max_pct * pl or fh >= top:
            continue
        if c[i] > c[p + 1] + slope_atr * a:
            continue
        lvl[i] = fh + tick
    return lvl


@njit(cache=True)
def det_trendline(o, h, l, c, A, left, right, min_span, max_span, max_age, brk, tol, side):
    """Long: falling line through the last two confirmed pivot highs, first close above it.
    tol = a close may stand up to tol x ATR beyond the line (between the pivots / before the break) without
    killing it (0 = strict). opp = the other side of the pattern (long: highest high since the 1st pivot) =
    the stop of the reverse trade."""
    n = len(c)
    lvl = np.full(n, np.nan); stp = np.full(n, np.nan); a1 = np.full(n, -1); a2 = np.full(n, -1)
    opp = np.full(n, np.nan)
    x = h if side == 1 else l
    p1 = -1; p2 = -1                             # the two latest confirmed pivots
    used = -1                                    # line (by its p2) already broken -> wait for a new pivot
    for i in range(left + right, n):
        j = i - right                            # candidate pivot, confirmed now
        piv = True
        for k in range(j - left, j + right + 1):
            if k == j:
                continue
            if (side == 1 and x[k] >= x[j]) or (side == -1 and x[k] <= x[j]):
                piv = False
                break
        if piv:
            p1 = p2; p2 = j
        if p1 < 0 or used == p2 or np.isnan(A[i]):
            continue
        span = p2 - p1
        if span < min_span or span > max_span or i - p2 > max_age:
            continue
        if (side == 1 and x[p2] >= x[p1]) or (side == -1 and x[p2] <= x[p1]):
            continue
        sl = (x[p2] - x[p1]) / span
        ok = True
        for k in range(p1 + 1, p2):              # line not already broken between the pivots
            ln = x[p1] + sl * (k - p1)
            tk = tol * A[k] if not np.isnan(A[k]) else 0.0
            if (side == 1 and c[k] > ln + tk) or (side == -1 and c[k] < ln - tk):
                ok = False
                break
        if not ok:
            used = p2
            continue
        for k in range(p2 + 1, i):               # broke earlier (before this bar): not a fresh break
            ln = x[p1] + sl * (k - p1)
            tk = tol * A[k] if not np.isnan(A[k]) else 0.0
            if (side == 1 and c[k] > ln + tk) or (side == -1 and c[k] < ln - tk):
                ok = False
                break
        if not ok:
            used = p2
            continue
        ln = x[p1] + sl * (i - p1)
        if side == 1 and c[i] > ln + brk * A[i]:
            ex = l[p1]; ox = h[p1]
            for k in range(p1, i + 1):
                ex = min(ex, l[k]); ox = max(ox, h[k])
        elif side == -1 and c[i] < ln - brk * A[i]:
            ex = h[p1]; ox = l[p1]
            for k in range(p1, i + 1):
                ex = max(ex, h[k]); ox = min(ox, l[k])
        else:
            continue
        lvl[i] = c[i]; stp[i] = ex; a1[i] = p1; a2[i] = p2; opp[i] = ox
        used = p2
    return lvl, stp, a1, a2, opp


@njit(cache=True)
def det_multitouch(o, h, l, c, x, A, pv, K, min_t, tol, smin, smax, brk, prom, gap, span):
    """Input coordinates: rising support on series x (lows or closes), its break = a short signal (flip the prices for falling resistance / longs).
    Returns entry, stop (highest high since the last touch), opposite stop, then the line's 1st pivot, slope in ATR per bar, touches, last touch."""
    n = len(c); en = np.full(n, np.nan); st = np.full(n, np.nan); opp = np.full(n, np.nan)
    fa = np.full(n, -1); fs = np.full(n, np.nan); ft = np.full(n, -1); fl = np.full(n, -1)
    pts = np.full(K, -1); np_ = 0; a_ = -1; s_ = 0.0; active = False; lastT = 0; tch = 0
    w_ = 3 * pv
    for i in range(max(2 * pv + 3, w_ + pv + 3), n):
        if np.isnan(A[i]) or A[i] <= 0:
            continue
        j = i - pv; piv = True
        for k in range(j - pv, j + pv + 1):
            if k != j and x[k] <= x[j]:
                piv = False
                break
        if piv and prom > 0 and not (np.max(h[j - w_:j]) - x[j] >= prom * A[j] and np.max(h[j:i + 1]) - x[j] >= prom * A[j]):
            piv = False
        if piv:
            pts[1:] = pts[:-1].copy(); pts[0] = j; np_ = min(np_ + 1, K)
            best = 0; ba = -1; bs = 0.0; bl = 0
            for u in range(1, np_):
                a = pts[u]
                if j - a > 400:
                    break
                for v in range(u):
                    b = pts[v]
                    if b - a < span:
                        continue
                    m = (x[b] - x[a]) / (b - a); sa = m / A[j]
                    if sa < smin or sa > smax:
                        continue
                    ok = True
                    for k in range(a, i + 1):
                        if c[k] < x[a] + m * (k - a) - tol * A[k]:
                            ok = False
                            break
                    if not ok:
                        continue
                    t = 0; lt = a; lc = -10**9
                    for w in range(np_ - 1, -1, -1):
                        p = pts[w]
                        if p >= a and abs(x[p] - (x[a] + m * (p - a))) <= tol * A[p] and p - lc >= gap:
                            t += 1; lc = p; lt = max(lt, p)
                    if t > best or (t == best and b > bl):
                        best = t; ba = a; bs = m; bl = lt
            if best >= min_t:
                a_ = ba; s_ = bs; active = True; lastT = bl; tch = best
        if not active:
            continue
        ln = x[a_] + s_ * (i - a_)
        if c[i] < ln - brk * A[i]:
            active = False
            en[i] = c[i]; st[i] = np.max(h[lastT:i + 1]); opp[i] = np.min(l[lastT:i + 1])
            fa[i] = a_; fs[i] = s_ / A[i]; ft[i] = tch; fl[i] = lastT
    return en, st, opp, fa, fs, ft, fl


@njit(cache=True)
def det_zoneline(o, h, l, c, x, y, A, pv, prom, K, min_t, gap, zone, smin, smax, need_trend, ab, amax, need_lh, rb, cb):
    """Input coordinates: rising support on series x, pivot highs on series y. Returns (bounce entry, stop, opposite stop) = longs,
    (retest entry, stop, opposite stop) = shorts, then the line's 1st pivot, slope in ATR per bar, touches, last touch."""
    n = len(c)
    eb = np.full(n, np.nan); sb = np.full(n, np.nan); ob = np.full(n, np.nan); er = np.full(n, np.nan); sr = np.full(n, np.nan); orr = np.full(n, np.nan)
    fa = np.full(n, -1); fs = np.full(n, np.nan); ft = np.full(n, -1); fl = np.full(n, -1)
    pts = np.full(K, -1); np_ = 0; a_ = -1; s_ = 0.0; active = False; lastT = 0; tch = 0
    ph1 = np.nan; ph2 = np.nan; state = 0; tb = 0; rbar = 0; rhi = 0.0; rlo = 0.0
    w_ = 3 * pv
    for i in range(w_ + 2 * pv + 3, n):
        if np.isnan(A[i]) or A[i] <= 0:
            continue
        j = i - pv
        ish = True; isl = True
        for k in range(j - pv, j + pv + 1):
            if k != j:
                if y[k] >= y[j]: ish = False
                if x[k] <= x[j]: isl = False
        if ish:
            ph2 = ph1; ph1 = y[j]
        if isl and np.max(h[j - w_:j]) - x[j] >= prom * A[j] and np.max(h[j:i + 1]) - x[j] >= prom * A[j]:
            pts[1:] = pts[:-1].copy(); pts[0] = j; np_ = min(np_ + 1, K)
            if state == 0:
                best = 0; ba = -1; bs = 0.0; bl = 0
                for u in range(1, np_):
                    a = pts[u]
                    if j - a > 500:
                        break
                    for v in range(u):
                        b = pts[v]
                        if b - a < gap:
                            continue
                        m = (x[b] - x[a]) / (b - a); sa = m / A[j]
                        if sa < smin or sa > smax:
                            continue
                        ok = True
                        for k in range(a, i + 1):
                            if c[k] < x[a] + m * (k - a) - zone * A[k]:
                                ok = False
                                break
                        if not ok:
                            continue
                        t = 0; lc = -10**9
                        for w in range(np_ - 1, -1, -1):
                            p = pts[w]
                            if p >= a and abs(x[p] - (x[a] + m * (p - a))) <= zone * A[p] and p - lc >= gap:
                                t += 1; lc = p
                        if t > best or (t == best and lc > bl):
                            best = t; ba = a; bs = m; bl = lc
                if best >= min_t:
                    a_ = ba; s_ = bs; active = True; lastT = bl; tch = best
        if a_ < 0:
            continue
        ln = x[a_] + s_ * (i - a_); z = zone * A[i]
        if state == 1:
            if i - tb > rb or c[i] > ln + z:
                state = 0
            elif y[i] >= ln - z:
                state = 2; rbar = i; rhi = h[i]; rlo = l[i]
            continue
        if state == 2:
            if h[i] > rhi:
                rhi = h[i]; rlo = l[i]
            if c[i] > ln + z or i - rbar > cb:
                state = 0
            elif c[i] < rlo:
                er[i] = c[i]; sr[i] = rhi; orr[i] = np.min(l[tb:i + 1]); state = 0
                fa[i] = a_; fs[i] = s_ / A[i]; ft[i] = tch; fl[i] = lastT
            continue
        if not active:
            continue
        if c[i] < ln - z:
            active = False
            if (not need_lh) or ph1 < ph2:
                state = 1; tb = i
            continue
        if i > lastT + 3 and x[i] <= ln + z and c[i] > ln and c[i] > o[i]:
            okt = (not need_trend) or ph1 > ph2
            oka = amax <= 0 or c[i - ab] - x[i] <= amax * A[i]
            if okt and oka:
                eb[i] = c[i]; sb[i] = min(l[i], ln - z); ob[i] = np.max(h[lastT:i + 1])
                fa[i] = a_; fs[i] = s_ / A[i]; ft[i] = tch; fl[i] = lastT
                lastT = i
    return eb, sb, ob, er, sr, orr, fa, fs, ft, fl


def _line_series(p, o, h, l, c, flip):
    """(o, h, l, c, x, y) in detector coordinates: flipped for falling lines; x / y = the series the lines and pivots use."""
    on = str(p["line_on"]).strip().lower()
    if on not in ("wick", "close"):
        raise SystemExit(f"line_on must be wick or close, not {on!r}")
    oo, hh, ll, cc = (-o, -l, -h, -c) if flip else (o, h, l, c)
    return oo, hh, ll, cc, (ll if on == "wick" else cc), (hh if on == "wick" else cc)


def _finish_line(p, sd, en, st, opp, A, h, l):
    """stop_mode + the risk range; the range is checked on the stop the trade will really use (opp when reversed)."""
    mode = str(p["stop_mode"]).strip().lower(); k = float(p["stop_atr"])
    if mode == "atr":
        st = en - sd * k * A; opp = en + sd * k * A
    elif mode == "bar":
        st, opp = (l, h) if sd == 1 else (h, l)
    elif mode != "swing":
        raise SystemExit(f"stop_mode must be swing | atr | bar, not {mode!r}")
    with np.errstate(invalid="ignore"):
        r = ((opp - en) * sd if p["reverse"] else (en - st) * sd) / A
        bad = ~((r >= float(p["risk_min_atr"])) & (r <= float(p["risk_max_atr"])))
    en = np.where(bad, np.nan, en)
    anc = np.where(np.isnan(en), -1, np.arange(len(en)))
    return en, st, anc, anc, opp


# ----------------------------------------------------------------------------- simulation
# Position state X: 0 side, 1 entry, 2 risk (R in price), 3 leg-1 qty open, 4 leg-2 qty open, 5 leg-1 stop,
# 6 leg-2 stop, 7 TP1, 8 TP2 (nan = none), 9 best price since entry, 10 TP1 done, 11 leg-1 stop kind,
# 12 leg-2 stop kind (0 initial, 1 break-even, 2 trailing), 13 total qty
# S: 0 equity, 1 peak, 2 max DD, 3 gross profit, 4 gross loss, 5 legs, 6 entries, 7 winning entries, 8 sum R,
#    9 P&L of the open trade
@njit(cache=True)
def leg_out(T, nt, rec, S, X, leg, i, px, reason, sig, ebar, pv, comm, Q1, Q2):
    # X[3] / X[4] are only open flags (1 / 0); the contract counts are Q1[k] / Q2[k], one S row per qty pair k
    # (the trade path does not depend on the counts, so all pairs with the same zero pattern share one run)
    sd = X[0]; entry = X[1]; risk = X[2]
    u = (px - entry) * sd * pv - 2 * comm      # P&L of 1 contract
    for k in range(S.shape[0]):
        # X[13] > 0 = risk sizing: X[13] contracts in total for this trade, split in the Q1 : Q2 ratio
        mk_ = X[13] / (Q1[k] + Q2[k]) if X[13] > 0 else 1.0
        pk = u * (Q1[k] if leg == 1 else Q2[k]) * mk_
        S[k, 0] += pk; S[k, 5] += 1; S[k, 9] += pk
        if pk > 0:
            S[k, 3] += pk
        else:
            S[k, 4] -= pk
    q = (Q1[0] if leg == 1 else Q2[0]) * (X[13] / (Q1[0] + Q2[0]) if X[13] > 0 else 1.0)
    pnl = u * q
    if rec:
        T[nt, 0] = sig; T[nt, 1] = ebar; T[nt, 2] = i; T[nt, 3] = sd; T[nt, 4] = entry
        T[nt, 5] = entry - sd * risk; T[nt, 6] = X[7] if leg == 1 else X[8]; T[nt, 7] = px; T[nt, 8] = pnl
        T[nt, 9] = reason; T[nt, 10] = risk; T[nt, 11] = leg; T[nt, 12] = q
        nt += 1
    if leg == 1:
        X[3] = 0.0
    else:
        X[4] = 0.0
    return nt


@njit(cache=True)
def manage(X, m0, m1_, i, T, nt, rec, S, mo, mh, ml, slip, tick, pv, comm,
           be_r, trail, trail_r, trail_start, be_tp1, sig, ebar, Q1, Q2, lock_r):
    """Walk the open legs over 1m rows m0..m1_-1: stops first, then targets, then move BE / trail for the next minute.
    lock_r: when TP1 fills (be_tp1), the runner's stop goes to entry + lock_r x R (0 = break-even)."""
    sd = X[0]; entry = X[1]; risk = X[2]
    for m in range(m0, m1_):
        for leg in range(1, 3):
            q = X[3] if leg == 1 else X[4]
            if q <= 0:
                continue
            st = X[5] if leg == 1 else X[6]
            if (sd > 0 and ml[m] <= st) or (sd < 0 and mh[m] >= st):
                px = (min(st, mo[m]) if sd > 0 else max(st, mo[m])) - sd * slip * tick
                k = X[11] if leg == 1 else X[12]
                nt = leg_out(T, nt, rec, S, X, leg, i, px, 1 if k == 0 else (3 if k == 1 else 7), sig, ebar, pv, comm, Q1, Q2)
        if X[10] == 0 and ((sd > 0 and mh[m] >= X[7]) or (sd < 0 and ml[m] <= X[7])):   # TP1 level reached
            if X[3] > 0:
                px = max(X[7], mo[m]) if sd > 0 else min(X[7], mo[m])
                nt = leg_out(T, nt, rec, S, X, 1, i, px, 2, sig, ebar, pv, comm, Q1, Q2)
            X[10] = 1.0
            lk = entry + sd * (lock_r * risk + X[15])               # TP1 hit -> runner stop to entry + lock_r R + lock_atr ATR
            if be_tp1 and X[4] > 0 and sd * (lk - X[6]) > 0:
                X[6] = lk; X[12] = 1.0
        if X[4] > 0 and not np.isnan(X[8]) and ((sd > 0 and mh[m] >= X[8]) or (sd < 0 and ml[m] <= X[8])):
            px = max(X[8], mo[m]) if sd > 0 else min(X[8], mo[m])
            nt = leg_out(T, nt, rec, S, X, 2, i, px, 8, sig, ebar, pv, comm, Q1, Q2)
        if X[3] <= 0 and X[4] <= 0:
            return nt
        fav = mh[m] if sd > 0 else ml[m]
        if sd * (fav - X[9]) > 0:
            X[9] = fav
        move = sd * (X[9] - entry)
        if be_r > 0 and move >= be_r * risk:                        # whole position to break-even
            if sd * (entry - X[5]) > 0:
                X[5] = entry; X[11] = 1.0
            if sd * (entry - X[6]) > 0:
                X[6] = entry; X[12] = 1.0
        if trail and X[4] > 0 and ((trail_start < 0 and X[10] > 0) or (trail_start >= 0 and move >= trail_start * risk)):
            # runner trails trail_r R behind the best price (from TP1 when trail_start < 0)
            ts = X[9] - sd * trail_r * risk
            if sd * (ts - X[6]) > 0:
                X[6] = ts; X[12] = 2.0
        adv = ml[m] if sd > 0 else mh[m]
        for k in range(S.shape[0]):
            mk_ = X[13] / (Q1[k] + Q2[k]) if X[13] > 0 else 1.0
            S[k, 2] = max(S[k, 2], S[k, 1] - (S[k, 0] + sd * (adv - entry) * pv * (X[3] * Q1[k] + X[4] * Q2[k]) * mk_))
    return nt


@njit(cache=True)
def simulate(c, bstart, mo, mh, ml, ok, flat, trend, A, day, max_day,
             LL, LS, LT, LA, SL_, SS, ST, SA, kind, sl_atr, tp_r, use_pole, be_r, max_bars,
             pv, tick, comm, slip, Q1, Q2, trail, trail_r, trail_start, tp2_r, be_tp1, rec,
             ETH, e_sl, e_tp, e_tr, risk_usd, s_sl, s_tp, s_tr, lock_r, lock_atr,
             to_bars, fail_bars, fail_mode, cap, LNL, SPL, LNS, SPS):
    """One position at a time, 2 legs: leg 1 (Q1) takes TP1, leg 2 (Q2) = runner (trail / TP2 / BE after TP1).
    Q1 / Q2 = arrays of qty pairs that all share the same zero pattern -> one stats row per pair (same trades).
    kind 1 = stop order from bar i-1 working during bar i; kind 2 = market at the close of bar i.
    Trade rows (rec): sig, entry bar, exit bar, side, entry, initial stop, leg target, exit, pnl, reason, R, leg, qty."""
    nb = len(c)
    T = np.zeros((2 * nb // 3 + 10 if rec else 1, 13))
    nt = 0
    nq = len(Q1)
    S = np.zeros((nq, 10))
    o1 = 1.0 if Q1[0] > 0 else 0.0; o2 = 1.0 if Q2[0] > 0 else 0.0   # leg open flags
    X = np.zeros(18)                             # 16 = far side of the pattern, 17 = 1 while a normal trade can still fail; 13 = risk-sizing contracts (0 = fixed Q1 / Q2), 14 = this trade's trail distance (R)
                                                 # 15 = extra lock after TP1 in price (lock_atr x ATR at the signal)
    inpos = False; ebar = 0; sig = 0
    lastL = -2; lastS = -2                       # anchor of the last pattern traded per side: one trade per pattern
    nb = len(c)
    dcur = -1; dn = 0                            # entries in the current session
    # nxt[i] = first bar >= i where a trade can start (kind 1: signal on the bar before; kind 2: signal on it)
    nxt = np.full(nb + 1, nb, np.int64)
    for j in range(nb - 1, 0, -1):
        k = j - 1 if kind == 1 else j
        nxt[j] = j if (not np.isnan(LL[k]) or not np.isnan(SL_[k])) else nxt[j + 1]
    i = 0
    while True:
        i += 1
        if i >= nb:
            break
        if not inpos:                            # flat: skip the bars without any signal
            i = nxt[i]
            if i >= nb:
                break
        if day[i] != dcur:
            dcur = day[i]; dn = 0
        room = max_day == 0 or dn < max_day
        closed_now = False
        if not inpos and kind == 1 and ok[i] and room:
            for sd in (1, -1):
                lv = LL[i - 1] if sd == 1 else SL_[i - 1]
                if np.isnan(lv) or not (trend[i - 1] == 2 or trend[i - 1] == sd):
                    continue
                anc = LA[i - 1] if sd == 1 else SA[i - 1]
                if anc == (lastL if sd == 1 else lastS):
                    continue
                x_sl = e_sl if ETH[i] else sl_atr        # extended-session exits (entry bar opens outside RTH)
                x_tp = e_tp if ETH[i] else tp_r
                x_tr = e_tr if ETH[i] else trail_r
                if sd == -1:                             # short-side exits (< 0 = same as longs)
                    if s_sl >= 0: x_sl = s_sl
                    if s_tp >= 0: x_tp = s_tp
                    if s_tr >= 0: x_tr = s_tr
                st0 = LS[i - 1] - x_sl * A[i - 1] if sd == 1 else SS[i - 1] + x_sl * A[i - 1]
                for m in range(bstart[i], bstart[i + 1]):
                    if (sd == 1 and mh[m] >= lv) or (sd == -1 and ml[m] <= lv):
                        f = max(lv, mo[m]) if sd == 1 else min(lv, mo[m])
                        f += sd * slip * tick
                        risk = (f - st0) * sd
                        if risk > 0:
                            pt = LT[i - 1] if sd == 1 else ST[i - 1]
                            d1 = pt if use_pole and not np.isnan(pt) else x_tp * risk
                            X[0] = sd; X[1] = f; X[2] = risk; X[3] = o1; X[4] = o2; X[5] = st0; X[6] = st0
                            X[7] = f + sd * d1; X[8] = f + sd * tp2_r * risk if tp2_r > 0 else np.nan
                            X[9] = f; X[10] = 0.0; X[11] = 0.0; X[12] = 0.0
                            X[13] = risk_usd / (risk * pv) if risk_usd > 0 else 0.0
                            X[14] = x_tr
                            X[15] = lock_atr * A[i - 1]
                            inpos = True; ebar = i; sig = i - 1; dn += 1
                            if sd == 1:
                                lastL = anc
                            else:
                                lastS = anc
                            # fill minute: only the stop can still be hit after the fill
                            if (sd == 1 and ml[m] <= st0) or (sd == -1 and mh[m] >= st0):
                                for leg in range(1, 3):
                                    if (X[3] if leg == 1 else X[4]) > 0:
                                        nt = leg_out(T, nt, rec, S, X, leg, i, st0 - sd * slip * tick, 1, sig, ebar,
                                                     pv, comm, Q1, Q2)
                            else:
                                nt = manage(X, m + 1, bstart[i + 1], i, T, nt, rec, S, mo, mh, ml, slip, tick, pv,
                                            comm, be_r, trail, X[14], trail_start, be_tp1, sig, ebar, Q1, Q2, lock_r)
                        break
                if inpos:
                    break
        elif inpos:
            nt = manage(X, bstart[i], bstart[i + 1], i, T, nt, rec, S, mo, mh, ml, slip, tick, pv, comm,
                        be_r, trail, X[14], trail_start, be_tp1, sig, ebar, Q1, Q2, lock_r)
        if inpos:
            sd = X[0]
            why = 0
            can_fail = X[17] > 0 and X[10] == 0 and i > ebar and i - ebar <= fail_bars
            if X[3] + X[4] > 0:
                px = 0.0
                back = False
                if can_fail and fail_mode == 2:          # close back through the line
                    back = (sd > 0 and c[i] < LNL[sig] + SPL[sig] * (i - sig)) or \
                           (sd < 0 and c[i] > LNS[sig] + SPS[sig] * (i - sig))
                if back:
                    why = 9; px = c[i] - sd * slip * tick
                elif flat[i]:
                    why = 5; px = c[i] - sd * slip * tick
                elif max_bars > 0 and i - ebar >= max_bars:
                    why = 4; px = c[i] - sd * slip * tick
                elif to_bars > 0 and X[10] == 0 and i - ebar >= to_bars:
                    why = 4; px = c[i] - sd * slip * tick
                elif i == nb - 1:
                    why = 6; px = c[i]
                if why:
                    for leg in range(1, 3):
                        if (X[3] if leg == 1 else X[4]) > 0:
                            nt = leg_out(T, nt, rec, S, X, leg, i, px, why, sig, ebar, pv, comm, Q1, Q2)
            if X[3] + X[4] <= 0:                 # trade finished
                for k in range(nq):
                    S[k, 6] += 1
                    if S[k, 9] > 0:
                        S[k, 7] += 1
                    S[k, 8] += S[k, 9] / (X[2] * pv * (X[13] if X[13] > 0 else (Q1[k] + Q2[k])))
                    S[k, 9] = 0.0
                    S[k, 1] = max(S[k, 1], S[k, 0]); S[k, 2] = max(S[k, 2], S[k, 1] - S[k, 0])
                inpos = False; closed_now = True
                # normal trade failed inside the window -> reverse at this close
                if can_fail and (why == 9 or (why == 0 and X[11] == 0 and X[12] == 0)) and ok[i] and not flat[i] and i < nb - 1:
                    sd2 = -sd
                    base = X[16]
                    for m in range(bstart[sig + 1], bstart[i + 1]):
                        base = max(base, mh[m]) if sd2 < 0 else min(base, ml[m])
                    x_sl = e_sl if ETH[i] else sl_atr
                    x_tp = e_tp if ETH[i] else tp_r
                    x_tr = e_tr if ETH[i] else trail_r
                    if sd2 < 0:
                        if s_sl >= 0: x_sl = s_sl
                        if s_tp >= 0: x_tp = s_tp
                        if s_tr >= 0: x_tr = s_tr
                    st0 = base - sd2 * x_sl * A[i]
                    if cap > 0:
                        st0 = max(st0, c[i] - cap * A[i]) if sd2 > 0 else min(st0, c[i] + cap * A[i])
                    f = c[i] + sd2 * slip * tick
                    risk = (f - st0) * sd2
                    if risk > 0:
                        X[0] = sd2; X[1] = f; X[2] = risk; X[3] = o1; X[4] = o2; X[5] = st0; X[6] = st0
                        X[7] = f + sd2 * x_tp * risk
                        X[8] = f + sd2 * tp2_r * risk if tp2_r > 0 else np.nan
                        X[9] = f; X[10] = 0.0; X[11] = 0.0; X[12] = 0.0
                        X[13] = risk_usd / (risk * pv) if risk_usd > 0 else 0.0
                        X[14] = x_tr
                        X[15] = lock_atr * A[i]
                        X[16] = 0.0; X[17] = 0.0
                        inpos = True; ebar = i; sig = i; dn += 1
        if not inpos and kind == 2 and ok[i] and not flat[i] and not closed_now and room:
            for sd in (1, -1):
                lv = LL[i] if sd == 1 else SL_[i]
                if np.isnan(lv) or not (trend[i] == 2 or trend[i] == sd):
                    continue
                anc = LA[i] if sd == 1 else SA[i]
                if anc == (lastL if sd == 1 else lastS):
                    continue
                f = c[i] + sd * slip * tick
                x_sl = e_sl if ETH[i] else sl_atr
                x_tp = e_tp if ETH[i] else tp_r
                x_tr = e_tr if ETH[i] else trail_r
                if sd == -1:
                    if s_sl >= 0: x_sl = s_sl
                    if s_tp >= 0: x_tp = s_tp
                    if s_tr >= 0: x_tr = s_tr
                st0 = LS[i] - x_sl * A[i] if sd == 1 else SS[i] + x_sl * A[i]
                risk = (f - st0) * sd
                if risk <= 0:
                    continue
                X[0] = sd; X[1] = f; X[2] = risk; X[3] = o1; X[4] = o2; X[5] = st0; X[6] = st0
                X[7] = f + sd * x_tp * risk
                X[8] = f + sd * tp2_r * risk if tp2_r > 0 else np.nan
                X[9] = f; X[10] = 0.0; X[11] = 0.0; X[12] = 0.0
                X[13] = risk_usd / (risk * pv) if risk_usd > 0 else 0.0
                X[14] = x_tr
                X[15] = lock_atr * A[i]
                X[16] = LT[i] if sd == 1 else ST[i]
                X[17] = 1.0 if fail_bars > 0 else 0.0
                inpos = True; ebar = i; sig = i; dn += 1
                if sd == 1:
                    lastL = anc
                else:
                    lastS = anc
                break
    st = np.zeros((nq, 8))
    for k in range(nq):
        st[k, 0] = S[k, 0]; st[k, 1] = S[k, 3] / S[k, 4] if S[k, 4] > 0 else 99.0; st[k, 2] = S[k, 2]
        st[k, 3] = S[k, 0] / S[k, 2] if S[k, 2] > 0 else 0.0; st[k, 4] = S[k, 6]
        st[k, 5] = 100.0 * S[k, 7] / S[k, 6] if S[k, 6] else 0.0; st[k, 6] = S[k, 8] / S[k, 6] if S[k, 6] else 0.0
        st[k, 7] = S[k, 5]
    return st, T[:nt]


STAT_COLS = ["net_profit", "profit_factor", "max_drawdown", "net_to_dd", "trades", "win_rate", "avg_r", "legs"]


# ----------------------------------------------------------------------------- driver
class Market:
    def __init__(self, inst, cfg):
        _PREP.clear()                        # prepared signals are keyed by id(Market): a new Market can reuse a freed id
        self.inst = inst
        self.m1 = load_big(inst, cfg["data"].get("back_adjust", True))
        bt = cfg.get("backtest") or {}
        if bt.get("start"):                  # keep a warm-up month before the start for the indicators
            s = pd.Timestamp(bt["start"]).tz_localize(NY) - pd.Timedelta(days=40)
            self.m1 = self.m1[self.m1.t >= s].reset_index(drop=True)
        self.m1.attrs["htf_anchor"] = cfg["data"].get("htf_anchor")   # higher-TF candle start (see htf_bars)
        self.mo, self.mh, self.ml, self.mc = (self.m1[k].to_numpy() for k in ("open", "high", "low", "close"))
        if cfg["data"].get("stock"):
            SPECS.setdefault(inst, (1.0, 0.01))
        if inst not in SPECS:
            raise SystemExit(f"instrument must be one of {list(SPECS)} (or set data: stock: true), not {inst!r}")
        self.pv, self.tick = SPECS[inst]
        self.cache = {}

    def bars(self, tf):
        if ("bars", tf) not in self.cache:
            self.cache[("bars", tf)] = chart_bars(self.m1, tf)
        return self.cache[("bars", tf)]

    def vol(self, tf):
        """chart-bar volume + rvol helpers"""
        if ("vol", tf) not in self.cache:
            bstart = self.bars(tf)[5]
            self.cache[("vol", tf)] = np.add.reduceat(self.m1.volume.to_numpy(float), bstart[:-1])
        return self.cache[("vol", tf)]

    def rvol(self, tf, n):
        """bar volume / average volume of the n bars BEFORE it"""
        def f():
            v = pd.Series(self.vol(tf))
            return (v / v.shift(1).rolling(int(n), min_periods=int(n)).mean()).to_numpy()
        return self.memo(("rvol", tf, n), f)

    def tvol(self, tf, days):
        """bar volume / average volume of the SAME time-of-day bar over the last `days` sessions (before it)"""
        def f():
            t = self.bars(tf)[0]
            v = pd.Series(self.vol(tf))
            slot = np.asarray(t.hour * 60 + t.minute)
            avg = v.groupby(slot).transform(lambda x: x.shift(1).rolling(int(days), min_periods=max(3, int(days) // 2)).mean())
            return (v / avg).to_numpy()
        return self.memo(("tvol", tf, days), f)

    def const(self, n):
        """shared all-nan / all -1 arrays (never written to)"""
        return self.memo(("const", n), lambda: (np.full(n, np.nan), np.full(n, -1)))

    def memo(self, key, fn):
        if key not in self.cache:
            cap = MEMO_CAP.get(key[0])
            if cap:                              # pattern results are big: keep only the latest few (RAM)
                old = [k for k in self.cache if k[0] == key[0]]
                for k in old[:max(0, len(old) - cap + 1)]:
                    del self.cache[k]
            self.cache[key] = fn()
        return self.cache[key]


def signals(mk, p):
    tf = int(p["timeframe"])
    t, o, h, l, c, bstart = mk.bars(tf)
    A = mk.memo(("atr", tf, p["atr_len"]), lambda: atr(h, l, c, int(p["atr_len"])))
    s = p["setup"]
    out = {}
    for sd in (1, -1):
        if s == "pullback":
            EF = mk.memo(("ema", tf, p["ema_fast"]), lambda: ema(c, int(p["ema_fast"])))
            ESl = mk.memo(("ema", tf, p["ema_slow"]), lambda: ema(c, int(p["ema_slow"])))
            ADX = mk.memo(("adx", tf), lambda: adx_ind(h, l, c, 14, 14))
            key = ("pb", tf, sd) + tuple(p[k] for k in ["atr_len"] + list(SETUP_KEYS["pullback"]))

            def pb():
                hh, ll, cc, ef, es = (h, l, c, EF, ESl) if sd == 1 else (-l, -h, -c, -EF, -ESl)
                lv, st_, x1, x2 = det_pullback(hh, ll, cc, A, ef, es, ADX, float(p["adx_min"] or 0),
                                               int(p["swing_bars"]), float(p["push_atr"]), int(p["min_pull_bars"]),
                                               float(p["pull_atr"]), float(p["touch_atr"]), mk.tick,
                                               int(p["ema_slope_bars"] or 0), int(p["new_high_bars"] or 0),
                                               int(p["leg_bars"] or 0), float(p["retr_min"] or 0),
                                               float(p["retr_max"] or 0), int(p["pb_max"] or 0),
                                               float(p["sig_close"] or 0), float(p["max_risk_atr"] or 0))
                return (lv, st_, x1, x2) if sd == 1 else (-lv, -st_, x1, x2)
            out[sd] = mk.memo(key, pb) + (mk.const(len(c))[0],)
        elif s == "flag":
            key = ("fl", tf, sd) + tuple(p[k] for k in ["atr_len"] + list(SETUP_KEYS["flag"])[:7])   # pattern only
            # = det_flag, split: the slow scan once per pole_bars / flag_max_bars, then the cheap thresholds
            def fl():
                hh, ll, cc = (h, l, c) if sd == 1 else mk.memo(("mirror", tf), lambda: (-l, -h, -c))
                def raw():
                    r = flag_raw(hh, ll, int(p["pole_bars"]), int(p["flag_max_bars"]))
                    return r + ((r[4] if sd == 1 else -r[4]),)          # + the stop in real prices
                P, B, pole, FH, FL, stp = mk.memo(("flraw", tf, sd, int(p["pole_bars"]), int(p["flag_max_bars"])), raw)
                lv = flag_apply(hh, cc, A, P, pole, FH, FL, float(p["pole_atr"]), int(p["flag_min_bars"]),
                                float(p["max_retrace"]), float(p["flag_max_pct"]), float(p["flag_slope_atr"]), mk.tick)
                return (lv if sd == 1 else -lv), stp, B, P, pole
            out[sd] = mk.memo(key, fl)
        elif s == "zoneline":
            pk = ["atr_len"] + [k for k in SETUP_KEYS["zoneline"] if k not in ("entry_mode", "risk_min_atr", "risk_max_atr", "stop_mode", "stop_atr")]

            def raw(flip):
                oo, hh, ll, cc, xx, yy = _line_series(p, o, h, l, c, flip)
                return det_zoneline(oo, hh, ll, cc, xx, yy, A, int(p["pivot_bars"]), float(p["prominence_atr"]), int(p["max_pivots"]), int(p["min_touches"]),
                                    int(p["min_gap_bars"]), float(p["zone_atr"]), float(p["slope_min_atr"]), float(p["slope_max_atr"]), bool(p["bounce_need_trend"]),
                                    int(p["approach_bars"]), float(p["approach_max_atr"] or 0), bool(p["break_need_lh"]), int(p["retest_bars"]), int(p["confirm_bars"]))
            n_ = mk.memo(("zl", tf, False) + tuple(p[k] for k in pk), lambda: raw(False))
            f_ = mk.memo(("zl", tf, True) + tuple(p[k] for k in pk), lambda: raw(True))
            if sd == 1:                                  # longs: bounce at a rising line, retest of a broken falling line
                be, bs_, bo, re_, rs_, ro = n_[0], n_[1], n_[2], -f_[3], -f_[4], -f_[5]
            else:                                        # shorts: bounce at a falling line, retest of a broken rising line
                be, bs_, bo, re_, rs_, ro = -f_[0], -f_[1], -f_[2], n_[3], n_[4], n_[5]
            em = str(p["entry_mode"]).strip().lower()
            if em not in ("bounce", "retest", "both"):
                raise SystemExit(f"entry_mode must be bounce | retest | both, not {em!r}")
            if em == "bounce":
                en, st, op = be, bs_, bo
            elif em == "retest":
                en, st, op = re_, rs_, ro
            else:
                use = ~np.isnan(be)
                en = np.where(use, be, re_); st = np.where(use, bs_, rs_); op = np.where(use, bo, ro)
            out[sd] = _finish_line(p, sd, en, st, op, A, h, l)
        elif s == "multitouch":
            pk = ["atr_len"] + [k for k in SETUP_KEYS["multitouch"] if k not in ("risk_min_atr", "risk_max_atr", "stop_mode", "stop_atr")]

            def raw_mt(flip):
                oo, hh, ll, cc, xx, _ = _line_series(p, o, h, l, c, flip)
                pv_ = int(p["pivot_bars"])
                return det_multitouch(oo, hh, ll, cc, xx, A, pv_, int(p["max_pivots"]), int(p["min_touches"]), float(p["touch_atr"]), float(p["slope_min_atr"]),
                                      float(p["slope_max_atr"]), float(p["break_atr"]), float(p["prominence_atr"]), int(p["min_gap_bars"]),
                                      int(p["min_span_bars"] or 3 * pv_))
            r_ = mk.memo(("mt", tf, sd == 1) + tuple(p[k] for k in pk), lambda: raw_mt(sd == 1))   # long = break of a falling line = flipped prices
            en, st, op = (-r_[0], -r_[1], -r_[2]) if sd == 1 else r_[:3]
            out[sd] = _finish_line(p, sd, en, st, op, A, h, l)
        else:
            key = ("tl", tf, sd) + tuple(p[k] for k in ["atr_len"] + [k for k in SETUP_KEYS["trendline"]
                                                                       if k not in ("slope_min", "slope_max")])
            # 5th array = the opposite pattern extreme (only the reverse trade uses it)
            out[sd] = mk.memo(key, lambda: det_trendline(o, h, l, c, A, int(p["pv_left"]), int(p["pv_right"]),
                                                         int(p["min_span"]), int(p["max_span"]), int(p["max_age"]),
                                                         float(p["break_atr"]), float(p["line_tol_atr"] or 0), sd))
    return out, A


def eth_bars(t):
    """True for chart bars opening outside the 09:30-16:00 New York regular session."""
    m = np.asarray(t.hour * 60 + t.minute, np.int64)
    return ~((m >= 570) & (m < 960))


def htf_supertrend(m1, t, tf, htf, n, mult):
    """+1 green / -1 red / 0 unknown: Supertrend of the last completed htf-minute candle at each chart bar's close."""
    ht, ho, hh, hl, hc, _ = htf_bars(m1, int(htf))
    d = -supertrend_dir(hh, hl, hc, float(mult), int(n))      # supertrend_dir: -1 = up
    k = np.searchsorted((ht + pd.Timedelta(minutes=htf)).asi8, (t + pd.Timedelta(minutes=tf)).asi8, side="right") - 1
    out = np.zeros(len(t))
    out[k >= 0] = d[k[k >= 0]]
    return np.nan_to_num(out)


def htf_adx(m1, t, tf, htf, n=14):
    """ADX(n) of the last completed htf-minute candle at each chart bar's close (no look-ahead)."""
    ht, ho, hh, hl, hc, _ = htf_bars(m1, int(htf))
    a = adx_ind(hh, hl, hc, int(n), int(n))
    k = np.searchsorted((ht + pd.Timedelta(minutes=htf)).asi8, (t + pd.Timedelta(minutes=tf)).asi8, side="right") - 1
    out = np.full(len(t), np.nan)
    out[k >= 0] = a[k[k >= 0]]
    return out


def entry_flags(t, tf, p, cfg):
    ok, flat = bar_flags(t, tf, p["flat_time"], cfg)
    if p["trade_hours"]:                         # entries only on bars OPENING inside this New York window
        a_, b_ = (hhmm(x) for x in str(p["trade_hours"]).split("-"))
        m = np.asarray(t.hour * 60 + t.minute, np.int64)
        ok = ok & (((m >= a_) & (m < b_)) if a_ <= b_ else ((m >= a_) | (m < b_)))
    return ok, flat


def vud_share(v, o, c, n):
    """(up share, down share) of the volume of the last n bars incl. this one: up = candles closing above their open."""
    up = pd.Series(np.where(c > o, v, 0.0)).rolling(n, min_periods=n).sum().to_numpy()
    tot = pd.Series(v).rolling(n, min_periods=n).sum().to_numpy()
    den = np.maximum(1.0, tot)
    return up / den, (tot - up) / den


def session_ids(t):
    """CME session id per bar (18:00 New York starts the next day)."""
    d = (t.tz_localize(None) + pd.Timedelta(hours=6)).normalize()
    return np.asarray((d - pd.Timestamp(0)) // pd.Timedelta(days=1), np.int64)


def flag_filtered(mk, p):
    """flag signals of one parameter set with the pole-quality / trend-strength filters applied + its trend array"""
    tf = int(p["timeframe"])
    t, o, h, l, c, bstart = mk.bars(tf)
    sig, A = signals(mk, p)
    trend = mk.memo(("trend", tf, p["trend_tf"], p["trend_ema"], p["trend_slope"]),
                    lambda: htf_trend(mk.m1, t, tf, p["trend_tf"], p["trend_ema"], p["trend_slope"]))
    pkey = ("fl", tf) + tuple(p[k] for k in ["atr_len"] + list(SETUP_KEYS["flag"])[:7])
    out = {}
    for sd in (1, -1):
        lv, stp, a1, a2, pole = sig[sd]
        ix = np.flatnonzero(~np.isnan(lv))      # only the flag bars (a tiny share of all bars) are checked
        keep = np.ones(len(ix), np.bool_)
        if p["pole_min_bars"]:
            keep &= (a2[ix] - a1[ix]) >= int(p["pole_min_bars"])
        if p["pole_min_datr"]:
            DA = mk.memo(("datr", tf), lambda: daily_atr(t, h, l, c))
            with np.errstate(invalid="ignore"):
                keep &= pole[ix] / DA[ix] >= float(p["pole_min_datr"])
        if p["pole_max_bar"]:
            bb = mk.memo(("bigbar", sd) + pkey, lambda: pole_bigbar(h, l, a1, a2, pole))
            with np.errstate(invalid="ignore"):
                keep &= bb[ix] <= float(p["pole_max_bar"])
        if p["adx_min"]:
            ADX = mk.memo(("adx", tf), lambda: adx_ind(h, l, c, 14, 14))
            keep &= ADX[ix] >= float(p["adx_min"])
        if p["adx_max"]:                         # trend still young: chart ADX at most x
            ADX = mk.memo(("adx", tf), lambda: adx_ind(h, l, c, 14, 14))
            keep &= ADX[ix] <= float(p["adx_max"])
        if p["fast_slope_atr"] is not None:
            fs = mk.memo(("slope", tf, p["fast_tf"], p["fast_ema"], p["fast_slope_bars"]),
                         lambda: htf_slope(mk.m1, t, tf, p["fast_tf"], p["fast_ema"], p["fast_slope_bars"]))
            with np.errstate(invalid="ignore"):
                keep &= sd * fs[ix] >= float(p["fast_slope_atr"])
        if p["trend_slope_atr"] is not None:
            n_ = p["trend_ema"] or 50                # works without the trend filter too (EMA 50)
            ts = mk.memo(("slope", tf, p["trend_tf"], n_, p["trend_slope"] or 1),
                         lambda: htf_slope(mk.m1, t, tf, p["trend_tf"], n_, p["trend_slope"] or 1))
            with np.errstate(invalid="ignore"):
                keep &= sd * ts[ix] >= float(p["trend_slope_atr"])
        if p["min_risk_atr"]:
            with np.errstate(invalid="ignore"):
                keep &= sd * (lv[ix] - stp[ix]) / A[ix] + float(p["sl_atr"]) >= float(p["min_risk_atr"])
        out[sd] = (ix[keep], lv, stp, a1, a2, pole)    # bars that pass + the unfiltered arrays
    return out, A, trend


def rth_mask(t, hours):
    a_, b_ = (hhmm(x) for x in str(hours).split("-"))
    m = np.asarray(t.hour * 60 + t.minute, np.int64)
    return ((m >= a_) & (m < b_)) if a_ <= b_ else ((m >= a_) | (m < b_))


def flag_signals(mk, p):
    """RTH signals with the normal settings, ETH signals with the eth_* overrides, merged by the signal bar's time."""
    tf = int(p["timeframe"])
    t = mk.bars(tf)[0]
    if p["sessions"] not in ("Both", "RTH only", "ETH only"):
        raise SystemExit(f"sessions must be Both | RTH only | ETH only, not {p['sessions']!r}")
    rth = mk.memo(("rth", tf, p["rth_hours"]), lambda: rth_mask(t, p["rth_hours"]))
    pe = dict(p)
    for k in FLAG_ETH:
        v = p.get(f"eth_{k}")
        if v is not None:
            # "off" (YAML reads a bare off as False) = that filter off in ETH
            pe[k] = None if v is False or (isinstance(v, str) and v.strip().lower() == "off") else v
    s1, A, tr1 = flag_filtered(mk, p)
    if all(pe[k] == p[k] for k in FLAG_ETH):
        s2, tr2 = s1, tr1
    else:
        s2, _, tr2 = flag_filtered(mk, pe)
    off = np.zeros(len(t), np.bool_)
    use1 = rth if p["sessions"] != "ETH only" else off
    use2 = ~rth if p["sessions"] != "RTH only" else off
    out = {}
    for sd in (1, -1):
        a, b = s1[sd][1:], s2[sd][1:]
        lv = np.full(len(t), np.nan)             # RTH bars from the normal settings, ETH bars from the eth_* ones
        for s_, use in ((s1, use1), (s2, use2)):
            k = s_[sd][0]
            k = k[use[k]]
            lv[k] = s_[sd][1][k]
        # same pattern settings in RTH and ETH -> the same arrays, nothing to merge
        out[sd] = (lv,) + tuple(x if x is y else np.where(rth, x, y) for x, y in zip(a[1:], b[1:]))
    return out, A, (tr1 if tr1 is tr2 else np.where(rth, tr1, tr2)).astype(np.int8)


# settings used only by the trade simulation (exits / sizing): the prepared signal arrays never depend on them
EXIT_ONLY = ("qty1", "qty2", "trail", "trail_r", "trail_start_r", "tp2_r", "be_after_tp1", "tp1_lock_r", "tp1_lock_atr", "tp_r", "eth_tp_r",
             "eth_trail_r", "be_r", "max_bars", "tp_mode", "max_day_trades", "risk_usd",
             "short_tp_r", "short_trail_r", "tp_sl_bars", "fail_reverse_bars", "fail_mode")
_PREP = {}                                       # last prepared signal sets (big arrays -> keep only 2)


def prepared(mk, p, cfg):
    """signals + all entry filters of one parameter set, cached by every non-exit setting"""
    key = (id(mk),) + tuple((k, p[k]) for k in sorted(p) if k not in EXIT_ONLY)
    if key not in _PREP:
        while len(_PREP) >= 2:                  # sweep jobs come grouped by entry settings: 1 would do
            del _PREP[next(iter(_PREP))]
        r = _prepare(mk, p, cfg)
        if p.get("reverse"):
            r0 = r
            r = _reverse_tl(r) if p["setup"] in ("trendline", "multitouch", "zoneline") else _reverse(mk, r)
            if p.get("trend_on_trade"):          # trend filter on the TRADE's side (with the trend) instead of the signal's
                r = r[:8] + (r0[8],) + r[9:]
        if p.get("max_stop_atr"):
            r = _cap_stop(r, float(p["max_stop_atr"]), float(p["sl_atr"]))
        if p.get("risk_min_pts"):
            r = _min_stop(r, float(p["risk_min_pts"]))
        _PREP[key] = r
    return _PREP[key]


def _cap_stop(r, cap, sl):
    """max_stop_atr: the stop (pattern stop + sl_atr ATR) is at most cap x ATR from the entry level; a wider one is
    pulled in to cap ATR (the trade is still taken). Keeps R - and so TP1 / lock / trail in R - sane on wild days."""
    if cap <= sl:
        raise SystemExit(f"max_stop_atr ({cap}) must be above sl_atr ({sl})")
    LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig = r
    LS = np.maximum(LS, LL - (cap - sl) * A)    # simulate subtracts sl_atr ATR again -> final distance = cap ATR
    SS = np.minimum(SS, SL_ + (cap - sl) * A)
    return LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig


def _min_stop(r, pts):
    """risk_min_pts: drop the signals whose stop is closer than pts points to the entry level"""
    LL, LS, LT, LA, SL_, SS, ST, SA, *rest = r
    with np.errstate(invalid="ignore"):
        a = np.abs(LL - LS) >= pts
        b = np.abs(SS - SL_) >= pts
    return (np.where(a, LL, np.nan), LS, LT, LA, np.where(b, SL_, np.nan), SS, ST, SA, *rest)


def _reverse_tl(r):
    """trendline reverse: a break UP of a falling line -> SELL at that close, stop = highest high since the 1st
    pivot + sl_atr ATR (the other side of the pattern); a break DOWN -> BUY at the close, stop = lowest low since
    the 1st pivot - sl_atr ATR. Same one-trade-per-line, target tp_r x R from the new entry / stop."""
    LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig = r
    nanv = np.full(len(LL), np.nan)
    tr = np.where(trend == 2, 2, -trend).astype(np.int8)
    # new long = the old short signal (entry at the close, stop = its opposite extreme ST), new short = mirror
    return (SL_, ST, nanv, SA, LL, LT, nanv, LA, tr, A, ok, flat, day, ETH, {1: sig[-1], -1: sig[1]})


def _reverse(mk, r):
    """reverse: true = trade every pattern the OTHER way (failed-pattern trade). A long signal (buy stop at the
    pattern high, stop at the pattern low) becomes a SELL stop 1 tick below the pattern low with the stop at the
    pattern high (sl_atr beyond it); a short signal becomes a buy stop 1 tick above the pattern high. Same filters
    (they confirmed the ORIGINAL side), same one-trade-per-pattern, exits / R from the new entry and stop."""
    LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig = r
    tk = mk.tick
    nanv = np.full(len(LL), np.nan)
    nL, nLS = SS + tk, SL_ + tk                 # bear pattern -> buy stop above its high, stop at its low
    nS, nSS = LS - tk, LL - tk                  # bull pattern -> sell stop below its low, stop at its high
    tr = np.where(trend == 2, 2, -trend).astype(np.int8)
    return (nL, nLS, nanv, SA, nS, nSS, nanv, LA, tr, A, ok, flat, day, ETH, {1: sig[-1], -1: sig[1]})


def run_one(mk, p, cfg, rec, Q1=None, Q2=None):
    """one parameter set -> (stats, trades, signals). Q1 / Q2 = arrays of qty pairs (same zero pattern):
    stats then has one row per pair; without them it is the single qty1 / qty2 row."""
    tf = int(p["timeframe"])
    t, o, h, l, c, bstart = mk.bars(tf)
    LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig = prepared(mk, p, cfg)
    kind = 2 if p["setup"] in ("trendline", "multitouch", "zoneline") else 1
    co = cfg["costs"]
    single = Q1 is None
    if single:
        Q1 = np.array([float(p["qty1"])]); Q2 = np.array([float(p["qty2"])])
    fb = 0 if p["reverse"] else int(p["fail_reverse_bars"] or 0)   # reverse: true = immediate fade, no normal trade to fail
    if fb > 0 and p["setup"] != "trendline":
        raise SystemExit("fail_reverse_bars works with setup: trendline only")
    fm = str(p["fail_mode"] or "stop").strip().lower()
    if fm not in ("stop", "line"):
        raise SystemExit(f"fail_mode must be stop or line, not {fm!r}")
    LNL, SPL, LNS, SPS = (np.full(len(c), np.nan) for _ in range(4))
    if fb > 0 and fm == "line":                  # the line's level and slope at each signal bar
        for sd_, x, ln_, sp_ in ((1, h, LNL, SPL), (-1, l, LNS, SPS)):
            if sd_ in sig:
                b = np.where(sig[sd_][2] >= 0)[0]
                a1_, a2_ = sig[sd_][2][b], sig[sd_][3][b]
                sp_[b] = (x[a2_] - x[a1_]) / (a2_ - a1_)
                ln_[b] = x[a1_] + sp_[b] * (b - a1_)
    st, T = simulate(c, bstart, mk.mo, mk.mh, mk.ml, ok, flat, trend, A, day, int(p["max_day_trades"] or 0),
                     LL, LS, LT, LA, SL_, SS, ST, SA, kind, float(p["sl_atr"]), float(p["tp_r"]),
                     p["tp_mode"] == "pole", float(p["be_r"] or 0), int(p["max_bars"] or 0),
                     mk.pv, mk.tick, float(co.get("commission", 2.5)), float(co.get("slippage_ticks", 0)),
                     np.asarray(Q1, np.float64), np.asarray(Q2, np.float64), bool(p["trail"]), float(p["trail_r"]),
                     -1.0 if p["trail_start_r"] is None else float(p["trail_start_r"]), float(p["tp2_r"] or 0), bool(p["be_after_tp1"]), rec,
                     ETH, float(p["sl_atr"] if p["eth_sl_atr"] is None else p["eth_sl_atr"]),
                     float(p["tp_r"] if p["eth_tp_r"] is None else p["eth_tp_r"]),
                     float(p["trail_r"] if p["eth_trail_r"] is None else p["eth_trail_r"]), float(p["risk_usd"] or 0),
                     *(-1.0 if p.get(k) is None else float(p[k]) for k in ("short_sl_atr", "short_tp_r", "short_trail_r")),
                     float(p["tp1_lock_r"] or 0), float(p.get("tp1_lock_atr") or 0),
                     int(p["tp_sl_bars"] or 0), fb, 2 if fm == "line" else 1, float(p["max_stop_atr"] or 0), LNL, SPL, LNS, SPS)
    return (st[0] if single else st), T, sig


def _off(v):
    return None if v is False or (isinstance(v, str) and v.strip().lower() == "off") else v


def _prepare(mk, p, cfg):
    """flag + Both with short_* overrides: longs from the normal settings, shorts from the overridden ones."""
    if p["setup"] == "flag" and p["direction"] == "Both" and any(p.get(f"short_{k}") is not None for k in FLAG_SHORT):
        ps = dict(p, direction="Short only")
        for k in FLAG_SHORT:
            if p.get(f"short_{k}") is not None:
                ps[k] = _off(p[f"short_{k}"])
        L = _prepare1(mk, dict(p, direction="Long only"), cfg)
        S = _prepare1(mk, ps, cfg)
        lt, st_ = L[8], S[8]
        la = (lt == 2) | (lt == 1); sa = (st_ == 2) | (st_ == -1)
        trend = np.where(la & sa, 2, np.where(la, 1, np.where(sa, -1, 0))).astype(np.int8)
        return L[:4] + S[4:8] + (trend,) + L[9:]
    return _prepare1(mk, p, cfg)


def _prepare1(mk, p, cfg):
    tf = int(p["timeframe"])
    t, o, h, l, c, bstart = mk.bars(tf)
    if p["setup"] not in SETUPS:
        raise SystemExit(f"setup must be one of {SETUPS}, not {p['setup']!r}")
    if p["direction"] not in ("Long only", "Short only", "Both"):
        raise SystemExit(f"direction must be Long only | Short only | Both, not {p['direction']!r}")
    if p["setup"] == "flag":
        sig, A, trend = flag_signals(mk, p)
    else:
        sig, A = signals(mk, p)
        trend = None
    nanv, none = mk.const(len(c))
    # anchor = the pattern's id (pullback: swing bar, flag: pole top, trend line: 1st pivot)
    pick = 2 if p["setup"] in ("pullback", "trendline", "multitouch", "zoneline") else 3
    LL, LS, LT, LA = ((sig[1][0], sig[1][1], sig[1][4], sig[1][pick]) if p["direction"] != "Short only"
                      else (nanv, nanv, nanv, none))
    SL_, SS, ST, SA = ((sig[-1][0], sig[-1][1], sig[-1][4], sig[-1][pick]) if p["direction"] != "Long only"
                       else (nanv, nanv, nanv, none))
    if p["setup"] == "trendline" and (p["slope_min"] is not None or p["slope_max"] is not None):
        lo_ = -np.inf if p["slope_min"] is None else float(p["slope_min"])
        hi_ = np.inf if p["slope_max"] is None else float(p["slope_max"])

        def in_angle(x, s):                      # s = det_trendline result; only bars with a signal have pivots
            d = np.full(len(c), np.nan)
            b = s[2] >= 0
            d[b] = np.degrees(np.arctan(np.abs(x[s[3][b]] - x[s[2][b]]) / (s[3][b] - s[2][b])))
            return (d >= lo_) & (d <= hi_)

        if p["direction"] != "Short only":
            LL = np.where(in_angle(h, sig[1]), LL, np.nan)
        if p["direction"] != "Long only":
            SL_ = np.where(in_angle(l, sig[-1]), SL_, np.nan)
    if any(p[k] is not None for k in ("vol_min", "vol_max", "tvol_min", "tvol_max")):
        keep = np.ones(len(c), np.bool_)        # volume filter on the SIGNAL bar (known at its close)
        if p["vol_min"] is not None or p["vol_max"] is not None:
            rv = mk.rvol(tf, p["vol_len"])
            keep &= (rv >= (p["vol_min"] if p["vol_min"] is not None else -np.inf)) & \
                    (rv <= (p["vol_max"] if p["vol_max"] is not None else np.inf))
        if p["tvol_min"] is not None or p["tvol_max"] is not None:
            tv = mk.tvol(tf, p["tvol_days"])
            keep &= (tv >= (p["tvol_min"] if p["tvol_min"] is not None else -np.inf)) & \
                    (tv <= (p["tvol_max"] if p["tvol_max"] is not None else np.inf))
        LL = np.where(keep, LL, np.nan)
        SL_ = np.where(keep, SL_, np.nan)
    if p["vud_min"] is not None or p["vud_max"] is not None:
        # share of the last vud_bars bars' volume that sits in candles of the SIGNAL's direction (long: up candles
        # close > open, short: the others), known at the signal bar close. Keep vud_min < share <= vud_max.
        upL, upS = mk.memo(("vud", tf, int(p["vud_bars"])), lambda: vud_share(mk.vol(tf), o, c, int(p["vud_bars"])))
        lo_ = -np.inf if p["vud_min"] is None else float(p["vud_min"])
        hi_ = np.inf if p["vud_max"] is None else float(p["vud_max"])
        LL = np.where((upL > lo_) & (upL <= hi_), LL, np.nan)
        SL_ = np.where((upS > lo_) & (upS <= hi_), SL_, np.nan)
    if p["atr_reg_min"] is not None or p["atr_reg_max"] is not None:
        # volatility regime at the signal bar: ATR / SMA(ATR, atr_reg_bars) (5m: 5760 bars = 20 sessions)
        nreg = int(p["atr_reg_bars"])
        reg = mk.memo(("atrreg", tf, p["atr_len"], nreg),
                      lambda: A / pd.Series(A).rolling(nreg, min_periods=nreg).mean().to_numpy())
        lo_ = -np.inf if p["atr_reg_min"] is None else float(p["atr_reg_min"])
        hi_ = np.inf if p["atr_reg_max"] is None else float(p["atr_reg_max"])
        with np.errstate(invalid="ignore"):
            keep = (reg >= lo_) & (reg <= hi_)
        LL = np.where(keep, LL, np.nan)
        SL_ = np.where(keep, SL_, np.nan)
    if p["min_risk_atr"] and p["setup"] != "flag":   # stop too close (flag: inside flag_filtered) (known at the signal bar close)
        sl = float(p["sl_atr"]); mr = float(p["min_risk_atr"])
        with np.errstate(invalid="ignore"):
            LL = np.where((LL - LS) / A + sl >= mr, LL, np.nan)
            SL_ = np.where((SS - SL_) / A + sl >= mr, SL_, np.nan)
    if p["min_trend_bars"]:                      # trend too young
        if p["setup"] != "pullback":
            raise SystemExit("min_trend_bars works with setup: pullback only")
        EF = mk.memo(("ema", tf, p["ema_fast"]), lambda: ema(c, int(p["ema_fast"])))
        ESl = mk.memo(("ema", tf, p["ema_slow"]), lambda: ema(c, int(p["ema_slow"])))
        aL, aS = mk.memo(("age", tf, p["ema_fast"], p["ema_slow"]), lambda: trend_age(EF, ESl))
        LL = np.where(aL >= int(p["min_trend_bars"]), LL, np.nan)
        SL_ = np.where(aS >= int(p["min_trend_bars"]), SL_, np.nan)
    if p["max_room_r"]:                          # swing extreme too far beyond the entry level (in R)
        if p["setup"] != "pullback":
            raise SystemExit("max_room_r works with setup: pullback only")
        sl = float(p["sl_atr"]); mx = float(p["max_room_r"])
        with np.errstate(invalid="ignore", divide="ignore"):
            roomL = (h[np.clip(LA, 0, None)] - LL) / ((LL - LS) + sl * A)
            roomS = (SL_ - l[np.clip(SA, 0, None)]) / ((SS - SL_) + sl * A)
            LL = np.where(roomL > mx, np.nan, LL)
            SL_ = np.where(roomS > mx, np.nan, SL_)
    if p["setup"] == "flag":                     # flag: night (ETH) exits = entry bar outside rth_hours
        ETH = mk.memo(("eth", tf, p["rth_hours"]), lambda: ~rth_mask(t, p["rth_hours"]))
    else:
        ETH = mk.memo(("eth", tf), lambda: eth_bars(t))       # per bar: opens outside 09:30-16:00 NY
    if p["st_tf"]:                               # higher-TF Supertrend: green blocks shorts, red blocks longs
        sdir = mk.memo(("st", tf, p["st_tf"], p["st_atr"], p["st_mult"]),
                       lambda: htf_supertrend(mk.m1, t, tf, int(p["st_tf"]), p["st_atr"], p["st_mult"]))
        LL = np.where(sdir != -1, LL, np.nan)
        SL_ = np.where(sdir != 1, SL_, np.nan)
    if p["htf_adx_tf"]:                          # higher-TF trend strength for every entry
        ha = mk.memo(("htf_adx", tf, int(p["htf_adx_tf"]), int(p["htf_adx_len"])),
                     lambda: htf_adx(mk.m1, t, tf, int(p["htf_adx_tf"]), int(p["htf_adx_len"])))
        with np.errstate(invalid="ignore"):
            good = ha >= float(p["htf_adx_min"] or 0)
            if p["htf_adx_max"]:
                good &= ha <= float(p["htf_adx_max"])
        LL = np.where(good, LL, np.nan)
        SL_ = np.where(good, SL_, np.nan)
    if trend is None:
        trend = mk.memo(("trend", tf, p["trend_tf"], p["trend_ema"], p["trend_slope"]),
                        lambda: htf_trend(mk.m1, t, tf, p["trend_tf"], p["trend_ema"], p["trend_slope"]))
    ok, flat = mk.memo(("flags", tf, p["flat_time"], p["trade_hours"]), lambda: entry_flags(t, tf, p, cfg))
    day = mk.memo(("day", tf), lambda: session_ids(t))
    return LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig


def trades_frame(mk, p, T):
    t = mk.bars(int(p["timeframe"]))[0]
    df = pd.DataFrame(T, columns=["sig", "ebar", "xbar", "side", "entry", "stop", "target", "exit", "pnl", "why", "risk",
                                  "leg", "qty"])
    for k in ("sig", "ebar", "xbar", "side", "why", "leg"):
        df[k] = df[k].astype(int)
    df.insert(0, "signal_time", t[df.sig].tz_localize(None))
    df.insert(1, "exit_time", t[df.xbar].tz_localize(None))
    df["side"] = df.side.map({1: "Long", -1: "Short"})
    df["reason"] = df.why.map(REASON)
    q1, q2 = float(p["qty1"]), float(p["qty2"])
    scale = df.qty / np.where(df.leg == 1, q1 or 1.0, q2 or 1.0)        # risk_usd sizing: contracts / qty setting
    df["r"] = df.pnl / (df.risk * mk.pv * (q1 + q2) * scale)          # legs add up to the trade's R
    return df


def per_trade(df):
    """One row per entry (legs summed)."""
    return df.groupby(["sig", "ebar"], sort=False).agg(
        signal_time=("signal_time", "first"), side=("side", "first"), entry=("entry", "first"),
        risk=("risk", "first"), xbar=("xbar", "max"), pnl=("pnl", "sum"), r=("r", "sum"),
        reason=("reason", lambda x: " + ".join(x))).reset_index()


def volume_study(mk, p, E, sig):
    """Per trade (entry): how volume behaved at the signal / entry and how far the price then moved.
    rvol_* = bar volume / average of the vol_len bars before it; tvol_* = bar volume / average of the same
    time-of-day bar over the last tvol_days sessions; pb_vol = average volume of the pullback (flag) bars /
    average volume of the impulse leg (pole) bars (< 1 = volume dried up in the pullback);
    mfe_r / mae_r = best / worst move from the entry until the trade's last exit, in R (chart bars);
    mfe_n_r / mae_n_r = the same over the first study_bars chart bars from the entry bar, whatever the exit."""
    tf = int(p["timeframe"])
    t, o, h, l, c, _ = mk.bars(tf)
    v = mk.vol(tf)
    rv = mk.rvol(tf, p["vol_len"])
    tv = mk.tvol(tf, p["tvol_days"])
    n = int(p["study_bars"])
    leg_bars = int(p.get("leg_bars") or 30)
    out = {k: [] for k in ("rvol_sig", "tvol_sig", "rvol_entry", "tvol_entry", "pb_vol", "mfe_r", "mae_r",
                           "mfe_n_r", "mae_n_r")}
    for r in E.itertuples():
        sd = 1 if r.side == "Long" else -1
        out["rvol_sig"].append(rv[r.sig]); out["tvol_sig"].append(tv[r.sig])
        out["rvol_entry"].append(rv[r.ebar]); out["tvol_entry"].append(tv[r.ebar])
        a1, a2 = sig[sd][2][r.sig], sig[sd][3][r.sig]
        pbv = np.nan
        if p["setup"] == "pullback" and a1 > 0 and r.sig > a1:
            seg = slice(max(0, a1 - leg_bars), a1 + 1)
            lb = seg.start + int(np.argmin(l[seg]) if sd == 1 else np.argmax(h[seg]))
            imp = v[lb:a1 + 1].mean()
            pbv = v[a1 + 1:r.sig + 1].mean() / imp if imp > 0 else np.nan
        elif p["setup"] == "flag" and a1 >= 0 and r.sig > a2:
            imp = v[a1:a2 + 1].mean()
            pbv = v[a2 + 1:r.sig + 1].mean() / imp if imp > 0 else np.nan
        out["pb_vol"].append(pbv)
        for k, end in (("", r.xbar), ("_n", min(len(c) - 1, r.ebar + n - 1))):
            hi, lo = h[r.ebar:end + 1].max(), l[r.ebar:end + 1].min()
            fav, adv = (hi - r.entry, r.entry - lo) if sd == 1 else (r.entry - lo, hi - r.entry)
            out[f"mfe{k}_r"].append(fav / r.risk)
            out[f"mae{k}_r"].append(adv / r.risk)
    for k, val in out.items():
        E[k] = val
    return E


def print_volume_study(E, p):
    n = int(p["study_bars"])
    print(f"\n--- volume study (signal bar = the bar the order came from; move = first {n} bars after entry, in R)")
    up = (E.rvol_sig > 1).mean() * 100
    print(f"signal bar volume ABOVE its {p['vol_len']}-bar average in {up:.0f}% of trades, below in {100 - up:.0f}%")
    edges = [0, 0.8, 1.2, 1.5, 2, np.inf]
    names = ["<0.8 (low)", "0.8-1.2 (normal)", "1.2-1.5", "1.5-2", ">2 (spike)"]
    tabs = [("rvol_sig", "signal bar volume / last bars average", edges, names),
            ("tvol_sig", "signal bar volume / same time of day average", edges, names),
            ("rvol_entry", "entry bar volume / last bars average (known only after the entry bar)", edges, names)]
    if E.pb_vol.notna().any():
        tabs.append(("pb_vol", "pullback avg volume / impulse avg volume (<1 = volume dried up)",
                     [0, 0.6, 0.8, 1.0, 1.25, np.inf], ["<0.6", "0.6-0.8", "0.8-1.0", "1.0-1.25", ">1.25"]))
    for col, title, b, lab in tabs:
        g = E.assign(bucket=pd.cut(E[col], b, labels=lab, right=False)).groupby("bucket", observed=False)
        t = g.agg(trades=("r", "size"), win_pct=("pnl", lambda x: 100 * (x > 0).mean()), avg_r=("r", "mean"),
                  net=("pnl", "sum"), mfe_n_r=("mfe_n_r", "mean"), mae_n_r=("mae_n_r", "mean"),
                  hit_2r=("mfe_n_r", lambda x: 100 * (x >= 2).mean()))
        t.insert(1, "pct", 100 * t.trades / max(1, len(E)))
        t["net"] = t.net.round(0)
        print(f"\n{title}:")
        print(t.round(2).to_string())


def plot_trades(mk, p, df, sig, outdir, n):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    tf = int(p["timeframe"])
    t, o, h, l, c, _ = mk.bars(tf)
    EF = ema(c, int(p.get("ema_fast", 20))) if p["setup"] == "pullback" else None
    ESl = ema(c, int(p.get("ema_slow", 50))) if p["setup"] == "pullback" else None
    outdir.mkdir(parents=True, exist_ok=True)
    E = per_trade(df)
    for _, r in E.tail(n).iterrows():
        legs = df[(df.sig == r.sig) & (df.ebar == r.ebar)]
        sd = 1 if r.side == "Long" else -1
        a1, a2 = sig[sd][2][r.sig], sig[sd][3][r.sig]
        lo = max(0, min(a1 if a1 >= 0 else r.sig, r.sig) - 25)
        hi = min(len(c) - 1, r.xbar + 10, r.sig + 120)
        x = np.arange(lo, hi + 1)
        fig, ax = plt.subplots(figsize=(14, 6))
        up = c[x] >= o[x]
        ax.vlines(x, l[x], h[x], color="#555", lw=0.8)
        ax.bar(x[up], (c[x] - o[x])[up], bottom=o[x][up], color="#26a69a", width=0.7)
        ax.bar(x[~up], (c[x] - o[x])[~up], bottom=o[x][~up], color="#ef5350", width=0.7)
        if EF is not None:
            ax.plot(x, EF[x], color="#1e88e5", lw=1, label=f"EMA {p['ema_fast']}")
            ax.plot(x, ESl[x], color="#fb8c00", lw=1, label=f"EMA {p['ema_slow']}")
        if p["setup"] == "flag" and a1 >= 0:
            top = h[a2] if sd == 1 else l[a2]
            base = l[a1] if sd == 1 else h[a1]
            ax.plot([a1, a2], [base, top], color="#8e24aa", lw=2.5, label="pole")
            ax.add_patch(plt.Rectangle((a2 + 0.5, min(l[a2 + 1:r.sig + 1])), r.sig - a2,
                                       max(h[a2 + 1:r.sig + 1]) - min(l[a2 + 1:r.sig + 1]),
                                       fill=True, alpha=0.15, color="#8e24aa", label="flag"))
        if p["setup"] == "trendline" and a1 >= 0:
            ls = -sd if p.get("reverse") else sd          # reverse: a long comes from a line through lows
            y1 = h[a1] if ls == 1 else l[a1]
            y2 = h[a2] if ls == 1 else l[a2]
            xs = np.array([a1, r.sig + 3])
            ax.plot(xs, y1 + (y2 - y1) / (a2 - a1) * (xs - a1), color="#8e24aa", lw=2, label="trend line")
            ax.scatter([a1, a2], [y1, y2], color="#8e24aa", zorder=5)
        if p["setup"] == "pullback" and a1 >= 0:
            ax.scatter([a1], [h[a1] if sd == 1 else l[a1]], marker="v" if sd == 1 else "^", color="#8e24aa",
                       s=80, zorder=5, label="swing")
        ax.axvline(r.sig, color="#999", ls=":", lw=1)
        ax.hlines(r.entry, r.ebar, r.xbar, color="#1565c0", lw=1.5, label="entry")
        ax.hlines(r.entry - sd * r.risk, r.ebar, r.xbar, color="#c62828", lw=1.2, ls="--",
                  label="stop")
        for _, g in legs.iterrows():
            if not np.isnan(g.target):
                ax.hlines(g.target, r.ebar, r.xbar, color="#2e7d32", lw=1.2, ls="--",
                          label="TP1" if g.leg == 1 else "TP2")
            ax.scatter([g.xbar], [g.exit], marker="x", color="k" if g.leg == 1 else "#8e24aa", s=60, zorder=6)
            ax.annotate(f"leg {g.leg}: {g.reason}", (g.xbar, g.exit), fontsize=8, xytext=(4, 4),
                        textcoords="offset points")
        step = max(1, len(x) // 10)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([t[i].strftime("%m-%d %H:%M") for i in x[::step]], fontsize=8)
        ax.set_title(f"{mk.inst} {tf}m {p['setup']} {r.side}  signal {r.signal_time:%Y-%m-%d %H:%M} NY  "
                     f"-> {r.reason}  {r.r:+.2f}R  ${r.pnl:,.0f}")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.2)
        fig.tight_layout()
        fig.savefig(outdir / f"{r.signal_time:%Y%m%d_%H%M}_{r.side}.png", dpi=90)
        plt.close(fig)


def run_single(mk, p, cfg, stamp):
    t0 = time.time()
    st, T, sig = run_one(mk, p, cfg, True)
    df = trades_frame(mk, p, T)
    print(f"\n=== {mk.inst} {p['timeframe']}m {p['setup']} {p['direction']}  ({time.time() - t0:.1f}s)")
    if df.empty:
        print("no trades")
        return
    print(f"Net profit     ${st[0]:,.0f}\nProfit factor  {st[1]:.2f}  (legs)\nMax drawdown   ${st[2]:,.0f}"
          f"\nNet / max DD   {st[3]:.2f}\nTrades         {int(st[4])}  win rate {st[5]:.1f}%  avg {st[6]:+.3f} R"
          f"  ({int(st[7])} legs)")
    E = per_trade(df)
    E = volume_study(mk, p, E, sig)
    E["year"] = E.signal_time.dt.year
    yr = E.groupby("year").agg(trades=("pnl", "size"), net=("pnl", "sum"),
                                win=("pnl", lambda x: 100 * (x > 0).mean()), avg_r=("r", "mean"))
    yr["net"] = yr.net.round(0)
    print("\n" + yr.round(2).to_string())
    side = E.groupby("side").agg(trades=("pnl", "size"), net=("pnl", "sum"), avg_r=("r", "mean"))
    print("\n" + side.round(2).to_string())
    for lg in sorted(df.leg.unique()):
        d = df[df.leg == lg]
        print(f"\nleg {lg}: net ${d.pnl.sum():,.0f}  exits: " +
              ", ".join(f"{k} {v}" for k, v in d.reason.value_counts().items()))
    RESULTS.mkdir(exist_ok=True)
    base = RESULTS / f"patterns_{p['setup']}_{stamp}"
    print_volume_study(E, p)
    cols = ["sig", "ebar", "rvol_sig", "tvol_sig", "rvol_entry", "tvol_entry", "pb_vol", "mfe_r", "mae_r",
            "mfe_n_r", "mae_n_r"]
    df = df.merge(E[cols], on=["sig", "ebar"], how="left")
    df.to_csv(f"{base}_trades.csv", index=False)
    print(f"\n-> {base}_trades.csv")
    n = int(cfg.get("plot") or 0)
    if n:
        plot_trades(mk, p, df, sig, base, n)
        print(f"-> {base}/  ({min(n, len(E))} charts of the last trades)")


_W = {}


def low_priority():
    """Run this process below normal priority so the PC stays usable (Windows: BELOW_NORMAL, else nice 10)."""
    try:
        if sys.platform == "win32":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)     # BELOW_NORMAL_PRIORITY_CLASS
        else:
            import os
            os.nice(10)
    except Exception:
        pass


def free_ram_gb():
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                            ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]
            m = MS(); m.len = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.avail / 2 ** 30
        import os
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2 ** 30
    except Exception:
        return None


def pick_workers(cfg, combos):
    """`workers: auto` (or missing) = CPU cores - 2, also limited by free RAM (a worker needs ~1.2 GB on 3m+
    charts, ~2 GB with 1m / 2m charts) and always leaving ~3 GB free. A number = exactly that many."""
    import os
    w = cfg.get("workers", "auto")
    if w not in (None, "auto", "max"):
        return max(1, int(w))
    cores = os.cpu_count() or 2
    need = 2.0 if min(int(c["timeframe"]) for c in combos) <= 2 else 1.2
    free = free_ram_gb()
    keep = float(cfg.get("keep_free_gb", 3))     # RAM left free for the PC
    by_ram = int((free - keep) // need) if free else cores
    # max = every logical core (PC left alone during the run), auto = 2 cores kept free
    return max(1, min(cores if w == "max" else cores - 2, by_ram))


def _worker_init(inst, cfg, base=None):
    low_priority()
    _W["mk"] = Market(inst, cfg)
    _W["cfg"] = cfg
    _W["base"] = base or {}


def _run_jobs(mk, cfg, jobs):
    """jobs = [(params, [combo index...], Q1, Q2)] -> [(indices, stats rows)]"""
    return [(ix, run_one(mk, p, cfg, False, Q1, Q2)[0]) for p, ix, Q1, Q2 in jobs]


def _worker_run(jobs):
    # jobs carry only the swept settings; the fixed ones came once with the worker start
    b = _W["base"]
    return _run_jobs(_W["mk"], _W["cfg"], [({**b, **d}, ix, Q1, Q2) for d, ix, Q1, Q2 in jobs])


def make_jobs(combos, swept=None):
    """combos that differ only in qty1 / qty2 (with the same legs switched on) share one simulation; jobs with the
    same entry settings are kept next to each other so the prepared signals are built once.
    swept = the settings that differ between combos (the others are equal everywhere -> not compared)."""
    keys = list(combos[0]) if swept is None else list(swept)
    nq = [k for k in keys if k not in ("qty1", "qty2")]
    pks = [k for k in sorted(keys) if k not in EXIT_ONLY]
    groups = {}
    for n, p in enumerate(combos):
        q1, q2 = float(p["qty1"]), float(p["qty2"])
        base = tuple(p[k] for k in nq) + (q1 > 0, q2 > 0)
        g = groups.setdefault(base, [p, [], [], []])
        g[1].append(n); g[2].append(q1); g[3].append(q2)
    by_prep = {}
    for p, ix, q1, q2 in groups.values():
        pk = tuple(p[k] for k in pks)
        by_prep.setdefault(pk, []).append((p, ix, np.array(q1), np.array(q2)))
    return [j for js in by_prep.values() for j in js]


def run_sweep(combos, cfg, stamp, min_trades, inst, swept=None):
    t0 = time.time()
    workers = pick_workers(cfg, combos)
    jobs = make_jobs(combos, swept)
    print(f"  prepared in {time.time() - t0:.0f}s")
    print(f"  {len(jobs):,} simulations (qty1 / qty2 pairs share one run)")
    mk = Market(inst, cfg) if workers <= 1 or len(jobs) < 300 else None   # parallel: only the workers load data
    stats = [None] * len(combos)
    done = 0

    def collect(res):
        nonlocal done
        for ix, st in res:
            for n, row in zip(ix, st):
                stats[n] = row
            done += len(ix)

    if workers <= 1 or len(jobs) < 300:
        step = max(1, len(jobs) // 10)
        for a in range(0, len(jobs), step):
            collect(_run_jobs(mk, cfg, jobs[a:a + step]))
            el = time.time() - t0
            print(f"  {done:,}/{len(combos):,}  {el / 60:.1f} min, ~{el / done * (len(combos) - done) / 60:.1f} min left")
    else:
        # contiguous chunks: each worker sees a few entry settings with all their exits (signals prepared once)
        from concurrent.futures import ProcessPoolExecutor
        size = max(10, min(400, -(-len(jobs) // (workers * 4))))   # >= 4 chunks per worker
        # only the swept settings travel to the workers; the fixed ones go once per worker (base)
        sw = list(combos[0]) if swept is None else list(swept)
        if "qty1" not in sw: sw.append("qty1")
        if "qty2" not in sw: sw.append("qty2")
        base = {k: v for k, v in combos[0].items() if k not in sw}
        jobs = [({k: p[k] for k in sw}, ix, q1, q2) for p, ix, q1, q2 in jobs]
        chunks = [jobs[a:a + size] for a in range(0, len(jobs), size)]
        print(f"  {workers} workers (low priority), {len(chunks)} chunks (each worker loads the data first)")
        # workers use this file imported by its real name (not "__mp_main__"): otherwise numba's on-disk cache
        # stores "__mp_main__" and a later normal run fails with "No module named '__mp_main__'"
        import patterns_backtest as _me
        nxt, last = 0.0, time.time()
        with ProcessPoolExecutor(workers, initializer=_me._worker_init, initargs=(inst, cfg, base)) as ex:
            for res in ex.map(_me._worker_run, chunks):
                collect(res)
                # a line every ~5 % or every 2 minutes, whichever comes first (and on the first chunk)
                if done / len(combos) >= nxt or time.time() - last > 120 or done == len(combos):
                    nxt = done / len(combos) + 0.05
                    last = time.time()
                    el = time.time() - t0
                    print(f"  {done:,}/{len(combos):,}  {el / 60:.1f} min, ~{el / done * (len(combos) - done) / 60:.1f} min left")
    rows = [{**p, **dict(zip(STAT_COLS, st))} for p, st in zip(combos, stats)]
    df = pd.DataFrame(rows).sort_values("net_profit", ascending=False)
    fixed = [c for c in df.columns if c not in STAT_COLS and df[c].nunique(dropna=False) <= 1]
    show = [c for c in df.columns if c not in fixed]
    pd.set_option("display.width", 250)
    print(f"\n=== {inst} {len(df)} combinations, {time.time() - t0:.0f}s")
    print("\nTop 5 by net profit:")
    print(df[show].head(5).round(3).to_string(index=False))
    dd = df[(df.net_profit > 0) & (df.trades >= min_trades)].sort_values("net_to_dd", ascending=False)
    print(f"\nTop 5 by net profit / max DD (profitable, >= {min_trades} trades):")
    print(dd[show].head(5).round(3).to_string(index=False))
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"patterns_sweep_{combos[0]['setup']}_{stamp}.csv"
    df.to_csv(out, index=False)
    print(f"\n-> {out}")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = Path(sys.argv[1])
    cfg = load_cfg(path)
    cfg.setdefault("costs", {})
    cfg.setdefault("data", {})
    s = cfg["strategy"]
    setup = s.get("setup", "pullback")
    allowed = {**COMMON, **SETUP_KEYS.get(setup, {}), **(FLAG_EXTRA if setup == "flag" else {})}
    bad = [k for k in s if k not in allowed]
    if bad:
        raise SystemExit(f"strategy: unknown setting(s) {bad} for setup {setup} (allowed: {list(allowed)})")
    def clean(x):                                # "null" / "none" / "" written in quotes = off as well
        return None if isinstance(x, str) and x.strip().lower() in ("null", "none", "") else x
    grid = {k: [clean(x) for x in values(s.get(k, v))] for k, v in allowed.items()}
    # pattern settings change slowest, exits fastest -> each pattern is detected once, then all exits reuse it
    first = ["setup", "timeframe", "atr_len"] + list(SETUP_KEYS.get(setup, {}))
    if setup == "flag":                          # the slow flag scan depends on these two only -> loop them slowest
        first = first[:3] + ["pole_bars", "flag_max_bars"] + [k for k in first[3:] if k not in ("pole_bars", "flag_max_bars")]
    order = first + [k for k in grid if k not in first]
    # settings that do nothing while their switch is off -> one combination instead of many identical ones
    idle = [("st_tf", ("st_atr", "st_mult")), ("htf_adx_tf", ("htf_adx_len", "htf_adx_min", "htf_adx_max"))]
    idle = [(sw, deps) for sw, deps in idle if None in grid[sw]]
    # only the swept settings (lists) are looped / compared: the fixed ones are one shared dict (fast + less RAM)
    swept = [k for k in order if len(grid[k]) > 1 or any(k in deps for _, deps in idle)]
    fixed = {k: grid[k][0] for k in order}
    loop = [k for k in order if len(grid[k]) > 1]
    combos = [{**fixed, **dict(zip(loop, v))} for v in itertools.product(*(grid[k] for k in loop))]
    n_all = len(combos)
    if grid.get("retr_min", [None]) != [None] and grid.get("retr_max", [None]) != [None]:
        combos = [c for c in combos               # retrace min >= max can never trade
                  if not (c.get("retr_min") and c.get("retr_max") and c["retr_min"] >= c["retr_max"])]
    if idle:
        seen, uniq = set(), []
        for c in combos:
            for sw, deps in idle:
                if c[sw] is None:
                    for k in deps:
                        c[k] = allowed[k]
            key = tuple(c[k] for k in swept)
            if key not in seen:
                seen.add(key); uniq.append(c)
        combos = uniq
    if not combos:
        raise SystemExit("no valid combination (retr_min must be below retr_max)")
    if n_all > 1:
        print(f"{len(combos):,} combinations per market" +
              (f" ({n_all - len(combos):,} skipped: retr_min >= retr_max or duplicates of an OFF filter)" if n_all > len(combos) else ""))
    low_priority()                               # keep the PC usable while it runs
    insts = cfg["data"].get("instrument", "ES")
    for inst in (insts if isinstance(insts, list) else [insts]):
        stamp = f"{inst}_{datetime.now():%Y%m%d_%H%M%S}"
        if len(combos) == 1:
            run_single(Market(inst, cfg), combos[0], cfg, stamp)
        else:
            run_sweep(combos, cfg, stamp, int(cfg.get("min_trades", 100)), inst, swept)


if __name__ == "__main__":
    main()
