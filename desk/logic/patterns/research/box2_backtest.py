"""Sideways-box breakout backtest with 3 legs, on NQ or ES (same data and fills as others/orb_backtest.py).

    python box/box_backtest.py                          # settings from box/box_ES_params.yaml (NQ: box/box_NQ_params.yaml)
    python box/box_backtest.py other.yaml

Any value under `strategy:` written as a list or {from, to, step} -> every combination is tested
(results/box_sweep_<time>.csv, highest net profit first). Otherwise one run -> summary + list of trades.

Rules:
  * box = the last `box_bars` chart bars before the current one; top/bottom = their highest/lowest close
    (box_edges: close) or high/low (box_edges: wick);
  * sideways: box height <= max_box_atr x ATR(box_atr_len) of the bar before (null = no check);
  * breakout: a bar CLOSES above top + break_buffer x ATR(atr_len) (long) or below bottom - buffer (short),
    and the bar before was not already a breakout (fresh break); fills at that close (+ slippage);
  * trend filter: long only when the close is above EMA(ema_len), short only below (null = off);
  * one position at a time; no entries on bars opening 16:45-18:00 New York or from flat_time to 18:00;
  * stop = sl_box_pct % of the box height from the entry; TP1 / TP3 = tp1_mult / tp3_mult x that stop distance (R);
  * 3 legs of qty1 / qty2 / qty3: leg 1 takes profit at TP1, then the stop of legs 2 + 3 moves to entry;
    leg 2 has no target: the first bar that CLOSES below EMA(leg2_ema_len) (above it for shorts) closes leg 2 at
    that close, and leg 3 starts trailing, giving back trail_pct % of the best open profit since the entry
    (trail_pct 60 = secure 40 % of the move), until the trail stop or TP3;
  * reverse: if the breakout's ORIGINAL stop is hit within reverse_bars bars after the entry bar, the position
    is turned around at the stop fill with the same stop / target distances (once per breakout; never after a
    break-even or trailing stop);
  * flat_time closes everything (null = hold); fills on the 1-minute bars as in orb_backtest.py;
  * max_hold_hours: a position open this many MARKET hours (chart bars x timeframe; closed hours / weekends not
    counted) is closed at that bar's close, exit "Max hold" (null = no limit). 6A / ES trade ~23 h a day.
Short exits (every short position: breakout shorts AND reverse shorts; null = same as the long setting):
  short_tp1_mult, short_tp3_mult, short_trail_pct, short_leg2_ema_len; short_tp2_mult = leg 2 of shorts takes profit
  at this R instead of the EMA exit, and leg 3 starts trailing from there (null = EMA exit as longs).
  tp2_mult (box2 addition): leg 2 of EVERY position (long and short) takes profit at this R instead of the EMA exit,
  leg 3 trails from there = 3 fixed targets (null = off; do not combine with short_tp2_mult).
  Targets must rise for shorts too: TP1 < TP2 < TP3. Longs always use tp1_mult / leg2_ema_len / tp3_mult / trail_pct.
  short_sl_box_pct: stop of shorts in % of the box height (null = sl_box_pct; R of a short = this distance). With it set,
  a reverse trade takes the stop of ITS side (reverse short: short_sl_box_pct, reverse long: sl_box_pct) instead of the
  failed breakout's stop distance.
Leg 3 guard (null = off): when leg 2 leaves on the EMA close and that close is less than leg3_guard_r R from the entry,
  leg 3 does not trail yet: its stop = the nearer of break-even and leg3_guard_sl_r R from that close; the trail_pct
  trail starts once the price is leg3_guard_r R from the entry again. EMA close >= leg3_guard_r R -> trail as before.
Entry filters (breakouts only, null = off): skip_hours "HH:MM-HH:MM" (New York, bar open; may wrap midnight);
  min_box_atr: box height >= this x ATR(box_atr_len); min_squeeze: ATR(atr_len) / ATR(box_atr_len) >= this
  (volatility expanding); max_trend_er: skip when the daily efficiency ratio of the last er_days sessions
  (|net move| / sum of |daily moves|, up to yesterday) is above this (market already trending hard).
be_mult: once the trade is be_mult R in profit (before TP1) the stop moves to entry (no reverse after that stop).
risk_usd: size every trade to this $ risk: qty1-3 x N contracts, N = floor(risk_usd / stop $ per contract), 1..max_mult.
Whole-trade risk sizing (sizes DOWN too; null = off; either one replaces risk_usd):
  risk_trade_usd: every trade risks this $ at its initial stop (all 3 legs together);
  risk_pct: every trade risks this % of the account = start_capital + closed P&L (compounding).
  Contracts per qty unit N = that risk / (stop $ per contract x (qty1 + qty2 + qty3)), rounded down to size_step
  (0.1 = 1 micro per unit), size_step..max_mult; the stop used for sizing is at least size_atr_floor x ATR(atr_len)
  (a tiny / zero box - e.g. 2020 limit days - would otherwise get a huge size; null = no floor).
  The part of N below 1 trades as micros (1/10 size) and pays costs.micro_commission per micro per fill. max_dd_pct = max drawdown in % of the account peak.
IV filter (breakouts only, null = off): implied volatility = the CBOE index of the instrument (ES VIX, NQ VXN, GC GVZ, CL OVX,
  data/iv/<NAME>.csv, refresh with tools/download_iv.py), its close of the last trading day BEFORE the bar's session
  (no look-ahead). iv_min / iv_max: that close must be >= iv_min / <= iv_max; ivr_min / ivr_max: its IV percentile
  (0-100 = % of the last ivr_days closes at or below it) must be >= ivr_min / <= ivr_max.
"""
import os
import itertools
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from numba import njit, prange

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "others"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import filters as FLT  # noqa: E402  (box2: entry-condition filters + score sizing)
from orb_backtest import (NY, REASONS, RESULTS, STATS, atr, back_adjust, bar_flags, book, chart_bars, fmt_stats,
                          hhmm, load_1m, sides, values)

HERE = Path(__file__).resolve().parent
CHUNK = 2000                               # sweep combinations per progress line
REASONS = REASONS + ["EMA exit", "Max hold"]   # 8: leg 2 closed beyond its EMA, 9: max_hold_hours reached
BIG = HERE.parent / "data"                    # every instrument's 1m data: data/<ROOT>_1min.parquet
IVDIR = HERE.parent / "data" / "iv"
IV_INDEX = {"ES": "VIX", "NQ": "VXN", "GC": "GVZ", "CL": "OVX",
            "YM": "VIX", "RTY": "VIX", "NKD": "VIX", "SI": "GVZ"}   # box2: proxies for markets without their own index
SPECS = {"ES": (50.0, 0.25), "NQ": (20.0, 0.25), "GC": (100.0, 0.10), "CL": (1000.0, 0.01),   # $ per point, tick size
         "YM": (5.0, 1.0), "RTY": (50.0, 0.10), "NKD": (5.0, 5.0), "SI": (5000.0, 0.005),
         "6E": (125000.0, 0.00005), "6A": (100000.0, 0.00005), "ZB": (1000.0, 0.03125), "ZN": (1000.0, 0.015625),
         "ZC": (50.0, 0.25), "ZS": (50.0, 0.25)}     # ZC / ZS: price in cents per bushel, 5,000 bushels
