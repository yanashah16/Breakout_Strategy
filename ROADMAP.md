# Pattern Desk — Roadmap

Pattern Desk runs the chart-pattern strategies from `nq_wae_backtest/patterns` (flags,
double tops/bottoms, pullbacks, trendlines) live on IBKR through the shared **Trading Hub**
(port 8000), next to the existing Strategy Manager (port 8001). Pattern Desk runs on **port 8002**.

It is built for an operator who is not technical: one row per running strategy, an on/off switch,
quantity boxes, and a Live page that explains every open trade in plain English.

---

## Design in one picture

```
 strategies/*.yaml  ──►  Catalog  (what strategies exist: name, market, texts, frozen logic)
                            │
 state/deployments.json ─►  Deployments  (what the operator runs: strategy + market + qty + on/off)
                            │
 Trading Hub ──bars──►  BarBook (1 per signal market: ES, NQ, GC, CL) ──► Logic engine ("patterns")
             ──prices─►        │                                      = the research code, verbatim
             ◄─orders──  Deployment ── Trade (2-leg exits: target, runner, break-even, trailing stop)
             ──positions► Reconciler (only our own contracts; everything else is "outside", never touched)
                            │
                        Web UI  (Strategies · Live · History)
```

* **Strategy** = one YAML card. Logic is frozen into the card, so research edits never change live
  trading by accident. Re-run `tools/import_strategies.py` to pick up a new backtest on purpose.
* **Deployment** = a strategy on one market with a quantity. The same strategy can run on ES and MES at
  once (two rows). Signals always come from the full-size market's bars (MES trades on ES signals),
  exactly like the backtest.
* **Logic engines** are plugins (`desk/logic/`). `patterns` covers all four setups. A new kind of
  strategy = a new engine class + its cards; the rest of the platform (orders, positions, UI) is shared.
* **Ownership**: a deployment's position is the sum of ITS OWN filled orders. Positions already in
  IBKR, manual trades and the Strategy Manager's trades are never closed, added to or counted.

## Naming

Every strategy got a name describing its behaviour; the original version is kept in `source:`.
See `strategies/README.md` for the full table (e.g. `ES_v1` → **S&P Spike Flag**).

---

## Phases

### Phase 1 — Foundation  ✅ (this build)
- [x] Project layout, settings file, one-click `start.bat`
- [x] Research code vendored verbatim with source hashes (`desk/logic/patterns/research/`)
- [x] Strategy catalog: 37 cards with behaviour-based names and plain-English texts
- [x] Hub client (bars, history, prices, orders, positions, health)
- [x] Futures specs + automatic front-month roll (ES/MES, NQ/MNQ, GC/MGC, CL/MCL …)

### Phase 2 — Live engine  ✅ (this build)
- [x] BarBook: 90-day warm-up (30-min + 5-min history + 10 days of 1-min + local disk cache), live 1-min stream
- [x] Pattern engine adapter: runs the research signal code on the rolling window, reads the last bar
- [x] Trade state machine: 2 legs, TP1 / TP2, break-even, trailing stop, flat time, max bars
- [x] Entry handling: stop-entry (flag, double, pullback) armed for one bar; market entry (trendline)
- [x] Orders through the hub OMS (target-position model, idempotent keys, partial / reject healing)
- [x] Per-deployment state on disk after every change; full restore after a restart
- [x] Broker reconciliation: own contracts only; mismatch → "Needs attention", no new entries

### Phase 3 — Operator UI  ✅ (this build)
- [x] **Strategies** page: grouped by market, on/off switch, quantity boxes, live status, "Add strategy"
- [x] **Live** page: open trades with entry, stop, targets, P&L and "what happens next"; armed entries
- [x] **History** page: closed trades with result in plain words
- [x] Hub / IBKR connection banner, emergency "close everything Pattern Desk opened"
- [x] Turning a strategy off with an open trade asks: close now, or stop after the trade ends

### Phase 4 — Proof before money  ◐ (started)
- [x] **Replay parity test** (`tests/replay.py`): the live Deployment, fed minute by minute, vs the
      backtest's `simulate()` — **all 37 strategies, 2026-04 → 2026-10: 3,575 / 3,575 trades identical**
      (entry bar, side, price, stop, every leg's exit bar and reason)
- [x] Rolling-window check: signals from the live 90-day window = signals from full history
      (1,480 / 1,480 sampled bar closes)
- [x] Offline integration test: fake hub → warm-up layers → live stream → engine cycle (`tests/test_offline.py`)
- [ ] First run against the real hub + paper account (practice mode on, then real paper orders)
- [ ] Paper account run for 2+ weeks; compare fills with the backtest trade lists
- [ ] Measure slippage of the synthetic stop entries vs the backtest's stop fills
- [ ] Load test: all 37 strategies on at once (bar-close compute: ~0.6 s for the 9 S&P strategies)

### Phase 5 — Hardening  (after paper)
- [ ] Telegram alerts (entry, exit, needs attention) via the hub's telegram route
- [ ] Daily loss limit per deployment and for the whole desk
- [ ] Login (reuse the Strategy Manager's auth) if the UI is opened from another machine
- [ ] Watchdog: restart the desk if it stops answering; alert if the hub is down > N minutes
- [ ] Roll-day handling review (continuous bars vs traded month around expiry)

### Phase 6 — Growth
- [ ] New logic engines (box breakout, PO, …) as plugins, sharing orders / positions / UI
- [ ] Per-strategy performance page (live vs backtest expectation)
- [ ] Strategy Manager integration: show Pattern Desk positions on its Live page

---

## Known limits (read before going live)

* The hub has no native stop orders: stops, targets and stop-entries are watched by the desk on live
  prices (every 2 s) and re-checked on every completed 1-minute candle, then sent as marketable orders.
  Fills therefore include slippage the 0-slippage backtest does not have.
* The backtest numbers are in-sample (see `best_versions/README.md`); CL showed no edge on unseen data.
* While the desk is stopped, nothing protects an open position. Close trades before shutting it down,
  or keep it running.
