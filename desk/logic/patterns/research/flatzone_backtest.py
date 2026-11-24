"""Flat Zone Breakout backtest = flatzone/pine/flatzone_NQ.pine ("Flat Zone Breakout Strategy"), on the 1-minute
futures data in data/ (same loader and roll handling as box/box_backtest.py).

    python flatzone/flatzone_backtest.py                              # flatzone/flatzone_NQ_best.yaml
    python flatzone/flatzone_backtest.py other.yaml

Any value under `strategy:` written as a list or {from, to, step} -> every combination is tested
(results/flatzone_sweep_<time>.csv, highest net/DD first). Otherwise one run -> summary + list of trades.

Rules (chart = `timeframe`-minute bars built from the 1-minute data, like TradingView NQ1!):
  * zone values: close (box_src: close) or candle body max/min(open, close) (box_src: body) - wicks never count;
  * a zone starts when the last min_bars bars are "flat": height (top - bottom) > 0 and <= max_h_atr x ATR(atr_len),
    and the linear-regression line of their CLOSES moves <= max_slope % of the height across the zone;
  * while active, every bar first checks the breakout against the zone so far:
      close mode: close >= top + break_pct % x height (long) / close <= bottom - break_pct % x height (short)
      body mode : close beyond the edge AND the candle body (same colour) >= break_pct % x height;
    no breakout -> the bar joins the zone if the grown zone is still flat (and <= max_bars bars), else the zone ends;
  * a new zone can start on the bar an old one ended (not on a breakout bar);
  * entry: breakout bar close -> market order filled at the NEXT chart bar's open (+ slippage), only when flat;
  * qty_t1 contracts: target t1_atr x ATR(t_atr_len) from the breakout close, plus the stop;
    qty_t2 contracts: the stop plus a trailing stop trail_atr x ATR behind the best price, active once the price
    reaches breakout close + trail_act x ATR (0 = right away). ATR values of the breakout bar.
    t2_atr (optional): fixed Target 2 for the qty_t2 contracts at breakout close +/- t2_atr x ATR (null = none);
    trail_atr null / 0 = no trailing stop. 2 fixed targets = t2_atr set + trail_atr null;
    single target + trailing = qty_t1 0, qty_t2 N (+ t2_atr if wanted). (Python only - not in the Pine yet.)
  * stop: sl_mode box (opposite side of the zone) | mid (zone middle) | atr (sl_atr x ATR from the breakout close);
  * be_after_t1: after the T1 fill the stop of the T2 legs moves to the entry price from the next chart bar;
  * exits fill on the 1-minute bars (path open -> nearer extreme -> other extreme), at their price or at the
    1-minute open when it gaps past it; slippage on entries and stops, not on targets;
  * skip_hours "HH:MM-HH:MM" New York (bar open, may wrap midnight): no entries from breakouts on those bars.
  * rev_bars N (null / 0 = off): if the INITIAL stop is hit within the first N chart bars of the trade (entry bar = 1),
    reverse at once: a new position the other way at the stop fill price (stop-and-reverse, no extra slippage),
    stop = the same points as the first trade's risk (entry -> stop) on the other side, T1 / T2 / trail as usual
    from the new entry (ATR of the breakout bar), same quantities. Only once: a reverse trade does not reverse.
    Its exits are checked from the next 1-minute bar.
  * volume (breakout bar): vol_ratio = its volume / the reference volume. vol_ref zone = average volume of the
    zone's bars | tod = average volume of the same time-of-day bar over the last vol_days sessions (NQ volume
    has a strong time-of-day pattern: 9:30 is always big). vol_min x (null = off): trade only if vol_ratio >= x.
    vol_boost y (null = off): vol_ratio >= y -> qty_t1 and qty_t2 x vol_mult (2 = double). A reverse trade keeps
    the size of the trade it reverses.
    risk_usd R (null = off): size so the whole trade (qty_t1 + qty_t2 units, x volume boost) loses R at the first stop
    (shares / contracts can be fractional). For stocks (data: stock: true) with costs.commission_bps / slippage_bps.
    rev_vol_min / rev_vol_max (null = off): reverse only if the BREAKOUT bar's vol_ratio is >= min / < max.
  * adx_min A (null = off): trade only if ADX(adx_len) of the chart bars (Wilder, at the breakout bar close) >= A.
    Low ADX = sideways market.
  * max_hold_days D (null = off): a trade still open D x 24 h after its entry (a reverse trade: after the reverse)
    is closed at the open of that 1-minute bar (+ slippage), exit "Time exit".
  * qty_tiers "P1:m1, P2:m2, ..." (null = off): quantities x m of the highest level P <= the REAL (unadjusted)
    price at the breakout bar; below the lowest level -> its m. "2000:4, 4000:3, 6000:2, 8000:1" = <4000 x4,
    4000-6000 x3, 6000-8000 x2, >= 8000 x1.
  * qty_step S (null = off): quantities grow with the price level: qty_t1 / qty_t2 x floor(REAL price / S)
    (at least 1), price = the unadjusted close of the breakout bar. S 1000 on NQ: 4,800 -> x4, 25,000 -> x25.
    Works on top of the volume boost (x vol_mult).
  * be_atr A (null = off): once the price is A x ATR beyond the ENTRY (in profit), the stop of every open leg moves
    to entry + be_lock x ATR (locked profit, 0 = break-even), from the next 1-minute bar. ATR = ATR(t_atr_len) of
    the breakout bar. Also on reverse trades. Exit reason "Lock stop". A locked stop never triggers a reverse.
"""
import itertools
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from numba import njit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "others"))
sys.path.insert(0, str(HERE.parent / "box"))
from orb_backtest import NY, RESULTS, STATS, atr, chart_bars, fmt_stats, hhmm, values   # noqa: E402
from box_backtest import SPECS, load_big, load_cfg                                       # noqa: E402

