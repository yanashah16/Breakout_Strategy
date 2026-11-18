/* Pattern Desk front end. Plain JavaScript, no build step, no external libraries. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const PAGE = document.body.dataset.page;
let STATE = null;

/* ------------------------------------------------------------------ helpers */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function money(v, sign = true) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (v < 0 ? "−$" : (sign && v > 0 ? "+$" : "$")) + s;
}
function tone(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
  return data;
}
let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), err ? 5000 : 2600);
}
const ICON = {
  close: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  more: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>',
  plus: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  mark: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-6 4 3 5-8 4 5"/></svg>',
};

/* ------------------------------------------------------------------ dialog */
function openDialog(html, wide = false) {
  const d = $("#dialog");
  d.className = "dialog show" + (wide ? " wide" : "");
  d.innerHTML = html;
  $("#scrim").className = "scrim show";
  const x = $("[data-close]", d);
  if (x) x.focus();
  $$("[data-close]", d).forEach(b => b.addEventListener("click", closeDialog));
  return d;
}
function closeDialog() {
  $("#dialog").className = "dialog";
  $("#scrim").className = "scrim";
}
function dialogHead(title) {
  return `<div class="d-head"><h3>${esc(title)}</h3><button class="icon-btn" data-close aria-label="Close">${ICON.close}</button></div>`;
}
function confirmDialog({ title, text, ok, danger }) {
  return new Promise(res => {
    const d = openDialog(`${dialogHead(title)}<div class="d-body"><p>${text}</p></div>
      <div class="d-foot"><button class="btn" data-close>Cancel</button>
      <button class="btn ${danger ? "danger-solid" : "primary"}" id="d-ok">${esc(ok)}</button></div>`);
    $$("[data-close]", d).forEach(b => b.addEventListener("click", () => res(false)));
    $("#d-ok", d).addEventListener("click", () => { closeDialog(); res(true); });
  });
}
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeDialog(); hideMenu(); } });

/* ------------------------------------------------------------------ shell */
function shell() {
  const nav = [["/", "Strategies", "strategies"], ["/live", "Live", "live"], ["/stocks", "Stocks", "stocks"], ["/history", "History", "history"]];
  document.body.insertAdjacentHTML("afterbegin", `
    <header class="topbar"><div class="topbar-in">
      <div class="brand"><div class="brand-mark">${ICON.mark}</div><span>Pattern Desk</span></div>
      <nav class="nav">${nav.map(([h, t, p]) =>
        `<a href="${h}" class="${p === PAGE ? "on" : ""}">${t}${p === "live" ? '<span class="count" id="nav-live" hidden></span>' : ""}</a>`).join("")}</nav>
      <div class="topbar-right">
        <span class="badge dry" id="dry" hidden>Practice mode · no real orders</span>
        <div class="conn"><span class="dot" id="conn-dot"></span><span class="txt" id="conn-txt">Connecting…</span></div>
        <button class="badge acct" id="acct" type="button" title="Choose the IBKR account for all strategies"></button>
      </div>
    </div></header>`);
  document.body.insertAdjacentHTML("beforeend",
    '<div class="scrim" id="scrim"></div><div class="dialog" id="dialog" role="dialog" aria-modal="true"></div><div class="toast" id="toast" role="status"></div><div class="menu" id="menu"></div>');
  $("#scrim").addEventListener("click", closeDialog);
  $("#acct").addEventListener("click", allAccountsDialog);
}

async function allAccountsDialog() {
  const d = openDialog(`${dialogHead("IBKR account")}<div class="d-body"><p class="muted">Loading the accounts from IBKR…</p></div>`);
  const accts = await loadAccounts(true);
  const cur = (STATE && STATE.account) || "";
  if (!accts.length) {
    d.innerHTML = `${dialogHead("IBKR account")}<div class="d-body"><p>The Trading Hub cannot list the IBKR accounts right now.
      Check that the hub is running and connected to IBKR, then try again.</p></div>
      <div class="d-foot"><button class="btn" data-close>Close</button></div>`;
    $$("[data-close]", d).forEach(b => b.addEventListener("click", closeDialog));
    return;
  }
  d.innerHTML = `${dialogHead("IBKR account")}<div class="d-body">
    <div class="field"><label for="aa-a">Account for all strategies</label>
      <select id="aa-a">${accountOptions(accts, cur)}</select>
      <div class="hint">Every strategy sends its orders to this account, and new strategies use it too.
        A strategy with an open trade keeps its account until the trade is closed. The list comes from your IBKR login
        (accounts starting with DU are paper accounts).</div></div>
    </div><div class="d-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="aa-ok">Use this account</button></div>`;
  $$("[data-close]", d).forEach(b => b.addEventListener("click", closeDialog));
  $("#aa-ok", d).addEventListener("click", async () => {
    const account = $("#aa-a", d).value;
    try {
      const res = await api("/api/accounts/all", { method: "POST", body: { account } });
      closeDialog();
      toast(`Orders now go to ${account}.` + (res.busy.length ? ` Still on the old account until their trade closes: ${res.busy.join(", ")}.` : ""), res.busy.length > 0);
      if (typeof strat !== "undefined") strat.keys = "";
      poll();
    } catch (e) { toast(e.message, true); }
  });
}

