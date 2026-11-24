"""box2 entry-condition filters + score-based sizing, shared by box/box_backtest.py and po/po_backtest.py.

Every condition is evaluated on the bar the entry decision is made on (its close), per side: "signed" features are
multiplied by the trade direction (+ = in the trade's favour). null = condition off.
  f_day_used_min     today's range so far (18:00 session) / average daily range of the 20 previous sessions >= x
  f_vwap_min         (close - session VWAP) / ATR14, signed >= x        (price already beyond VWAP in the trade's way)
  f_ema50_slope_min  1-hour change of EMA50 / ATR14, signed >= x       (trend already moving the trade's way)
  f_ema200_slope_min 4-hour change of EMA200 / ATR14, signed >= x
  f_ema50_dist_min   (close - EMA50) / ATR14, signed >= x
  f_adx_min          ADX(14) >= x
  f_body_max         entry bar body / ATR14, signed <= x                (no exhaustion spike bar)
  f_range_max        entry bar range / ATR14 <= x
  f_squeeze_max      ATR14 / ATR150 <= x                                (volatility not already exploded)
  f_vol_max          bar volume / 20-bar average volume <= x
  f_iv_min           implied volatility index (previous day close) >= x   (ES/YM/RTY/NKD VIX, NQ VXN, GC/SI GVZ, CL OVX)
  f_box_min          (box only) box height / ATR14 >= x
Score = number of the ENABLED conditions a bar meets.
  f_min_score   null = every enabled condition must pass; a number = at least that many must pass.
  size_up_score / size_up_mult: entries whose score >= size_up_score trade size_up_mult x the contracts (null = off).
"""
import numpy as np
import pandas as pd

CONDS = {  # name: (feature, signed, op)
    "f_day_used_min": ("day_used", False, ">="), "f_vwap_min": ("vwap", True, ">="),
    "f_ema50_slope_min": ("e50s", True, ">="), "f_ema200_slope_min": ("e200s", True, ">="),
    "f_ema50_dist_min": ("e50d", True, ">="), "f_adx_min": ("adx", False, ">="),
    "f_body_max": ("body", True, "<="), "f_range_max": ("rng", False, "<="),
    "f_squeeze_max": ("sq", False, "<="), "f_vol_max": ("vr", False, "<="), "f_iv_min": ("iv", False, ">="),
}
DEFAULTS = {k: None for k in CONDS} | {"f_box_min": None, "f_min_score": None, "size_up_score": None,
                                       "size_up_mult": 2}
KEYS = list(DEFAULTS)


def off(v):
    return v is None or (isinstance(v, float) and np.isnan(v)) or v in ("null", "off")


def _wilder(x, n):
    return pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def features(o, h, l, c, v, sess, tf, iv=None):
    """Per chart bar feature arrays. sess = session id per bar (18:00 -> 17:00 New York day)."""
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    a14, a150 = _wilder(tr, 14), _wilder(tr, 150)
    up, dn = h - np.r_[h[0], h[:-1]], np.r_[l[0], l[:-1]] - l
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi, mdi = 100 * _wilder(pdm, 14) / a14, 100 * _wilder(mdm, 14) / a14
        adx = _wilder(np.nan_to_num(100 * np.abs(pdi - mdi) / (pdi + mdi)), 14)
    e50 = pd.Series(c).ewm(span=50, adjust=False).mean().to_numpy()
    e200 = pd.Series(c).ewm(span=200, adjust=False).mean().to_numpy()
    k1, k4 = max(1, round(60 / tf)), max(1, round(240 / tf))
    sh = lambda x, k: np.r_[np.full(k, np.nan), x[:-k]]
    g = pd.DataFrame({"s": sess, "pv": (h + l + c) / 3 * v, "v": v, "h": h, "l": l})
    gs = g.groupby("s")
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = (gs.pv.cumsum() / gs.v.cumsum()).to_numpy()
    rng_so_far = gs.h.cummax().to_numpy() - gs.l.cummin().to_numpy()
    day = gs.agg(hi=("h", "max"), lo=("l", "min"))
    adr = (day.hi - day.lo).rolling(20).mean().shift(1)                       # previous 20 sessions only
    adr_bar = adr.reindex(sess).to_numpy()
    vr = v / pd.Series(v).rolling(20).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        F = {"day_used": rng_so_far / adr_bar, "vwap": (c - vwap) / a14, "e50s": (e50 - sh(e50, k1)) / a14,
             "e200s": (e200 - sh(e200, k4)) / a14, "e50d": (c - e50) / a14, "adx": adx, "body": (c - o) / a14,
             "rng": (h - l) / a14, "sq": a14 / a150, "vr": vr,
             "iv": np.full(len(c), np.nan) if iv is None else np.asarray(iv, float), "atr14": a14}
    return F


def cond_flags(F, name, thr, side):
    feat, signed, op = CONDS[name]
    x = F[feat] * side if signed else F[feat]
    with np.errstate(invalid="ignore"):
        out = (x >= thr) if op == ">=" else (x <= thr)
    return out & ~np.isnan(x)


def tables(rows, F):
    """rows: DataFrame of settings with the f_* columns. -> FL, FS (R, n) bool flags of every distinct
    (condition, threshold) and per row: bitmask of its conditions, min score, size-up score / mult, box min."""
    pairs = []
    for name in CONDS:
        if name in rows:
            pairs += [(name, float(x)) for x in rows[name] if not off(x)]
    pairs = list(dict.fromkeys(pairs))
    if len(pairs) > 62:
        raise SystemExit("too many distinct filter thresholds in one sweep (max 62)")
    n = len(F["adx"])
    FL = np.zeros((max(1, len(pairs)), n), np.bool_)
    FS = np.zeros((max(1, len(pairs)), n), np.bool_)
    for r, (name, thr) in enumerate(pairs):
        FL[r], FS[r] = cond_flags(F, name, thr, 1), cond_flags(F, name, thr, -1)
    mask = np.zeros(len(rows), np.int64)
    for name in CONDS:
        if name in rows:
            for j, x in enumerate(rows[name]):
                if not off(x):
                    mask[j] |= 1 << pairs.index((name, float(x)))
    num = lambda col: np.array([np.nan if off(x) else float(x) for x in rows[col]]) if col in rows else np.full(len(rows), np.nan)
    upm = np.array([1.0 if off(x) else float(x) for x in rows["size_up_mult"]]) if "size_up_mult" in rows else np.ones(len(rows))
    return FL, FS, [mask, num("f_min_score"), num("size_up_score"), upm, num("f_box_min")]


def score_side(F, p, side):
    """po: per bar score of the enabled conditions for one side, and how many are enabled."""
    sc = np.zeros(len(F["adx"]), np.int64)
    m = 0
    for name in CONDS:
        if not off(p.get(name)):
            sc += cond_flags(F, name, float(p[name]), side)
            m += 1
    return sc, m
