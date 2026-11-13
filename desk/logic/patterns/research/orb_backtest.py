"""Opening Range Breakout backtest with 3 ATR targets, on NQ (IBKR 1-minute files in data/) or ES
(data/es_1min_old.parquet) - pick with `data: instrument:` in the YAML.

    python orb_backtest.py                  # settings from orb_params.yaml
    python orb_backtest.py other.yaml

Any value under `strategy:` written as a list or {from, to, step} -> every combination is tested
(results/orb_sweep_<time>.csv, highest net profit first). Otherwise one run -> summary + list of trades.

Rules:
  * ranges start at each time in `sessions` (New York, e.g. "18:00,03:00,09:30" = Globex, London, NY open)
    and last `range_minutes`; the range's breakout window runs until the next range starts;
  * chart = `timeframe`-minute bars built from the 1-minute data; breakout = a bar CLOSE above the range high
    (long) or below the range low (short); the order fills at that close (+ slippage);
  * first_break_only: only the first close outside the range counts - if that side isn't allowed, no trade;
  * skip the range if it is wider than max_range_pct % of price; one trade per range, only when flat;
  * no entries on bars opening 16:45-18:00 New York (CME daily close) or from flat_time to 18:00;
  * 3 legs of qty1 / qty2 / qty3 contracts; stop = sl_mult x ATR(atr_len) of the signal bar, targets
    tp1/tp2/tp3 x ATR (rounded to ticks, tp1 < tp2 < tp3);
  * TP1 reached -> stop to entry; TP2 reached -> stop to TP1 and leg 3 trails: the stop gives back trail_pct %
    of the best open profit (never below TP1, only tightens); leg 3 still takes profit at TP3;
    stop moves apply from the next 1-minute bar;
  * flat_time: everything still open is closed at the close of the bar that reaches it (null = hold);
  * targets/stops fill on the 1-minute bars (path O -> nearer extreme -> other extreme -> C): at their price,
    or at the 1m open when it gaps past it. Slippage on entries, stops and flat_time exits, not on targets.
"""
import itertools
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from numba import njit, prange

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
RESULTS = HERE.parent / "results"
NY = "America/New_York"
TICK = 0.25
STATS = ["net_profit", "profit_factor", "max_drawdown", "net_to_dd", "trades", "win_rate",
         "entries", "gross_profit", "gross_loss", "open_pnl"]
REASONS = ["TP1", "TP2", "TP3", "Stop loss", "BE stop", "Trail stop", "Flat time", "Open"]


# ----------------------------------------------------------------------------- data
INSTRUMENTS = {"NQ": 20.0, "ES": 50.0}    # $ per point
ES_FILE = "es_1min_old.parquet"           # older Databento ES.n.0 extract (Aug 2026), in data/


