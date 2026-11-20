# Pattern Desk

Runs the chart-pattern strategies (flags, double tops / bottoms, pullbacks, trendlines) live on
IBKR through the **Trading Hub**. Built to be operated by someone who does not need to know how the
strategies work inside.

## Start

1. Start **TWS / IB Gateway** and log in.
2. Start the **Trading Hub** (`..\hub`, `python supervisor.py`).
3. Double-click **`start.bat`**. The page opens at <http://127.0.0.1:8002>.

Keep the black window open while trading. Nothing protects an open trade while Pattern Desk is closed.

## The pages

| Page | What it is for |
|---|---|
| **Strategies** | Every strategy on one line: on/off switch, market (ES, MES …), quantity boxes, what it is doing now. Click a name to read what it does. **Add strategy** runs one on another market too (e.g. ES *and* MES). |
| **Live** | Every open trade: entry, stop loss, target, price, profit / loss and in plain words what happens next. Below it: entries that are ready to trigger. The **Close everything** button is at the bottom. |
| **Stocks** | The US-stock strategies (box breakout 10m + 15m, pole & flag 10m): open stock positions (entry, stop, targets, P&L, what happens next), what every stock's last candle said, closed stock trades, a limit for the total open stock value and its own emergency button. |
| **History** | Closed trades with their result. |

### US stocks (box breakout, 10m + 15m copies)
The **US Stocks** group at the top of the Strategies page has two rows - *Stock Box Breakout 10m* and *15m*. Each is
one switch for all 20 stocks (NVDA AAPL TSLA META AVGO JPM AMZN GOOGL MSFT BRK.B LLY WMT ORCL V NFLX MA XOM COST JNJ HD)
and an **Amount $** box: the fixed $ amount every trade buys, the same for every stock (empty at the start - a copy
cannot be switched on without it). Shares = amount / price, split 1 : 1 : 4 over the three legs; rounded up when that
stays within amount + 5 %, else down. Strong-volume breakouts trade legs 2 + 3 at 1.5x / 2x as backtested (up to
~1.83x the amount); a reverse keeps its breakout's shares. The two copies each hold their own position per stock, so
several open trades use several amounts. Orders are sent 09:30-16:00 New York only; positions are held overnight.
The entry / exit rules are exactly the backtested ones (the backtest itself sized by $1,000 risk per trade).
* Code: `desk/stocks/` - `core.py` (the rules, a port of the backtest), `bot.py` (one stock in one copy: orders, state),
  `desk.py` (loop, broker check, API), `copies.py` (the two copies' settings). State: `state/stocks/`.
* Tests: `python tests/test_stocks_replay.py` (live rules vs the backtest, leg by leg - 6 stocks x 2 copies identical),
  `python tests/test_stocks_bot.py` (the bot in practice mode on real NVDA minutes, incl. a restart inside a trade).
* TradingView: `..\nq_wae_backtest\box\pine\box_stocks.pine` (10m) and `box_stocks_15m.pine` (15m).

### US stocks (pole & flag, 10m)
A third row in the **US Stocks** group, *Stock Pole & Flag 10m*: one switch for its 19 stocks (the box list without
BRK.B: AAPL AMZN AVGO COST GOOGL HD JNJ JPM LLY MA META MSFT NFLX NVDA ORCL TSLA V WMT XOM) and its own **Amount $**
box (shares = amount / price, split 1 : 2 over TP1 leg + runner, rounded like the box copies). It holds its own position
per stock, next to the box copies.
* Entry: a pole (>= 2 ATR in <= 7 candles and >= 0.35 x the daily range) + a flag (1-15 candles) arms a buy stop
  1 cent above the flag high for the next 10-minute candle only, only with the 60-minute trend (clock-hour candles
  close above a rising EMA 50) and only for candles opening 10:00-15:00. The order is sent the moment the price
  crosses the level. Shorts: strict own rules, rare.
* Exits: stop 0.5 ATR beyond the flag; 1/3 at 3 R, then the runner's stop goes to the entry and trails 6 R behind
  the best price. Held overnight.
* Backtest (19 stocks, 2016-11 -> 2026-10, $1,000 risk): +$2.67M, DD $95k, net/DD 28.1, ~400 trades per stock.
* Code: `desk/stocks/flag_core.py` (the rules, a port of the research flag backtest; `bot.py` / `desk.py` are shared
  with the box copies). Tests: `python tests/test_stocks_flag_replay.py [SYMBOLS]` (live rules vs the backtest, leg
  by leg) and `python tests/test_stocks_bot.py flag10` (practice mode on real NVDA minutes, incl. a restart).
* TradingView: `..\nq_wae_backtest\patterns\flag_stocks_10m\flag_stocks_10m.pine`.

### Quantity boxes
* **Target** = contracts that close at the profit target.
* **Runner** = contracts that stay in to ride the trend with a trailing stop.
* Strategies with one box trade all contracts the same way.
* A change is used from the **next** trade. Double top / bottom strategies trade double the size on
  strong-volume signals.

### Account and contract month
Each strategy row shows its contract month and IBKR account under the market (e.g. `Z6 · DU1234567`).
Change them with **⋯ → Account and contract month…**:
* **Account** - picked from the accounts of your IBKR login (the list comes from the hub). The strategy's
  orders go to that account only. It can be changed only while the strategy is flat. If the account
  disappears from the login, the row shows "Account not found in IBKR" and opens no new trades.
* **Contract month** - *Automatic* trades the front month and moves an open trade to the next month on
  the roll date by itself. A fixed month (e.g. H7) is traded until shortly before it expires.
  Picking another month while a trade is open asks: move the trade now, or keep it where it is.

**Moving a trade to the next month** (Live page button **Move to …**, or changing the month): Pattern Desk
sends both orders in one go - sell the old month, buy the new one (a short: the other way round). The trade
is not closed: stop loss, targets and runner carry on. P&L includes the roll (History shows it).
If only one side fills, the row asks for attention and says exactly what IBKR should hold.

### Switching off
Switching off a strategy with an open trade asks what to do: let the trade finish (stops and targets
keep working, no new trades) or close it now.

### "Needs attention"
Pattern Desk compares its own trades with IBKR every few minutes. If they do not match (for example a
position was closed by hand in TWS), orders for that contract pause and the Live page shows two buttons:
**Positions are correct** (carry on) or **I closed it by hand** (forget that trade).

## Ownership - what Pattern Desk touches

Only its own trades. Each strategy row counts only the contracts its own orders filled. Positions you
open by hand, positions that were already in the account, and trades from the Strategy Manager are never
closed, added to or counted. The emergency button closes only Pattern Desk's positions.

## Settings

`config/settings.yaml`: hub address, IBKR account, web port, and `dry_run: true` for a practice run
(signals and trades are simulated, no orders are sent; the top bar shows "Practice mode").

## For developers

* `ROADMAP.md` - design and phases.
* `strategies/README.md` - the strategy cards and how to add one.
* `desk/logic/patterns/research/` - the backtest code, vendored verbatim (`SOURCE_HASHES.txt`).
* `python -m pytest tests` - trade-rule tests, roll / account tests and the backtest parity check.
* `python tools/live_paper_check.py [--market MNQ]` - real orders on the PAPER account through the real engine:
  entry, take profit, roll to the next month, restart, broker check, close (refuses non-DU accounts).