REASONS = ["T1", "Stop loss", "BE stop", "Trail stop", "Open", "T2", "Lock stop", "Time exit"]
DEFAULTS = {"timeframe": 15, "min_bars": 8, "max_bars": 60, "box_src": "close", "atr_len": 50, "max_h_atr": 1.4,
            "max_slope": 30, "break_mode": "close", "break_pct": 50, "direction": "Both", "t_atr_len": 14,
            "qty_t1": 1, "t1_atr": 1.5, "qty_t2": 2, "t2_atr": None, "trail_atr": 2.0, "trail_act": 0.0, "sl_mode": "box",
            "sl_atr": 1.5, "be_after_t1": False, "skip_hours": None, "rev_bars": None,
            "vol_ref": "zone", "vol_days": 20, "vol_min": None, "vol_boost": None, "vol_mult": 2, "qty_step": None, "be_atr": None, "be_lock": 0.5,
            "rev_vol_min": None, "rev_vol_max": None,
            "adx_len": 14, "adx_min": None, "risk_usd": None,
            "max_hold_days": None, "qty_tiers": None}


# ----------------------------------------------------------------------------- zones (same as the Pine)
@njit(cache=True)
def drift(c, i, n):
    """|slope| x (n - 1) of the least-squares line through c[i-n+1 .. i]."""
    sx = 0.0
    sy = 0.0
    sxx = 0.0
    sxy = 0.0
    for k in range(n):
        x = float(n - 1 - k)
        y = c[i - k]
        sx += x
        sy += y
        sxx += x * x
        sxy += x * y
    den = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / den if den != 0 else 0.0
    return abs(slope) * (n - 1)


@njit(cache=True)
def flat_ok(c, i, top, bot, n, za, max_h, max_slope):
    h = top - bot
    if not (h > 0 and h <= max_h * za[i]):           # NaN ATR -> False, like Pine's na
        return False
    return drift(c, i, n) <= max_slope / 100.0 * h


@njit(cache=True)
def zones(o, c, hi_v, lo_v, za, min_b, max_b, max_h, max_slope, body_mode, bpct):
    """Per chart bar: sig (+1 / -1 breakout at its close), the broken zone's top / bottom / bars / first bar.
    Also the number of zones started."""
    n = len(c)
    sig = np.zeros(n, np.int8)
    ztop = np.full(n, np.nan)
    zbot = np.full(n, np.nan)
    zlen = np.zeros(n, np.int64)
    zfirst = np.full(n, -1, np.int64)
    active = False
    start = 0
    top = 0.0
    bot = 0.0
    started = 0
    for i in range(n):
        brk = 0
        if active:
            h = top - bot
            ln = i - start + 1
            need = bpct / 100.0 * h
            if body_mode:
                up = c[i] > top and c[i] > o[i] and c[i] - o[i] >= need
                dn = c[i] < bot and c[i] < o[i] and o[i] - c[i] >= need
            else:
                up = c[i] >= top + need
                dn = c[i] <= bot - need
            if up or dn:
                brk = 1 if up else -1
                sig[i] = brk
                ztop[i] = top
                zbot[i] = bot
                zlen[i] = ln - 1
                zfirst[i] = start
                active = False
            else:
                nt = max(top, hi_v[i])
                nb = min(bot, lo_v[i])
                if ln <= max_b and flat_ok(c, i, nt, nb, ln, za, max_h, max_slope):
                    top = nt
                    bot = nb
                else:
                    active = False
        if not active and brk == 0 and i >= min_b:
            wt = -np.inf
            wb = np.inf
            for k in range(i - min_b + 1, i + 1):
                wt = max(wt, hi_v[k])
                wb = min(wb, lo_v[k])
            if flat_ok(c, i, wt, wb, min_b, za, max_h, max_slope):
                active = True
                start = i - min_b + 1
                top = wt
                bot = wb
                started += 1
    return sig, ztop, zbot, zlen, zfirst, started