function renderShell(s) {
  const h = s.hub;
  const dot = $("#conn-dot"), txt = $("#conn-txt");
  if (!h.ok) { dot.className = "dot bad"; txt.textContent = "Trading Hub offline"; }
  else if (!h.ibkr) { dot.className = "dot warn"; txt.textContent = "IBKR not connected"; }
  else { dot.className = "dot good"; txt.textContent = "Connected"; }
  txt.title = h.message || "";
  $("#dry").hidden = !s.dry_run;
  const accts = (s.accounts_in_use && s.accounts_in_use.length) ? s.accounts_in_use : (s.account ? [s.account] : []);
  $("#acct").textContent = (accts.length ? `Account${accts.length > 1 ? "s" : ""} ${accts.join(", ")}` : "Choose account") + " ▾";
  const n = $("#nav-live");
  n.hidden = !s.summary.open;
  n.textContent = s.summary.open;
}

function connectionBanner(s) {
  const h = s.hub;
  if (!h.ok) return `<div class="banner bad"><div><div class="b-title">The Trading Hub is not running</div>
      <div class="b-text">Pattern Desk gets its prices and sends its orders through the hub. Start the hub (port 8000); this page reconnects by itself.</div></div></div>`;
  if (!h.ibkr) return `<div class="banner warn"><div><div class="b-title">IBKR is not connected</div>
      <div class="b-text">${esc(h.message)} Check that TWS or IB Gateway is open and logged in.</div></div></div>`;
  return "";
}

async function poll() {
  try {
    STATE = await api("/api/state");
    renderShell(STATE);
    if (PAGES[PAGE] && PAGES[PAGE].update) PAGES[PAGE].update(STATE);
  } catch (e) {
    $("#conn-dot").className = "dot bad";
    $("#conn-txt").textContent = "Pattern Desk not responding";
  }
}

/* ------------------------------------------------------------------ menu */
let menuFor = null;
function showMenu(anchor, items) {
  const m = $("#menu");
  m.innerHTML = items.map((it, i) => `<button data-i="${i}" class="${it.danger ? "danger" : ""}">${esc(it.label)}</button>`).join("");
  const r = anchor.getBoundingClientRect();
  m.style.top = `${window.scrollY + r.bottom + 4}px`;
  m.style.left = `${Math.max(8, window.scrollX + r.right - 190)}px`;
  m.className = "menu show";
  menuFor = anchor;
  $$("button", m).forEach(b => b.addEventListener("click", () => { hideMenu(); items[+b.dataset.i].run(); }));
}
function hideMenu() { $("#menu").className = "menu"; menuFor = null; }
document.addEventListener("click", e => { if (menuFor && !e.target.closest("#menu") && !e.target.closest(".c-menu")) hideMenu(); });

/* ------------------------------------------------------------------ accounts and contract months */
let ACCOUNTS = null;                          // null = not loaded, [] = the hub could not say
async function loadAccounts(force = false) {
  if (ACCOUNTS && ACCOUNTS.length && !force) return ACCOUNTS;
  try {
    const d = await api("/api/accounts");
    ACCOUNTS = (d.accounts && d.accounts.length) ? d.accounts : (d.managed || []).map(a => ({ account: a }));
  } catch (_) { ACCOUNTS = []; }
  return ACCOUNTS;
}
async function loadMonths(market) {
  try { return (await api(`/api/months/${encodeURIComponent(market)}`)).months; } catch (_) { return []; }
}
function accountOptions(list, current) {
  const ids = list.map(a => a.account);
  if (current && !ids.includes(current)) list = [{ account: current, missing: true }, ...list];
  return list.map(a => `<option value="${esc(a.account)}" ${a.account === current ? "selected" : ""}>${esc(a.account)}${
    a.missing ? " (not in this IBKR login)" : (a.net_liquidation ? ` · ${money(a.net_liquidation, false)}` : "")}</option>`).join("");
}
function monthOptions(months, current) {
  const front = months.find(m => m.front);
  return `<option value="auto" ${current === "auto" ? "selected" : ""}>Automatic · front month${front ? ` (now ${esc(front.label)})` : ""}</option>` +
    months.map(m => `<option value="${esc(m.expiry)}" ${m.expiry === current ? "selected" : ""}>${esc(m.label)}${m.front ? " · front month" : ""}</option>`).join("");
}

/* ================================================================== STRATEGIES PAGE */
const FAMILY_ORDER = ["S&P 500", "Nasdaq 100", "Gold", "Crude Oil"];
const strat = { filter: "All", onlyOn: false, q: "", keys: "", catalog: null };

function qtyLabels(legs) {
  return legs.length > 1 ? ["Target", "Runner"] : ["Contracts"];
}

function rowHtml(r) {
  const labels = qtyLabels(r.legs);
  const q = r.legs.map((leg, i) => `<label title="${leg === 0 ? "Contracts closed at the profit target" : (r.legs.length > 1 ? "Contracts that stay in to ride the trend" : "Contracts traded")}">
      ${labels[i]} <input type="number" min="0" max="100" step="1" inputmode="numeric" data-leg="${leg}" value="${r.qty[leg]}" aria-label="${labels[i]} quantity"></label>`).join("");
  return `<div class="row ${r.enabled ? "" : "off"}" data-id="${esc(r.id)}">
    <div class="c-toggle"><button class="toggle" role="switch" aria-label="Switch ${esc(r.name)} on or off"></button></div>
    <div class="c-name" style="min-width:0">
      <div class="s-name"><span class="nm">${esc(r.name)}</span>${r.experimental ? '<span class="tag exp" title="Not fully tested yet">Experimental</span>' : ""}</div>
      <div class="s-sub">${esc(r.summary)}</div>
    </div>
    <div class="c-mkt"><span class="mkt" title="${esc(r.market_name)}">${esc(r.market)}</span><div class="mkt-sub" title="Contract month · IBKR account"></div></div>
    <div class="c-qty qty">${q}<span class="saved">Saved</span></div>
    <div class="c-status"><span class="pill"><span class="pdot"></span><span class="ptxt"></span></span></div>
    <div class="c-menu"><button class="icon-btn" aria-label="More">${ICON.more}</button></div>
  </div>`;
}

