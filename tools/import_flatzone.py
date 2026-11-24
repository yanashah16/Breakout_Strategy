"""
Flat Zone Breakout cards (engine: flatzone) from the optimised yamls in nq_wae_backtest/flatzone/.

    python tools/import_flatzone.py

Writes strategies/flatzone/<id>.yaml. The logic block = the yaml's strategy: settings (frozen); the header keeps
the backtest numbers. Re-run after a new optimisation on purpose - live trading never follows research edits.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FZ = ROOT.parent / "nq_wae_backtest" / "flatzone"
OUT = ROOT / "strategies" / "flatzone"

FAMILY = {"NQ": ("Nasdaq 100", "Nasdaq", ["MNQ", "NQ"]), "ES": ("S&P 500", "S&P", ["MES", "ES"]),
          "YM": ("Dow", "Dow", ["MYM", "YM"])}
# (yaml, id, label)  - every flat zone version with backtest net/DD above 15 (2026-11-24)
CARDS = [
    ("flatzone_NQ_15m_hi.yaml", "nq-flatzone-15m", "15m"),
    ("flatzone_NQ_15m_lo.yaml", "nq-flatzone-15m-select", "15m Select"),
    ("flatzone_NQ_10m_hi.yaml", "nq-flatzone-10m", "10m"),
    ("flatzone_NQ_10m_lo.yaml", "nq-flatzone-10m-select", "10m Select"),
    ("flatzone_NQ_3m_hi.yaml", "nq-flatzone-3m", "3m"),
    ("flatzone_NQ_3m_lo.yaml", "nq-flatzone-3m-select", "3m Select"),
    ("flatzone_YM_15m_both.yaml", "dow-flatzone-15m", "15m"),
    ("flatzone_YM_15m_both2k.yaml", "dow-flatzone-15m-active", "15m Active"),
    ("flatzone_YM_15m.yaml", "dow-flatzone-15m-long", "15m Long"),
    ("flatzone_ES_15m.yaml", "sp-flatzone-15m-long", "15m Long"),
    ("flatzone_ES_15m_long.yaml", "sp-flatzone-15m-long-active", "15m Long Active"),
]


def words(p: dict) -> tuple:
    d = {"Both": "long or short", "Long only": "long only", "Short only": "short only"}[p["direction"]]
    src = "closes" if p["box_src"] == "close" else "candle bodies"
    enter = (f"Finds a flat zone: {p['min_bars']}+ candles ({p['timeframe']}-minute) whose {src} stay within "
             f"{p['max_h_atr']} ATR and drift at most {p['max_slope']} % of the zone height. A candle closing "
             f"{p['break_pct']} % of the height beyond the zone ({'with a body that size' if p['break_mode'] == 'body' else 'by close'}) "
             f"enters at that close, {d}.")
    if p.get("vol_min") is not None:
        enter += f" Only if the candle's volume is >= {p['vol_min']} x the same time of day's average ({p['vol_days']} days)."
    if p.get("adx_min") is not None:
        enter += f" Only when ADX({p['adx_len']}) >= {p['adx_min']}."
    if p.get("skip_hours"):
        enter += f" No entries {p['skip_hours']} New York."
    if p.get("vol_boost") is not None:
        enter += f" Volume >= {p['vol_boost']} x average: x{p['vol_mult']} quantity."
    sl = {"atr": f"{p['sl_atr']} ATR from the breakout close", "box": "the other side of the zone",
          "mid": "the middle of the zone"}[p["sl_mode"]]
    ex = f"Stop at {sl}. Target contracts take profit {p['t1_atr']} ATR from the breakout close"
    ex += "; the stop of the rest then moves to the entry (from the next candle)." if p["be_after_t1"] else "."
    if p.get("trail_atr"):
        ex += f" Runner contracts trail {p['trail_atr']} ATR behind the best price"
        ex += f", from {p['trail_act']} ATR in profit." if p.get("trail_act") else "."
    if p.get("t2_atr"):
        ex += f" Runner cap {p['t2_atr']} ATR."
    if p.get("be_atr"):
        ex += f" At {p['be_atr']} ATR profit every stop locks entry + {p['be_lock']} ATR."
    if p.get("rev_bars"):
        ex += f" If the first stop is hit within {p['rev_bars']} candles it reverses once (same size, same stop distance)."
    if p.get("max_hold_days"):
        ex += f" Closed after {p['max_hold_days']} days."
    return enter, ex


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for fn, cid, label in CARDS:
        path = FZ / fn
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        p = dict(cfg["strategy"])
        inst = cfg["data"]["instrument"]
        fam, short, markets = FAMILY[inst]
        head = [ln.rstrip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
        enter, ex = words(p)
        card = {
            "id": cid, "name": f"{short} Flat Zone {label}", "family": fam, "signal": inst, "markets": markets,
            "pattern": "Flat zone breakout",
            "summary": f"Breakout of a tight sideways zone on {p['timeframe']}-minute candles, { {"Both": "long and short", "Long only": "long only", "Short only": "short only"}[p["direction"]] }.",
            "how_it_enters": enter, "how_it_exits": ex, "experimental": False,
            "default_qty": [int(p["qty_t1"]), int(p["qty_t2"])], "engine": "flatzone",
            "source": f"nq_wae_backtest/flatzone/{fn}", "data": {}, "logic": p,
        }
        text = "\n".join([f"# {card['name']}  -  from {card['source']} (backtest: research/flatzone_backtest.py)",
                          "# Research notes:"] + head) + "\n"
        text += yaml.safe_dump(card, sort_keys=False, allow_unicode=True, width=120)
        (OUT / f"{cid}.yaml").write_text(text, encoding="utf-8")
        print(f"{cid:30s} {inst} {p['timeframe']}m {p['direction']:10s} qty {card['default_qty']}")


if __name__ == "__main__":
    main()