# ----------------------------------------------------------------------------- trades
@njit(cache=True)
def simulate(sig, ztop, zbot, c, ta, ok, bstart, mo, mh, ml, mc, allow_l, allow_s, q1, q2, t1m, t2m, trm, tram,
             sl_mode, slm, be_t1, comm, pv, slip, rec, rev_n, mult, be_a, be_l, rev_ok, cmb, slb, mt, hold_m):
    """Pine exits on 1-minute paths. Prices of a short are handled as x = -price, so every check is the long one.
    Returns stats (orb STATS order) and the legs T: entry row, exit row, side, entry, exit, pnl, reason, leg, signal bar."""
    n = len(c)
    T = np.zeros((4 * n + 2 if rec else 1, 11))
    nt = 0
    real = 0.0
    gp = 0.0
    gl = 0.0
    wins = 0
    legs = 0
    entries = 0
    peak = 0.0
    max_dd = 0.0
    pos = 0                    # side of the open position, 0 = flat
    pend = 0                   # side to enter at the next bar's open
    sb = -1                    # signal bar of the position
    e_row = 0
    xe = 0.0                   # entry, x space
    sl = 0.0
    lim = 0.0
    lim2 = 0.0
    act = 0.0
    off = 0.0
    best = 0.0
    tr_on = False
    be_on = False
    be_pend = False
    open1 = False
    open2 = False
    p_sl = 0.0
    p_lim = 0.0
    p_lim2 = 0.0
    p_act = 0.0
    p_off = 0.0
    p_bea = 0.0                # be_atr distance (price) of the pending entry
    p_bel = 0.0                # be_lock distance
    bea = 0.0
    bel = 0.0
    lk_on = False              # locked stop active
    lk_next = False            # lock reached -> active from the next minute
    e_bar = 0                  # chart bar of the entry
    is_rev = False             # the open position is a reverse trade
    rv_go = False              # initial stop hit early -> reverse when the position is closed
    rv_px = 0.0
    p_mult = 1.0               # size multiplier of the pending entry (volume boost)
    cq1 = float(q1)            # contracts of the open position (leg 1 / leg 2)
    cq2 = float(q2)
    e_min = 0                  # entry minute (time exit)
    for i in range(n):
        if pend != 0:
            j = bstart[i]
            s = pend
            pend = 0
            pos = s
            e_row = j
            xe = s * mo[j] + slip + slb * mo[j]
            sl, lim, lim2, act, off = p_sl, p_lim, p_lim2, p_act, p_off
            bea, bel = p_bea, p_bel
            lk_on = False
            lk_next = False
            best = -np.inf
            tr_on = False
            be_on = False
            be_pend = False
            open1 = q1 > 0
            open2 = q2 > 0
            cq1 = q1 * p_mult
            cq2 = q2 * p_mult
            e_min = mt[j]
            entries += 1
            e_bar = i
            is_rev = False
            rv_go = False
        elif be_pend:
            be_on = True
            be_pend = False
        if pos != 0:
            s = pos
            for j in range(bstart[i], bstart[i + 1]):
                if lk_next:
                    lk_on = True
                    lk_next = False
                if hold_m > 0 and mt[j] - e_min >= hold_m:      # time exit at this minute's open
                    xo_ = s * mo[j]
                    fill = xo_ - slip - slb * abs(xo_)
                    for leg in range(2):
                        if (leg == 0 and not open1) or (leg == 1 and not open2):
                            continue
                        q = cq1 if leg == 0 else cq2
                        pnl = (fill - xe) * q * pv - 2 * q * (comm + cmb * abs(xe))
                        real += pnl
                        legs += 1
                        if pnl > 0:
                            gp += pnl
                            wins += 1
                        else:
                            gl -= pnl
                        if rec:
                            T[nt, 0] = e_row
                            T[nt, 1] = j
                            T[nt, 2] = s
                            T[nt, 3] = s * xe
                            T[nt, 4] = s * fill
                            T[nt, 5] = pnl
                            T[nt, 6] = 7
                            T[nt, 7] = leg + 1
                            T[nt, 8] = sb
                            T[nt, 9] = 1.0 if is_rev else 0.0
                            T[nt, 10] = q
                            nt += 1
                    open1 = False
                    open2 = False
                    rv_go = False
                    if real > peak:
                        peak = real
                    if peak - real > max_dd:
                        max_dd = peak - real
                    pos = 0
                    break
                if s > 0:
                    xo, xh, xl, xc = mo[j], mh[j], ml[j], mc[j]
                else:
                    xo, xh, xl, xc = -mo[j], -ml[j], -mh[j], -mc[j]
                up_first = (xh - xo) <= (xo - xl)           # nearer extreme first
                for step in range(3):
                    if step == 0:
                        px = xo
                        gap = True
                    elif (step == 1) == up_first:
                        px = xh
                        gap = False
                    else:
                        px = xl
                        gap = False
                    hi_pt = step == 0 or px == xh
                    lo_pt = step == 0 or px == xl
                    # stops (worse fills: slippage)
                    if lo_pt:
                        lk = xe + bel if lk_on else -np.inf
                        base2 = max(max(sl, xe) if be_on else sl, lk)
                        st2 = max(base2, best - off) if tr_on else base2
                        for leg in range(2):
                            if (leg == 0 and not open1) or (leg == 1 and not open2):
                                continue
                            lvl = max(sl, lk) if leg == 0 else st2
                            if px <= lvl:
                                fill = (px if gap else lvl) - slip - slb * abs(lvl)
                                q = cq1 if leg == 0 else cq2
                                pnl = (fill - xe) * q * pv - 2 * q * (comm + cmb * abs(xe))
                                real += pnl
                                legs += 1
                                if pnl > 0:
                                    gp += pnl
                                    wins += 1
                                else:
                                    gl -= pnl
                                why = 1
                                if leg == 1:
                                    if tr_on and best - off > base2:
                                        why = 3
                                    elif be_on and xe > sl:
                                        why = 2
                                if lk_on and lvl == lk and lk > sl and why != 3:
                                    why = 6
                                if rec:
                                    T[nt, 0] = e_row
                                    T[nt, 1] = j
                                    T[nt, 2] = s
                                    T[nt, 3] = s * xe
                                    T[nt, 4] = s * fill
                                    T[nt, 5] = pnl
                                    T[nt, 6] = why
                                    T[nt, 7] = leg + 1
                                    T[nt, 8] = sb
                                    T[nt, 9] = 1.0 if is_rev else 0.0
                                    T[nt, 10] = cq1 if T[nt, 7] == 1 else cq2
                                    nt += 1
                                if (rev_n > 0 and not is_rev and not rv_go and lvl == sl and why == 1
                                        and i - e_bar < rev_n and rev_ok[sb]):
                                    rv_go = True
                                    rv_px = s * fill
                                if leg == 0:
                                    open1 = False
                                else:
                                    open2 = False
                    # target 1 and the trail
                    if hi_pt:
                        if bea > 0 and not lk_on and px >= xe + bea:
                            lk_next = True
                        if open1 and px >= lim:
                            fill = px if gap else lim
                            pnl = (fill - xe) * cq1 * pv - 2 * cq1 * (comm + cmb * abs(xe))
                            real += pnl
                            legs += 1
                            if pnl > 0:
                                gp += pnl
                                wins += 1
                            else:
                                gl -= pnl
                            if rec:
                                T[nt, 0] = e_row
                                T[nt, 1] = j
                                T[nt, 2] = s
                                T[nt, 3] = s * xe
                                T[nt, 4] = s * fill
                                T[nt, 5] = pnl
                                T[nt, 6] = 0
                                T[nt, 7] = 1
                                T[nt, 8] = sb
                                T[nt, 9] = 1.0 if is_rev else 0.0
                                T[nt, 10] = cq1 if T[nt, 7] == 1 else cq2
                                nt += 1
                            open1 = False
                            if be_t1 and open2:
                                be_pend = True
                        if open2 and px >= lim2:                    # fixed Target 2 (t2_atr)
                            fill = px if gap else lim2
                            pnl = (fill - xe) * cq2 * pv - 2 * cq2 * (comm + cmb * abs(xe))
                            real += pnl
                            legs += 1
                            if pnl > 0:
                                gp += pnl
                                wins += 1
                            else:
                                gl -= pnl
                            if rec:
                                T[nt, 0] = e_row
                                T[nt, 1] = j
                                T[nt, 2] = s
                                T[nt, 3] = s * xe
                                T[nt, 4] = s * fill
                                T[nt, 5] = pnl
                                T[nt, 6] = 5
                                T[nt, 7] = 2
                                T[nt, 8] = sb
                                T[nt, 9] = 1.0 if is_rev else 0.0
                                T[nt, 10] = cq1 if T[nt, 7] == 1 else cq2
                                nt += 1
                            open2 = False
                        if open2:
                            if not tr_on and px >= act:
                                tr_on = True
                            if tr_on and px > best:
                                best = px
                    if not (open1 or open2):
                        break
                # equity: worst point of the minute, peak on its close
                oq = (cq1 if open1 else 0.0) + (cq2 if open2 else 0.0)
                low_eq = real + (xl - xe) * oq * pv
                if peak - low_eq > max_dd:
                    max_dd = peak - low_eq
                cl_eq = real + (xc - xe) * oq * pv
                if cl_eq > peak:
                    peak = cl_eq
                if not (open1 or open2):
                    if rv_go:                                  # stop-and-reverse at the stop fill price
                        ta_ = ta[sb]
                        risk_x = xe - sl
                        s = -s
                        pos = s
                        xe = s * rv_px
                        sl = xe - risk_x
                        lim = xe + t1m * ta_
                        act = xe + tram * ta_
                        off = trm * ta_ if trm > 0 else np.inf
                        bea = be_a * ta_ if be_a > 0 else 0.0
                        bel = be_l * ta_
                        lk_on = False
                        lk_next = False
                        lim2 = xe + t2m * ta_ if t2m > 0 else np.inf
                        best = -np.inf
                        tr_on = False
                        be_on = False
                        be_pend = False
                        open1 = q1 > 0
                        open2 = q2 > 0
                        e_row = j
                        e_bar = i
                        e_min = mt[j]
                        entries += 1
                        is_rev = True
                        rv_go = False
                        continue
                    pos = 0
                    break
        else:
            if real > peak:
                peak = real
            if peak - real > max_dd:
                max_dd = peak - real
        # signal at this bar's close -> order for the next bar's open
        if pos == 0 and sig[i] != 0 and ok[i] and i + 1 < n:
            s = sig[i]
            if (s > 0 and allow_l) or (s < 0 and allow_s):
                xc_ = s * c[i]
                if sl_mode == 0:
                    lvl = zbot[i] if s > 0 else ztop[i]
                elif sl_mode == 1:
                    lvl = (ztop[i] + zbot[i]) / 2
                else:
                    lvl = c[i] - s * slm * ta[i]
                p_sl = s * lvl
                p_lim = xc_ + t1m * ta[i]
                p_act = xc_ + tram * ta[i]
                p_off = trm * ta[i] if trm > 0 else np.inf          # trail_atr null/0 = no trailing stop
                p_bea = be_a * ta[i] if be_a > 0 else 0.0           # be_atr null/0 = no lock
                p_bel = be_l * ta[i]
                p_lim2 = xc_ + t2m * ta[i] if t2m > 0 else np.inf   # t2_atr null/0 = no fixed Target 2
                pend = s
                sb = i
                p_mult = mult[i]
    open_pnl = 0.0
    if pos != 0:
        j = bstart[n] - 1
        xc = pos * mc[j]
        for leg in range(2):
            if (leg == 0 and open1) or (leg == 1 and open2):
                q = cq1 if leg == 0 else cq2
                pnl = (xc - xe) * q * pv
                open_pnl += pnl
                if rec:
                    T[nt, 0] = e_row
                    T[nt, 1] = j
                    T[nt, 2] = pos
                    T[nt, 3] = pos * xe
                    T[nt, 4] = pos * xc
                    T[nt, 5] = pnl
                    T[nt, 6] = 4
                    T[nt, 7] = leg + 1
                    T[nt, 8] = sb
                    T[nt, 9] = 1.0 if is_rev else 0.0
                    T[nt, 10] = cq1 if T[nt, 7] == 1 else cq2
                    nt += 1
    st = np.zeros(len_stats())
    st[0] = real
    st[1] = gp / gl if gl > 0 else np.inf
    st[2] = max_dd
    st[3] = real / max_dd if max_dd > 0 else np.inf
    st[4] = legs
    st[5] = 100.0 * wins / legs if legs else 0.0
    st[6] = entries
    st[7] = gp
    st[8] = gl
    st[9] = open_pnl
    return st, T[:nt]