function visibleRows(s) {
  const q = strat.q.trim().toLowerCase();
  return s.strategies.filter(r =>
    (strat.filter === "All" || r.family === strat.filter) &&
    (!strat.onlyOn || r.enabled || r.has_trade) &&
    (!q || (r.name + " " + r.market + " " + r.pattern).toLowerCase().includes(q)));
}

function renderStrategies(s) {
  const rows = visibleRows(s);
  const keys = strat.filter + "|" + strat.onlyOn + "|" + strat.q + "|" + rows.map(r => r.id).join(",");
  if (keys !== strat.keys) {
    strat.keys = keys;
    const fams = [...new Set(rows.map(r => r.family))].sort((a, b) => FAMILY_ORDER.indexOf(a) - FAMILY_ORDER.indexOf(b));
    $("#groups").innerHTML = rows.length ? fams.map(f => {
      const fr = rows.filter(r => r.family === f).sort((a, b) => (a.experimental - b.experimental) || a.name.localeCompare(b.name) || a.market.localeCompare(b.market));
      return `<section class="group"><div class="group-head"><h2>${esc(f)}</h2><span data-fam="${esc(f)}"></span></div>
        <div class="list">${fr.map(rowHtml).join("")}</div></section>`;
    }).join("") : `<div class="empty"><div class="e-title">Nothing matches</div>Try another filter, or add a strategy.</div>`;
    bindRows();
  }
  for (const r of rows) updateRow(r);
  for (const el of $$("[data-fam]")) {
    const f = el.dataset.fam;
    const on = s.strategies.filter(r => r.family === f && r.enabled).length;
    el.textContent = on ? `${on} on` : "";
  }
  $("#banner").innerHTML = connectionBanner(s) + (s.problems && s.problems.length ?
    `<div class="banner warn"><div><div class="b-title">Some strategy files could not be loaded</div><div class="b-text">${s.problems.map(esc).join("<br>")}</div></div></div>` : "");
  $("#sum-on").textContent = `${s.summary.on} of ${s.summary.total} switched on`;
}

function updateRow(r) {
  const el = $(`.row[data-id="${CSS.escape(r.id)}"]`);
  if (!el) return;
  el.classList.toggle("off", !r.enabled);
  const tg = $(".toggle", el);
  tg.classList.toggle("on", r.enabled && !r.stop_after_trade);
  tg.classList.toggle("pending", r.enabled && r.stop_after_trade);
  tg.setAttribute("aria-checked", r.enabled ? "true" : "false");
  const p = $(".pill", el);
  p.className = "pill " + r.status.tone;
  $(".ptxt", p).textContent = r.status.text;
  p.title = r.attention || r.status.text;
  const sub = $(".mkt-sub", el);
  if (sub) {
    const month = r.pos_contract && r.has_trade ? r.pos_contract : (r.contract || "");
    sub.textContent = [month ? month.replace(r.market, "") + (r.expiry === "auto" ? "" : " (fixed)") : "", r.account].filter(Boolean).join(" · ");
  }
  for (const inp of $$("input[data-leg]", el)) {
    if (document.activeElement !== inp && !inp.dataset.dirty) inp.value = r.qty[+inp.dataset.leg];
  }
}

function rowById(id) { return STATE.strategies.find(r => r.id === id); }

function bindRows() {
  for (const el of $$(".row")) {
    const id = el.dataset.id;
    $(".toggle", el).addEventListener("click", () => toggleRow(id));
    $(".c-menu .icon-btn", el).addEventListener("click", e => {
      e.stopPropagation();
      const r = rowById(id);
      showMenu(e.currentTarget, [
        { label: "About this strategy", run: () => aboutDialog(r) },
        { label: "Account and contract month…", run: () => tradeSetupDialog(rowById(id)) },
        { label: `Also run on ${r.micro ? "full-size" : "micro"} contract…`, run: () => addDialog(r.strategy) },
        { label: "Remove from list", danger: true, run: () => removeRow(r) },
      ]);
    });
    $(".c-name", el).addEventListener("click", () => aboutDialog(rowById(id)));
    $(".c-name", el).style.cursor = "pointer";
    for (const inp of $$("input[data-leg]", el)) {
      inp.addEventListener("input", () => (inp.dataset.dirty = "1"));
      inp.addEventListener("keydown", e => { if (e.key === "Enter") inp.blur(); });
      inp.addEventListener("change", () => saveQty(el, id));
    }
  }
}

