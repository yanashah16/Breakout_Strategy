"""
Pole & flag for US stocks - the rules with no I/O: bars and prices go in, a target position comes out.

A port of nq_wae_backtest/patterns/patterns_backtest.py (setup: flag, simulate kind 1) for the settings of
nq_wae_backtest/patterns/flag_stocks_10m/flag_stocks_10m_params.yaml: pole + flag pattern (flag_raw / flag_apply),
pole >= x daily ATR, 60m EMA trend (clock-hour candles), trade window, strict own short rules (pole bars / daily ATR,
60m ADX <= 20), stop order 1 tick beyond the flag, 2 legs (TP1 + runner: stop to entry at TP1, trails x R behind
the best price), one trade per pole and side, one position at a time. tests/test_stocks_flag_replay.py feeds the
backtest's own bars through this class and compares every trade with the backtest - run it after any change here.

Same feed as StockBoxCore (bot.py drives both):
    warmup(bar)            completed chart bars before the strategy trades (indicators only)
    add_day(date, h, l, c) completed regular-hours days (daily ATR for the pole size)
    on_tick(price, ...)    live prices inside a minute: entry stop / targets / stop fire at once
    on_minute(bar, ...)    a completed 1-minute bar: entry, then stops -> TP1 -> stop moves (backtest order)
    on_bar_close(bar, ...) the completed chart bar: a new flag arms the stop order for the NEXT chart bar only
Levels: the entry = the stop level (or the minute's open when it gaps through); 1 R, TP1 and the stop come from
it, as in the backtest. A different real fill changes the P&L, never the levels.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from desk.stocks.core import NAN, Atr, Bar, Ema, Event, _hhmm, _num, _off

DEFAULTS = {
    "timeframe": 10, "direction": "Both", "atr_len": 14, "trade_hours": "10:00-15:00",
    "trend_tf": 60, "trend_ema": 50, "trend_slope": 1,
    "pole_bars": 7, "pole_atr": 2.0, "flag_min_bars": 1, "flag_max_bars": 15, "max_retrace": 0.7,
    "flag_max_pct": 1.0, "flag_slope_atr": 1.0, "pole_min_datr": 0.35, "pole_min_bars": None,
    "short_pole_bars": 8, "short_pole_atr": 4.0, "short_pole_min_datr": 1.0, "short_pole_min_bars": 6,
    "short_flag_min_bars": 5, "short_flag_max_bars": 15, "short_max_retrace": 0.4, "short_flag_max_pct": 0.5,
    "short_flag_slope_atr": 5.0, "short_htf_adx_tf": 60, "short_htf_adx_len": 14, "short_htf_adx_min": 0,
    "short_htf_adx_max": 20,
    "sl_atr": 0.5, "tp_r": 3.0, "trail_r": 6.0, "be_after_tp1": True,
    "short_sl_atr": 0.15, "short_tp_r": 1.5, "short_trail_r": 2.0,
    "qty1": 1, "qty2": 2,
    "risk_trade_usd": 1000, "max_shares": 1_000_000,
    "capital_usd": None, "capital_over_pct": 5,       # set = fixed $ amount per trade instead of risk sizing (live)
}
REASON = {"tp1": "Profit target 1", "stop": "Stop loss", "tp1_be": "Stop at entry (after target 1)",
          "trail": "Trailing stop", "manual": "Closed by you", "shutdown": "Strategy stopped"}
STOP_REASON = {0: "stop", 1: "tp1_be", 2: "trail"}


class Side:
    """Pattern + filter settings of one side."""

    def __init__(self, p: dict, short: bool):
        g = (lambda k: p[f"short_{k}"] if short and not _off(p.get(f"short_{k}")) else p[k])
        self.pole_bars, self.pole_atr = int(g("pole_bars")), float(g("pole_atr"))
        self.fmin, self.fmax = int(g("flag_min_bars")), int(g("flag_max_bars"))
        self.max_ret, self.max_pct, self.slope = float(g("max_retrace")), float(g("flag_max_pct")), float(g("flag_slope_atr"))
        self.min_datr = _num(g("pole_min_datr"))
        self.min_bars = None if _off(g("pole_min_bars")) else int(g("pole_min_bars"))
        self.adx_on = short and not _off(p.get("short_htf_adx_tf"))
        self.adx_min, self.adx_max = _num(p.get("short_htf_adx_min")), _num(p.get("short_htf_adx_max"))
        self.sl, self.tp, self.trail = (float(g("sl_atr")), float(g("tp_r")), float(g("trail_r")))


class Params:
    """The copy's settings (the backtest names); nan / None = off."""

    def __init__(self, raw: dict):
        unknown = set(raw) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown setting(s): {sorted(unknown)}")
        p = {**DEFAULTS, **raw}
        self.raw = p
        self.tf = int(p["timeframe"])
        d = str(p["direction"]).lower()
        self.allow_long, self.allow_short = d in ("both", "long only"), d in ("both", "short only")
        self.atr_len = int(p["atr_len"])
        self.win = None if _off(p["trade_hours"]) else tuple(_hhmm(x) for x in str(p["trade_hours"]).split("-"))
        self.trend_tf = None if _off(p["trend_tf"]) or _off(p["trend_ema"]) else int(p["trend_tf"])
        if self.trend_tf not in (None, 60):
            raise ValueError("trend_tf: only 60 (clock-hour candles) is ported")
        self.trend_ema, self.trend_slope = int(p["trend_ema"] or 0), int(p["trend_slope"] or 0)
        if not _off(p["short_htf_adx_tf"]) and int(p["short_htf_adx_tf"]) != 60:
            raise ValueError("short_htf_adx_tf: only 60 is ported")
        self.adx_len = int(p["short_htf_adx_len"])
        self.L, self.S = Side(p, False), Side(p, True)
        self.be_tp1 = bool(p["be_after_tp1"])
        self.qty = (int(p["qty1"]), int(p["qty2"]))
        self.risk, self.max_shares = float(p["risk_trade_usd"]), int(p["max_shares"])
        self.capital, self.capital_over = _num(p["capital_usd"]), float(p["capital_over_pct"])

    def side(self, d: int) -> Side:
        return self.L if d > 0 else self.S

    def window_ok(self, t: dt.datetime) -> bool:
        """The entry bar (opening at t) may trade: inside trade_hours and not 16:45-18:00 (backtest bar_flags)."""
        m = t.hour * 60 + t.minute
        if 16 * 60 + 45 <= m < 18 * 60:
            return False
        if not self.win:
            return True
        a, b = self.win
        return (a <= m < b) if a <= b else (m >= a or m < b)


