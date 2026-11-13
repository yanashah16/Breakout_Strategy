"""
Build the strategy catalog (strategies/<pattern>/<id>.yaml) from the research yamls.

    python tools/import_strategies.py            # writes / refreshes every card below
    python tools/import_strategies.py --check    # only shows what would change

Each card is self-contained: the research settings are FROZEN into it (base yaml merged),
so a later edit in the research folder never changes what trades live. To pick up a
research change on purpose, run this tool again.

Names describe behaviour, not version numbers. The original version is kept in
`source:` so every card can be traced back to its backtest.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESEARCH = ROOT.parent / "nq_wae_backtest" / "patterns"
OUT = ROOT / "strategies"

FAMILY = {  # signal market -> (family shown in the UI, tradable markets)
    "ES": ("S&P 500", ["MES", "ES"]),
    "NQ": ("Nasdaq 100", ["MNQ", "NQ"]),
    "GC": ("Gold", ["MGC", "GC"]),
    "CL": ("Crude Oil", ["MCL", "CL"]),
}

# ----------------------------------------------------------------------------- pattern texts
PATTERN_ABOUT = {
    "flag": ("Flag breakout",
             "Waits for a fast, strong move (the pole) followed by a short, quiet pause (the flag). "
             "When price breaks out of the pause in the direction of the move, it enters. "
             "The stop goes just beyond the other side of the pause."),
    "double": ("Double top / bottom",
               "Looks for price testing the same level twice and failing (a W shape at the bottom or an "
               "M shape at the top). When price breaks the middle of the pattern, it enters in the new "
               "direction. The stop goes just beyond the two tests."),
    "pullback": ("Trend pullback",
                 "In a clear trend it waits for price to dip back to its moving average, then enters "
                 "when price turns back in the trend direction. The stop goes below the dip."),
    "trendline": ("Trendline break",
                  "Draws the line connecting the last two swing points of a pullback. When price "
                  "closes through that line, it enters in the main trend direction."),
}


def exit_text(s: dict) -> str:
    q1, q2 = int(s.get("qty1") or 0), int(s.get("qty2") or 0)
    trail = bool(s.get("trail"))
    if q1 and q2:
        t = "Takes part of the profit at the first target and lets the rest run"
        t += " with a trailing stop." if trail else "."
        if s.get("be_after_tp1"):
            t += " After the first target the stop moves to break-even, so the rest cannot lose."
        return t
    if q2:
        return ("Holds the whole position as one runner with a trailing stop that follows the price, "
                "aiming to catch the big moves.") if trail else "Holds the whole position to one target."
    return "Holds the whole position to a single profit target."


# ----------------------------------------------------------------------------- the catalog
# id, source yaml (relative to the research patterns folder), name, one-line character
CARDS = [
    # ---- S&P 500 flags
    ("sp-spike-flag", "best_versions/ES_v1.yaml", "S&P Spike Flag",
     "Trades flags after a sudden 2-3 candle spike. Takes profit early, so it wins often."),
    ("sp-thrust-flag", "best_versions/ES_v2.yaml", "S&P Thrust Flag",
     "Needs a powerful thrust before the flag. Half the position banks a target, half runs far."),
    ("sp-long-flag-runner", "best_versions/ES_v3.yaml", "S&P Long Flag Runner",
     "Accepts slower moves and longer pauses, then rides the breakout as one runner."),
    ("sp-strong-trend-flag", "best_versions/ES_v4.yaml", "S&P Strong-Trend Flag",
     "Only trades when the trend is very strong. Few trades, but each one aims big."),
    ("sp-quick-bank-flag", "best_versions/ES_v5.yaml", "S&P Quick-Bank Flag",
     "Short, sharp moves; banks most of the position at the first target."),
    ("sp-big-move-flag", "best_versions/ES_v6.yaml", "S&P Big-Move Flag",
     "Waits for an unusually large move and a tight pause, in line with the 4-hour trend."),
    # ---- Nasdaq flags
    ("nq-active-flag", "best_versions/NQ_v1.yaml", "Nasdaq Active Flag",
     "The busiest Nasdaq flag: quick moves in a strong trend, managed as one runner."),
    ("nq-thrust-runner", "best_versions/NQ_v2.yaml", "Nasdaq Thrust Runner",
     "Strong thrust, tight stop, then a runner with a close trailing stop."),
    ("nq-big-move-flag", "best_versions/NQ_v3.yaml", "Nasdaq Big-Move Flag",
     "Only trades large moves compared to a normal day, in line with the 4-hour trend."),
    ("nq-steady-flag", "best_versions/NQ_v4.yaml", "Nasdaq Steady Flag",
     "Moderate targets and an early break-even stop: smoother, more frequent wins."),
    ("nq-classic-flag", "best_versions/NQ_v5.yaml", "Nasdaq Classic Flag",
     "The original Nasdaq flag. Half the position banks a target, half trails."),
    ("nq-young-trend-flag", "best_versions/NQ_v6.yaml", "Nasdaq Young-Trend Flag",
     "Prefers trends that are just starting, before they get crowded."),
    ("nq-swift-flag", "best_versions/extra_NQ_a.yaml", "Nasdaq Swift Flag",
     "Fast moves in a strong trend with a nearby break-even stop. Similar to the Active Flag."),
    ("nq-bank-first-flag", "best_versions/extra_NQ_b.yaml", "Nasdaq Bank-First Flag",
     "Very large moves only; banks most of the position at the first target."),
    ("nq-selective-flag", "best_versions/extra_NQ_c.yaml", "Nasdaq Selective Flag",
     "The strictest Nasdaq filters: few trades, aligned with the 4-hour trend."),
    ("nq-finder-flag", "best_versions/extra_NQ_finder.yaml", "Nasdaq Finder Flag",
     "A broad flag finder with balanced targets."),
    # ---- Gold flags
    ("gold-tight-flag", "best_versions/GC_v1.yaml", "Gold Tight Flag",
     "Quick moves with very short pauses and a tight stop."),
    ("gold-deep-pause-flag", "best_versions/GC_v2.yaml", "Gold Deep-Pause Flag",
     "Allows deeper and longer pauses before the breakout."),
    ("gold-active-flag", "best_versions/GC_v3.yaml", "Gold Active Flag",
     "The busiest gold flag, filtered to moves that are large for the day."),
    ("gold-trend-flag", "best_versions/GC_v4.yaml", "Gold Trend-Aligned Flag",
     "Slower moves that agree with the 4-hour trend, managed as one runner."),
    ("gold-big-pole-runner", "best_versions/GC_v5.yaml", "Gold Big-Pole Runner",
     "Strong trend and a big move first, then one runner with a wide trailing stop."),
    ("gold-high-win-flag", "best_versions/GC_v6.yaml", "Gold High-Win Flag",
     "Banks part early and protects the rest at break-even: wins about half of its trades."),
    ("gold-mature-flag", "best_versions/GC_v7.yaml", "Gold Mature Flag",
     "Waits for a longer pause (8-12 candles) before trading the breakout."),
    ("gold-strong-trend-flag", "best_versions/GC_v8.yaml", "Gold Strong-Trend Flag",
     "Very short, sharp moves, but only in a very strong trend."),
    # ---- Crude flags
    ("crude-early-trend-flag", "best_versions/CL_v1.yaml", "Crude Early-Trend Flag",
     "Trades flags while the bigger trend is still young."),
    ("crude-big-thrust-runner", "best_versions/CL_v2.yaml", "Crude Big-Thrust Runner",
     "Needs a very large move in a strong trend, then rides one runner."),
    # ---- double top / bottom
    ("sp-double-measured", "double_top/double_ES_mm.yaml", "S&P Double Top/Bottom",
     "Targets the pattern's measured move. Trades bigger when volume is strong."),
    ("nq-double-measured", "double_top/double_NQ_mm.yaml", "Nasdaq Double Top/Bottom",
     "Targets the pattern's measured move. Trades bigger when volume is strong."),
    ("gold-double-measured", "double_top/double_GC_mm.yaml", "Gold Double Top/Bottom",
     "Targets the pattern's measured move. Trades bigger when volume is strong."),
    ("crude-double-measured", "double_top/double_CL_mm.yaml", "Crude Double Top/Bottom",
     "Targets the pattern's measured move. Trades bigger when volume is strong."),
    ("sp-double-classic", "double_top/double_ES.yaml", "S&P Wide Double Top/Bottom",
     "The earlier version: wider patterns, only in calm (non-trending) markets, one trade a day."),
    ("gold-double-classic", "double_top/double_GC.yaml", "Gold Wide Double Top/Bottom",
     "The earlier version: wide, clean patterns with a quick trailing stop."),
    # ---- pullback / trendline
    ("nq-trend-pullback", "pullback_NQ_params.yaml", "Nasdaq Trend Pullback",
     "Buys dips (sells rallies) to the moving average in an established trend."),
]
# (id, source, name, character, signal market) - not tuned for one market: experimental on all four
GENERIC = [
    ("trendline-break", "trendline_params.yaml", "Trendline Break",
     "Enters at the close that breaks a pullback's trendline. Not yet tested on live data."),
]
EXPERIMENTAL = {"sp-double-classic", "gold-double-classic", "nq-trend-pullback", "trendline-break"}


def load_merged(path: Path) -> dict:
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = d.pop("base", None)
    if base:
        b = load_merged((path.parent / base).resolve())
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                b[k] = {**b[k], **v}
            else:
                b[k] = v
        d = b
    return d


def card(cid, src, name, character, market=None) -> dict:
    d = load_merged(RESEARCH / src)
    s = dict(d.get("strategy") or {})
    lists = [k for k, v in s.items() if isinstance(v, list)]
    if lists:
        raise SystemExit(f"{src}: {lists} are lists (a sweep), not one strategy")
    sig = market or (d.get("data") or {}).get("instrument")
    if sig not in FAMILY:
        raise SystemExit(f"{src}: unknown market {sig!r}")
    family, markets = FAMILY[sig]
    title, how = PATTERN_ABOUT[s["setup"]]
    legs = [int(s.get("qty1") or 0), int(s.get("qty2") or 0)]
    out = {
        "id": cid,
        "name": name,
        "family": family,
        "signal": sig,
        "markets": markets,
        "pattern": title,
        "summary": character,
        "how_it_enters": how,
        "how_it_exits": exit_text(s),
        "experimental": cid in EXPERIMENTAL,
        "default_qty": legs,
        "engine": "patterns",
        "source": src.replace("\\", "/"),
        "data": {"htf_anchor": (d.get("data") or {}).get("htf_anchor")},
        "logic": s,
    }
    if s.get("boost_qty1") is not None or s.get("boost_qty2") is not None:
        out["how_it_exits"] += " On signals with strong volume it trades double the size."
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    jobs = [(c, s, n, ch, None) for c, s, n, ch in CARDS]
    short = {"ES": "sp", "NQ": "nq", "GC": "gold", "CL": "crude"}
    label = {"ES": "S&P", "NQ": "Nasdaq", "GC": "Gold", "CL": "Crude"}
    for c, s, n, ch in GENERIC:
        for m in FAMILY:
            cid = f"{short[m]}-{c}"
            jobs.append((cid, s, f"{label[m]} {n}", ch, m))
            EXPERIMENTAL.add(cid)
    ids = set()
    for cid, src, name, ch, m in jobs:
        if cid in ids:
            raise SystemExit(f"duplicate id {cid}")
        ids.add(cid)
        c = card(cid, src, name, ch, m)
        folder = OUT / c["logic"]["setup"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{cid}.yaml"
        text = (f"# {name}  -  generated by tools/import_strategies.py from {c['source']}\n"
                "# Edit `name`, `summary` and the texts freely. `logic` is the frozen backtest setup:\n"
                "# change it only by re-running the import tool after a new backtest.\n"
                + yaml.safe_dump(c, sort_keys=False, allow_unicode=True, width=110))
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == text:
            continue
        print(("would write " if a.check else "wrote ") + str(path.relative_to(ROOT)))
        if not a.check:
            path.write_text(text, encoding="utf-8")
    print(f"{len(jobs)} strategies")


if __name__ == "__main__":
    sys.exit(main())