async function saveQty(el, id) {
  const r = rowById(id);
  const q = [...r.qty];
  for (const inp of $$("input[data-leg]", el)) {
    let v = parseInt(inp.value, 10);
    if (!Number.isFinite(v) || v < 0) v = 0;
    if (v > 100) v = 100;
    inp.value = v;
    q[+inp.dataset.leg] = v;
  }
  try {
    await api(`/api/deployments/${encodeURIComponent(id)}/qty`, { method: "POST", body: { qty: q } });
    r.qty = q;
    $$("input[data-leg]", el).forEach(i => delete i.dataset.dirty);
    const sv = $(".saved", el);
    sv.classList.add("show");
    setTimeout(() => sv.classList.remove("show"), 1600);
    if (r.has_trade) toast("Saved. The new quantity is used from the next trade.");
  } catch (e) {
    toast(e.message, true);
    $$("input[data-leg]", el).forEach(i => { delete i.dataset.dirty; i.value = r.qty[+i.dataset.leg]; });
  }
}

async function toggleRow(id) {
  const r = rowById(id);
  const turnOn = !r.enabled || r.stop_after_trade;
  if (turnOn) {
    if (r.experimental) {
      const ok = await confirmDialog({ title: `Switch on ${r.name}?`, ok: "Switch on",
        text: "This strategy is marked <b>Experimental</b>: it has not been fully tested on unseen data yet. Consider a small quantity first." });
      if (!ok) return;
    }
    return setEnabled(id, true);
  }
  if (r.has_trade) {
    const d = openDialog(`${dialogHead(`Switch off ${r.name}?`)}<div class="d-body">
      <p>This strategy has an open trade on ${esc(r.market)}. What should happen to it?</p>
      <button class="choice" data-m="after_trade"><b>Let the trade finish, then switch off</b><span>Stops and targets keep working. No new trades are opened.</span></button>
      <button class="choice" data-m="close"><b>Close the trade now and switch off</b><span>Sends an order to close this strategy's position at the market price.</span></button>
      </div><div class="d-foot"><button class="btn" data-close>Cancel</button></div>`);
    $$(".choice", d).forEach(b => b.addEventListener("click", () => { closeDialog(); setEnabled(id, false, b.dataset.m); }));
    return;
  }
  setEnabled(id, false);
}

async function setEnabled(id, on, mode) {
  try {
    await api(`/api/deployments/${encodeURIComponent(id)}/enable`, { method: "POST", body: { on, mode } });
    const r = rowById(id);
    toast(on ? `${r.name} is on and watching ${r.market}` : (mode === "after_trade" ? `${r.name} switches off after its trade` : `${r.name} is off`));
    poll();
  } catch (e) { toast(e.message, true); }
}

async function removeRow(r) {
  if (r.has_trade) return toast("Close the open trade first, then remove the strategy.", true);
  const ok = await confirmDialog({ title: "Remove from the list?", ok: "Remove", danger: true,
    text: `<b>${esc(r.name)}</b> on ${esc(r.market)} will be removed from your list. You can add it back any time.` });
  if (!ok) return;
  try { await api(`/api/deployments/${encodeURIComponent(r.id)}`, { method: "DELETE" }); toast("Removed"); strat.keys = ""; poll(); }
  catch (e) { toast(e.message, true); }
}

async function tradeSetupDialog(r) {
  const d = openDialog(`${dialogHead(`${r.name} · ${r.market}`)}<div class="d-body"><p class="muted">Loading…</p></div>`, true);
  const [accts, months] = await Promise.all([loadAccounts(true), loadMonths(r.market)]);
  const locked = r.has_trade;
  d.innerHTML = `${dialogHead(`${r.name} · ${r.market}`)}<div class="d-body">
    <div class="field"><label for="ts-a">IBKR account</label>
      <select id="ts-a" ${locked ? "disabled" : ""}>${accts.length ? accountOptions(accts, r.account) : `<option>${esc(r.account || "—")}</option>`}</select>
      <div class="hint">${locked ? `The open trade is in ${esc(r.trade_account || r.account)}. The account can be changed once the strategy is flat.` :
        (accts.length ? "Orders of this strategy go to this account only. The list comes from your IBKR login." : "The Trading Hub cannot list the accounts right now.")}</div></div>
    <div class="field"><label for="ts-m">Contract month</label>
      <select id="ts-m">${monthOptions(months, r.expiry)}</select>
      <div class="hint">Automatic trades the front month and moves an open trade to the next month on the roll date.
        A fixed month is traded until shortly before it expires, then the front month is used.</div></div>
    <div id="ts-move" hidden>
      <button class="choice on" data-mv="1" type="button"><b>Move the open trade to the new month now</b><span>Sells the old month and buys the new one at the same moment (for a short: the other way round). The trade, its stop loss and its targets stay as they are.</span></button>
      <button class="choice" data-mv="0" type="button"><b>Keep the open trade where it is</b><span>Only new trades use the new month.</span></button>
    </div>
    </div><div class="d-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="ts-ok">Save</button></div>`;
  $$("[data-close]", d).forEach(b => b.addEventListener("click", closeDialog));
  let move = true;
  const mSel = $("#ts-m", d);
  const showMove = () => { $("#ts-move", d).hidden = !(r.has_trade && mSel.value !== r.expiry); };
  mSel.addEventListener("change", showMove);
  $$("#ts-move .choice", d).forEach(b => b.addEventListener("click", () => {
    move = b.dataset.mv === "1";
    $$("#ts-move .choice", d).forEach(x => x.classList.toggle("on", x === b));
  }));
  $("#ts-ok", d).addEventListener("click", async () => {
    try {
      const msgs = [];
      const a = $("#ts-a", d);
      if (!locked && accts.length && a.value !== r.account) {
        await api(`/api/deployments/${encodeURIComponent(r.id)}/account`, { method: "POST", body: { account: a.value } });
        msgs.push(`Orders go to ${a.value}.`);
      }
      if (mSel.value !== r.expiry) {
        const res = await api(`/api/deployments/${encodeURIComponent(r.id)}/expiry`, { method: "POST", body: { expiry: mSel.value, move_trade: move } });
        msgs.push(res.message);
      }
      closeDialog();
      toast(msgs.join(" ") || "Nothing changed");
      poll();
    } catch (e) { toast(e.message, true); }
  });
}