DEFAULTS = {"timeframe": 5, "box_bars": 30, "box_edges": "wick", "max_box_atr": None, "box_atr_len": 150,
            "break_buffer": 1.5, "ema_len": None, "direction": "Both", "reverse": False, "reverse_bars": 10,
            "atr_len": 14, "sl_box_pct": 50, "tp1_mult": 3, "leg2_ema_len": 200, "tp3_mult": 30, "trail_pct": 60,
            "qty1": 1, "qty2": 2, "qty3": 3, "flat_time": None,
            "vol_fast": 5, "vol_slow": 30, "vol_pct": None, "vol_avg": "sma",
            "skip_hours": None, "min_box_atr": None, "min_squeeze": None, "max_trend_er": None, "er_days": 20,
            "be_mult": None, "risk_usd": None, "max_mult": 3,
            "iv_min": None, "iv_max": None, "ivr_min": None, "ivr_max": None, "ivr_days": 252,
            "tp2_mult": None, "short_tp1_mult": None, "short_tp2_mult": None, "short_tp3_mult": None, "short_trail_pct": None,
            "short_leg2_ema_len": None, "leg3_guard_r": None, "leg3_guard_sl_r": 1,
            "risk_trade_usd": None, "risk_pct": None, "start_capital": 100000, "size_step": 0.1,
            "size_atr_floor": 1, "short_sl_box_pct": None, "max_hold_hours": None}
DEFAULTS.update(FLT.DEFAULTS)              # box2: f_* filters, f_min_score, size_up_score / size_up_mult
QCOLS = ["qty1", "qty2", "qty3"]
STATS = STATS + ["max_dd_pct"]             # max drawdown in % of the account peak (start_capital + P&L)


# ----------------------------------------------------------------------------- indicators
@njit(cache=True)
def ema(c, n):
    """ta.ema, seeded with the SMA of the first n closes."""
    out = np.full(len(c), np.nan)
    a = 2.0 / (n + 1)
    s = 0.0
    for i in range(len(c)):
        if i < n - 1:
            s += c[i]
        elif i == n - 1:
            out[i] = (s + c[i]) / n
        else:
            out[i] = a * c[i] + (1 - a) * out[i - 1]
    return out


def vol_ratio(v, fast, slow, kind):
    """Average volume of the last `fast` bars / of the last `slow` bars, both windows ending with (and including)
    the current bar; kind sma = simple average, ema = ta.ema of the volume."""
    if kind not in ("sma", "ema"):
        raise SystemExit(f"vol_avg must be sma | ema, not {kind!r}")
    if kind == "sma":
        a, b = (pd.Series(v).rolling(n).mean().to_numpy() for n in (fast, slow))
    else:
        a, b = ema(v, fast), ema(v, slow)
    with np.errstate(divide="ignore", invalid="ignore"):
        return a / b


def hours_ok(t, skip):
    """Per chart bar: False when its open (New York) is inside skip "HH:MM-HH:MM" (may wrap midnight)."""
    if is_off(skip):
        return np.ones(len(t), np.bool_)
    a, b = (hhmm(x) for x in str(skip).split("-"))
    m = np.asarray(t.hour * 60 + t.minute, np.int64)
    inside = (m >= a) & (m < b) if a < b else (m >= a) | (m < b)
    return ~inside


def trend_er(t, c, days):
    """Per chart bar: efficiency ratio of the daily closes of the `days` sessions before the bar's session
    (CME session = 18:00 -> 17:00 New York): |close[d-1] - close[d-1-days]| / sum of |daily changes|."""
    sess = (t.tz_localize(None) + pd.Timedelta(hours=6)).normalize()
    close = pd.Series(c).groupby(np.asarray(sess)).last()
    ch = close.diff()
    er = (close - close.shift(days)).abs() / ch.abs().rolling(days).sum()
    return er.shift(1).reindex(np.asarray(sess)).to_numpy()


def implied_vol(t, instrument, days):
    """Per chart bar: close of the instrument's CBOE IV index on the last trading day before the bar's CME session
    (18:00 -> 17:00 New York), and its percentile among the last `days` closes (0-100)."""
    if instrument not in IV_INDEX:                       # no IV index wired for this market: IV filters see nan
        return np.full(len(t), np.nan), np.full(len(t), np.nan)
    path = IVDIR / f"{IV_INDEX[instrument]}.csv"
    if not path.exists():
        raise SystemExit(f"{path} not found: run python tools/download_iv.py")
    d = pd.read_csv(path)
    s = pd.Series(pd.to_numeric(d.iloc[:, -1], errors="coerce").to_numpy(),
                  pd.to_datetime(d.iloc[:, 0], format="%m/%d/%Y")).dropna().sort_index()
    rank = s.rolling(int(days)).rank(pct=True) * 100
    sess = pd.DatetimeIndex((t.tz_localize(None) + pd.Timedelta(hours=6)).normalize())
    u = sess.unique()
    k = np.searchsorted(s.index.values, u.values, side="left") - 1     # last IV day strictly before the session
    lvl = np.where(k >= 0, s.to_numpy()[np.maximum(k, 0)], np.nan)
    rk = np.where(k >= 0, rank.to_numpy()[np.maximum(k, 0)], np.nan)
    pos = u.get_indexer(sess)
    return lvl[pos], rk[pos]


def box_edges(h, l, c, n, edges):
    """Top / bottom of the n bars before each bar (nan until there are n bars)."""
    if edges not in ("close", "wick"):
        raise SystemExit(f"box_edges must be close | wick, not {edges!r}")
    hi, lo = (c, c) if edges == "close" else (h, l)
    top = pd.Series(hi).rolling(n).max().shift(1).to_numpy()
    bot = pd.Series(lo).rolling(n).min().shift(1).to_numpy()
    return top, bot


@njit(cache=True)
def bar_path(bstart, mh, ml, mc):
    """Per chart bar, from its 1-minute bars: high, low, highest / lowest close, and the largest drop from an earlier
    close to a later low (long) / rise from an earlier close to a later high (short) inside the bar. Lets the
    simulation pass a quiet bar (no target, stop or new best touched) in one step with the same results."""
    n = len(bstart) - 1
    out = np.full((6, n), np.nan)
    for i in range(n):
        hi = -np.inf
        lo = np.inf
        cmax = -np.inf
        cmin = np.inf
        dl = -np.inf
        ds = -np.inf
        for j in range(bstart[i], bstart[i + 1]):
            hi = max(hi, mh[j])
            lo = min(lo, ml[j])
            dl = max(dl, cmax - ml[j])
            ds = max(ds, mh[j] - cmin)
            cmax = max(cmax, mc[j])
            cmin = min(cmin, mc[j])
        out[0, i], out[1, i], out[2, i], out[3, i], out[4, i], out[5, i] = hi, lo, cmax, cmin, dl, ds
    return out


# ----------------------------------------------------------------------------- simulation
@njit(cache=True)
def realized(QS, q, acc, col):
    """Sum over legs of quantity x acc[leg, col] for quantity set q."""
    r = 0.0
    for k in range(3):
        r += QS[q, k] * acc[k, col]
    return r


@njit(cache=True)
def mark(QS, acc, leg_open, side, entry, pv, adv, fav, inner, peak, max_dd, mult, cap0, max_ddp):
    """Equity / drawdown of every quantity set QS[q] = (qty1, qty2, qty3) in a trade: worst price adv (and the drop
    of inner points inside the bar), best price fav. acc[k, 0] = realized P&L per contract of leg k."""
    for q in range(len(QS)):
        real = realized(QS, q, acc, 0)
        oq = (leg_open[0] * QS[q, 0] + leg_open[1] * QS[q, 1] + leg_open[2] * QS[q, 2]) * mult
        dd = peak[q] - (real + side * (adv - entry) * oq * pv)
        if inner * oq * pv > dd:
            dd = inner * oq * pv
        if dd > max_dd[q]:
            max_dd[q] = dd
        if cap0 > 0 and dd / (cap0 + peak[q]) > max_ddp[q]:
            max_ddp[q] = dd / (cap0 + peak[q])
        eq = real + side * (fav - entry) * oq * pv
        if eq > peak[q]:
            peak[q] = eq