@njit(cache=True)
def len_stats():
    return 10                                  # = len(STATS) of orb_backtest


# ----------------------------------------------------------------------------- driver
SL_MODES = {"box": 0, "mid": 1, "atr": 2}


def check(p):
    if p["direction"] not in ("Long only", "Short only", "Both"):
        raise SystemExit(f"direction must be Long only | Short only | Both, not {p['direction']!r}")
    if p["box_src"] not in ("close", "body"):
        raise SystemExit(f"box_src must be close | body, not {p['box_src']!r}")
    if p["break_mode"] not in ("close", "body"):
        raise SystemExit(f"break_mode must be close | body, not {p['break_mode']!r}")
    if p["vol_ref"] not in ("zone", "tod"):
        raise SystemExit(f"vol_ref must be zone | tod, not {p['vol_ref']!r}")
    if p["sl_mode"] not in SL_MODES:
        raise SystemExit(f"sl_mode must be one of {list(SL_MODES)}, not {p['sl_mode']!r}")
    if int(p["qty_t1"]) + int(p["qty_t2"]) <= 0:
        raise SystemExit("qty_t1 + qty_t2 must be > 0")


def skip_mask(t, skip):
    """True on bars whose open lies in "HH:MM-HH:MM" New York (may wrap midnight)."""
    m = np.asarray(t.hour * 60 + t.minute, np.int64)
    if skip is None:
        return np.zeros(len(m), np.bool_)
    a, b = (hhmm(x) for x in str(skip).split("-"))
    return ((m >= a) & (m < b)) if a <= b else ((m >= a) | (m < b))