function aboutDialog(r) {
  const card = (strat.catalog || []).find(c => c.id === r.strategy) || {};
  openDialog(`${dialogHead(r.name)}<div class="d-body">
    <p class="muted">${esc(r.pattern)} · trades ${esc(r.market)} (${esc(r.market_name)})</p>
    <div class="about-block"><h4>In short</h4><p>${esc(r.summary)}</p></div>
    <div class="about-block"><h4>How it enters</h4><p>${esc(r.how_it_enters)}</p></div>
    <div class="about-block"><h4>How it exits</h4><p>${esc(r.how_it_exits)}</p></div>
    ${r.experimental ? '<div class="about-block"><h4>Experimental</h4><p>Not yet tested on unseen data. Use a small quantity.</p></div>' : ""}
    <p class="source">Backtest version: ${esc(card.source || "")}</p>
    </div><div class="d-foot"><button class="btn primary" data-close>Done</button></div>`, true);
}

async function addDialog(preselect) {
  if (!strat.catalog) strat.catalog = (await api("/api/catalog")).strategies;
  const cats = strat.catalog;
  let sel = cats.find(c => c.id === preselect) || cats[0];
  let market = sel.markets[0];
  const fams = FAMILY_ORDER.filter(f => cats.some(c => c.family === f));
  const opts = fams.map(f => `<optgroup label="${esc(f)}">${cats.filter(c => c.family === f).map(c =>
      `<option value="${esc(c.id)}">${esc(c.name)}${c.experimental ? " (experimental)" : ""}</option>`).join("")}</optgroup>`).join("");
  const d = openDialog(`${dialogHead("Add a strategy")}<div class="d-body">
      <div class="field"><label for="a-s">Strategy</label><select id="a-s">${opts}</select><div class="hint" id="a-sum"></div></div>
      <div class="field"><label>Market</label><div class="seg" id="a-m"></div>
        <div class="hint">The same strategy can run on both. Micro contracts are 1/10 the size.</div></div>
      <div class="field"><label>Quantity</label><div class="qty" id="a-q"></div></div>
      <div class="field"><label for="a-acct">IBKR account</label><select id="a-acct"><option>Loading…</option></select></div>
      <div class="field"><label for="a-mon">Contract month</label><select id="a-mon"></select></div>
    </div><div class="d-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="a-ok">Add (switched off)</button></div>`, true);
  const sSel = $("#a-s", d);
  sSel.value = sel.id;
  function draw() {
    sel = cats.find(c => c.id === sSel.value);
    if (!sel.markets.includes(market)) market = sel.markets[0];
    $("#a-sum", d).textContent = sel.summary;
    $("#a-m", d).innerHTML = sel.markets.map(m => `<button type="button" data-m="${m}" class="${m === market ? "on" : ""}">${m}<small>${["MES", "MNQ", "MGC", "MCL"].includes(m) ? "micro" : "full-size"}</small></button>`).join("");
    $$("#a-m button", d).forEach(b => b.addEventListener("click", () => { market = b.dataset.m; draw(); }));
    const labels = qtyLabels(sel.legs);
    $("#a-q", d).innerHTML = sel.legs.map((leg, i) => `<label>${labels[i]} <input type="number" min="0" max="100" data-leg="${leg}" value="${sel.default_qty[leg]}"></label>`).join("");
    loadMonths(market).then(ms => { $("#a-mon", d).innerHTML = monthOptions(ms, "auto"); });
  }
  loadAccounts().then(list => {
    const def = (STATE && STATE.account) || "";
    $("#a-acct", d).innerHTML = list.length ? accountOptions(list, def) : `<option value="">${esc(def || "default")}</option>`;
  });
  sSel.addEventListener("change", draw);
  draw();
  $("#a-ok", d).addEventListener("click", async () => {
    const q = [...sel.default_qty];
    $$("#a-q input", d).forEach(i => (q[+i.dataset.leg] = Math.max(0, parseInt(i.value, 10) || 0)));
    try {
      const account = $("#a-acct", d).value || null, expiry = $("#a-mon", d).value || "auto";
      await api("/api/deployments", { method: "POST", body: { strategy: sel.id, market, qty: q, account, expiry } });
      closeDialog();
      toast(`${sel.name} on ${market} added. Switch it on when ready.`);
      strat.keys = ""; strat.filter = "All"; renderChips(); poll();
    } catch (e) { toast(e.message, true); }
  });
}