@njit(cache=True)
def mark_flat(QS, acc, peak, max_dd, cap0, max_ddp):
    """Equity / drawdown of every quantity set when flat."""
    for q in range(len(QS)):
        real = realized(QS, q, acc, 0)
        if real > peak[q]:
            peak[q] = real
        if peak[q] - real > max_dd[q]:
            max_dd[q] = peak[q] - real
        if cap0 > 0 and (peak[q] - real) / (cap0 + peak[q]) > max_ddp[q]:
            max_ddp[q] = (peak[q] - real) / (cap0 + peak[q])


@njit(cache=True)
def simulate(c, bstart, ok, flat, mo, mh, ml, mc, bpath, ATRS, BATRS, TOPS, BOTS, EMAS,
             ia, ib, ix, ie, il2, max_box, buf, allow_long, allow_short, sl_pct, tp1, tp3, trail, rev, rev_bars,
             VR, iv, vmin, eok, ER, IVL, IVR, FL, FS, max_er, min_box, min_sq, be, risk, max_mult, iv_lo, iv_hi, ivr_lo, ivr_hi,
             s_tp1, s_tp2, s_tp3, s_trail, s_il2, g_r, g_sl, r_tr, r_pc, cap0, s_step, s_floor, s_slp, hold, cmask, min_sc, up_sc, up_m, bx_min, QS, comm, mcomm, pv, tk, slip, rec):
    """Indicator rows: ATRS[ia], BATRS[ib], TOPS / BOTS[ix], trend EMA EMAS[ie] (-1 = off), leg 2 EMA EMAS[il2].
    max_box = nan when off. Volume filter: enter only when VR[iv] (fast / slow average volume) > vmin (nan = off).
    eok = breakout allowed per bar (skip_hours), ER = daily efficiency ratio per bar; max_er, min_box, min_sq,
    be (break-even trigger in R), risk ($ per trade) = nan when off. IVL / IVR = IV level / percentile per bar,
    filtered by iv_lo..iv_hi and ivr_lo..ivr_hi (nan = off). Sizing of the whole trade (nan = off): r_tr = $ risk,
    r_pc = % of the account (cap0 + closed P&L of quantity set 0), in steps of s_step per qty unit, s_step..max_mult,
    sized on a stop of at least s_floor x ATR(atr_len);
    the part below 1 contract per unit trades as micros (1/10) at mcomm per micro per fill. tk = tick size. QS = quantity sets (qty1-3), one per row, all with the same legs at 0 (quantities do
    not change the trades, only their size): one simulation gives the stats of every set.
    Returns stats (one row per set, STATS order), one row per leg (entry/exit 1m row, side, prices, pnl PER
    CONTRACT, reason, leg) and, when rec, one row per entry (entry 1m row, box height, box / box ATR, ATR, reverse)."""
    n = len(c)
    T = np.empty((6 * n + 6 if rec else 1, 8))
    E = np.empty((2 * n if rec else 1, 6))
    nt = 0
    ne = 0
    acc = np.zeros((3, 5))            # per leg, contract: realized, gross profit, gross loss, wins, trades
    live = np.zeros(3, np.bool_)      # legs that trade
    for k in range(3):
        live[k] = QS[0, k] > 0
    sl_px = slip * tk

    leg_open = np.zeros(3, np.bool_)
    reached = np.zeros(3, np.bool_)   # targets touched in the current 1m bar (allocated once: speed)
    tps = np.zeros(3)                 # TP1, leg 2 (no target: 1e9 away), TP3
    # exits of the open position: the long settings, or for shorts the short_* ones (nan / -1 = as longs)
    x_tp1 = tp1
    x_tp2 = np.nan                    # nan = leg 2 leaves on the EMA close
    x_tp3 = tp3
    x_trail = trail
    x_il2 = il2
    guard = False                     # leg 3 guard: fixed stop, no trail until the price is g_r R from the entry
    side = 0
    entry = 0.0
    entry_row = 0
    stage = 0                         # 0 = initial stop, 1 = TP1 reached (stop at entry), 2 = leg 3 trailing
    stop = 0.0
    best = 0.0
    peak = np.zeros(len(QS))
    max_dd = np.zeros(len(QS))
    max_ddp = np.zeros(len(QS))       # max drawdown in % of the account peak (cap0 > 0)
    sumq = QS[0, 0] + QS[0, 1] + QS[0, 2]
    cm = comm                         # commission per contract per fill of the open position
    entries = 0
    i_entry = 0                       # chart bar of the breakout entry
    i_open = 0                        # chart bar the open position started (breakout or reverse): max hold
    unit = 0.0                        # stop distance = 1 R
    height = 0.0
    ext = 0.0                         # best price the trade reached
    is_rev = False
    stop_px = 0.0
    stop_stage = 0
    mult = 1.0                        # contracts per unit of qty1-3 (risk_usd sizing)
    be_on = False                     # stop moved to entry by be_mult

    for i in range(n):
        # ---- quiet bar in a trade: no target, stop, stage change or new best -> update equity in one step
        quiet = False
        if side != 0:
            fx = bpath[0, i] if side > 0 else bpath[1, i]
            ax = bpath[1, i] if side > 0 else bpath[0, i]
            quiet = side * (ax - stop) > 0 and (stage == 2 and side * (fx - best) <= 0 or stage < 2)
            if guard and side * (fx - entry) >= g_r * unit:
                quiet = False                                      # the trail starts in this bar
            for k in range(3):
                if side * (fx - tps[k]) >= 0 and (leg_open[k] or (k == 0 and stage == 0) or (k == 1 and stage == 1)):
                    quiet = False
            if quiet:
                ext = max(ext, fx) if side > 0 else min(ext, fx)
                mark(QS, acc, leg_open, side, entry, pv, ax, bpath[2, i] if side > 0 else bpath[3, i],
                     bpath[4, i] if side > 0 else bpath[5, i], peak, max_dd, mult, cap0, max_ddp)
        # ---- intrabar on the 1-minute bars: O -> nearer extreme -> other extreme -> C
        for j in range(bstart[i], bstart[i + 1] if side != 0 and not quiet else bstart[i]):   # flat / quiet: skip
            if side == 0:
                continue
            fav = mh[j] if side > 0 else ml[j]
            adv = ml[j] if side > 0 else mh[j]
            high_first = mh[j] - mo[j] <= mo[j] - ml[j]
            fav_first = high_first if side > 0 else not high_first
            reached[0] = reached[1] = reached[2] = False
            stopped = False
            ext = max(ext, fav) if side > 0 else min(ext, fav)
            for step in range(2):
                if (step == 0) == fav_first:                       # favourable part of the path: targets
                    for k in range(3):
                        if side * (fav - tps[k]) >= 0:
                            reached[k] = True
                            if leg_open[k]:
                                px = mo[j] if side * (mo[j] - tps[k]) >= 0 else tps[k]
                                nt = book(acc[k], T, nt, rec, entry_row, j, side, entry, px, mult, cm, pv, k, k)
                                leg_open[k] = False
                elif side * (adv - stop) <= 0:                     # adverse part: stop for every open leg
                    px = (mo[j] if side * (mo[j] - stop) <= 0 else stop) - side * sl_px
                    for k in range(3):
                        if leg_open[k]:
                            nt = book(acc[k], T, nt, rec, entry_row, j, side, entry, px, mult, cm, pv,
                                      4 if be_on and stage == 0 else 3 + stage, k)
                            leg_open[k] = False
                            stopped = True
                    stop_px = px
                    stop_stage = 1 if be_on and stage == 0 else stage
            mark(QS, acc, leg_open, side, entry, pv, adv, mc[j], -np.inf, peak, max_dd, mult, cap0, max_ddp)
            if not (leg_open[0] or leg_open[1] or leg_open[2]):
                # reverse: the breakout's original stop hit within rev_bars bars of the entry -> the stop order
                # turns the position around at the same fill, once per breakout, same stop / target distances
                if (rev and stopped and stop_stage == 0 and not reached[0] and not is_rev
                        and i - i_entry <= rev_bars and ok[i] and not flat[i]):
                    d = -side
                    side = d
                    entry = stop_px
                    entry_row = j
                    entries += 1
                    is_rev = True
                    i_open = i
                    if not np.isnan(s_slp):            # own stop per side: the reversed side's % of the box
                        pct = s_slp if d < 0 else sl_pct
                        unit = max(1.0, np.floor(pct / 100.0 * height / tk + 0.5)) * tk
                    if d > 0:
                        x_tp1, x_tp2, x_tp3, x_trail, x_il2 = tp1, (-s_tp2 if s_tp2 < 0 else np.nan), tp3, trail, il2
                    else:
                        x_tp1 = tp1 if np.isnan(s_tp1) else s_tp1
                        x_tp2 = abs(s_tp2)
                        x_tp3 = tp3 if np.isnan(s_tp3) else s_tp3
                        x_trail = trail if np.isnan(s_trail) else s_trail
                        x_il2 = il2 if s_il2 < 0 else int(s_il2)
                    tps[0] = entry + d * max(1.0, np.floor(x_tp1 * unit / tk + 0.5)) * tk
                    tps[1] = entry + d * (1e9 if np.isnan(x_tp2) else max(1.0, np.floor(x_tp2 * unit / tk + 0.5)) * tk)
                    tps[2] = entry + d * max(1.0, np.floor(x_tp3 * unit / tk + 0.5)) * tk
                    stop = entry - d * max(1.0, np.floor(unit / tk + 0.5)) * tk
                    ext = entry                # best price of the reversed trade from here on
                    for k in range(3):
                        leg_open[k] = live[k]
                    stage = 0
                    be_on = False
                    guard = False
                    if rec:
                        E[ne, 0] = entry_row
                        E[ne, 1] = height
                        E[ne, 2] = E[ne - 1, 2]
                        E[ne, 3] = E[ne - 1, 3]
                        E[ne, 4] = 1
                        E[ne, 5] = mult
                        ne += 1
                    continue
                side = 0
                continue
            # stop moves, effective from the next 1-minute bar
            if reached[0] and stage == 0:
                stage = 1
                stop = entry
            if reached[1] and stage == 1 and not np.isnan(x_tp2):    # short TP2: leg 3 starts trailing
                stage = 2
                best = ext
            if stage == 0 and not be_on and not np.isnan(be) and side * (fav - entry) >= be * unit:
                be_on = True                                       # break-even before TP1
                stop = entry
            if stage == 2:
                best = max(best, fav) if side > 0 else min(best, fav)
                if guard and side * (fav - entry) >= g_r * unit:
                    guard = False
            if stage == 2 and not guard:
                trail_stop = best - side * x_trail / 100.0 * abs(best - entry)
                stop = max(stop, trail_stop) if side > 0 else min(stop, trail_stop)
        if side == 0:
            mark_flat(QS, acc, peak, max_dd, cap0, max_ddp)

        # ---- bar close
        # leg 2 EMA exit: after TP1, a bar closing beyond EMA(leg2_ema_len) closes leg 2 at that close;
        # leg 3 then trails, giving back trail_pct % of the best open profit (never below the BE stop)
        if side != 0 and stage == 1 and np.isnan(x_tp2) and side * (c[i] - EMAS[x_il2, i]) < 0:
            if leg_open[1]:
                last = bstart[i + 1] - 1
                nt = book(acc[1], T, nt, rec, entry_row, last, side, entry, c[i] - side * sl_px, mult, cm, pv, 8, 1)
                leg_open[1] = False
            stage = 2
            best = ext
            if not np.isnan(g_r) and side * (c[i] - entry) < g_r * unit:
                guard = True                                       # close near the entry: BE or g_sl R, no trail yet
                g_stop = c[i] - side * g_sl * unit
                stop = max(stop, g_stop) if side > 0 else min(stop, g_stop)
            else:
                trail_stop = best - side * x_trail / 100.0 * abs(best - entry)
                stop = max(stop, trail_stop) if side > 0 else min(stop, trail_stop)
            if not (leg_open[0] or leg_open[2]):
                side = 0
                mark_flat(QS, acc, peak, max_dd, cap0, max_ddp)
        timed = not np.isnan(hold) and i - i_open >= hold          # max_hold_hours reached (in chart bars)
        if (flat[i] or timed) and side != 0:
            last = bstart[i + 1] - 1
            for k in range(3):
                if leg_open[k]:
                    nt = book(acc[k], T, nt, rec, entry_row, last, side, entry, c[i] - side * sl_px, mult,
                              cm, pv, 6 if flat[i] else 9, k)
                    leg_open[k] = False
            side = 0
            mark_flat(QS, acc, peak, max_dd, cap0, max_ddp)

        if side != 0 or not ok[i] or not eok[i] or i == 0 or i == n - 1:
            continue
        hi = TOPS[ix, i]
        lo = BOTS[ix, i]
        xa = ATRS[ia, i]
        xa1 = ATRS[ia, i - 1]
        if np.isnan(hi) or np.isnan(xa) or np.isnan(xa1):
            continue
        height = hi - lo
        ba1 = BATRS[ib, i - 1]
        ratio = height / ba1
        if not np.isnan(max_box):                                  # sideways check
            if np.isnan(ba1) or height > max_box * ba1:
                continue
        if not np.isnan(min_box) and not height >= min_box * ba1:  # box too tight for its long ATR
            continue
        if not np.isnan(min_sq) and not xa >= min_sq * BATRS[ib, i]:   # volatility not expanding
            continue
        if not np.isnan(max_er) and not ER[i] <= max_er:          # daily market already trending hard
            continue
        if not np.isnan(iv_lo) and not IVL[i] >= iv_lo:            # implied volatility (nan IV -> no trade)
            continue
        if not np.isnan(iv_hi) and not IVL[i] <= iv_hi:
            continue
        if not np.isnan(ivr_lo) and not IVR[i] >= ivr_lo:
            continue
        if not np.isnan(ivr_hi) and not IVR[i] <= ivr_hi:
            continue
        b = buf * xa
        bp = buf * xa1
        d = 0
        if c[i] > hi + b and not c[i - 1] > TOPS[ix, i - 1] + bp:           # fresh break up (nan top -> no trade)
            d = 1
        elif c[i] < lo - b and not c[i - 1] < BOTS[ix, i - 1] - bp:
            d = -1
        if d == 0 or (d > 0 and not allow_long) or (d < 0 and not allow_short):
            continue
        if ie >= 0 and not (d * (c[i] - EMAS[ie, i]) > 0):         # trend filter (nan EMA -> no trade)
            continue
        if not np.isnan(vmin) and not VR[iv, i] > vmin:            # volume filter (nan average -> no trade)
            continue
        sc = 0                                                     # box2: score of the enabled f_* conditions
        if cmask != 0 or not np.isnan(bx_min):
            nen = 0
            for r in range(FL.shape[0]):
                if (cmask >> r) & 1:
                    nen += 1
                    if (FL[r, i] if d > 0 else FS[r, i]):
                        sc += 1
            if not np.isnan(bx_min):
                nen += 1
                if height >= bx_min * xa:
                    sc += 1
            if sc < (nen if np.isnan(min_sc) else min_sc):
                continue
        pct = s_slp if d < 0 and not np.isnan(s_slp) else sl_pct    # shorts: their own stop (short_sl_box_pct)
        unit = max(1.0, np.floor(pct / 100.0 * height / tk + 0.5)) * tk
        if not np.isnan(r_tr) or not np.isnan(r_pc):           # size the whole trade to a $ / % risk
            budget = r_tr if not np.isnan(r_tr) else (cap0 + realized(QS, 0, acc, 0)) * r_pc / 100.0
            sz = max(unit, s_floor * xa) if not np.isnan(s_floor) else unit   # tiny / zero box: size on the ATR
            mult = np.floor(budget / (sz * pv * sumq * s_step) + 1e-9) * s_step
            mult = min(max_mult, max(s_step, mult))
            whole = np.floor(mult + 1e-9)
            cm = (whole * comm + (mult - whole) * 10.0 * mcomm) / mult
        else:
            mult = 1.0 if np.isnan(risk) else min(max_mult, max(1.0, np.floor(risk / (unit * pv))))
            cm = comm
        if not np.isnan(up_sc) and sc >= up_sc:                    # box2: size up high-score entries
            mult = mult * up_m
        be_on = False
        side = d
        entry = c[i] + d * sl_px
        entry_row = bstart[i + 1] - 1
        entries += 1
        if d > 0:
            x_tp1, x_tp2, x_tp3, x_trail, x_il2 = tp1, (-s_tp2 if s_tp2 < 0 else np.nan), tp3, trail, il2
        else:
            x_tp1 = tp1 if np.isnan(s_tp1) else s_tp1
            x_tp2 = abs(s_tp2)
            x_tp3 = tp3 if np.isnan(s_tp3) else s_tp3
            x_trail = trail if np.isnan(s_trail) else s_trail
            x_il2 = il2 if s_il2 < 0 else int(s_il2)
        tps[0] = c[i] + d * max(1.0, np.floor(x_tp1 * unit / tk + 0.5)) * tk
        # leg 2: no target (1e9 away), it leaves on the EMA close - unless short_tp2_mult gives shorts a TP2
        tps[1] = c[i] + d * (1e9 if np.isnan(x_tp2) else max(1.0, np.floor(x_tp2 * unit / tk + 0.5)) * tk)
        tps[2] = c[i] + d * max(1.0, np.floor(x_tp3 * unit / tk + 0.5)) * tk
        for k in range(3):
            leg_open[k] = live[k]
        stop = c[i] - d * max(1.0, np.floor(unit / tk + 0.5)) * tk
        stage = 0
        guard = False
        i_entry = i
        i_open = i
        ext = entry
        is_rev = False
        if rec:
            E[ne, 0] = entry_row
            E[ne, 1] = height
            E[ne, 2] = ratio
            E[ne, 3] = xa
            E[ne, 4] = 0
            E[ne, 5] = mult
            ne += 1

    open_leg = np.zeros(3)            # open P&L per contract of each leg
    if side != 0:
        last = len(mc) - 1
        for k in range(3):
            if leg_open[k]:
                open_leg[k] = side * (mc[last] - entry) * pv * mult
                if rec:
                    T[nt, 0], T[nt, 1], T[nt, 2], T[nt, 3] = entry_row, last, side, entry
                    T[nt, 4], T[nt, 5], T[nt, 6], T[nt, 7] = mc[last], open_leg[k] - 2 * cm * mult, 7, k
                    nt += 1
    wins = acc[0, 3] + acc[1, 3] + acc[2, 3]
    trades = acc[0, 4] + acc[1, 4] + acc[2, 4]
    stats = np.empty((len(QS), 11))
    for q in range(len(QS)):
        gp = realized(QS, q, acc, 1)
        gl = realized(QS, q, acc, 2)
        net = gp - gl
        dd = max_dd[q]
        stats[q, 0], stats[q, 1], stats[q, 2] = net, gp / gl if gl > 0 else np.inf, dd
        stats[q, 3], stats[q, 4] = net / dd if dd > 0 else np.inf, trades
        stats[q, 5], stats[q, 6], stats[q, 7] = wins / trades * 100 if trades else 0.0, entries, gp
        stats[q, 8] = gl
        stats[q, 9] = QS[q, 0] * open_leg[0] + QS[q, 1] * open_leg[1] + QS[q, 2] * open_leg[2]
        stats[q, 10] = max_ddp[q] * 100
    return stats, T[:nt], E[:ne]