class Rma:
    """ta.rma seeded with the SMA of the first n values (backtest rma(); a nan restarts the seed)."""

    def __init__(self, n: int):
        self.n, self.run, self.s, self.value = n, 0, 0.0, NAN

    def update(self, x: float) -> float:
        if math.isnan(self.value):
            if math.isnan(x):
                self.run, self.s = 0, 0.0
                return NAN
            self.run += 1
            self.s += x
            if self.run == self.n:
                self.value = self.s / self.n
            return self.value
        self.value = (self.value * (self.n - 1) + x) / self.n      # a nan here restarts the seed (as backtest)
        if math.isnan(self.value):
            self.run, self.s = 0, 0.0
        return self.value


class Adx:
    """ADX of ta.dmi(n, n) on completed candles (backtest dmi())."""

    def __init__(self, n: int):
        self.tr, self.pd, self.md, self.ax = Rma(n), Rma(n), Rma(n), Rma(n)
        self.prev = None
        self.value = NAN

    def update(self, h: float, l: float, c: float) -> float:
        if self.prev is None:
            self.prev = (h, l, c)
            return NAN
        ph, pl, pc = self.prev
        self.prev = (h, l, c)
        up, dn = h - ph, pl - l
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        t, a, b = self.tr.update(tr), self.pd.update(pdm), self.md.update(mdm)
        if math.isnan(t) or t == 0:
            x = NAN
        else:
            p, m = 100 * a / t, 100 * b / t
            s = p + m
            x = abs(p - m) / (s if s != 0 else 1.0)
        v = self.ax.update(x)
        self.value = 100 * v if not math.isnan(v) else NAN
        return self.value