function renderChips() {
  const fams = ["All", ...FAMILY_ORDER];
  $("#chips").innerHTML = fams.map(f => `<button class="chip ${strat.filter === f ? "on" : ""}" data-f="${esc(f)}">${esc(f)}</button>`).join("") +
    `<button class="chip ${strat.onlyOn ? "on" : ""}" id="only-on">Switched on</button>`;
  $$("#chips [data-f]").forEach(b => b.addEventListener("click", () => { strat.filter = b.dataset.f; renderChips(); STATE && renderStrategies(STATE); }));
  $("#only-on").addEventListener("click", () => { strat.onlyOn = !strat.onlyOn; renderChips(); STATE && renderStrategies(STATE); });
}

async function rollDialog() {
  const d = openDialog(`${dialogHead("Contract roll")}<div class="d-body"><p class="muted">Loading…</p></div>`, true);
  async function draw(data) {
    const rows = data.instruments.map(i => {
      const v = i.volumes || {};
      const cur = v[i.month], nxt = i.next ? v[i.next] : null;
      return `<tr><td><b>${esc(i.symbol)}</b></td><td>${esc(i.label)}</td>
        <td class="r">${cur != null ? cur.toLocaleString() : "—"}</td>
        <td>${esc(i.next_label || "—")}</td><td class="r">${nxt != null ? nxt.toLocaleString() : "—"}</td>
        <td class="r">${i.share != null ? i.share.toFixed(2) + "×" : "—"}</td>
        <td>${esc(i.session || "")}${i.error ? `<div class="hint" style="color:var(--warn)">${esc(i.error)}</div>` : ""}</td>
        <td>${esc(i.hold_until)}</td></tr>`;
    }).join("");
    d.innerHTML = `${dialogHead("Contract roll")}<div class="d-body">
      <p>Every market trades its <b>nearest contract month</b>. Once a day (after each session) Pattern Desk compares the
      volume of the next month with the month it trades. When the next month trades at least the ratio below,
      the market moves to it: new trades use it, and open trades move there (old month sold, new month bought at the
      same moment; stop and targets move with the price difference). It never moves back.</p>
      <div class="field"><label for="r-ratio">Move when the next month trades at least</label>
        <div style="display:flex;gap:8px;align-items:center"><input id="r-ratio" type="number" min="1" max="5" step="0.05" value="${data.ratio}" style="width:110px;height:38px;padding:0 10px;border-radius:var(--radius-sm);border:1px solid var(--line-strong)">
        <span>× the volume of the current month</span></div>
        <div class="hint">1.2 = 20% more. The same ratio is used for every market. Whatever the volume, a month is
        never traded past its last safe day (column "Trade until").</div></div>
      <div class="table-wrap"><table><thead><tr><th>Market</th><th>Trading</th><th class="r">Volume</th><th>Next</th>
        <th class="r">Volume</th><th class="r">Next / now</th><th>Session</th><th>Trade until</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8">No market in use.</td></tr>'}</tbody></table></div>
      </div><div class="d-foot"><button class="btn" id="r-check">Check volumes now</button>
      <button class="btn" data-close>Close</button><button class="btn primary" id="r-save">Save ratio</button></div>`;
    $$("[data-close]", d).forEach(b => b.addEventListener("click", closeDialog));
    $("#r-save", d).addEventListener("click", async () => {
      try { const r = await api("/api/roll/ratio", { method: "POST", body: { ratio: parseFloat($("#r-ratio", d).value) } });
        toast(`Saved: moves at ${r.ratio}× volume`); draw(await api("/api/roll")); }
      catch (e) { toast(e.message, true); }
    });
    $("#r-check", d).addEventListener("click", async () => {
      try { await api("/api/roll/check", { method: "POST" }); toast("Checking the volumes - this takes a few seconds");
        setTimeout(async () => draw(await api("/api/roll")), 8000); }
      catch (e) { toast(e.message, true); }
    });
  }
  try { draw(await api("/api/roll")); } catch (e) { toast(e.message, true); }
}

function initStrategies() {
  renderChips();
  $("#roll-btn").addEventListener("click", rollDialog);
  $("#search").addEventListener("input", e => { strat.q = e.target.value; STATE && renderStrategies(STATE); });
  $("#add").addEventListener("click", () => addDialog());
  api("/api/catalog").then(c => (strat.catalog = c.strategies)).catch(() => {});
}