@njit(parallel=True, cache=True)
def sweep_kernel(c, bstart, ok, flat, mo, mh, ml, mc, bpath, ATRS, BATRS, TOPS, BOTS, EMAS,
                 IA, IB, IX, IE, IL2, MAXBOX, BUF, ALONG, ASHORT, SLP, TP1, TP3, TRAIL, REV, REVB,
                 VR, IV, VMIN, eok, ER, IVL, IVR, FL, FS, MAXER, MINBOX, MINSQ, BE, RISK, MAXM, IVLO, IVHI, IVRLO, IVRHI,
                 STP1, STP2, STP3, STRAIL, SIL2, GR, GSL, RTR, RPC, CAP0, SSTEP, SFLOOR, SSLP, HOLD, CMASK, MINSC, UPSC, UPM, BXMIN, QALL, qoff, comm, mcomm, pv, tk, slip):
    """One simulation per setting k (row k of each per-setting array); its quantity sets are QALL[qoff[k]:qoff[k + 1]]
    (output rows in that order)."""
    out = np.empty((qoff[-1] - qoff[0], 11))       # len(STATS)
    for k in prange(len(IA)):
        out[qoff[k] - qoff[0]:qoff[k + 1] - qoff[0]] = simulate(
            c, bstart, ok, flat, mo, mh, ml, mc, bpath, ATRS, BATRS, TOPS, BOTS, EMAS,
            IA[k], IB[k], IX[k], IE[k], IL2[k], MAXBOX[k], BUF[k], ALONG[k], ASHORT[k], SLP[k], TP1[k], TP3[k],
            TRAIL[k], REV[k], REVB[k], VR, IV[k], VMIN[k], eok, ER, IVL, IVR, FL, FS, MAXER[k], MINBOX[k], MINSQ[k], BE[k], RISK[k],
            MAXM[k], IVLO[k], IVHI[k], IVRLO[k], IVRHI[k], STP1[k], STP2[k], STP3[k], STRAIL[k], SIL2[k], GR[k], GSL[k],
            RTR[k], RPC[k], CAP0[k], SSTEP[k], SFLOOR[k], SSLP[k], HOLD[k], CMASK[k], MINSC[k], UPSC[k], UPM[k], BXMIN[k], QALL[qoff[k]:qoff[k + 1]], comm, mcomm, pv, tk, slip, False)[0]
    return out