def load_contract(path):
    df = pd.read_csv(path)
    df["t"] = pd.to_datetime(df.date, utc=True)
    df = df.drop_duplicates("t", keep="last").sort_values("t")
    # IBKR fills minutes without a trade with a flat volume-0 bar; TradingView has no bar there
    flat = (df.volume == 0) & (df.open == df.high) & (df.high == df.low) & (df.low == df.close)
    return df.loc[~flat, ["t", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def session_day(t):
    """CME session date: the session starting 18:00 New York belongs to the next day."""
    return (t.dt.tz_convert(NY).dt.tz_localize(None) + pd.Timedelta(hours=6)).dt.date


def back_adjust(df, rolls):
    """Remove the price jump at each roll: shift everything before it by the jump (rolls = [(row, jump)])."""
    shift = np.zeros(len(df))
    for row, jump in rolls:
        shift[:row] += jump
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] + shift
    return df


def load_ibkr(symbol):
    """IBKR contract files data/<symbol>??_1min.csv stitched like NQ1!: switch to the next contract on the
    first session (within 35 days of expiry) where its volume beats the current one.
    Returns the unadjusted bars and the rolls [(row, jump)]; None when there are no files."""
    meta = json.loads((DATA / "contracts.json").read_text()) if (DATA / "contracts.json").exists() else {}
    cs = sorted((pd.Timestamp(m["expiry"]).date(), local) for local, m in meta.items()
                if local[:-2] == symbol and (DATA / f"{local}_1min.csv").exists())
    if not cs:
        return None, []
    frames = [(exp, load_contract(DATA / f"{local}_1min.csv")) for exp, local in cs]
    # the oldest file starts while it was still a back month: drop its thin sessions
    exp0, df0 = frames[0]
    vol = df0.groupby(session_day(df0.t)).volume.sum()
    first = vol[vol >= 0.25 * vol.median()].index[0]
    frames[0] = (exp0, df0[(session_day(df0.t) >= first).to_numpy()])

    switches, jumps = [], []
    for (ea, A), (_, B) in zip(frames, frames[1:]):
        va, vb = A.groupby(session_day(A.t)).volume.sum(), B.groupby(session_day(B.t)).volume.sum()
        days = sorted(d for d in set(va.index) & set(vb.index) if (ea - d).days <= 35 and d <= ea)
        day = next((d for d in days if vb[d] > va[d]), ea)
        later = B.t[(session_day(B.t) >= day).to_numpy()]
        sw = later.iloc[0] if len(later) else A.t.iloc[-1] + pd.Timedelta(minutes=1)
        switches.append(sw)
        both = A[A.t < sw].merge(B[B.t < sw], on="t", suffixes=("_a", "_b"))
        jumps.append(float(both.close_b.iloc[-1] - both.close_a.iloc[-1]) if len(both) else 0.0)
    pieces = []
    for k, (_, df) in enumerate(frames):
        lo = switches[k - 1] if k else df.t.iloc[0]
        hi = switches[k] if k < len(switches) else df.t.iloc[-1] + pd.Timedelta(minutes=1)
        pieces.append(df[(df.t >= lo) & (df.t < hi)])
    m1 = pd.concat(pieces).sort_values("t").reset_index(drop=True)
    return m1, [(int(m1.t.searchsorted(sw)), j) for sw, j in zip(switches, jumps)]


def load_es_parquet():
    """Databento ES.n.0 (unadjusted). It rolls at 00:00 UTC a few days before each quarterly expiry:
    the roll is the 00:00 UTC bar with the largest open-vs-previous-close jump in that window."""
    path = DATA / ES_FILE
    df = pd.read_parquet(path).rename(columns={"ts_utc": "t"})
    m1 = df[["t", "open", "high", "low", "close", "volume"]].sort_values("t").drop_duplicates("t")
    m1 = m1.reset_index(drop=True)
    gap = (m1.open - m1.close.shift()).fillna(0).to_numpy()
    midnight = ((m1.t.dt.hour == 0) & (m1.t.dt.minute == 0)).to_numpy()
    rolls = []
    for y in range(m1.t.dt.year.min(), m1.t.dt.year.max() + 1):
        for mth in (3, 6, 9, 12):
            d = pd.Timestamp(y, mth, 15, tz="UTC")
            expiry = d + pd.Timedelta(days=(4 - d.weekday()) % 7)          # third Friday
            win = (midnight & (m1.t >= expiry - pd.Timedelta(days=12)) & (m1.t <= expiry)).to_numpy()
            rows = np.flatnonzero(win)
            if len(rows):
                r = rows[np.argmax(np.abs(gap[rows]))]
                rolls.append((int(r), float(gap[r])))
    return m1, rolls


def load_es():
    """data/es_1min_old.parquet, continued after its last bar by the IBKR ES contract files in data/ (if any)."""
    m1, rolls = load_es_parquet()
    ib, ib_rolls = load_ibkr("ES")
    if ib is None:
        return m1, rolls
    end = m1.t.iloc[-1]
    k0 = int(ib.t.searchsorted(end, side="right"))
    if k0 >= len(ib):
        return m1, rolls
    both = m1[["t", "close"]].merge(ib.iloc[:k0][["t", "close"]], on="t", suffixes=("_a", "_b"))
    if not len(both):
        raise SystemExit(f"IBKR ES files do not overlap the end of {ES_FILE} ({end}) - download older ES bars")
    n = len(m1)
    rolls = rolls + [(n, float(both.close_b.iloc[-1] - both.close_a.iloc[-1]))]
    rolls += [(n + r - k0, j) for r, j in ib_rolls if r >= k0]
    out = pd.concat([m1.assign(t=m1.t.dt.as_unit("ns")), ib.iloc[k0:].assign(t=ib.t.iloc[k0:].dt.as_unit("ns"))],
                    ignore_index=True)
    return out, rolls


def load_1m(instrument="NQ", adjust=True):
    if instrument not in INSTRUMENTS:
        raise SystemExit(f"instrument must be one of {list(INSTRUMENTS)}, not {instrument!r}")
    m1, rolls = load_ibkr("NQ") if instrument == "NQ" else load_es()
    if m1 is None:
        raise SystemExit(f"no {instrument} data found")
    return back_adjust(m1, rolls) if adjust else m1


def chart_bars(m1, tf):
    """tf-minute bars (clock aligned, bar time = open) + the 1m rows each one covers."""
    minute = ((m1.t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
    bucket = minute - minute % tf
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    bstart = np.r_[starts, len(m1)].astype(np.int64)
    o = m1.open.to_numpy()[starts]
    h = np.maximum.reduceat(m1.high.to_numpy(), starts)
    l = np.minimum.reduceat(m1.low.to_numpy(), starts)
    c = m1.close.to_numpy()[bstart[1:] - 1]
    t = pd.to_datetime(bucket[starts], unit="m", utc=True).tz_convert(NY)
    return t, o, h, l, c, bstart


def hhmm(s):
    """"09:30" -> 570 (an unquoted 09:30 in YAML already arrives as 570)."""
    if isinstance(s, (int, np.integer)):
        return int(s)
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


def range_slots(t, sessions):
    """For each bar: id of the range it belongs to (its start, in New York clock minutes) and the minutes
    from that start to the bar's open. A range runs from its start time until the next one starts."""
    starts = [hhmm(s) for s in (str(sessions).split(",") if isinstance(sessions, str) else [sessions])]
    m = np.asarray(t.hour * 60 + t.minute, np.int64)
    day = np.asarray((t.tz_localize(None).normalize() - pd.Timestamp(0)) // pd.Timedelta(days=1), np.int64)
    age = np.min(np.stack([(m - s) % 1440 for s in starts]), axis=0)
    return day * 1440 + m - age, age


def bar_flags(t, tf, flat_time, cfg):
    """entry allowed per bar, and the bars whose close reaches flat_time."""
    m = np.asarray(t.hour * 60 + t.minute, np.int64)
    ok = ~((m >= 16 * 60 + 45) & (m < 18 * 60))
    if (cfg.get("data") or {}).get("all_hours"):     # 24/7 markets (crypto): no CME close / reopen window
        ok = np.ones(len(m), np.bool_)
    flat = np.zeros(len(m), np.bool_)
    if flat_time is not None:
        f = hhmm(flat_time)
        flat = (m < f) & (m + tf >= f)
        ok &= ~(((m >= f) & (m < 18 * 60)) | flat)
    bt = cfg.get("backtest") or {}
    if bt.get("start"):
        ok &= np.asarray(t >= pd.Timestamp(bt["start"]).tz_localize(NY))
    if bt.get("end"):
        ok &= np.asarray(t < pd.Timestamp(bt["end"]).tz_localize(NY) + pd.Timedelta(days=1))
    return ok, flat


# ----------------------------------------------------------------------------- indicators
@njit(cache=True)
def atr(h, l, c, n):
    """ta.atr: RMA of the true range, seeded with the SMA of the first n values."""
    out = np.full(len(c), np.nan)
    s = 0.0
    for i in range(len(c)):
        tr = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if i < n - 1:
            s += tr
        elif i == n - 1:
            out[i] = (s + tr) / n
        else:
            out[i] = (out[i - 1] * (n - 1) + tr) / n
    return out


# ----------------------------------------------------------------------------- simulation
@njit(cache=True)
def book(acc, T, nt, rec, entry_row, row, side, entry, px, qty, comm, pv, why, leg):
    """Close one leg at px. acc = realized, gross profit, gross loss, wins, trades."""
    pnl = side * (px - entry) * qty * pv - 2 * qty * comm
    acc[0] += pnl
    acc[4] += 1
    if pnl > 0:
        acc[1] += pnl
        acc[3] += 1
    else:
        acc[2] -= pnl
    if rec:
        T[nt, 0] = entry_row
        T[nt, 1] = row
        T[nt, 2] = side
        T[nt, 3] = entry
        T[nt, 4] = px
        T[nt, 5] = pnl
        T[nt, 6] = why
        T[nt, 7] = leg
        return nt + 1
    return nt


@njit(cache=True)
def simulate(c, bstart, slot, age, ok, flat, xatr, mo, mh, ml, mc,
             range_min, max_pct, first_only, allow_long, allow_short,
             sl, tp, trail, qty, comm, pv, slip, rec):
    """tp = TP1-3 multipliers, qty = contracts of legs 1-3, max_pct = nan when off, trail = % given back.
    Returns stats (STATS order) and, when rec, one row per leg: entry/exit 1m row, side, prices, pnl, reason, leg."""
    n = len(c)
    T = np.empty((3 * n + 3 if rec else 1, 8))
    nt = 0
    acc = np.zeros(5)
    sl_px = slip * TICK

    leg_open = np.zeros(3, np.bool_)
    tps = np.zeros(3)
    side = 0
    entry = 0.0
    entry_row = 0
    stage = 0                         # 0 = initial stop, 1 = TP1 reached (stop at entry), 2 = TP2 reached (trailing)
    stop = 0.0
    best = 0.0
    peak = 0.0
    max_dd = 0.0
    entries = 0

    cur = -1
    or_hi = 0.0
    or_lo = 0.0
    nbars = 0
    done = False

    for i in range(n):
        # ---- intrabar on the 1-minute bars: O -> nearer extreme -> other extreme -> C
        for j in range(bstart[i], bstart[i + 1]):
            if side == 0:
                continue
            fav = mh[j] if side > 0 else ml[j]
            adv = ml[j] if side > 0 else mh[j]
            high_first = mh[j] - mo[j] <= mo[j] - ml[j]
            fav_first = high_first if side > 0 else not high_first
            reached = np.zeros(3, np.bool_)
            for step in range(2):
                if (step == 0) == fav_first:                       # favourable part of the path: targets
                    for k in range(3):
                        if side * (fav - tps[k]) >= 0:
                            reached[k] = True
                            if leg_open[k]:
                                px = mo[j] if side * (mo[j] - tps[k]) >= 0 else tps[k]
                                nt = book(acc, T, nt, rec, entry_row, j, side, entry, px, qty[k], comm, pv, k, k)
                                leg_open[k] = False
                elif side * (adv - stop) <= 0:                     # adverse part: stop for every open leg
                    px = (mo[j] if side * (mo[j] - stop) <= 0 else stop) - side * sl_px
                    for k in range(3):
                        if leg_open[k]:
                            nt = book(acc, T, nt, rec, entry_row, j, side, entry, px, qty[k], comm, pv, 3 + stage, k)
                            leg_open[k] = False
            oq = leg_open[0] * qty[0] + leg_open[1] * qty[1] + leg_open[2] * qty[2]
            # drawdown: equity (closed + open at the 1m close) peak vs the 1m bar's worst price
            dd = peak - (acc[0] + side * (adv - entry) * oq * pv)
            if dd > max_dd:
                max_dd = dd
            eq = acc[0] + side * (mc[j] - entry) * oq * pv
            if eq > peak:
                peak = eq
            if not (leg_open[0] or leg_open[1] or leg_open[2]):
                side = 0
                continue
            # stop moves, effective from the next 1-minute bar
            if reached[0] and stage == 0:
                stage = 1
                stop = entry
            if reached[1] and stage < 2:
                stage = 2
                stop = tps[0]
                best = fav
            if stage == 2:
                best = max(best, fav) if side > 0 else min(best, fav)
                trail_stop = best - side * trail / 100.0 * abs(best - entry)
                stop = max(stop, trail_stop) if side > 0 else min(stop, trail_stop)
        if side == 0:
            if acc[0] > peak:
                peak = acc[0]
            if peak - acc[0] > max_dd:
                max_dd = peak - acc[0]

        # ---- bar close
        if flat[i] and side != 0:
            last = bstart[i + 1] - 1
            for k in range(3):
                if leg_open[k]:
                    nt = book(acc, T, nt, rec, entry_row, last, side, entry, c[i] - side * sl_px, qty[k],
                              comm, pv, 6, k)
                    leg_open[k] = False
            side = 0
            if acc[0] > peak:
                peak = acc[0]
            if peak - acc[0] > max_dd:
                max_dd = peak - acc[0]

        if slot[i] != cur:
            cur = slot[i]
            nbars = 0
            done = False
        if age[i] < range_min:                                    # building the range
            if nbars == 0:
                or_hi = -np.inf
                or_lo = np.inf
            or_hi = max(or_hi, mh[bstart[i]:bstart[i + 1]].max())
            or_lo = min(or_lo, ml[bstart[i]:bstart[i + 1]].min())
            nbars += 1
            continue
        if nbars == 0 or done or side != 0 or not ok[i] or i == n - 1:
            continue
        if not np.isnan(max_pct) and (or_hi - or_lo) / c[i] * 100 > max_pct:
            done = True
            continue
        d = 1 if c[i] > or_hi else (-1 if c[i] < or_lo else 0)
        if d == 0:
            continue
        if (d > 0 and allow_long) or (d < 0 and allow_short):
            if np.isnan(xatr[i]):
                continue
            side = d
            entry = c[i] + d * sl_px
            entry_row = bstart[i + 1] - 1
            entries += 1
            for k in range(3):
                tps[k] = c[i] + d * np.floor(tp[k] * xatr[i] / TICK + 0.5) * TICK
                leg_open[k] = qty[k] > 0
            stop = c[i] - d * np.floor(sl * xatr[i] / TICK + 0.5) * TICK
            stage = 0
            done = True
        elif first_only:
            done = True

    open_pnl = 0.0
    if side != 0:
        last = len(mc) - 1
        for k in range(3):
            if leg_open[k]:
                open_pnl += side * (mc[last] - entry) * qty[k] * pv
                if rec:
                    pnl = side * (mc[last] - entry) * qty[k] * pv - 2 * qty[k] * comm
                    T[nt, 0], T[nt, 1], T[nt, 2], T[nt, 3] = entry_row, last, side, entry
                    T[nt, 4], T[nt, 5], T[nt, 6], T[nt, 7] = mc[last], pnl, 7, k
                    nt += 1
    gp, gl, wins, trades = acc[1], acc[2], acc[3], acc[4]
    net = gp - gl
    stats = np.array([net, gp / gl if gl > 0 else np.inf, max_dd, net / max_dd if max_dd > 0 else np.inf,
                      trades, wins / trades * 100 if trades else 0.0, entries, gp, gl, open_pnl])
    return stats, T[:nt]


@njit(parallel=True, cache=True)
def sweep_kernel(c, bstart, slot, age, ok, flat, ATRS, mo, mh, ml, mc, atr_i, range_min, max_pct, first_only,
                 along, ashort, sl, TP, trail, QTY, comm, pv, slip):
    out = np.empty((len(atr_i), 10))       # len(STATS)
    for k in prange(len(atr_i)):
        out[k] = simulate(c, bstart, slot, age, ok, flat, ATRS[atr_i[k]], mo, mh, ml, mc,
                          range_min[k], max_pct[k], first_only[k], along[k], ashort[k],
                          sl[k], TP[k], trail[k], QTY[k], comm, pv, slip, False)[0]
    return out


# ----------------------------------------------------------------------------- driver
def values(v):
    """A YAML value -> list of values to test (list, {from, to, step} with both ends, or a single value)."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        n = int(round((v["to"] - v["from"]) / v["step"])) + 1
        return [round(v["from"] + k * v["step"], 10) for k in range(n)]
    return [v]


def sides(direction):
    if direction not in ("Long only", "Short only", "Both"):
        raise SystemExit(f"direction must be Long only | Short only | Both, not {direction!r}")
    return direction != "Short only", direction != "Long only"


def fmt_stats(st):
    return (f"Net profit        ${st[0]:,.0f}\n"
            f"Gross profit/loss ${st[7]:,.0f} / ${st[8]:,.0f}\n"
            f"Profit factor     {st[1]:.2f}\n"
            f"Max drawdown      ${st[2]:,.0f}   (intrabar, 1m bars)\n"
            f"Net / max DD      {st[3]:.2f}\n"
            f"Closed trades     {int(st[4])} legs, win rate {st[5]:.1f}%\n"
            f"Entries           {int(st[6])}\n"
            f"Open position P&L ${st[9]:,.0f}\n")


def run_single(m1, mo, mh, ml, mc, p, cfg, stamp):
    tf = int(p["timeframe"])
    t, o, h, l, c, bstart = chart_bars(m1, tf)
    slot, age = range_slots(t, p["sessions"])
    ok, flat = bar_flags(t, tf, p["flat_time"], cfg)
    if not p["tp1_mult"] < p["tp2_mult"] < p["tp3_mult"]:
        raise SystemExit("targets must rise: tp1_mult < tp2_mult < tp3_mult")
    along, ashort = sides(p["direction"])
    cost = cfg["costs"]
    qty = np.array([p["qty1"], p["qty2"], p["qty3"]], float)
    tp = np.array([p["tp1_mult"], p["tp2_mult"], p["tp3_mult"]], float)
    max_pct = np.nan if p["max_range_pct"] is None else float(p["max_range_pct"])
    st, T = simulate(c, bstart, slot, age, ok, flat, atr(h, l, c, int(p["atr_len"])), mo, mh, ml, mc,
                     int(p["range_minutes"]), max_pct, bool(p["first_break_only"]), along, ashort,
                     float(p["sl_mult"]), tp, float(p["trail_pct"]), qty,
                     float(cost["commission"]), float(cost["point_value"]), float(cost["slippage_ticks"]), True)
    tm = m1.t.dt.tz_convert(NY)
    trades = pd.DataFrame({
        "entry_time": tm.iloc[T[:, 0].astype(int)].dt.strftime("%Y-%m-%d %H:%M").to_numpy(),
        "exit_time": tm.iloc[T[:, 1].astype(int)].dt.strftime("%Y-%m-%d %H:%M").to_numpy(),
        "side": np.where(T[:, 2] > 0, "Long", "Short"),
        "leg": T[:, 7].astype(int) + 1, "entry_price": T[:, 3], "exit_price": T[:, 4],
        "contracts": qty[T[:, 7].astype(int)],
        "pnl": T[:, 5].round(2), "exit": [REASONS[int(w)] for w in T[:, 6]]})
    trades["cum_pnl"] = trades.pnl.where(trades.exit != "Open", 0).cumsum().round(2)
    data = cfg["data"]
    head = (f"ORB 3 Targets - {data['instrument']} {tf}m, bar magnifier 1m, "
            f"{'back-adjusted' if data['back_adjust'] else 'unadjusted'} rolls\n"
            f"Data {t[0]:%Y-%m-%d %H:%M} -> {t[-1]:%Y-%m-%d %H:%M} New York\n"
            f"Settings {p}\n"
            f"Costs ${cost['commission']}/contract/fill, {cost['slippage_ticks']} tick(s) slippage, "
            f"${cost['point_value']}/point\n\n")
    text = head + fmt_stats(st)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"orb_{stamp}_summary.txt").write_text(text)
    trades.to_csv(RESULTS / f"orb_{stamp}_trades.csv", index=False)
    print(text)
    print(f"-> results/orb_{stamp}_summary.txt, results/orb_{stamp}_trades.csv")


def run_sweep(m1, mo, mh, ml, mc, grid, cfg, stamp):
    names = list(grid)
    combos = pd.DataFrame(list(itertools.product(*grid.values())), columns=names)
    rising = (combos.tp1_mult < combos.tp2_mult) & (combos.tp2_mult < combos.tp3_mult)
    print(f"{rising.sum():,} combinations ({(~rising).sum():,} skipped: targets must rise tp1 < tp2 < tp3)")
    combos = combos[rising]
    cost = cfg["costs"]
    parts = []
    t0 = time.time()
    keys = ["timeframe", "sessions", "flat_time"]
    for (tf, sess, ft), g in combos.groupby(keys, sort=False, dropna=False):
        tf = int(tf)
        ft = None if pd.isna(ft) else ft
        t, o, h, l, c, bstart = chart_bars(m1, tf)
        slot, age = range_slots(t, sess)
        ok, flat = bar_flags(t, tf, ft, cfg)
        lens = list(dict.fromkeys(int(x) for x in g.atr_len))
        ATRS = np.stack([atr(h, l, c, n) for n in lens])
        lr = np.array([sides(d) for d in g.direction], np.bool_)
        out = sweep_kernel(
            c, bstart, slot, age, ok, flat, ATRS, mo, mh, ml, mc,
            np.array([lens.index(int(x)) for x in g.atr_len], np.int64),
            g.range_minutes.to_numpy(np.int64),
            np.array([np.nan if pd.isna(x) else float(x) for x in g.max_range_pct]),
            g.first_break_only.to_numpy(np.bool_), lr[:, 0].copy(), lr[:, 1].copy(),
            g.sl_mult.to_numpy(float), g[["tp1_mult", "tp2_mult", "tp3_mult"]].to_numpy(float),
            g.trail_pct.to_numpy(float), g[["qty1", "qty2", "qty3"]].to_numpy(float),
            float(cost["commission"]), float(cost["point_value"]), float(cost["slippage_ticks"]))
        parts.append(pd.concat([g.reset_index(drop=True), pd.DataFrame(out, columns=STATS)], axis=1))
    res = pd.concat(parts).sort_values("net_profit", ascending=False).reset_index(drop=True)
    for col in ("trades", "entries"):
        res[col] = res[col].astype(int)
    res = res.round({"net_profit": 0, "max_drawdown": 0, "gross_profit": 0, "gross_loss": 0, "open_pnl": 0,
                     "profit_factor": 3, "net_to_dd": 2, "win_rate": 1})
    RESULTS.mkdir(exist_ok=True)
    res.to_csv(RESULTS / f"orb_sweep_{stamp}.csv", index=False)
    show = [c for c in names if len(grid[c]) > 1] + ["net_profit", "profit_factor", "max_drawdown", "net_to_dd",
                                                     "trades", "win_rate", "entries"]
    by_dd = res[res.net_profit > 0].sort_values("net_to_dd", ascending=False)
    print(f"done in {time.time() - t0:.1f}s")
    print("\nTop 5 by net profit:")
    print(res[show].head(5).to_string(index=False))
    print("\nTop 5 by net profit / max drawdown:")
    print(by_dd[show].head(5).to_string(index=False))
    print(f"\n-> results/orb_sweep_{stamp}.csv (every combination, highest net profit first)")


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "orb_params.yaml"
    cfg = yaml.safe_load(path.read_text())
    grid = {k: values(v) for k, v in cfg["strategy"].items()}
    data = cfg.setdefault("data", {})
    data.setdefault("instrument", "NQ")
    data.setdefault("back_adjust", True)
    cfg["costs"].setdefault("slippage_ticks", 0)
    m1 = load_1m(data["instrument"], data["back_adjust"])
    if cfg["costs"].get("point_value") is None:
        cfg["costs"]["point_value"] = INSTRUMENTS[data["instrument"]]
    mo, mh, ml, mc = (m1[k].to_numpy() for k in ("open", "high", "low", "close"))
    stamp = f"{data['instrument']}_{datetime.now():%Y%m%d_%H%M%S}"
    if all(len(v) == 1 for v in grid.values()):
        run_single(m1, mo, mh, ml, mc, {k: v[0] for k, v in grid.items()}, cfg, stamp)
    else:
        run_sweep(m1, mo, mh, ml, mc, grid, cfg, stamp)


if __name__ == "__main__":
    main()