class Hourly:
    """Clock-hour candles built from the chart bars (9:30-10:00, 10-11 ... 15-16 New York; backtest htf_bars with
    htf_anchor null on regular-hours data): trend +1 / -1 / 0 and ADX of the last COMPLETED candle."""

    def __init__(self, ema_len: int, slope: int, adx_len: int):
        self.ema = Ema(ema_len) if ema_len else None
        self.slope = slope
        self.hist = deque(maxlen=slope + 1)
        self.n = 0
        self.adx = Adx(adx_len)
        self.cur = None                # [bucket, h, l, c]
        self.trend = 0
        self.adx_v = NAN
        self.ema_len = ema_len

    def add(self, bar: Bar, tf: int):
        b = bar.t.replace(minute=0, second=0, microsecond=0)
        if self.cur is not None and self.cur[0] != b:
            self._close()
        if self.cur is None:
            self.cur = [b, bar.h, bar.l, bar.c]
        else:
            self.cur[1], self.cur[2], self.cur[3] = max(self.cur[1], bar.h), min(self.cur[2], bar.l), bar.c
        if bar.t + dt.timedelta(minutes=tf) >= b + dt.timedelta(hours=1):
            self._close()

    def _close(self):
        _, h, l, c = self.cur
        self.cur = None
        self.n += 1
        self.adx_v = self.adx.update(h, l, c)
        if not self.ema:
            self.trend = 2
            return
        e = self.ema.update(c)
        self.hist.append(e)
        tr = 0
        if self.n > self.slope + self.ema_len and not math.isnan(e):
            up, dn = c > e, c < e
            if self.slope > 0:
                ep = self.hist[0]
                up, dn = up and e > ep, dn and e < ep
            tr = 1 if up else -1 if dn else 0
        self.trend = tr


@dataclass
class Position:
    side: int = 0
    entry: float = 0.0             # rule entry (stop level, or the minute open when it gapped through)
    entry_time: str = ""
    signal_bar: str = ""
    unit: float = 0.0              # 1 R in $ per share
    stop0: float = 0.0             # initial stop
    stops: list = field(default_factory=lambda: [0.0, 0.0])    # leg 1 / runner stop
    stop_kind: list = field(default_factory=lambda: [0, 0])    # 0 stop, 1 at entry (after TP1), 2 trail
    tps: list = field(default_factory=lambda: [0.0, NAN])      # TP1 (no final target)
    stage: int = 0                 # 0 before TP1, 1 TP1 reached
    best: float = 0.0
    trail_r: float = 0.0
    legs: list = field(default_factory=lambda: [0, 0])
    sizes: list = field(default_factory=lambda: [0, 0])
    entry_minute: str = ""         # minute of the entry (only the stop can hit in it)
    is_rev: bool = False
    be_on: bool = False
    bars_since: int = 0
    boost: str = ""

    @property
    def stop(self) -> float:
        """The stop shown on the page: the runner's once leg 1 is gone."""
        return self.stops[1] if (self.legs[1] and not self.legs[0]) or self.stage else self.stops[0]

    def open_qty(self) -> int:
        return sum(self.legs)