# ----------------------------------------------------------------------------- driver
def is_off(v):
    return v is None or (isinstance(v, float) and np.isnan(v))


def build(tg, h, l, c, v, edges):
    """Indicator tables and per-setting arrays for the rows of tg."""
    def uniq(*cols):
        return list(dict.fromkeys(int(x) for col in cols for x in tg[col] if not is_off(x)))

    lens = uniq("atr_len")
    blens = uniq("box_atr_len")
    boxes = uniq("box_bars")
    emas = uniq("ema_len", "leg2_ema_len", "short_leg2_ema_len")
    edge = [box_edges(h, l, c, n, edges) for n in boxes]
    tables = (np.stack([atr(h, l, c, n) for n in lens]), np.stack([atr(h, l, c, n) for n in blens]),
              np.stack([e[0] for e in edge]), np.stack([e[1] for e in edge]), np.stack([ema(c, n) for n in emas]))
    index = lambda col, table: np.array([-1 if is_off(x) else table.index(int(x)) for x in tg[col]], np.int64)
    num = lambda col: np.array([np.nan if is_off(x) else float(x) for x in tg[col]])
    vkeys = [(int(a), int(b), k) for a, b, k in zip(tg.vol_fast, tg.vol_slow, tg.vol_avg)]
    vu = list(dict.fromkeys(vkeys))
    vr = np.stack([vol_ratio(v, *k) for k in vu])
    iv = np.array([vu.index(k) for k in vkeys], np.int64)
    vmin = np.array([np.nan if is_off(x) else 1 + float(x) / 100 for x in tg.vol_pct])
    lr = np.array([sides(x) for x in tg.direction], np.bool_)
    arrays = [index("atr_len", lens), index("box_atr_len", blens), index("box_bars", boxes), index("ema_len", emas),
              index("leg2_ema_len", emas), num("max_box_atr"), num("break_buffer"), lr[:, 0].copy(), lr[:, 1].copy(),
              num("sl_box_pct"), num("tp1_mult"), num("tp3_mult"), num("trail_pct"),
              tg.reverse.astype(bool).to_numpy(), tg.reverse_bars.astype(np.int64).to_numpy()]
    extra = [num("max_trend_er"), num("min_box_atr"), num("min_squeeze"), num("be_mult"), num("risk_usd"), num("max_mult"),
             num("iv_min"), num("iv_max"), num("ivr_min"), num("ivr_max"),
             num("short_tp1_mult"), tp2_slot(tg), num("short_tp3_mult"), num("short_trail_pct"),
             index("short_leg2_ema_len", emas).astype(float), num("leg3_guard_r"), num("leg3_guard_sl_r"),
             num("risk_trade_usd"), num("risk_pct"), num("start_capital"), num("size_step"), num("size_atr_floor"), num("short_sl_box_pct"),
             num("max_hold_hours") * 60.0 / tg.timeframe.astype(float).to_numpy()]     # max hold in chart bars
    return tables, arrays, vr, iv, vmin, extra


