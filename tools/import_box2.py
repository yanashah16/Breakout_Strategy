"""
Box Breakout cards (engine: box) from the box2 best versions in nq_wae_backtest/box2/versions/.

    python tools/import_box2.py

Writes strategies/box/<id>.yaml with the frozen settings; the header keeps the backtest numbers.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT.parent / "nq_wae_backtest" / "box2" / "versions"
OUT = ROOT / "strategies" / "box"
# (file, id, family, short name, markets) - box2 versions with backtest net/DD above 15 (2026-11-24)
CARDS = [
    ("box_NKD.yaml", "nikkei-box-breakout", "Nikkei 225", "Nikkei", ["NKD"]),
    ("box_SI.yaml", "silver-box-breakout", "Silver", "Silver", ["SIL", "SI"]),
    ("box_YM.yaml", "dow-box-breakout", "Dow", "Dow", ["MYM", "YM"]),
    ("box_RTY.yaml", "russell-box-breakout", "Russell 2000", "Russell", ["M2K", "RTY"]),
]


def words(p):
    enter = (f"Box = the last {p['box_bars']} candles ({p['timeframe']}-minute, {p['box_edges']}s), at most "
             f"{p['max_box_atr']} x ATR({p['box_atr_len']}) tall. A fresh close {p['break_buffer']} ATR beyond the box "
             f"enters at that close ({p['direction'].lower()})")
    enter += f", with EMA({p['ema_len']}) in the trade's direction." if p.get("ema_len") else "."
    f = [k for k in p if k.startswith("f_") and k not in ("f_min_score",) and p[k] is not None]
    if f:
        enter += (f" Entry-condition score: at least {p['f_min_score']} of {len(f)} conditions"
                  f" ({', '.join(x[2:] for x in f)}).")
    if p.get("size_up_score") is not None:
        enter += f" Score >= {p['size_up_score']}: x{p['size_up_mult']} size."
    if p.get("iv_min") is not None or p.get("iv_max") is not None:
        enter += f" IV index between {p.get('iv_min') or 0} and {p.get('iv_max') or 'any'}."
    if p.get("skip_hours"):
        enter += f" No entries {p['skip_hours']} New York."
    ex = (f"Stop {p['sl_box_pct']} % of the box height. Leg 1 takes profit at {p['tp1_mult']} R, then the stop goes "
          f"to the entry. ")
    ex += (f"Leg 2 at {p['tp2_mult']} R. " if p.get("tp2_mult") is not None else
           f"Leg 2 leaves on a close beyond EMA({p['leg2_ema_len']}). ")
    ex += f"Runner trails (gives back {p['trail_pct']} % of the best profit), final target {p['tp3_mult']} R."
    if p.get("reverse"):
        ex += f" Original stop hit within {p['reverse_bars']} candles: reverses once."
    if p.get("max_hold_hours"):
        ex += f" Closed after {p['max_hold_hours']} market hours."
    return enter, ex


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for fn, cid, fam, short, markets in CARDS:
        path = SRC / fn
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        p = dict(cfg["strategy"])
        inst = cfg["data"]["instrument"]
        head = [ln.rstrip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("#")]
        enter, ex = words(p)
        card = {"id": cid, "name": f"{short} Box Breakout", "family": fam, "signal": inst, "markets": markets,
                "pattern": "Box breakout, 3 targets",
                "summary": f"Breakout of a sideways box on {p['timeframe']}-minute candles, 3 legs, long and short.",
                "how_it_enters": enter, "how_it_exits": ex, "experimental": False,
                "default_qty": [int(p["qty1"]), int(p["qty2"]), int(p["qty3"])], "engine": "box",
                "source": f"nq_wae_backtest/box2/versions/{fn}", "data": {}, "logic": p}
        text = "\n".join([f"# {card['name']}  -  from {card['source']} (backtest: research/box2_backtest.py)",
                          "# Research notes:"] + head) + "\n"
        text += yaml.safe_dump(card, sort_keys=False, allow_unicode=True, width=120)
        (OUT / f"{cid}.yaml").write_text(text, encoding="utf-8")
        print(f"{cid:24s} {inst} qty {card['default_qty']}")


if __name__ == "__main__":
    main()