/* ================================================================== LIVE PAGE */
function tradeCard(t) {
  const pnl = t.pnl;
  return `<article class="tcard ${t.attention ? "attention" : ""}">
    <div class="tc-head"><div><div class="tc-name">${esc(t.name)}</div><div class="tc-sub">${esc(t.contract || t.market)} · ${t.qty} contract${t.qty === 1 ? "" : "s"} · ${esc(t.account || "")}</div></div>
      <span class="side ${t.side.toLowerCase()}">${t.side.toUpperCase()}</span></div>
    <div class="tc-grid">
      <div><div class="k">Entry</div><div class="v" title="${esc(t.entry_note || "")}">${esc(t.entry ?? "—")}</div></div>
      <div><div class="k">Stop loss</div><div class="v">${esc(t.stop ?? "—")}</div></div>
      <div><div class="k">${esc(t.target_label)}</div><div class="v">${esc(t.target ?? "—")}</div></div>
      <div><div class="k">Price now</div><div class="v">${esc(t.price ?? "—")}</div></div>
      <div style="grid-column: span 2"><div class="k">Open profit / loss</div><div class="pnl-big ${tone(pnl)}">${money(pnl)}</div>
        ${t.realised ? `<div class="k">plus ${money(t.realised)} already taken</div>` : ""}</div>
    </div>
    <div class="tc-story"><div class="now">${esc(t.state)}</div>${t.next ? `<div class="next">${esc(t.next)}</div>` : ""}
      ${t.attention ? `<div class="next" style="color:var(--warn)">${esc(t.attention)}</div>` : ""}
      ${t.month_differs ? `<div class="next">This strategy now trades ${esc(t.wanted_contract)}; the trade moves there when both months have a live price.</div>` : ""}</div>
    <div class="tc-foot">Opened ${esc(t.opened)}${t.working ? (t.rolling ? " · moving to the next month…" : " · order working…") : ""}
      <span class="grow"></span>
      ${t.next_month && !t.working && !t.attention ? `<button class="btn small" data-roll="${esc(t.id)}" data-from="${esc(t.contract)}" data-to="${esc(t.next_month)}" data-qty="${t.qty}" data-side="${esc(t.side)}">Move to ${esc(t.next_month)}</button>` : ""}
      <button class="btn small danger" data-close-trade="${esc(t.id)}" data-name="${esc(t.name)}">Close trade</button></div>
  </article>`;
}

function renderLive(s) {
  $("#banner").innerHTML = connectionBanner(s) + s.attention.map(a => `
    <div class="banner warn"><div><div class="b-title">${esc(a.name)} · ${esc(a.market)} needs attention</div><div class="b-text">${esc(a.text)}</div></div>
      <div class="b-actions"><button class="btn small" data-att="${esc(a.id)}" data-how="correct">Positions are correct</button>
      <button class="btn small" data-att="${esc(a.id)}" data-how="closed_by_hand">I closed it by hand</button></div></div>`).join("");
  $$("[data-att]").forEach(b => b.addEventListener("click", () => resolveAttention(b.dataset.att, b.dataset.how)));
  const sm = s.summary;
  $("#tiles").innerHTML = `
    <div class="tile"><div class="t-label">Open trades</div><div class="t-value">${sm.open}</div></div>
    <div class="tile"><div class="t-label">Open profit / loss</div><div class="t-value ${tone(sm.open_pnl)}">${sm.open ? money(sm.open_pnl) : "—"}</div></div>
    <div class="tile"><div class="t-label">Closed today</div><div class="t-value ${tone(sm.today)}">${money(sm.today)}</div></div>
    <div class="tile"><div class="t-label">Strategies on</div><div class="t-value">${sm.on}<span style="font-size:14px;color:var(--muted);font-weight:500"> / ${sm.total}</span></div></div>`;
  $("#trades").innerHTML = s.live.length ? `<div class="cards">${s.live.map(tradeCard).join("")}</div>` :
    `<div class="empty"><div class="e-title">No open trades</div>${sm.on ? `${sm.on} strateg${sm.on === 1 ? "y is" : "ies are"} watching the market. Trades appear here the moment they open.` : 'No strategy is switched on. Go to <a href="/">Strategies</a> to switch one on.'}</div>`;
  $$("[data-close-trade]").forEach(b => b.addEventListener("click", () => closeTrade(b.dataset.closeTrade, b.dataset.name)));
  $$("[data-roll]").forEach(b => b.addEventListener("click", () => rollTrade(b.dataset)));
  $("#armed-count").textContent = s.armed.length ? `${s.armed.length} ready` : "";
  $("#armed").innerHTML = s.armed.length ? `<div class="armed-list">${s.armed.map(a => a.plans.map(p => `
      <div class="armed-row"><span class="side ${p.side.toLowerCase()}" style="margin:0">${p.side === "Long" ? "BUY" : "SELL"}</span>
        <div class="what"><b>${esc(a.name)}</b> · ${esc(a.market)}<div class="meta">${p.until === "next candle" ? "A setup was found. The entry level is set when the next candle starts." :
          `${p.side === "Long" ? "Buys" : "Sells"} ${p.qty} if the price reaches <b class="num">${esc(p.level)}</b> before ${esc(p.until)}${p.stop ? ` · stop loss ${esc(p.stop)}` : ""}${p.boost ? " · strong volume: double size" : ""}`}</div></div></div>`).join("")).join("")}</div>`
    : `<div class="empty">No entries are waiting right now.</div>`;
  const ds = Object.entries(s.data);
  $("#data-line").textContent = (ds.length ? "Market data: " + ds.map(([k, v]) => `${k} ${v.ready ? (v.age === null ? "ready" : (v.age < 180 ? "live" : `quiet ${Math.round(v.age / 60)} min`)) : "loading"}`).join(" · ") : "") +
    (s.warming.length ? ` · loading ${s.warming.join(", ")}…` : "") + ` · updated ${s.time}`;
}

async function closeTrade(id, name) {
  const ok = await confirmDialog({ title: "Close this trade?", ok: "Close at market", danger: true,
    text: `Pattern Desk will close <b>${esc(name)}</b>'s position at the market price. The strategy stays on and can trade again later.` });
  if (!ok) return;
  try { await api(`/api/deployments/${encodeURIComponent(id)}/close`, { method: "POST" }); toast("Closing order sent"); poll(); }
  catch (e) { toast(e.message, true); }
}

