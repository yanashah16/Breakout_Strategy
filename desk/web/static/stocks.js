/* Pattern Desk - US stocks (box breakout 10m + 15m copies, pole & flag 10m copy).
   Loaded before app.js; uses its helpers ($, api, esc, money, tone, toast, dialogs) at call time. */
"use strict";

let STK = null;
let stkKeys = "";

async function stkPoll() {
  try {
    STK = await api("/api/stocks/state");
    if (PAGE === "strategies") renderStockRows();
    if (PAGE === "stocks") renderStocks();
  } catch (e) { /* the stock desk is not running: nothing to show */ }
}

function stkCopy(id) { return STK.copies.find(c => c.id === id); }
const STK_LABEL = { box10: "Box 10m", box15: "Box 15m", flag10: "Flag 10m" };
function stkLabel(id) { return STK_LABEL[id] || id; }

function stkStatus(c) {
  if (!c.enabled && !c.open) return { tone: "off", text: c.capital_usd ? "Off" : "Off · enter the amount per trade" };
  if (c.enabled && c.ready < c.stocks) return { tone: "idle", text: `Loading market data… ${c.ready}/${c.stocks}` };
  if (c.open) return { tone: "trade", text: `${c.open} open trade${c.open === 1 ? "" : "s"} · ${money(c.upnl)}` + (c.enabled ? "" : " · switching off after them") };
  return { tone: "idle", text: `Watching ${c.stocks} stocks` };
}

/* ------------------------------------------------------------------ Strategies page: the stock rows */
function renderStockRows() {
  const box = $("#stock-group");
  if (!box || !STK) return;
  const keys = STK.copies.map(c => c.id).join(",");
  if (keys !== stkKeys) {
    stkKeys = keys;
    box.innerHTML = `<section class="group"><div class="group-head"><h2>US Stocks</h2><span id="stk-on"></span>
        <a href="/stocks" class="btn small" style="margin-left:auto">Stock positions →</a></div>
      <div class="list">${STK.copies.map(c => `
        <div class="row" data-stk="${esc(c.id)}">
          <div class="c-toggle"><button class="toggle" role="switch" aria-label="Switch ${esc(c.name)} on or off"></button></div>
          <div class="c-name" style="min-width:0;cursor:pointer">
            <div class="s-name"><span class="nm">${esc(c.name)}</span></div>
            <div class="s-sub">${esc(c.summary)}</div></div>
          <div class="c-mkt"><span class="mkt" title="${esc((c.stock_list || STK.stocks).join(", "))}">${(c.stock_list || STK.stocks).length} stocks</span><div class="mkt-sub">${esc(STK.account || "")}</div></div>
          <div class="c-qty qty"><label title="Every trade buys about this much stock, on every stock (all legs together)">Amount $ <input type="number" min="1000" step="1000" data-risk value="${c.capital_usd || ""}" placeholder="per trade" style="width:100px"></label><span class="saved">Saved</span></div>
          <div class="c-status"><span class="pill"><span class="pdot"></span><span class="ptxt"></span></span></div>
          <div class="c-menu"><button class="icon-btn" aria-label="More">${ICON.more}</button></div>
        </div>`).join("")}</div></section>`;
    for (const el of $$("[data-stk]", box)) {
      const id = el.dataset.stk;
      $(".toggle", el).addEventListener("click", () => stkToggle(id));
      $(".c-name", el).addEventListener("click", () => stkAbout(id));
      $(".c-menu .icon-btn", el).addEventListener("click", e => {
        e.stopPropagation();
        showMenu(e.currentTarget, [{ label: "About this strategy", run: () => stkAbout(id) },
                                   { label: "Open the stock positions page", run: () => (location.href = "/stocks") }]);
      });
      const inp = $("input[data-risk]", el);
      inp.addEventListener("input", () => (inp.dataset.dirty = "1"));
      inp.addEventListener("keydown", e => { if (e.key === "Enter") inp.blur(); });
      inp.addEventListener("change", () => stkSaveRisk(id, inp, el));
    }
  }
  for (const c of STK.copies) {
    const el = $(`[data-stk="${CSS.escape(c.id)}"]`, box);
    if (!el) continue;
    el.classList.toggle("off", !c.enabled);
    const tg = $(".toggle", el);
    tg.classList.toggle("on", c.enabled);
    tg.setAttribute("aria-checked", c.enabled ? "true" : "false");
    const st = stkStatus(c), p = $(".pill", el);
    p.className = "pill " + st.tone;
    $(".ptxt", p).textContent = st.text;
    const inp = $("input[data-risk]", el);
    if (document.activeElement !== inp && !inp.dataset.dirty) inp.value = c.capital_usd || "";
  }
  const on = STK.copies.filter(c => c.enabled).length;
  $("#stk-on").textContent = on ? `${on} on` : "";
}