def tp2_slot(df):
    """Kernel slot for TP2: short_tp2_mult (shorts only) as is; tp2_mult (both sides) as a NEGATIVE value."""
    out = []
    for a, b in zip(df["tp2_mult"], df["short_tp2_mult"]):
        if not is_off(a) and not is_off(b):
            raise SystemExit("use tp2_mult (both sides) or short_tp2_mult (shorts only), not both")
        out.append(-float(a) if not is_off(a) else np.nan if is_off(b) else float(b))
    return np.array(out)


def filter_arrays(tg, t, o, h, l, c, v, tf, ivl):
    """box2: f_* condition flags (long / short) of this chart and per-setting mask / score / size arrays."""
    sess = pd.DatetimeIndex(t.tz_localize(None) + pd.Timedelta(hours=6)).normalize().asi8
    return FLT.tables(tg, FLT.features(o, h, l, c, v, sess, tf, ivl))


def short_rising(df):
    """Per row: the short targets rise (TP1 < TP2 < TP3, short_* values or the long ones where null)."""
    f = lambda col, alt: np.array([float(a) if not is_off(a) else float(b) for a, b in zip(df[col], alt)])
    t1, t3 = f("short_tp1_mult", df.tp1_mult), f("short_tp3_mult", df.tp3_mult)
    t2 = np.array([np.nan if is_off(x) else float(x) for x in df.short_tp2_mult])
    b2 = np.array([np.nan if is_off(x) else float(x) for x in df.tp2_mult]) if "tp2_mult" in df else np.full(len(df), np.nan)
    t2 = np.where(np.isnan(t2), b2, t2)
    l1, l3 = df.tp1_mult.astype(float).to_numpy(), df.tp3_mult.astype(float).to_numpy()
    long_ok = np.isnan(b2) | ((l1 < b2) & (b2 < l3))
    return (t1 < t3) & (np.isnan(t2) | ((t1 < t2) & (t2 < t3))) & long_ok