async function rollTrade(b) {
  const long = b.side === "Long";
  const ok = await confirmDialog({ title: `Move to ${b.to}?`, ok: `Move to ${b.to}`,
    text: `Pattern Desk ${long ? "sells" : "buys back"} <b>${b.qty} ${esc(b.from)}</b> and ${long ? "buys" : "sells"} <b>${b.qty} ${esc(b.to)}</b> at the same moment.
      The trade is not closed: its stop loss, targets and runner carry on in ${esc(b.to)}. The strategy keeps trading ${esc(b.to)} afterwards.` });
  if (!ok) return;
  try { const r = await api(`/api/deployments/${encodeURIComponent(b.roll)}/roll`, { method: "POST" }); toast(r.message); poll(); }
  catch (e) { toast(e.message, true); }
}

async function resolveAttention(id, how) {
  const ok = await confirmDialog(how === "correct" ?
    { title: "Positions are correct?", ok: "Yes, carry on", text: "Use this after you checked in TWS that the position of this strategy is really there. Orders for it are allowed again." } :
    { title: "Closed by hand?", ok: "Yes, I closed it", danger: true, text: "Pattern Desk will forget this trade (it will not send any order for it). Use this only if you closed the position yourself in TWS." });
  if (!ok) return;
  try { await api(`/api/deployments/${encodeURIComponent(id)}/attention`, { method: "POST", body: { how } }); toast("Done"); poll(); }
  catch (e) { toast(e.message, true); }
}

function initLive() {
  $("#panic").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Close everything?", ok: "Close everything", danger: true,
      text: "Every strategy is switched off and <b>every position Pattern Desk opened</b> is closed at market. Positions you or other programs opened are not touched." });
    if (!ok) return;
    try { await api("/api/close-everything", { method: "POST" }); toast("All strategies off. Closing orders sent."); poll(); }
    catch (e) { toast(e.message, true); }
  });
}

/* ================================================================== HISTORY PAGE */
const hist = { range: "week", rows: [] };
function inRange(r) {
  const t = new Date(r.closed);
  const now = new Date();
  if (hist.range === "today") return t.toDateString() === now.toDateString();
  if (hist.range === "week") return now - t < 7 * 864e5;
  if (hist.range === "month") return now - t < 31 * 864e5;
  return true;
}
function renderHistory() {
  const rows = hist.rows.filter(inRange);
  const tot = rows.reduce((a, r) => a + (r.pnl || 0), 0);
  const wins = rows.filter(r => (r.pnl || 0) > 0).length;
  $("#h-tiles").innerHTML = `
    <div class="tile"><div class="t-label">Trades</div><div class="t-value">${rows.length}</div></div>
    <div class="tile"><div class="t-label">Profit / loss</div><div class="t-value ${tone(tot)}">${money(tot)}</div></div>
    <div class="tile"><div class="t-label">Winning trades</div><div class="t-value">${rows.length ? Math.round(100 * wins / rows.length) + "%" : "—"}</div></div>
    <div class="tile"><div class="t-label">Average per trade</div><div class="t-value ${tone(tot)}">${rows.length ? money(tot / rows.length) : "—"}</div></div>`;
  $("#h-table").innerHTML = rows.length ? `<div class="table-wrap"><table><thead><tr>
      <th>Entry time</th><th>Exit time</th><th>Strategy</th><th>Market</th><th>Side</th><th class="r">Qty</th><th class="r">Entry</th><th>Result</th><th class="r">Profit / loss</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${esc(r.opened_txt || "—")}</td><td>${esc(r.closed_txt)}</td><td><b>${esc(r.name)}</b>${r.dry_run ? ' <span class="tag exp">practice</span>' : ""}</td>
        <td>${esc(r.contract || r.market)}</td><td><span class="side ${String(r.side).toLowerCase()}" style="margin:0">${esc(String(r.side).toUpperCase())}</span></td>
        <td class="r">${r.qty}</td><td class="r">${r.entry ?? "—"}</td><td class="result">${esc(r.result)}</td>
        <td class="r ${tone(r.pnl)}"><b>${money(r.pnl)}</b></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty"><div class="e-title">No closed trades in this period</div>Closed trades are listed here with their result.</div>`;
}
async function initHistory() {
  const ranges = [["today", "Today"], ["week", "Last 7 days"], ["month", "Last 30 days"], ["all", "All"]];
  $("#h-chips").innerHTML = ranges.map(([k, t]) => `<button class="chip ${hist.range === k ? "on" : ""}" data-r="${k}">${t}</button>`).join("");
  $$("#h-chips .chip").forEach(b => b.addEventListener("click", () => { hist.range = b.dataset.r; initHistoryChips(); renderHistory(); }));
  await loadHistory();
}
function initHistoryChips() { $$("#h-chips .chip").forEach(b => b.classList.toggle("on", b.dataset.r === hist.range)); }
async function loadHistory() {
  try { hist.rows = (await api("/api/history")).trades; renderHistory(); } catch (e) { /* retried */ }
}

/* ================================================================== boot */
const PAGES = {
  strategies: { init: initStrategies, update: renderStrategies },
  live: { init: initLive, update: renderLive },
  history: { init: initHistory, update: () => {} },
  stocks: { init: () => initStocks(), update: () => renderStocks() },
};
shell();
PAGES[PAGE].init();
poll();
setInterval(poll, 2000);
if (PAGE === "history") setInterval(loadHistory, 15000);