function usd(v) { return v ? "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "not set"; }

async function stkSaveRisk(id, inp, el) {
  const v = parseFloat(inp.value);
  if (!Number.isFinite(v) || v < 1000) { toast("Enter the amount per trade ($1,000 or more)", true); delete inp.dataset.dirty; return; }
  try {
    await api(`/api/stocks/copies/${encodeURIComponent(id)}/capital`, { method: "POST", body: { usd: v } });
    delete inp.dataset.dirty;
    const sv = $(".saved", el);
    if (sv) { sv.classList.add("show"); setTimeout(() => sv.classList.remove("show"), 1600); }
    toast(`Saved: each new trade buys about ${usd(v)} of stock. Open trades keep their size.`);
    stkPoll();
  } catch (e) { toast(e.message, true); delete inp.dataset.dirty; }
}

async function stkToggle(id) {
  const c = stkCopy(id);
  if (!c.enabled) {
    if (!c.capital_usd) { toast("Enter the amount per trade in the Amount $ box first", true); return; }
    const ok = await confirmDialog({ title: `Switch on ${c.name}?`, ok: "Switch on",
      text: `It will watch ${(c.stock_list || STK.stocks).length} stocks and trade in <b>${esc(STK.account || "the selected account")}</b>.
        Every trade buys about <b>${usd(c.capital_usd)}</b> of stock (up to 5 % more to keep the legs in ${esc(c.legs || "1 : 1 : 4")}${c.kind === "box" ? `;
        strong-volume breakouts up to about ${usd(c.capital_usd * 1.83)}` : ""}).<br><br>Each open trade uses its own amount and positions are
        held overnight, so several open trades together can use many times this amount.` });
    if (!ok) return;
    return stkSetEnabled(id, true);
  }
  if (c.open) {
    const d = openDialog(`${dialogHead(`Switch off ${c.name}?`)}<div class="d-body">
      <p>This copy has ${c.open} open trade${c.open === 1 ? "" : "s"}. What should happen to ${c.open === 1 ? "it" : "them"}?</p>
      <button class="choice" data-m="after_trade"><b>Let the trades finish</b><span>Stops and targets keep working. No new trades are opened.</span></button>
      <button class="choice" data-m="close_now"><b>Close them now</b><span>Sends orders to close this copy's positions at the market price.</span></button>
      </div><div class="d-foot"><button class="btn" data-close>Cancel</button></div>`);
    $$(".choice", d).forEach(b => b.addEventListener("click", () => { closeDialog(); stkSetEnabled(id, false, b.dataset.m); }));
    return;
  }
  stkSetEnabled(id, false);
}

async function stkSetEnabled(id, on, mode) {
  try {
    await api(`/api/stocks/copies/${encodeURIComponent(id)}/enable`, { method: "POST", body: { on, mode: mode || "after_trade" } });
    toast(on ? `${stkCopy(id).name} is on` : `${stkCopy(id).name} is off`);
    stkPoll();
  } catch (e) { toast(e.message, true); }
}

function stkAbout(id) {
  const c = stkCopy(id);
  openDialog(`${dialogHead(c.name)}<div class="d-body">
    <p>${esc(c.summary)}</p>
    <h4>How it enters</h4><p>${esc(c.how_it_enters || STK.how_it_enters)}</p>
    <h4>How it exits</h4><p>${esc(c.how_it_exits || STK.how_it_exits)}</p>
    <h4>Backtest (2016-11 to 2026-10, $1,000 risk per trade)</h4><p>${esc(c.backtest)}.${c.kind === "box" ? ` Both box copies together: +$5.07M, drawdown $130k,
      net / drawdown 38.9, 403 trades per stock.` : ""}</p>
    <h4>Stocks</h4><p>${esc((c.stock_list || STK.stocks).join(", "))}</p></div>
    <div class="d-foot"><button class="btn" data-close>Close</button></div>`, true);
}

/* ------------------------------------------------------------------ Stocks page */
const stkHist = { rows: [], at: 0 };

function fmt(v, d = 2) { return v === null || v === undefined ? "—" : Number(v).toFixed(d); }

function stkCard(p) {
  return `<article class="tcard ${p.attention ? "attention" : ""}">
    <div class="tc-head"><div><div class="tc-name">${esc(p.stock)} <span class="muted" style="font-weight:500">· ${esc(p.strategy)}</span></div>
      <div class="tc-sub">${Math.abs(p.held)} shares · legs ${p.legs.join(" / ")} · ${esc(p.setup)}${p.boost ? " · " + esc(p.boost) : ""}</div></div>
      ${p.side ? `<span class="side ${p.side.toLowerCase()}">${p.side.toUpperCase()}</span>` : ""}</div>
    <div class="tc-grid">
      <div><div class="k">Entry</div><div class="v" title="Rule price ${fmt(p.rule_entry)}">${fmt(p.fill ?? p.rule_entry)}</div></div>
      <div><div class="k">Stop loss</div><div class="v">${fmt(p.stop)}</div></div>
      <div><div class="k">Target 1</div><div class="v">${p.legs[0] ? fmt(p.tp1) : "done"}</div></div>
      <div><div class="k">Price now</div><div class="v">${fmt(p.price)}</div></div>
      <div style="grid-column: span 2"><div class="k">Open profit / loss</div><div class="pnl-big ${tone(p.upnl)}">${money(p.upnl)}</div>
        ${p.r_now !== null ? `<div class="k">${p.r_now >= 0 ? "+" : ""}${p.r_now.toFixed(2)} R · 1 R = $${fmt(p.r_size)} per share</div>` : ""}</div>
    </div>
    <div class="tc-story"><div class="now">${esc(p.stage)}</div>${p.next ? `<div class="next">${esc(p.next)}</div>` : ""}
      ${p.attention ? `<div class="next" style="color:var(--warn)">${esc(p.attention)}</div>` : ""}
      ${p.error ? `<div class="next" style="color:var(--warn)">${esc(p.error)}</div>` : ""}</div>
    <div class="tc-foot">Opened ${esc(p.opened ? new Date(p.opened).toLocaleString() : "—")}${p.working ? " · order working…" : ""}
      <span class="grow"></span>
      <button class="btn small danger" data-stk-close="${esc(p.id)}" data-name="${esc(p.stock + " · " + p.strategy)}">Close trade</button></div>
  </article>`;
}

function renderStocks() {
  const s = STK;
  if (!s) return;
  const pos = s.positions.filter(p => p.held || p.side);
  const att = s.positions.filter(p => p.attention);
  $("#banner").innerHTML = (STATE ? connectionBanner(STATE) : "") + att.map(a => `
    <div class="banner warn"><div><div class="b-title">${esc(a.stock)} · ${esc(a.strategy)} needs attention</div><div class="b-text">${esc(a.attention)}</div></div>
      <div class="b-actions"><button class="btn small" data-satt="${esc(a.id)}" data-how="correct">Positions are correct</button>
      <button class="btn small" data-satt="${esc(a.id)}" data-how="closed_by_hand">I closed it by hand</button></div></div>`).join("");
  $$("[data-satt]").forEach(b => b.addEventListener("click", () => stkAttention(b.dataset.satt, b.dataset.how)));
  const upnl = pos.reduce((a, p) => a + (p.upnl || 0), 0);
  const today = stkHist.rows.filter(r => new Date(r.closed).toDateString() === new Date().toDateString()).reduce((a, r) => a + (r.pnl || 0), 0);
  const on = s.copies.filter(c => c.enabled).length;
  $("#s-tiles").innerHTML = `
    <div class="tile"><div class="t-label">Open stock trades</div><div class="t-value">${pos.length}</div></div>
    <div class="tile"><div class="t-label">Open profit / loss</div><div class="t-value ${tone(upnl)}">${pos.length ? money(upnl) : "—"}</div></div>
    <div class="tile"><div class="t-label">Value of open positions</div><div class="t-value">${money(s.exposure, false)}</div>
      <div class="k">${s.max_exposure_usd ? "limit " + money(s.max_exposure_usd, false) : "no limit"}</div></div>
    <div class="tile"><div class="t-label">Closed today</div><div class="t-value ${tone(today)}">${money(today)}</div></div>`;
  $("#s-copies").innerHTML = s.copies.map(c => {
    const st = stkStatus(c);
    return `<span class="pill ${st.tone}" title="${esc(c.summary)}"><span class="pdot"></span><span class="ptxt">${esc(c.name)}: ${esc(st.text)} · ${usd(c.capital_usd)} per trade</span></span>`;
  }).join(" ") + (on ? "" : ` <span class="muted">All stock strategies are off - switch them on on the <a href="/">Strategies</a> page.</span>`);
  $("#s-trades").innerHTML = pos.length ? `<div class="cards">${pos.map(stkCard).join("")}</div>` :
    `<div class="empty"><div class="e-title">No open stock trades</div>${on ? "The strategy is watching the stocks; trades appear here the moment they open." : "Switch on a copy on the Strategies page."}</div>`;
  $$("[data-stk-close]").forEach(b => b.addEventListener("click", () => stkClose(b.dataset.stkClose, b.dataset.name)));
  const w = s.watch || [];
  $("#s-watch").innerHTML = w.length ? `<div class="table-wrap"><table><thead><tr><th>Stock</th><th>Copy</th><th>Last candle</th><th class="r">Close</th>
      <th class="r">Buys above</th><th class="r">Sells below</th><th>Why no trade</th></tr></thead><tbody>
      ${w.map(r => `<tr><td><b>${esc(r.stock)}</b></td><td>${esc(stkLabel(r.copy))}</td><td>${esc(r.at || "—")}</td>
        <td class="r">${fmt(r.close)}</td><td class="r">${fmt(r.long_above)}</td><td class="r">${fmt(r.short_below)}</td><td>${esc(r.state || "")}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">${on ? "Loading market data…" : "Nothing is being watched."}</div>`;
  const rows = stkHist.rows;
  $("#s-history").innerHTML = rows.length ? `<div class="table-wrap"><table><thead><tr><th>Opened</th><th>Closed</th><th>Stock</th><th>Copy</th>
      <th>Side</th><th class="r">Shares</th><th class="r">Entry</th><th class="r">Exit</th><th>How it ended</th><th class="r">Profit / loss</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${esc(r.opened ? new Date(r.opened).toLocaleString() : "—")}</td><td>${esc(new Date(r.closed).toLocaleString())}</td>
        <td><b>${esc(r.stock)}</b>${r.practice ? ' <span class="tag exp">practice</span>' : ""}</td><td>${esc(stkLabel(r.copy))}</td>
        <td><span class="side ${String(r.side).toLowerCase()}" style="margin:0">${esc(String(r.side).toUpperCase())}</span></td>
        <td class="r">${r.shares}</td><td class="r">${fmt(r.entry)}</td><td class="r">${fmt(r.exit)}</td><td class="result">${esc(r.exits)}</td>
        <td class="r ${tone(r.pnl)}"><b>${money(r.pnl)}</b></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">No closed stock trades yet.</div>`;
  const ex = $("#s-exposure");
  if (ex && document.activeElement !== ex && !ex.dataset.dirty) ex.value = s.max_exposure_usd || 0;
  const loading = (s.warming || []).length;
  $("#s-data").textContent = `Account ${s.account || "—"}${s.dry_run ? " · practice mode" : ""}` + (loading ? ` · loading ${loading} stock${loading === 1 ? "" : "s"}…` : "")
    + Object.entries(s.failed || {}).map(([k, v]) => ` · ${k}: ${v}`).join("");
}

async function stkClose(id, name) {
  const ok = await confirmDialog({ title: "Close this trade?", ok: "Close at market", danger: true,
    text: `Pattern Desk will close <b>${esc(name)}</b> at the market price. The copy stays on and can trade this stock again.` });
  if (!ok) return;
  try { await api(`/api/stocks/positions/${encodeURIComponent(id)}/close`, { method: "POST" }); toast("Closing order sent"); stkPoll(); }
  catch (e) { toast(e.message, true); }
}

async function stkAttention(id, how) {
  const ok = await confirmDialog(how === "correct" ?
    { title: "Positions are correct?", ok: "Yes, carry on", text: "Use this after you checked in TWS that the shares are really there. Orders for this stock are allowed again." } :
    { title: "Closed by hand?", ok: "Yes, I closed it", danger: true, text: "Pattern Desk will forget this trade and send no order for it. Use this only if you closed the shares yourself in TWS." });
  if (!ok) return;
  try { await api(`/api/stocks/positions/${encodeURIComponent(id)}/attention`, { method: "POST", body: { how } }); toast("Done"); stkPoll(); }
  catch (e) { toast(e.message, true); }
}

async function stkLoadHistory() {
  try { stkHist.rows = (await api("/api/stocks/history")).trades; } catch (e) { /* retried */ }
}

function initStocks() {
  const ex = $("#s-exposure");
  ex.addEventListener("input", () => (ex.dataset.dirty = "1"));
  ex.addEventListener("keydown", e => { if (e.key === "Enter") ex.blur(); });
  ex.addEventListener("change", async () => {
    let v = parseFloat(ex.value);
    if (!Number.isFinite(v) || v < 0) v = 0;
    try { await api("/api/stocks/exposure", { method: "POST", body: { usd: v } }); delete ex.dataset.dirty;
      toast(v ? `New trades are skipped while the open stock positions are worth more than ${money(v, false)}` : "No limit on the open stock value"); stkPoll(); }
    catch (e) { toast(e.message, true); delete ex.dataset.dirty; }
  });
  $("#s-panic").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Close every stock trade?", ok: "Close all stock trades", danger: true,
      text: "All stock strategies are switched off and <b>every stock position Pattern Desk opened</b> is closed at market. Shares you bought yourself are not touched." });
    if (!ok) return;
    try { await api("/api/stocks/close-all", { method: "POST" }); toast("Stock copies off. Closing orders sent."); stkPoll(); }
    catch (e) { toast(e.message, true); }
  });
  stkLoadHistory().then(renderStocks);
  setInterval(stkLoadHistory, 15000);
}

document.addEventListener("DOMContentLoaded", () => {
  if (PAGE === "strategies" || PAGE === "stocks") { stkPoll(); setInterval(stkPoll, 2000); }
});