def run_single(m1, mo, mh, ml, mc, p, cfg, stamp):
    if not p["tp1_mult"] < p["tp3_mult"]:
        raise SystemExit("targets must rise: tp1_mult < tp3_mult")
    if not short_rising(pd.DataFrame([p]))[0]:
        raise SystemExit("short targets must rise: TP1 < short_tp2_mult < TP3 (short_* or the long values)")
    tf = int(p["timeframe"])
    t, o, h, l, c, bstart = chart_bars(m1, tf)
    v = np.add.reduceat(m1.volume.to_numpy(float), bstart[:-1])
    ok, flat = bar_flags(t, tf, p["flat_time"], cfg)
    tg = pd.DataFrame([p])
    cost = cfg["costs"]
    if not is_off(p["risk_trade_usd"]) and not is_off(p["risk_pct"]):
        raise SystemExit("set only one of risk_trade_usd / risk_pct")
    tables, arrays, vr, iv, vmin, extra = build(tg, h, l, c, v, p["box_edges"])
    eok, er = hours_ok(t, p["skip_hours"]), trend_er(t, c, int(p["er_days"]))
    ivl, ivr = implied_vol(t, cfg["data"]["instrument"], int(p["ivr_days"]))
    FL, FS, fx = filter_arrays(tg, t, o, h, l, c, v, tf, ivl)
    extra = extra + fx
    qty = tg[QCOLS].to_numpy(float)
    st, T, E = simulate(c, bstart, ok, flat, mo, mh, ml, mc, bar_path(bstart, mh, ml, mc), *tables,
                        *[a[0] for a in arrays], vr, iv[0], vmin[0], eok, er, ivl, ivr, FL, FS, *[a[0] for a in extra], qty,
                        float(cost["commission"]), float(cost["micro_commission"]),
                        float(cost["point_value"]), float(cost["tick"]), float(cost["slippage_ticks"]), True)
    st = st[0]
    tm = m1.t.dt.tz_convert(NY)
    entry_t = tm.iloc[T[:, 0].astype(int)].reset_index(drop=True)
    trades = pd.DataFrame({
        "entry_time": entry_t.dt.strftime("%Y-%m-%d %H:%M").to_numpy(),
        "exit_time": tm.iloc[T[:, 1].astype(int)].dt.strftime("%Y-%m-%d %H:%M").to_numpy(),
        "hour": entry_t.dt.hour.to_numpy(),
        "side": np.where(T[:, 2] > 0, "Long", "Short"),
        "leg": T[:, 7].astype(int) + 1, "entry_price": T[:, 3], "exit_price": T[:, 4],
        "pnl": T[:, 5], "exit": [REASONS[int(w)] for w in T[:, 6]], "row": T[:, 0].astype(int)})
    info = pd.DataFrame(E, columns=["row", "box_height", "box_to_atr", "atr", "setup", "mult"]).round(2)
    info["row"] = info.row.astype(int)
    bar = np.searchsorted(bstart, info.row.to_numpy(), side="right") - 1    # chart bar of each entry
    info["iv"], info["iv_rank"] = ivl[bar].round(2), ivr[bar].round(1)
    info["setup"] = np.where(info.setup > 0, "Reverse", "Breakout")
    trades = trades.merge(info, on="row", how="left").drop(columns="row")
    trades.insert(trades.columns.get_loc("pnl"), "contracts", qty[0, trades.leg.to_numpy() - 1] * trades.mult)
    trades["pnl"] = (trades.pnl * qty[0, trades.leg.to_numpy() - 1]).round(2)   # booked for `mult` contracts per leg
    trades = trades.drop(columns="mult")
    trades["cum_pnl"] = trades.pnl.where(trades.exit != "Open", 0).cumsum().round(2)

    closed = trades[trades.exit != "Open"]
    by_year = closed.groupby(closed.entry_time.str[:4]).pnl.agg(net="sum", legs="count").round(0)
    by_hour = closed.groupby("hour").pnl.agg(net="sum", legs="count").round(0)
    by_setup = closed.groupby("setup").pnl.agg(net="sum", legs="count", win_rate=lambda x: (x > 0).mean() * 100)
    by_setup["entries"] = closed.drop_duplicates("entry_time").groupby("setup").size()
    by_setup = by_setup.round(1)
    data = cfg["data"]
    head = (f"Box Breakout 3 Targets - {data['instrument']} ({data['source']}) {tf}m, bar magnifier 1m, "
            f"{'back-adjusted' if data['back_adjust'] else 'unadjusted'} rolls\n"
            f"Data {t[0]:%Y-%m-%d %H:%M} -> {t[-1]:%Y-%m-%d %H:%M} New York\n"
            f"Settings {p}\n"
            f"Costs ${cost['commission']}/contract/fill, {cost['slippage_ticks']} tick(s) slippage, "
            f"${cost['point_value']}/point\n\n")
    text = (head + fmt_stats(st) + f"Max drawdown %    {st[10]:.1f}% of the account peak (start ${p['start_capital']:,.0f})\n"
            + "\nBy setup:\n" + by_setup.to_string()
            + "\n\nBy year (entry):\n" + by_year.to_string()
            + "\n\nBy entry hour (New York):\n" + by_hour.to_string() + "\n")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"box_{stamp}_summary.txt").write_text(text)
    trades.to_csv(RESULTS / f"box_{stamp}_trades.csv", index=False)
    print(text)
    print(f"-> results/box_{stamp}_summary.txt, results/box_{stamp}_trades.csv")


