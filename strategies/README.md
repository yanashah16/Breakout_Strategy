# Strategy cards

One YAML file = one strategy. Pattern Desk loads every `*.yaml` under this folder at start (or with
**Reload**). Bad files are skipped and listed on the Strategies page; they never stop the desk.

## Names

| Card | Name | Backtest version |
|---|---|---|
| **S&P 500 - flags** | | |
| `sp-spike-flag` | S&P Spike Flag | ES_v1 |
| `sp-thrust-flag` | S&P Thrust Flag | ES_v2 |
| `sp-long-flag-runner` | S&P Long Flag Runner | ES_v3 |
| `sp-strong-trend-flag` | S&P Strong-Trend Flag | ES_v4 |
| `sp-quick-bank-flag` | S&P Quick-Bank Flag | ES_v5 |
| `sp-big-move-flag` | S&P Big-Move Flag | ES_v6 |
| **Nasdaq 100 - flags** | | |
| `nq-active-flag` | Nasdaq Active Flag | NQ_v1 |
| `nq-thrust-runner` | Nasdaq Thrust Runner | NQ_v2 |
| `nq-big-move-flag` | Nasdaq Big-Move Flag | NQ_v3 |
| `nq-steady-flag` | Nasdaq Steady Flag | NQ_v4 |
| `nq-classic-flag` | Nasdaq Classic Flag | NQ_v5 (flag_NQ_26to1) |
| `nq-young-trend-flag` | Nasdaq Young-Trend Flag | NQ_v6 |
| `nq-swift-flag` | Nasdaq Swift Flag | extra_NQ_a |
| `nq-bank-first-flag` | Nasdaq Bank-First Flag | extra_NQ_b |
| `nq-selective-flag` | Nasdaq Selective Flag | extra_NQ_c |
| `nq-finder-flag` | Nasdaq Finder Flag | extra_NQ_finder |
| **Gold - flags** | | |
| `gold-tight-flag` | Gold Tight Flag | GC_v1 |
| `gold-deep-pause-flag` | Gold Deep-Pause Flag | GC_v2 |
| `gold-active-flag` | Gold Active Flag | GC_v3 |
| `gold-trend-flag` | Gold Trend-Aligned Flag | GC_v4 |
| `gold-big-pole-runner` | Gold Big-Pole Runner | GC_v5 |
| `gold-high-win-flag` | Gold High-Win Flag | GC_v6 (flag_finder_GC) |
| `gold-mature-flag` | Gold Mature Flag | GC_v7 |
| `gold-strong-trend-flag` | Gold Strong-Trend Flag | GC_v8 |
| **Crude oil - flags** | | |
| `crude-early-trend-flag` | Crude Early-Trend Flag | CL_v1 |
| `crude-big-thrust-runner` | Crude Big-Thrust Runner | CL_v2 |
| **Double top / bottom** | | |
| `sp-double-measured` | S&P Double Top/Bottom | double_ES_mm |
| `nq-double-measured` | Nasdaq Double Top/Bottom | double_NQ_mm |
| `gold-double-measured` | Gold Double Top/Bottom | double_GC_mm |
| `crude-double-measured` | Crude Double Top/Bottom | double_CL_mm |

How the names were chosen: the pattern + what makes the version different from its siblings
(a sudden spike pole, a strong-trend filter, a young trend, a single runner vs banking part early …).
The `summary` line of each card says it in one sentence.

## A card

```yaml
id: sp-spike-flag              # stable id - never change it once the strategy has traded
name: S&P Spike Flag           # free text, shown in the UI
family: S&P 500                # group on the Strategies page
signal: ES                     # whose candles drive the signals
markets: [MES, ES]             # where it may trade (first = the default row)
pattern: Flag breakout
summary: ...                   # one sentence, shown under the name
how_it_enters: ...             # plain English, shown in "About"
how_it_exits: ...
experimental: false            # true = shows a warning before switching on
default_qty: [1, 2]            # [target, runner] contracts; a 0 hides that box
engine: patterns               # which logic engine runs it (desk/logic/)
source: best_versions/ES_v1.yaml
data: {htf_anchor: "18:00"}
logic: {...}                   # the frozen backtest settings
```

## Adding a strategy

* **Another pattern setting** (new backtest of flag / double / pullback / trendline): add it to
  `CARDS` in `tools/import_strategies.py` and run `python tools/import_strategies.py`. Or write the card
  by hand (copy one and change `id`, the texts and `logic`).
* **A new kind of strategy**: write a logic engine in `desk/logic/<name>/` - a class with
  `@register("<name>")` that implements `on_bar_close`, `finalize` and `exit_rules`
  (see `desk/logic/base.py`) - then cards with `engine: <name>`. Orders, positions, the Live page and
  history work for it unchanged.