class StockFlagCore:
    def __init__(self, params: Params, tick: float = 0.01):
        p = self.p = params
        self.tk = tick
        self.atr = Atr(p.atr_len)
        self.hourly = Hourly(p.trend_ema if p.trend_tf else 0, p.trend_slope, p.adx_len)
        n = max(p.L.fmax + p.L.pole_bars, p.S.fmax + p.S.pole_bars) + 3
        self.H, self.Lo, self.C, self.A, self.T = (deque(maxlen=n) for _ in range(5))
        self.days: List[dt.date] = []
        self.tr_days: deque = deque(maxlen=14)
        self.prev_dc = NAN
        self.datr: dict = {}           # date -> daily ATR(14) of the 14 days BEFORE it
        self.nbars = 0
        self.last_anchor = {1: "", -1: ""}     # pole-top bar time of the last trade per side (one trade per pole)
        self.pend: dict = {}           # side -> armed stop order for the next chart bar
        self.pend_bar: Optional[str] = None
        self.pos = Position()
        self.closed: list = []
        self.trades: list = []         # one row per trade (replay)
        self.last: dict = {}

    # ------------------------------------------------------------------ daily ATR (backtest daily_atr)
    def add_day(self, d: dt.date, h: float, l: float, c: float):
        if self.days and d <= self.days[-1]:
            return
        # backtest: TR = max(h - l, |h - prev c|, |l - prev c|), nan on the first day (no previous close);
        # rolling(14).mean() = nan while any of the 14 days is nan
        tr = NAN if math.isnan(self.prev_dc) else max(h - l, abs(h - self.prev_dc), abs(l - self.prev_dc))
        self.prev_dc = c
        self.days.append(d)
        self.tr_days.append(tr)
        vals = list(self.tr_days)
        self._datr_next = sum(vals) / 14 if len(vals) == 14 and not any(math.isnan(x) for x in vals) else NAN

    def datr_for(self, d: dt.date) -> float:
        """Daily ATR known on day d = the 14 completed days before it."""
        if not self.days or self.days[-1] >= d:
            # the day itself was already added (warm-up with daily bars of today): not used before its end
            return self.datr.get(d, NAN)
        return getattr(self, "_datr_next", NAN)

    # ------------------------------------------------------------------ helpers
    def _rt(self, x: float) -> float:
        return round(x / self.tk) * self.tk

    def _book(self, k: int, px: float, why: str, now: str):
        ps = self.pos
        self.closed.append({"entry_time": ps.entry_time, "exit_time": now, "side": ps.side, "leg": k + 1,
                            "entry_price": ps.entry, "exit_price": px, "shares": ps.legs[k], "exit": why,
                            "unit": ps.unit, "setup": "Flag"})
        ps.legs[k] = 0

    def _exit(self, k_list, px, why, now, out):
        ps = self.pos
        legs = tuple(k for k in k_list if ps.legs[k])
        if not legs:
            return
        qty = sum(ps.legs[k] for k in legs)
        for k in legs:
            self._book(k, px, why, now)
        out.append(Event("exit", ps.side, qty, px, legs, why))

    def _done(self):
        if self.pos.side and self.pos.open_qty() == 0:
            self.pos = Position()

    # ------------------------------------------------------------------ entry (stop order armed at the last bar close)
    def _try_entry(self, o: float, h: float, l: float, minute: dt.datetime, now: str) -> List[Event]:
        if self.pos.side or not self.pend or self.pend_bar is None:
            return []
        b0 = dt.datetime.fromisoformat(self.pend_bar)
        if minute < b0:
            return []
        if minute >= b0 + dt.timedelta(minutes=self.p.tf):     # that candle never printed (half day, halt): expired
            self.pend, self.pend_bar = {}, None
            return []
        hit = None
        for d in (1, -1):                         # long first (backtest: the side whose level the minute hits first)
            q = self.pend.get(d)
            if q and ((d > 0 and h >= q["lv"]) or (d < 0 and l <= q["lv"])):
                hit = d
                break
        if hit is None:
            return []
        q = self.pend.pop(hit)
        self.pend.clear()
        d = hit
        f = max(q["lv"], o) if d > 0 else min(q["lv"], o)
        st0 = q["st0"]
        risk = (f - st0) * d
        if risk <= 0:
            return []
        sp = self.p.side(d)
        sizes = self._size(f, risk)
        if isinstance(sizes, str):
            self.last = {"state": sizes}
            return []
        ps = Position(side=d, entry=f, entry_time=now, signal_bar=q["bar"], unit=risk, stop0=st0, stops=[st0, st0],
                      tps=[f + d * sp.tp * risk, NAN], best=f, trail_r=sp.trail, legs=list(sizes), sizes=list(sizes),
                      entry_minute=minute.isoformat())
        self.pos = ps
        self.last_anchor[d] = q["anc"]
        self.trades.append({"entry_time": now, "signal_bar": q["bar"], "side": d, "entry": f, "stop": st0,
                            "risk": risk, "anchor": q["anc"]})
        out = [Event("entry", d, ps.open_qty(), f)]
        if (d > 0 and l <= st0) or (d < 0 and h >= st0):          # fill minute: only the stop can still hit
            self._exit((0, 1), st0, "stop", now, out)
            self._done()
        return out

    def _size(self, px: float, risk: float):
        q1, q2 = self.p.qty
        sumq = q1 + q2
        if not math.isnan(self.p.capital):
            per = px * sumq
            down, up = int(math.floor(self.p.capital / per + 1e-9)), int(math.ceil(self.p.capital / per - 1e-9))
            mult = up if up * per <= self.p.capital * (1 + self.p.capital_over / 100.0) + 1e-9 else down
            if mult < 1:
                return f"amount too small (one share per unit costs ${per:,.0f})"
            sizes = [q1 * mult, q2 * mult]
        else:
            tot = int(math.floor(self.p.risk / risk + 1e-9))
            if tot < 1:
                return "size 0"
            a = int(round(tot * q1 / sumq)) if q1 else 0
            sizes = [a, tot - a] if q2 else [tot, 0]
        if sum(sizes) > self.p.max_shares:
            f = self.p.max_shares / sum(sizes)
            sizes = [int(math.floor(x * f)) for x in sizes]
        return sizes if sum(sizes) > 0 else "size 0"

    # ------------------------------------------------------------------ exits inside a minute (backtest manage)
    def _manage(self, o: float, h: float, l: float, now: str, moves: bool) -> List[Event]:
        ps, out = self.pos, []
        d = ps.side
        for k in (0, 1):                                          # stops first
            if ps.legs[k] and ((d > 0 and l <= ps.stops[k]) or (d < 0 and h >= ps.stops[k])):
                px = min(ps.stops[k], o) if d > 0 else max(ps.stops[k], o)
                self._exit((k,), px, STOP_REASON[ps.stop_kind[k]], now, out)
        if ps.stage == 0 and ((d > 0 and h >= ps.tps[0]) or (d < 0 and l <= ps.tps[0])):
            if ps.legs[0]:
                px = max(ps.tps[0], o) if d > 0 else min(ps.tps[0], o)
                self._exit((0,), px, "tp1", now, out)
            ps.stage = 1
            if self.p.be_tp1 and ps.legs[1] and d * (ps.entry - ps.stops[1]) > 0:
                ps.stops[1], ps.stop_kind[1] = ps.entry, 1
        if ps.open_qty() == 0:
            self._done()
            return out
        if moves:
            fav = h if d > 0 else l
            if d * (fav - ps.best) > 0:
                ps.best = fav
            if ps.legs[1] and ps.stage:
                ts = ps.best - d * ps.trail_r * ps.unit
                if d * (ts - ps.stops[1]) > 0:
                    ps.stops[1], ps.stop_kind[1] = ts, 2
        return out

    # ------------------------------------------------------------------ public feed
    def on_tick(self, price: float, minute: Optional[dt.datetime], prev: Optional[float], now: str = "") -> List[Event]:
        """A live price; prev -> price is handled as a tiny bar (a level crossed on the way fills AT the level).
        Stop moves wait for the minute's close (backtest order)."""
        if price is None or not price > 0 or minute is None:
            return []
        o = prev if prev else price
        h, l = max(o, price), min(o, price)
        if self.pos.side == 0:
            return self._try_entry(o, h, l, minute, now)
        if self.pos.entry_minute == minute.isoformat():           # fill minute: the stop only
            ps, out = self.pos, []
            if (ps.side > 0 and l <= ps.stop0) or (ps.side < 0 and h >= ps.stop0):
                self._exit((0, 1), ps.stop0, "stop", now, out)
                self._done()
            return out
        return self._manage(o, h, l, now, moves=False)

    def on_minute(self, m: Bar, now: str = "") -> List[Event]:
        out: List[Event] = []
        if self.pos.side == 0:
            out = self._try_entry(m.o, m.h, m.l, m.t, now)
            return out
        if self.pos.entry_minute == m.t.isoformat():
            ps = self.pos
            if (ps.side > 0 and m.l <= ps.stop0) or (ps.side < 0 and m.h >= ps.stop0):
                self._exit((0, 1), ps.stop0, "stop", now, out)
                self._done()
            return out
        return self._manage(m.o, m.h, m.l, now, moves=True)

    # ------------------------------------------------------------------ chart bars
    def _push(self, bar: Bar):
        a = self.atr.update(bar.h, bar.l, bar.c)
        self.H.append(bar.h)
        self.Lo.append(bar.l)
        self.C.append(bar.c)
        self.A.append(a)
        self.T.append(bar.t)
        self.hourly.add(bar, self.p.tf)
        self.nbars += 1
        d = bar.t.date()
        if d not in self.datr:
            self.datr[d] = self.datr_for(d)
            if len(self.datr) > 10:
                for k in sorted(self.datr)[:-5]:
                    del self.datr[k]

    def warmup(self, bar: Bar):
        self._push(bar)
        self.pend, self.pend_bar = {}, None

    def _flag(self, d: int):
        """flag_raw + flag_apply at the newest bar for side d -> (level, raw stop, anchor, pole, pole bars) or why not."""
        sp = self.p.side(d)
        n = len(self.C)
        need = sp.fmax + sp.pole_bars + 2
        if n < need:
            return "warming up"
        i = n - 1
        hh = self.H if d > 0 else [-x for x in self.Lo]
        ll = self.Lo if d > 0 else [-x for x in self.H]
        cc = self.C if d > 0 else [-x for x in self.C]
        a = self.A[i]
        if math.isnan(a) or a <= 0:
            return "warming up"
        p = i - sp.fmax
        for k in range(i - sp.fmax, i + 1):
            if hh[k] >= hh[p]:
                p = k
        if p >= i:
            return "no flag (new high)" if d > 0 else "no flag (new low)"
        if i - p < sp.fmin:
            return "flag too short"
        b = p - sp.pole_bars
        lo = ll[b]
        for k in range(p - sp.pole_bars, p + 1):
            if ll[k] < lo:
                lo, b = ll[k], k
        top = hh[p]
        pole = top - lo
        if not pole >= sp.pole_atr * self.A[p]:
            return "pole too small"
        fh, fl = hh[p + 1], ll[p + 1]
        for k in range(p + 1, i + 1):
            fh, fl = max(fh, hh[k]), min(fl, ll[k])
        if top - fl > sp.max_ret * pole or fh - fl > sp.max_pct * pole or fh >= top:
            return "flag too deep / too wide"
        if cc[i] > cc[p + 1] + sp.slope * a:
            return "flag drifting"
        lv = fh + self.tk
        stp = fl
        if d < 0:
            lv, stp = -lv, -stp
        anchor = self.T[p].isoformat()
        return lv, stp, anchor, pole, p - b

    def on_bar_close(self, bar: Bar, now: str = "", allow_entry: bool = True) -> List[Event]:
        """The completed chart bar (bar.t = its open): arm the flag stop order(s) for the next chart bar."""
        self._push(bar)
        self.pend, self.pend_bar = {}, None
        if self.pos.side:
            self.pos.bars_since += 1
            self.last = {"state": "in a trade", "at": bar.t.strftime("%H:%M")}
            return []
        view = {"at": bar.t.strftime("%H:%M"), "close": round(bar.c, 4)}
        if not allow_entry:
            self.last = {"state": "switched off", **view}
            return []
        nxt = bar.t + dt.timedelta(minutes=self.p.tf)
        if not self.p.window_ok(nxt):
            self.last = {"state": "outside the trade window (entries 10:00-15:00)", **view}
            return []
        whys = []
        for d in (1, -1):
            if (d > 0 and not self.p.allow_long) or (d < 0 and not self.p.allow_short):
                continue
            r = self._flag(d)
            if isinstance(r, str):
                whys.append(r)
                continue
            lv, stp, anc, pole, plen = r
            sp = self.p.side(d)
            if sp.min_bars and plen < sp.min_bars:
                whys.append("pole built too fast")
                continue
            if not math.isnan(sp.min_datr):
                da = self.datr.get(bar.t.date(), NAN)
                if not (pole / da >= sp.min_datr if da and not math.isnan(da) else False):
                    whys.append("pole small vs the daily range" if not math.isnan(da) else "no daily data yet")
                    continue
            if sp.adx_on:
                ax = self.hourly.adx_v
                if math.isnan(ax) or not ax >= (0 if math.isnan(sp.adx_min) else sp.adx_min) or \
                        (not math.isnan(sp.adx_max) and sp.adx_max and ax > sp.adx_max):
                    whys.append("60m trend already strong (short rule)")
                    continue
            tr = self.hourly.trend
            if not (tr == 2 or tr == d):
                whys.append("against the 60m trend")
                continue
            if anc == self.last_anchor[d]:
                whys.append("this pole was already traded")
                continue
            st0 = stp - d * sp.sl * self.A[-1]
            if not d * (lv - st0) > 0:
                continue
            self.pend[d] = {"lv": lv, "st0": st0, "anc": anc, "bar": bar.t.isoformat()}
        if self.pend:
            self.pend_bar = nxt.isoformat()
            L, S = self.pend.get(1), self.pend.get(-1)
            self.last = {"state": "flag ready - stop order for the next candle", **view,
                         "long_above": round(L["lv"], 4) if L else None, "short_below": round(S["lv"], 4) if S else None}
        else:
            self.last = {"state": (whys[0] if whys else "no flag"), **view}
        return []

    # ------------------------------------------------------------------ persistence / manual
    def target(self) -> int:
        return self.pos.side * self.pos.open_qty()

    def state(self) -> dict:
        return {**asdict(self.pos), "_last_anchor": dict(self.last_anchor), "_nbars": self.nbars}

    def restore(self, d: Optional[dict]):
        d = dict(d or {})
        la = d.pop("_last_anchor", None)
        d.pop("_nbars", None)
        self.pos = Position(**d) if d.get("side") else Position()
        if la:
            self.last_anchor = {int(k): v for k, v in la.items()}

    def close_all(self, why: str, px: float, now: str = "") -> List[Event]:
        if self.pos.side == 0:
            return []
        out: List[Event] = []
        self._exit((0, 1), px, why, now, out)
        self.pos = Position()
        return out

    def scale_entry(self, filled: int):
        """Only part of the entry filled: keep what filled, leg 1 first."""
        ps = self.pos
        left = max(0, int(filled))
        new = []
        for k in range(2):
            q = min(ps.legs[k], left)
            new.append(q)
            left -= q
        ps.legs = new
        ps.sizes = list(new)
        if sum(new) == 0:
            self.pos = Position()

    def story(self):
        """(what is happening now, what happens next) for the page."""
        ps = self.pos
        if not ps.side:
            return "", ""
        if ps.stage == 0:
            return ("waiting for target 1",
                    f"Target 1 at {ps.tps[0]:.2f} closes {ps.legs[0]} shares; stop {ps.stops[0]:.2f} closes all")
        k = ps.stop_kind[1]
        now = "target 1 done - runner stop at the entry" if k == 1 else "trailing the runner"
        return now, f"Runner ({ps.legs[1]}) stop at {ps.stops[1]:.2f} (trails {ps.trail_r:g} R behind the best price)"