def run_sweep(m1, mo, mh, ml, mc, grid, cfg, stamp):
    names = list(grid)
    combos = pd.DataFrame(list(itertools.product(*grid.values())), columns=names)
    rising = (combos.tp1_mult < combos.tp3_mult).to_numpy() & short_rising(combos)
    combos = combos[rising].reset_index(drop=True)
    off = np.array([is_off(x) for x in combos.vol_pct], bool)     # volume filter off: its lengths have no effect
    for k in ("vol_fast", "vol_slow", "vol_avg"):
        combos.loc[off, k] = DEFAULTS[k]
    combos.loc[np.array([is_off(x) for x in combos.max_trend_er], bool), "er_days"] = DEFAULTS["er_days"]
    combos.loc[np.array([is_off(x) for x in combos.leg3_guard_r], bool), "leg3_guard_sl_r"] = DEFAULTS["leg3_guard_sl_r"]
    combos.loc[np.array([not is_off(x) or not is_off(y) for x, y in zip(combos.short_tp2_mult, combos.tp2_mult)], bool), "short_leg2_ema_len"] = None   # TP2: no EMA
    both = np.array([not is_off(a) and not is_off(b) for a, b in zip(combos.risk_trade_usd, combos.risk_pct)], bool)
    combos = combos[~both].reset_index(drop=True)                   # only one whole-trade risk setting per row
    whole = np.array([not is_off(a) or not is_off(b) for a, b in zip(combos.risk_trade_usd, combos.risk_pct)], bool)
    combos.loc[whole, "risk_usd"] = None                            # whole-trade sizing replaces risk_usd
    combos.loc[~whole, "size_step"] = DEFAULTS["size_step"]
    combos.loc[~whole, "size_atr_floor"] = DEFAULTS["size_atr_floor"]
    combos.loc[np.array([is_off(x) for x in combos.risk_pct], bool), "start_capital"] = DEFAULTS["start_capital"]
    rk_off = np.array([is_off(a) and is_off(b) for a, b in zip(combos.ivr_min, combos.ivr_max)], bool)
    combos.loc[rk_off, "ivr_days"] = DEFAULTS["ivr_days"]
    combos = combos.drop_duplicates().reset_index(drop=True)
    print(f"{len(combos):,} combinations ({(~rising).sum():,} skipped: targets must rise, TP1 < TP2 < TP3)")
    if combos.empty:
        raise SystemExit("nothing to test: every combination has TP1 >= TP3 - make tp1_mult (and short_tp1_mult) "
                         "smaller than tp3_mult (short_tp3_mult)")
    cost = cfg["costs"]
    parts = []
    t0 = time.time()
    done = 0
    keys = ["timeframe", "flat_time", "box_edges", "skip_hours", "er_days", "ivr_days"]
    pkeys = [n for n in combos.columns if n not in QCOLS]
    for (tf, ft, edges, skip, erd, ivd), g in combos.groupby(keys, sort=False, dropna=False):
        # quantities do not change the trades (only which legs are 0 does): one simulation per setting
        # serves all its quantity sets -> rows of a setting together, settings = `tg` (first row of each)
        z = (g[QCOLS].to_numpy(float) > 0) @ (2 ** np.arange(3))
        # whole-trade risk sizing depends on the quantities -> one simulation per quantity set there
        sized = np.array([not is_off(a) or not is_off(b) for a, b in zip(g.risk_trade_usd, g.risk_pct)], bool)
        z = np.where(sized, np.arange(len(g)) + 8, z)
        task = g.assign(_z=z).groupby(pkeys + ["_z"], sort=False, dropna=False).ngroup().to_numpy()
        order = np.argsort(task, kind="stable")
        g, task = g.iloc[order].reset_index(drop=True), task[order]
        starts = np.flatnonzero(np.r_[True, task[1:] != task[:-1]])
        qoff = np.r_[starts, len(g)].astype(np.int64)
        QALL = g[QCOLS].to_numpy(float)
        tg = g.iloc[starts].reset_index(drop=True)
        tf = int(tf)
        ft = None if is_off(ft) else ft
        t, o, h, l, c, bstart = chart_bars(m1, tf)
        v = np.add.reduceat(m1.volume.to_numpy(float), bstart[:-1])
        ok, flat = bar_flags(t, tf, ft, cfg)
        bpath = bar_path(bstart, mh, ml, mc)
        tables, arrays, vr, iv, vmin, extra = build(tg, h, l, c, v, edges)
        eok, er = hours_ok(t, None if is_off(skip) else skip), trend_er(t, c, int(erd))
        ivl, ivr = implied_vol(t, cfg["data"]["instrument"], int(ivd))
        FL, FS, fx = filter_arrays(tg, t, o, h, l, c, v, tf, ivl)
        extra = extra + fx
        out = np.empty((len(g), len(STATS)))
        step = max(12, CHUNK * len(tg) // len(g))  # settings per progress line (about CHUNK combinations)
        for s0 in range(0, len(tg), step):
            s1 = min(s0 + step, len(tg))
            out[qoff[s0]:qoff[s1]] = sweep_kernel(c, bstart, ok, flat, mo, mh, ml, mc, bpath, *tables,
                                                  *[a[s0:s1] for a in arrays], vr, iv[s0:s1], vmin[s0:s1],
                                                  eok, er, ivl, ivr, FL, FS, *[a[s0:s1] for a in extra], QALL, qoff[s0:s1 + 1], float(cost["commission"]),
                                                  float(cost["micro_commission"]), float(cost["point_value"]), float(cost["tick"]),
                                                  float(cost["slippage_ticks"]))
            done += qoff[s1] - qoff[s0]
            el = time.time() - t0
            print(f"  {done:,} / {len(combos):,} combinations, {el / 60:.1f} min, about "
                  f"{el / done * (len(combos) - done) / 60:.1f} min left", flush=True)
        parts.append(pd.concat([g.reset_index(drop=True), pd.DataFrame(out, columns=STATS)], axis=1))
    res = pd.concat(parts).sort_values("net_profit", ascending=False).reset_index(drop=True)
    for col in ("trades", "entries"):
        res[col] = res[col].astype(int)
    res = res.round({"net_profit": 0, "max_drawdown": 0, "gross_profit": 0, "gross_loss": 0, "open_pnl": 0,
                     "profit_factor": 3, "net_to_dd": 2, "win_rate": 1, "max_dd_pct": 1})
    RESULTS.mkdir(exist_ok=True)
    res.to_csv(RESULTS / f"box_sweep_{stamp}.csv", index=False)
    show = [c for c in names if len(grid[c]) > 1] + ["net_profit", "profit_factor", "max_drawdown", "net_to_dd",
                                                     "max_dd_pct", "trades", "win_rate", "entries"]
    by_dd = res[res.net_profit > 0].sort_values("net_to_dd", ascending=False)
    print(f"done in {time.time() - t0:.1f}s")
    print("\nTop 5 by net profit:")
    print(res[show].head(5).to_string(index=False))
    print("\nTop 5 by net profit / max drawdown:")
    print(by_dd[show].head(5).to_string(index=False))
    mt = int((cfg.get("report") or {}).get("min_trades") or 0)
    if mt:
        print(f"\nTop 5 by net profit / max drawdown, at least {mt} trades (entries):")
        print(by_dd[by_dd.entries >= mt][show].head(5).to_string(index=False))
    print(f"\n-> results/box_sweep_{stamp}.csv (every combination, highest net profit first)")


def load_big(instrument, adjust):
    """data/<ROOT>_1min.parquet (Databento <ROOT>.n.0, unadjusted). Rolls = where instrument_id changes;
    where it is missing (ES 2022-04 -> 2025-06) = the 00:00 UTC bar with the largest open-vs-previous-close jump in
    the 12 days before each quarterly third Friday. Jump = roll bar open - previous close."""
    path = BIG / f"{instrument}_1min.parquet"
    if not path.exists():
        raise SystemExit(f"{path} not found")
    df = pd.read_parquet(path, columns=["ts_utc", "open", "high", "low", "close", "volume", "instrument_id"])
    df = df.rename(columns={"ts_utc": "t"}).sort_values("t").drop_duplicates("t").reset_index(drop=True)
    iid = df.instrument_id.astype(float)
    gap = (df.open - df.close.shift()).fillna(0).to_numpy()
    rows = list(np.flatnonzero((iid != iid.shift()) & iid.notna() & iid.shift().notna()))
    na = iid.isna().to_numpy()
    if na.any():
        midnight = ((df.t.dt.hour == 0) & (df.t.dt.minute == 0)).to_numpy() & na
        for y in range(df.t.dt.year.min(), df.t.dt.year.max() + 1):
            for mth in (3, 6, 9, 12):
                d = pd.Timestamp(y, mth, 15, tz="UTC")
                expiry = d + pd.Timedelta(days=(4 - d.weekday()) % 7)      # third Friday
                win = np.flatnonzero(midnight & (df.t >= expiry - pd.Timedelta(days=12)).to_numpy()
                                     & (df.t <= expiry).to_numpy())
                if len(win):
                    rows.append(int(win[np.argmax(np.abs(gap[win]))]))
    m1 = df.drop(columns="instrument_id")
    return back_adjust(m1, [(int(r), float(gap[r])) for r in sorted(rows)]) if adjust else m1


def load_cfg(path):
    """A yaml with `base: other.yaml` = that file (path relative to this one) with this file's values on top."""
    cfg = yaml.safe_load(path.read_text()) or {}
    base = cfg.pop("base", None)
    if not base:
        return cfg
    out = load_cfg(path.parent / base)
    for sec, val in cfg.items():
        if isinstance(val, dict) and isinstance(out.get(sec), dict):
            out[sec].update(val)
        else:
            out[sec] = val
    return out


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "box_ES_params.yaml"
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise SystemExit(f"{path} is not a settings file - pass a .yaml, e.g. box/box_NQ_params.yaml")
    cfg = load_cfg(path)
    bad = [k for k in cfg["strategy"] if k not in DEFAULTS]
    if bad:
        raise SystemExit(f"strategy: unknown setting(s) {bad} (allowed: {list(DEFAULTS)})")
    if cfg.get("night"):
        raise SystemExit("night: settings are no longer supported")
    grid = {k: values(cfg["strategy"].get(k, v)) for k, v in DEFAULTS.items()}
    data = cfg.setdefault("data", {})
    data.setdefault("instrument", "ES")
    data.setdefault("back_adjust", True)
    data.setdefault("source", "local")
    if data["instrument"] not in SPECS:
        raise SystemExit(f"instrument must be one of {list(SPECS)}, not {data['instrument']!r}")
    if data["source"] == "futures_1m":
        m1 = load_big(data["instrument"], data["back_adjust"])
    elif data["source"] == "local":
        if data["instrument"] in ("GC", "CL"):
            raise SystemExit(f"{data['instrument']} is only in source: futures_1m")
        m1 = load_1m(data["instrument"], data["back_adjust"])
    else:
        raise SystemExit(f"source must be futures_1m | local, not {data['source']!r}")
    cfg["costs"].setdefault("slippage_ticks", 0)
    cfg["costs"].setdefault("micro_commission", 0.62)
    if cfg["costs"].get("point_value") is None:
        cfg["costs"]["point_value"] = SPECS[data["instrument"]][0]
    cfg["costs"]["tick"] = SPECS[data["instrument"]][1]
    mo, mh, ml, mc = (m1[k].to_numpy() for k in ("open", "high", "low", "close"))
    stamp = f"{data['instrument']}_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}"
    if all(len(v) == 1 for v in grid.values()):
        run_single(m1, mo, mh, ml, mc, {k: v[0] for k, v in grid.items()}, cfg, stamp)
    else:
        run_sweep(m1, mo, mh, ml, mc, grid, cfg, stamp)


if __name__ == "__main__":
    main()