class Bars:
    """Chart bars per timeframe and indicator arrays, built once and shared by every sweep row."""

    def __init__(self, m1, cfg):
        self.m1, self.cfg, self.cache = m1, cfg, {}

    def get(self, tf):
        if tf not in self.cache:
            t, o, h, l, c, bstart = chart_bars(self.m1, tf)
            bt = self.cfg.get("backtest") or {}
            ok = np.ones(len(c), np.bool_)
            if bt.get("start"):
                ok &= np.asarray(t >= pd.Timestamp(str(bt["start"])).tz_localize(NY))
            if bt.get("end"):
                ok &= np.asarray(t < pd.Timestamp(str(bt["end"])).tz_localize(NY) + pd.Timedelta(days=1))
            self.cache[tf] = {"t": t, "o": o, "h": h, "l": l, "c": c, "bstart": bstart, "ok": ok, "atr": {}}
        return self.cache[tf]

    def minutes(self):
        """1-minute bar times as integer minutes (time exit)."""
        if "_mt" not in self.__dict__:
            self._mt = ((self.m1.t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
        return self._mt

    def raw_close(self, tf):
        """Unadjusted close of each chart bar (back-adjusted prices shift old years; qty_step needs the real price)."""
        b = self.get(tf)
        if "raw" not in b:
            raw = load_big(self.cfg["data"]["instrument"], False)
            if len(raw) != len(self.m1) or not (raw.t.to_numpy() == self.m1.t.to_numpy()).all():
                raise SystemExit("raw / adjusted 1m rows do not line up")
            b["raw"] = raw.close.to_numpy()[b["bstart"][1:] - 1]
        return b["raw"]

    def vol(self, tf):
        b = self.get(tf)
        if "vol" not in b:
            b["vol"] = np.add.reduceat(self.m1.volume.to_numpy(np.float64), b["bstart"][:-1])
        return b["vol"]

    def vol_tod(self, tf, days):
        """Average volume of the same time-of-day bar over the last `days` sessions (today not included)."""
        b = self.get(tf)
        key = ("tod", days)
        if key not in b:
            v = pd.Series(self.vol(tf))
            slot = pd.Series(b["t"].hour * 60 + b["t"].minute)
            b[key] = v.groupby(slot).transform(
                lambda x: x.shift(1).rolling(days, min_periods=min(days, max(3, days // 2))).mean()).to_numpy()
        return b[key]

    def adx(self, tf, n):
        """Wilder ADX(n) of the chart bars (NaN during the warm-up)."""
        b = self.get(tf)
        key = ("adx", n)
        if key not in b:
            h, l, c = b["h"], b["l"], b["c"]
            pc = np.r_[c[0], c[:-1]]
            tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
            up, dn = np.r_[0.0, np.diff(h)], np.r_[0.0, -np.diff(l)]
            pdm = np.where((up > dn) & (up > 0), up, 0.0)
            mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
            rma = lambda x: pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
            a = rma(tr)
            with np.errstate(divide="ignore", invalid="ignore"):
                pdi, mdi = 100 * rma(pdm) / a, 100 * rma(mdm) / a
                dx = np.nan_to_num(100 * np.abs(pdi - mdi) / (pdi + mdi))
            adx = rma(dx).copy()
            adx[: 2 * n] = np.nan
            b[key] = adx
        return b[key]

    def atr(self, tf, n):
        b = self.get(tf)
        if n not in b["atr"]:
            b["atr"][n] = atr(b["h"], b["l"], b["c"], n)
        return b["atr"][n]


def vol_ratio(bars, tf, p, sig, zfirst):
    """Breakout bar volume / reference volume (NaN on bars without a breakout)."""
    v = bars.vol(tf)
    out = np.full(len(v), np.nan)
    ix = np.flatnonzero(sig != 0)
    if p["vol_ref"] == "tod":
        ref = bars.vol_tod(tf, int(p["vol_days"]))[ix]
    else:                                            # zone: average of the zone's bars before the breakout
        cs = np.r_[0.0, np.cumsum(v)]
        a = zfirst[ix]
        ref = (cs[ix] - cs[a]) / np.maximum(ix - a, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[ix] = np.where(ref > 0, v[ix] / ref, np.nan)
    return out


def run(bars, p, cfg, rec):
    check(p)
    tf = int(p["timeframe"])
    b = bars.get(tf)
    o, c = b["o"], b["c"]
    body = p["box_src"] == "body"
    hi_v = np.maximum(o, c) if body else c
    lo_v = np.minimum(o, c) if body else c
    zk = zone_key(p)
    zc = bars.__dict__.setdefault("zcache", {})
    if zk not in zc:
        if len(zc) >= 4:                                  # keep memory small: only the last few zone sets
            zc.pop(next(iter(zc)))
        zc[zk] = zones(o, c, hi_v, lo_v, bars.atr(tf, int(p["atr_len"])), int(p["min_bars"]), int(p["max_bars"]),
                       float(p["max_h_atr"]), float(p["max_slope"]), p["break_mode"] == "body", float(p["break_pct"]))
    sig, ztop, zbot, zlen, zfirst, started = zc[zk]
    ok = b["ok"] & ~skip_mask(b["t"], p["skip_hours"])
    vk = zk + (p["vol_ref"], int(p["vol_days"]))
    vc = bars.__dict__.setdefault("vcache", {})
    if vk not in vc:
        if len(vc) >= 4:
            vc.pop(next(iter(vc)))
        vc[vk] = vol_ratio(bars, tf, p, sig, zfirst)
    vr = vc[vk]
    if p["vol_min"] is not None:
        ok = ok & (vr >= float(p["vol_min"]))           # NaN ratio -> no trade
    mult = np.ones(len(c))
    if p["vol_boost"] is not None:
        mult[vr >= float(p["vol_boost"])] = float(p["vol_mult"])
    if p["qty_tiers"]:
        lv = sorted((float(a), float(m)) for a, m in (x.split(":") for x in str(p["qty_tiers"]).split(",")))
        px = bars.raw_close(tf)
        tm_ = np.full(len(c), lv[0][1])
        for a, m in lv:
            tm_[px >= a] = m
        mult = mult * tm_
    if p["qty_step"]:
        mult = mult * np.maximum(1.0, np.floor(bars.raw_close(tf) / float(p["qty_step"])))
    if p["risk_usd"]:                                  # size: whole trade (qty_t1 + qty_t2 units) risks risk_usd at the stop
        ta_s = bars.atr(tf, int(p["t_atr_len"]))
        if p["sl_mode"] == "atr":
            rk = float(p["sl_atr"]) * ta_s
        elif p["sl_mode"] == "mid":
            rk = np.abs(c - (ztop + zbot) / 2)
        else:
            rk = np.where(sig > 0, c - zbot, ztop - c)
        units = int(p["qty_t1"]) + int(p["qty_t2"])
        with np.errstate(divide="ignore", invalid="ignore"):
            sz = np.where(rk > 0, float(p["risk_usd"]) / (units * rk * float(cfg["costs"]["point_value"])), 0.0)
        mult = mult * np.nan_to_num(sz)
    rev_ok = np.ones(len(c), np.bool_)                 # reverse allowed for a trade from this breakout bar
    if p["adx_min"] is not None:
        ok = ok & (bars.adx(tf, int(p["adx_len"])) >= float(p["adx_min"]))      # NaN (warm-up) -> no trade
    if p["rev_vol_min"] is not None:
        rev_ok &= vr >= float(p["rev_vol_min"])
    if p["rev_vol_max"] is not None:
        rev_ok &= vr < float(p["rev_vol_max"])
    cost = cfg["costs"]
    m1 = bars.m1
    st, T = simulate(sig, ztop, zbot, c, bars.atr(tf, int(p["t_atr_len"])), ok, b["bstart"],
                     m1.open.to_numpy(), m1.high.to_numpy(), m1.low.to_numpy(), m1.close.to_numpy(),
                     p["direction"] != "Short only", p["direction"] != "Long only",
                     int(p["qty_t1"]), int(p["qty_t2"]), float(p["t1_atr"]), float(p["t2_atr"] or 0), float(p["trail_atr"] or 0),
                     float(p["trail_act"]), SL_MODES[p["sl_mode"]], float(p["sl_atr"]), bool(p["be_after_t1"]),
                     float(cost["commission"]), float(cost["point_value"]),
                     float(cost["slippage_ticks"]) * float(cost["tick"]), rec, int(p["rev_bars"] or 0), mult,
                     float(p["be_atr"] or 0), float(p["be_lock"] or 0), rev_ok,
                     float(cost.get("commission_bps") or 0) / 1e4, float(cost.get("slippage_bps") or 0) / 1e4,
                     bars.minutes(), int(round(float(p["max_hold_days"] or 0) * 1440)))
    return st, T, (sig, ztop, zbot, zlen, zfirst, started, vr)


def run_single(bars, p, cfg, stamp):
    st, T, (sig, ztop, zbot, zlen, zfirst, started, vr) = run(bars, p, cfg, True)
    tf = int(p["timeframe"])
    b = bars.get(tf)
    tm = bars.m1.t.dt.tz_convert(NY)
    sbar = T[:, 8].astype(int)
    entry_t = tm.iloc[T[:, 0].astype(int)].reset_index(drop=True)
    trades = pd.DataFrame({
        "signal_bar": b["t"][sbar].strftime("%Y-%m-%d %H:%M"),
        "entry_time": entry_t.dt.strftime("%Y-%m-%d %H:%M").to_numpy(),
        "exit_time": tm.iloc[T[:, 1].astype(int)].dt.strftime("%Y-%m-%d %H:%M").to_numpy(),
        "hour": entry_t.dt.hour.to_numpy(),
        "side": np.where(T[:, 2] > 0, "Long", "Short"),
        "leg": T[:, 7].astype(int),
        "contracts": T[:, 10].round(0).astype(int),
        "entry_price": T[:, 3].round(2), "exit_price": T[:, 4].round(2),
        "pnl": T[:, 5].round(2), "exit": [REASONS[int(w)] for w in T[:, 6]],
        "zone_bars": zlen[sbar], "zone_top": ztop[sbar].round(2), "zone_bottom": zbot[sbar].round(2),
        "zone_height": (ztop[sbar] - zbot[sbar]).round(2),
        "atr_t": bars.atr(tf, int(p["t_atr_len"]))[sbar].round(2),
        "reverse": T[:, 9] > 0,
        "vol_ratio": vr[sbar].round(2)})
    trades["cum_pnl"] = trades.pnl.where(trades.exit != "Open", 0).cumsum().round(2)

    closed = trades[trades.exit != "Open"]
    pos = closed.groupby("entry_time").agg(side=("side", "first"), pnl=("pnl", "sum"))
    by_year = closed.groupby(closed.entry_time.str[:4]).pnl.agg(net="sum", legs="count").round(0)
    by_year["trades"] = pos.groupby(pos.index.str[:4]).size()
    by_side = pos.groupby("side").pnl.agg(net="sum", trades="count", win_rate=lambda x: (x > 0).mean() * 100).round(1)
    by_exit = closed.groupby(["leg", "exit"]).pnl.agg(net="sum", legs="count").round(0)
    by_hour = closed.groupby("hour").pnl.agg(net="sum", legs="count").round(0)
    rpos = closed.groupby("entry_time").agg(rev=("reverse", "first"), pnl=("pnl", "sum"))
    by_rev = rpos.groupby("rev").pnl.agg(net="sum", trades="count", win_rate=lambda x: (x > 0).mean() * 100).round(1)
    by_rev.index = by_rev.index.map({False: "breakout trades", True: "reverse trades"})
    vpos = closed.groupby("entry_time").agg(vr=("vol_ratio", "first"), pnl=("pnl", "sum"), q=("contracts", "sum"))
    vpos["bucket"] = pd.cut(vpos.vr, [0, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, np.inf], right=False)
    by_vol = vpos.groupby("bucket", observed=True).agg(trades=("pnl", "size"), net=("pnl", "sum"),
                                                        win_rate=("pnl", lambda x: (x > 0).mean() * 100),
                                                        per_contract=("pnl", "sum")).round(1)
    by_vol["per_contract"] = (vpos.groupby("bucket", observed=True).pnl.sum()
                              / vpos.groupby("bucket", observed=True).q.sum()).round(1)
    data, cost = cfg["data"], cfg["costs"]
    t = b["t"]
    head = (f"Flat Zone Breakout - {data['instrument']} ({data['source']}) {tf}m, 1m fills, "
            f"{'back-adjusted' if data['back_adjust'] else 'unadjusted'} rolls\n"
            f"Data {t[0]:%Y-%m-%d %H:%M} -> {t[-1]:%Y-%m-%d %H:%M} New York\n"
            f"Settings {p}\n"
            f"Costs ${cost['commission']}/contract/fill, {cost['slippage_ticks']} tick(s) slippage, "
            f"${cost['point_value']}/point\n"
            f"Zones started {started}, breakouts {int((sig != 0).sum())}\n\n")
    text = (head + fmt_stats(st)
            + f"Positions         {len(pos)}, win rate {(pos.pnl > 0).mean() * 100:.1f}% (all legs together)\n"
            + "\nBy side (positions):\n" + by_side.to_string()
            + "\n\nBreakout vs reverse (positions):\n" + by_rev.to_string()
            + f"\n\nBy breakout volume ratio (vol_ref {p['vol_ref']}; per_contract = net / contracts):\n" + by_vol.to_string()
            + "\n\nBy leg / exit:\n" + by_exit.to_string()
            + "\n\nBy year (entry):\n" + by_year.to_string()
            + "\n\nBy entry hour (New York):\n" + by_hour.to_string() + "\n")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"flatzone_{stamp}_summary.txt").write_text(text, encoding="utf-8")
    trades.to_csv(RESULTS / f"flatzone_{stamp}_trades.csv", index=False)
    print(text)
    print(f"-> results/flatzone_{stamp}_summary.txt, results/flatzone_{stamp}_trades.csv")


ZONE_KEYS = ("timeframe", "box_src", "atr_len", "min_bars", "max_bars", "max_h_atr", "max_slope", "break_mode", "break_pct")


def zone_key(p):
    return tuple(p[k] for k in ZONE_KEYS)


def norm(p):
    """The same trades whatever these are -> one simulation: settings that have no effect are set to None."""
    q = dict(p)
    if q["sl_mode"] != "atr":
        q["sl_atr"] = None
    if not q["be_atr"]:
        q["be_atr"] = q["be_lock"] = None
    if q["vol_boost"] is None:
        q["vol_mult"] = None
    if q["vol_min"] is None and q["vol_boost"] is None:
        q["vol_ref"] = q["vol_days"] = None
    if not q["trail_atr"]:
        q["trail_atr"] = q["trail_act"] = None
    if not q["rev_bars"]:
        q["rev_bars"] = q["rev_vol_min"] = q["rev_vol_max"] = None
    if q["adx_min"] is None:
        q["adx_len"] = None
    if not (q["be_after_t1"] and int(q["qty_t1"]) > 0 and int(q["qty_t2"]) > 0):
        q["be_after_t1"] = False
    return q


_W = {}


def _worker_init(cfg):
    try:                                                  # low priority: the PC stays usable
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # below normal
        except Exception:
            pass
    _W["cfg"] = cfg
    _W["bars"] = Bars(load_big(cfg["data"]["instrument"], cfg["data"]["back_adjust"]), cfg)


def _worker_run(jobs):
    return [(k, run(_W["bars"], p, _W["cfg"], False)[0]) for k, p in jobs]


def free_gb():
    try:
        import psutil
        return psutil.virtual_memory().available / 1e9
    except Exception:
        pass
    try:                                                  # Windows without psutil
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                        ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
        m = MS(); m.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.avail / 1e9
    except Exception:
        return 8.0


def pick_workers(cfg, n_jobs):
    w = cfg.get("workers", "auto")
    cores = os.cpu_count() or 2
    if w in (None, "auto"):
        w = min(cores - 2, int((free_gb() - 2.0) / 1.0))   # ~1 GB per worker (NQ 1m data), 2 GB left free
    return max(1, min(int(w), n_jobs))


def run_sweep(bars, grid, cfg, stamp):
    names = list(grid)
    combos = [dict(zip(names, c)) for c in itertools.product(*grid.values())]
    uniq, seen = [], {}
    for p in combos:
        key = tuple(sorted((k, str(v)) for k, v in norm(p).items()))
        if key not in seen:
            seen[key] = len(uniq)
            uniq.append(p)
    varied = [k for k, v in grid.items() if len(v) > 1]
    print(f"sweep: {len(combos)} combinations of {varied} -> {len(uniq)} different ones "
          f"({len(combos) - len(uniq)} skipped: settings without effect, e.g. sl_atr with sl_mode box)")
    # one task = combos that share the zones (zones are computed once per task); big groups split
    groups = {}
    for k, p in enumerate(uniq):
        groups.setdefault(zone_key(p), []).append((k, p))
    workers = pick_workers(cfg, len(uniq))
    size = max(4, min(60, -(-len(uniq) // (workers * 4))))   # >= ~4 tasks per worker, zones shared inside a task
    tasks = []
    for g in groups.values():
        for s in range(0, len(g), size):
            tasks.append(g[s:s + size])
    workers = min(workers, len(tasks))
    print(f"  {workers} worker(s) of {os.cpu_count()} CPU cores, {len(groups)} zone settings, free RAM {free_gb():.1f} GB")
    res = {}
    t0 = time.time()
    last = 0.0
    if workers <= 1:
        for task in tasks:
            for k, st in _worker_run_local(bars, cfg, task):
                res[k] = st
            if time.time() - last > 10:
                last = time.time()
                _progress(len(res), len(uniq), t0)
    else:
        sys.path.insert(0, str(HERE))
        import flatzone_backtest as _me                   # real module name: numba's disk cache is reused by the workers
        with ProcessPoolExecutor(workers, initializer=_me._worker_init, initargs=(cfg,)) as ex:
            futs = [ex.submit(_me._worker_run, task) for task in tasks]
            for f in as_completed(futs):
                for k, st in f.result():
                    res[k] = st
                if time.time() - last > 10 or len(res) == len(uniq):
                    last = time.time()
                    _progress(len(res), len(uniq), t0)
    rows = []
    for k, p in enumerate(uniq):
        q = norm(p)
        rows.append({**{n: q[n] for n in varied}, **dict(zip(STATS, res[k]))})
    df = pd.DataFrame(rows).sort_values("net_to_dd", ascending=False)
    for col in ("net_profit", "max_drawdown", "gross_profit", "gross_loss", "open_pnl"):
        df[col] = df[col].round(0)
    df = df.round(2)
    RESULTS.mkdir(exist_ok=True)
    df.to_csv(RESULTS / f"flatzone_sweep_{stamp}.csv", index=False)
    show_best(df)
    print(f"\n-> results/flatzone_sweep_{stamp}.csv (every different combination, highest net/DD first; "
          f"empty = that setting has no effect in the row)  {time.time() - t0:.0f}s")


TRADE_SPLIT = 1000                                    # sweep summary: rows with fewer / at least this many trades


def show_best(df, n=5):
    """Top n by net profit and top n by net/DD (profitable rows), separately for < and >= TRADE_SPLIT trades."""
    for lab, part in ((f"trades < {TRADE_SPLIT}", df[df.trades < TRADE_SPLIT]),
                      (f"trades >= {TRADE_SPLIT}", df[df.trades >= TRADE_SPLIT])):
        print(f"\n==================== {lab}: {len(part)} rows")
        if part.empty:
            continue
        print(f"-- top {n} by NET PROFIT")
        print(part.sort_values("net_profit", ascending=False).head(n).to_string(index=False))
        prof = part[part.net_profit > 0]
        print(f"-- top {n} by NET / MAX DD (profitable only)")
        print(prof.sort_values("net_to_dd", ascending=False).head(n).to_string(index=False) if len(prof) else "   none profitable")


def _worker_run_local(bars, cfg, jobs):
    return [(k, run(bars, p, cfg, False)[0]) for k, p in jobs]


def _progress(done, total, t0):
    el = time.time() - t0
    left = el / done * (total - done) if done else 0
    print(f"  [{datetime.now():%H:%M:%S}] {done}/{total} ({100 * done / total:.1f} %)  {el / 60:.1f} min, ~{left / 60:.1f} min left",
          flush=True)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "flatzone_NQ_best.yaml"
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise SystemExit(f"{path} is not a settings file - pass a .yaml, e.g. flatzone/flatzone_NQ_best.yaml")
    cfg = load_cfg(path)
    bad = [k for k in cfg["strategy"] if k not in DEFAULTS]
    if bad:
        raise SystemExit(f"strategy: unknown setting(s) {bad} (allowed: {list(DEFAULTS)})")
    grid = {k: values(cfg["strategy"].get(k, v)) for k, v in DEFAULTS.items()}
    data = cfg.setdefault("data", {})
    data.setdefault("instrument", "NQ")
    data.setdefault("back_adjust", True)
    data.setdefault("source", "futures_1m")
    if data.get("stock"):                       # US stock (data/<SYM>_1min.parquet, RTH): $1 per share-point, 1 cent tick
        SPECS.setdefault(data["instrument"], (1.0, 0.01))
        data["back_adjust"] = False
    if data["instrument"] not in SPECS:
        raise SystemExit(f"instrument must be one of {list(SPECS)} (or data: stock: true), not {data['instrument']!r}")
    m1 = load_big(data["instrument"], data["back_adjust"])
    costs = cfg.setdefault("costs", {})
    costs.setdefault("commission", 2.5)
    costs.setdefault("slippage_ticks", 0)
    if costs.get("point_value") is None:
        costs["point_value"] = SPECS[data["instrument"]][0]
    costs["tick"] = SPECS[data["instrument"]][1]
    bars = Bars(m1, cfg)
    stamp = f"{data['instrument']}_{datetime.now():%Y%m%d_%H%M%S}"
    if all(len(v) == 1 for v in grid.values()):
        run_single(bars, {k: v[0] for k, v in grid.items()}, cfg, stamp)
    else:
        run_sweep(bars, grid, cfg, stamp)


if __name__ == "__main__":
    main()
