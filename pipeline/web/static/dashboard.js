"use strict";
// Siliconopolis: one app for the shop owner. Home (what needs you + the live office), Products,
// Research (brief the next run, see past runs), Orders (printed shirts/mugs), Team (the agents).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null || v === "" ? "—" : "$" + Number(v).toFixed(2));
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const AGENT_COLOR = { researcher: "#7cc4ff", creator: "#ff8fa3", qa: "#f3c34f", lister: "#6fd38a", analyst: "#b48cff", fulfiller: "#ffb36b" };
const STATE_WORD = { working: "Working", idle: "Idle", sleeping: "Asleep", waiting_human: "Waiting on you", needs_you: "Needs you", cap_reached: "At a limit", error: "Problem", stale_lock: "Stuck" };
const FILTERS = [
  { key: "all", label: "All", states: null },
  { key: "ideas", label: "Ideas", states: ["brief"] },
  { key: "built", label: "Built", states: ["created"] },
  { key: "approved", label: "Approved", states: ["approved"] },
  { key: "etsy", label: "Etsy drafts", states: ["drafted"] },
  { key: "live", label: "Live", states: ["published"] },
  { key: "rejected", label: "Rejected", states: ["rejected"] },
];
const SCORE_LABEL = { quality: "Visual quality", legibility: "Legibility", artifacts: "No glitches", matches_brief: "Matches the idea",
  design_polish: "Design polish", commercial_viability: "Would it sell?" };
const COMMERCIAL = ["design_polish", "commercial_viability"];

/* ---------------------------------------------------------------- helpers */
async function api(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}
async function send(method, path, body) {
  const r = await fetch(path, { method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Siliconopolis": "1" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Something went wrong (${r.status})`);
  return data;
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 86400 * 6) return Math.floor(s / 86400) + "d ago";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
function until(iso) {
  const d = new Date(iso), s = (d - Date.now()) / 1000;
  const day = d.toLocaleDateString(undefined, { weekday: "short" }), time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))} min`;
  if (sameDay(d, new Date())) return `today ${time}`;
  if (sameDay(d, new Date(Date.now() + 86400000))) return `tomorrow ${time}`;
  return `${day} ${time}`;
}
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), 3600);
}
function confirmBox({ title, body, ok = "OK", danger = false }) {
  return new Promise((resolve) => {
    const m = $("#modal"), card = $(".modal-card", m);
    card.innerHTML = `<h2>${esc(title)}</h2><p>${esc(body)}</p><div class="row"><button class="btn" data-a="no">Keep</button>
      <button class="btn ${danger ? "danger solid" : "primary"}" data-a="yes">${esc(ok)}</button></div>`;
    m.hidden = false;
    const done = (v) => { m.hidden = true; resolve(v); };
    $("[data-a=yes]", card).onclick = () => done(true);
    $("[data-a=no]", card).onclick = () => done(false);
    m.onclick = (e) => { if (e.target === m) done(false); };
    $("[data-a=yes]", card).focus();
  });
}
// A run's summary line, with the products it touched folded underneath ("QA reviewed 10 · 6 approved, 4 rejected").
const OUTCOME = { approved: "approved", rejected: "rejected", drafted: "drafted on Etsy", created: "built", brief: "proposed", published: "live" };
function groupActivity(list) {
  const out = []; let head = null;
  for (const a of list) {
    if (a.kind === "run_finished") { head = { ...a, children: [] }; out.push(head); }
    else if (a.kind === "transition" && head && head.agent === a.agent) head.children.push(a);
    else { head = null; out.push(a); }
  }
  return out;
}
function activityHTML(list, limit) {
  return groupActivity(list).slice(0, limit).map((a) => {
    const line = (x) => x.product_id ? `<a href="#product/${x.product_id}">${esc(x.text)}</a>` : esc(x.text);
    const cls = a.kind === "error" ? "err" : "";
    let body = `<span class="${cls}">${line(a)}</span>`;
    if (a.children && a.children.length) {
      const counts = {};
      a.children.forEach((c) => (counts[c.to_state] = (counts[c.to_state] || 0) + 1));
      const sum = Object.entries(counts).map(([k, n]) => `${n} ${OUTCOME[k] || k}`).join(", ");
      body = `<details class="group"><summary>${esc(a.text.replace(/ \(\$[\d.]+\)$/, ""))}<span class="muted"> · ${esc(sum)}</span><span class="more">show</span></summary>
        <ul>${a.children.map((c) => `<li>${line(c)}</li>`).join("")}</ul></details>`;
    }
    return `<li><time title="${esc(when(a.ts))}">${esc(ago(a.ts))}</time>${body}</li>`;
  }).join("") || `<li><span class="muted">Nothing yet.</span></li>`;
}
const thumb = (p) => (p.thumb ? `background-image:url('${esc(p.thumb)}')` : "");

/* ---------------------------------------------------------------- shared data */
const store = { overview: null, products: null, sel: new Set(), selecting: false, q: "", filter: "all" };

async function refreshOverview() {
  store.overview = await api("/api/dashboard/overview");
  const o = store.overview;
  const urgent = o.needs_you.filter((n) => n.level === "action" || n.level === "review").length;
  $("#n-home").textContent = urgent || "";
  $("#n-research").textContent = o.briefs_queued || "";
  $("#n-orders").textContent = o.orders.needs_review || "";
  const s = o.settings;
  $("#env").innerHTML = `
    <div class="row"><span><i class="dot ${s.etsy === "connected" || s.etsy === "mock" ? "ok" : "warn"}"></i>Etsy</span><span>${esc(s.etsy)}</span></div>
    <div class="row"><span><i class="dot ${s.app_env === "local" ? "" : "ok"}"></i>Mode</span><span>${esc(s.app_env)}${s.dry_run ? " · dry run" : ""}</span></div>
    <div class="row"><span>AI spend today</span><span>${money(o.spend.today)}</span></div>`;
  return o;
}

/* ---------------------------------------------------------------- router */
const pages = { home: renderHome, products: renderProducts, research: renderResearch, orders: renderOrders, team: renderTeam };
let current = null;
async function route() {
  const [page, arg] = (location.hash.slice(1) || "home").split("/");
  if (page === "product") { if (!current) await show("products"); return openProduct(+arg); }
  if (page === "agent") { if (!current) await show("team"); return openAgent(arg); }
  if (page === current && $("#drawer").classList.contains("open")) return closeDrawer(false);  // Back from a panel
  closeDrawer(false);
  await show(pages[page] ? page : "home", arg);
}
async function show(page, arg) {
  current = page;
  $$(".nav nav a").forEach((a) => a.classList.toggle("on", a.dataset.route === page));
  const view = $("#view");
  try { await pages[page](view, arg); } catch (e) { view.innerHTML = `<div class="panel pad">Couldn't load this page: ${esc(e.message)}</div>`; }
  document.title = `${page[0].toUpperCase() + page.slice(1)} · Siliconopolis`;
}
window.addEventListener("hashchange", route);
// The live office (village) runs in an iframe and hands clicks to the app.
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || e.data.source !== "village") return;
  document.querySelector(".fullscreen-office")?.remove();
  if (e.data.type === "agent") openAgent(e.data.name);
  if (e.data.type === "route") location.hash = e.data.route;
});

/* ================================================================ HOME */
async function renderHome(view) {
  const o = await refreshOverview();
  view.innerHTML = `
    <div class="page-head"><div><h1>Home</h1><p id="home-sub"></p></div></div>
    <div class="home">
      <div>
        <div class="section-title" style="margin-top:0"><h2>Needs you</h2></div>
        <div class="needs" id="needs"></div>
        <div class="section-title"><h2>The office, live</h2><button class="link" id="fullscreen">Full screen</button></div>
        <div class="office"><iframe src="/village?embed=1" title="Live view of the agents at work"></iframe>
          <div class="bar"><span>Each desk is one agent. Click someone to see what they're working on.</span></div></div>
        <div class="flow" id="flow"></div>
        <div class="flow-aside" id="flow-aside"></div>
      </div>
      <div class="side">
        <div class="panel pad"><h2>Coming up</h2><ul class="sched" id="sched"></ul></div>
        <div class="panel pad"><h2>Latest</h2><ul class="feed" id="feed"></ul>
          <p class="small" style="margin:10px 0 0"><a href="#team">Everything the team did →</a></p></div>
        <div class="panel pad"><h2>Spend & limits</h2><div id="limits"></div></div>
      </div>
    </div>`;
  fillHome(o);
  $("#fullscreen").onclick = () => {
    const fs = document.createElement("div");
    fs.className = "fullscreen-office";
    fs.innerHTML = `<div class="office" style="border-radius:0;border:0"><div class="bar"><span>The office, live · click someone to see their work</span><button class="btn small" id="fs-close">Close</button></div></div><iframe src="/village?embed=1" title="Live office"></iframe>`;
    document.body.appendChild(fs);
    $("#fs-close").onclick = () => fs.remove();
  };
  clearInterval(renderHome.t);
  renderHome.t = setInterval(async () => { if (current === "home") fillHome(await refreshOverview().catch(() => store.overview)); }, 30000);
}
function fillHome(o) {
  const urgent = o.needs_you.filter((n) => n.level === "action" || n.level === "review");
  $("#home-sub").textContent = urgent.length ? `${plural(urgent.length, "thing")} need${urgent.length === 1 ? "s" : ""} you. Everything else is running on schedule.`
    : "Nothing needs you right now. The team is running on schedule.";
  $("#needs").innerHTML = o.needs_you.length ? o.needs_you.map((n) => `
      <div class="panel need ${esc(n.level)}"><div><b>${esc(n.title)}</b><p>${esc(n.detail)}</p></div>
        ${n.cta ? `<a class="btn ${n.level === "action" ? "primary" : ""}" href="#${esc(n.route)}">${esc(n.cta)}</a>` : "<span></span>"}</div>`).join("")
    : `<div class="panel allclear"><span class="tick">✓</span><div><b>All clear</b><div class="muted small">Decisions that need you (drafts to publish, orders to check, rejected ideas) show up here.</div></div></div>`;
  const st = Object.fromEntries(o.stages.map((s) => [s.state, s.count]));
  const flow = [["brief", "Ideas", "ideas"], ["created", "Built", "built"], ["approved", "Approved", "approved"], ["drafted", "Etsy drafts", "etsy"], ["published", "Live on Etsy", "live"]];
  $("#flow").innerHTML = flow.map(([k, l, f]) => `<button data-f="${f}"><span class="n">${st[k] || 0}</span><span class="l">${l}</span></button>`).join("");
  $$("#flow button").forEach((b) => (b.onclick = () => (location.hash = `products/${b.dataset.f}`)));
  $("#flow-aside").innerHTML = st.rejected ? `${plural(st.rejected, "idea")} rejected · <a href="#products/rejected">review or clear them</a>` : "";
  $("#sched").innerHTML = o.schedule.map((s) => `<li><span>${esc(s.label)}</span><time title="${esc(when(s.at))}">${esc(until(s.at))}</time></li>`).join("")
    + (o.briefs_queued ? `<li><span><a href="#research">${plural(o.briefs_queued, "brief")} waiting</a></span><time>for the research run</time></li>` : "");
  $("#feed").innerHTML = activityHTML(o.activity, 7);
  const pct = (a, b) => Math.min(100, b ? (a / b) * 100 : 0);
  $("#limits").innerHTML = `
    <div class="meter"><span>AI spend today</span><b>${money(o.spend.today)} / ${money(o.spend.daily_limit)}</b><div class="track"><i style="width:${pct(o.spend.today, o.spend.daily_limit)}%"></i></div></div>
    <div class="meter"><span>Ideas this week</span><b>${o.limits.ideas_this_week} / ${o.limits.ideas_cap}</b><div class="track"><i style="width:${pct(o.limits.ideas_this_week, o.limits.ideas_cap)}%"></i></div></div>
    <div class="meter"><span>Etsy drafts today</span><b>${o.limits.drafts_today} / ${o.limits.drafts_cap}</b><div class="track"><i style="width:${pct(o.limits.drafts_today, o.limits.drafts_cap)}%"></i></div></div>
    <div class="muted small">Last 7 days ${money(o.spend.week)} · all time ${money(o.spend.total)}</div>`;
}

/* ================================================================ PRODUCTS */
async function renderProducts(view, arg) {
  store.filter = FILTERS.some((f) => f.key === arg) ? arg : "all";
  store.selecting = false; store.sel.clear();
  view.innerHTML = `
    <div class="page-head"><div><h1>Products</h1><p>Everything the team has come up with, from first idea to live on Etsy.</p></div></div>
    <div class="toolbar"><div class="seg" id="filters"></div><span class="grow"></span>
      <input type="search" id="search" placeholder="Search titles and niches" aria-label="Search products" value="${esc(store.q)}">
      <button class="btn" id="select">Select</button></div>
    <div id="banner"></div><div class="cards" id="cards"></div><p class="empty" id="cards-empty" hidden></p>
    <div class="selbar" id="selbar" hidden><span id="selcount"></span><span>
      <button class="btn small" id="sel-cancel">Cancel</button> <button class="btn small danger solid" id="sel-delete">Delete</button></span></div>`;
  $("#search").oninput = (e) => { store.q = e.target.value; drawProducts(); };
  $("#select").onclick = () => { store.selecting = !store.selecting; store.sel.clear(); drawProducts(); };
  $("#sel-cancel").onclick = () => { store.selecting = false; store.sel.clear(); drawProducts(); };
  $("#sel-delete").onclick = () => deleteProducts([...store.sel]);
  await loadProducts();
}
async function loadProducts() {
  store.products = (await api("/api/dashboard/products?limit=500")).products;
  drawProducts();
}
function drawProducts() {
  if (current !== "products") return;
  const all = store.products || [];
  const counts = Object.fromEntries(FILTERS.map((f) => [f.key, f.states ? all.filter((p) => f.states.includes(p.state)).length : all.length]));
  $("#filters").innerHTML = FILTERS.map((f) => `<button class="${store.filter === f.key ? "on" : ""}" data-f="${f.key}">${f.label}<b>${counts[f.key]}</b></button>`).join("");
  $$("#filters button").forEach((b) => (b.onclick = () => { history.replaceState(null, "", `#products/${b.dataset.f}`); store.filter = b.dataset.f; store.sel.clear(); drawProducts(); }));
  const f = FILTERS.find((x) => x.key === store.filter);
  const q = store.q.toLowerCase();
  const list = all.filter((p) => (!f.states || f.states.includes(p.state)) && (!q || `${p.title} ${p.niche} ${p.one_liner}`.toLowerCase().includes(q)));
  $("#banner").innerHTML = store.filter === "rejected" && list.length ? `<div class="banner"><span>These didn't pass QA or couldn't be built. Open one to see why and ask for a rebuild, or clear them out.</span>
      <button class="btn danger" id="del-rejected">Delete all ${list.length} rejected</button></div>`
    : store.filter === "etsy" && list.length ? `<div class="banner neutral"><span>These are drafts in your Etsy shop. Review and publish them in the Etsy app; nothing goes live without you.</span></div>` : "";
  $("#del-rejected")?.addEventListener("click", () => deleteProducts(null, "rejected", list.length));
  $("#select").textContent = store.selecting ? "Done" : "Select";
  $("#cards").innerHTML = list.map((p) => `
    <button class="card ${store.sel.has(p.id) ? "sel" : ""}" data-id="${p.id}" ${store.selecting && !p.deletable ? "disabled" : ""}>
      ${store.selecting ? `<span class="check">${store.sel.has(p.id) ? "✓" : ""}</span>` : ""}
      <div class="img" style="${thumb(p)}">${p.thumb ? "" : "Not built yet"}</div>
      <div class="body">
        <div class="meta"><span class="st ${esc(p.state)}">${esc(p.stage)}</span><span>${esc(p.type_label)}</span>${p.price ? `<span>· ${money(p.price)}</span>` : ""}</div>
        <div class="t">${esc(p.title)}</div>
        <div class="o">${esc(p.one_liner)}</div>
        ${p.revision_pending ? `<div class="why-rev">Your changes are being made</div>` : ""}
        ${p.top_reason ? `<div class="why-bad">${esc(p.top_reason)}</div>` : ""}
        <div class="meta">${esc(p.niche || "")}${p.from_brief ? " · from your brief" : ""} · ${esc(ago(p.updated_at))}</div>
      </div></button>`).join("");
  const empty = $("#cards-empty");
  empty.hidden = list.length > 0;
  empty.textContent = q ? "Nothing matches that search." : store.filter === "rejected" ? "No rejected ideas. Tidy!" : "Nothing here yet.";
  $$("#cards .card").forEach((c) => (c.onclick = () => {
    const id = +c.dataset.id;
    if (!store.selecting) return (location.hash = `product/${id}`);
    store.sel.has(id) ? store.sel.delete(id) : store.sel.add(id);
    drawProducts();
  }));
  $("#selbar").hidden = !store.selecting;
  $("#selcount").textContent = store.sel.size ? `${plural(store.sel.size, "product")} selected` : "Tap products to select them. Etsy drafts and live listings can't be deleted here.";
  $("#sel-delete").disabled = !store.sel.size;
}
async function deleteProducts(ids, state, n) {
  const count = ids ? ids.length : n;
  const ok = await confirmBox({ title: `Delete ${plural(count, "product")}?`, danger: true, ok: "Delete",
    body: "They're removed from the dashboard along with their files and notes. This can't be undone." });
  if (!ok) return;
  try {
    const r = await send("POST", "/api/dashboard/products/delete", ids ? { ids } : { state });
    toast(`Deleted ${plural(r.deleted.length, "product")}${r.skipped.length ? ` · ${r.skipped.length} skipped (${r.skipped[0].reason})` : ""}`);
    store.selecting = false; store.sel.clear();
    closeDrawer();
    if (current === "products") await loadProducts();
    refreshOverview().catch(() => {});
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- product drawer */
function scoreColor(v) { return v >= 8 ? "var(--good)" : v >= 6 ? "var(--warn)" : "var(--bad)"; }
function marketBlock(m, ref) {
  if (!m && !ref) return `<p class="muted">No market analysis was saved for this product.</p>`;
  if (!m) return `<p>${esc(ref.summary || "")}</p>`;
  const a = m.analysis || {};
  const list = (xs) => (xs && xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="muted">—</p>`);
  return `<p>${esc(a.summary || "")}</p>
    <div class="grid3"><div><div class="k">Demand</div><p>${esc(a.demand || "—")}</p></div>
      <div><div class="k">Competition</div><p>${esc(a.competition || "—")}</p></div>
      <div><div class="k">Typical prices</div><p>${esc(a.price_range || "—")}</p></div></div>
    <div class="grid3"><div><div class="k">What buyers want</div>${list(a.what_buyers_want)}</div>
      <div><div class="k">Gaps to fill</div>${list(a.gaps)}</div>
      <div><div class="k">Evidence</div><p class="muted">${esc(a.data_basis || "")} ${m.etsy_data ? `(${m.signals_count} Etsy listings sampled)` : "(no live Etsy data yet)"}</p></div></div>`;
}
function feedbackBlock(p) {
  const kindLabel = (c) => c.kind === "revision" ? `<span class="pill">Change request · ${c.status === "addressed" ? "done" : "being made"}</span>`
    : `<span class="pill">Note${c.status === "included" ? " · used" : ""}</span>`;
  const canRebuild = p.can_revise && !p.revision_pending;
  const cantWhy = p.revision_pending ? "Your last changes are still being made."
    : { brief: "It hasn't been built yet, so leave a note: the Creator reads it when it builds." }[p.state]
      || (p.can_revise ? "" : "An agent is working on it right now. Try again in a few minutes.");
  const nextBuild = store.overview?.schedule?.find((s) => s.key === "creator");
  return `<div class="sec" id="feedback"><h2>Your feedback</h2>
    <ul class="comments">${(p.comments || []).map((c) => `<li>${kindLabel(c)}<time>${esc(when(c.created_at))}</time><p>${esc(c.text)}</p></li>`).join("")
      || `<li class="muted">No feedback yet.</li>`}</ul>
    <textarea id="fb-text" rows="3" maxlength="2000" placeholder="What should change? e.g. warmer colours, bigger name, add a mileage tab"></textarea>
    <div class="choice">
      <label><input type="radio" name="fb" value="rebuild" ${canRebuild ? "checked" : "disabled"}>
        <span><b>Rebuild it with these changes</b><small>Goes back to the Creator${nextBuild ? ` (next build ${esc(until(nextBuild.at))})` : ""}; QA then checks your changes were made. The current version stays until the new one is ready.</small></span></label>
      <label><input type="radio" name="fb" value="note" ${canRebuild ? "" : "checked"}>
        <span><b>Just leave a note</b><small>Kept with the product and used the next time it's built or rebuilt.</small></span></label>
    </div>
    ${cantWhy ? `<p class="muted small">${esc(cantWhy)}</p>` : ""}
    <button class="btn primary" id="fb-send">Send</button></div>`;
}
async function openProduct(id) {
  let p;
  try { p = await api(`/api/dashboard/product/${id}`); } catch { toast("That product no longer exists.", true); return closeDrawer(); }
  const w = p.why || {}, imgs = p.images || [], qa = p.qa, design = p.design || {};
  const onEtsy = p.state === "drafted" || p.state === "published";
  $("#detail").innerHTML = `
    <div class="dhead">
      <div class="meta"><span class="st ${esc(p.state)}">${esc(p.stage)}</span><span>${esc(p.type_label)}</span><span>· ${esc(p.niche || "")}</span><span>· AI cost ${money(p.cost_usd)}</span></div>
      <h1>${esc(p.title)}</h1>
      <div class="dactions">
        ${onEtsy ? "" : `<button class="btn" id="go-feedback">Give feedback</button>`}
        ${p.listing?.etsy_url ? `<a class="btn" href="${esc(p.listing.etsy_url)}" target="_blank" rel="noopener">Open on Etsy ↗</a>` : ""}
        ${p.deletable ? `<button class="btn danger" id="del-one">Delete</button>` : ""}
      </div>
    </div>
    <div class="dgrid">
      <div class="gallery">
        ${imgs.length ? `<div class="main" style="background-image:url('${esc(imgs[0].full)}')" data-full="${esc(imgs[0].full)}"></div>
          <div class="thumbs">${imgs.map((im, i) => `<button class="${i ? "" : "on"}" style="background-image:url('${esc(im.thumb)}')" data-full="${esc(im.full)}" aria-label="Image ${i + 1}"></button>`).join("")}</div>`
        : `<div class="sec" style="margin:0"><p class="muted">Not built yet: the Creator builds new ideas every morning.</p>${p.errors.length ? `<p style="color:var(--bad)">Last build problem: ${esc(p.errors[0])}</p>` : ""}</div>`}
      </div>
      <div class="sec"><h2>Why this product</h2>
        <p>${esc(w.rationale || "—")}</p>
        <div class="lbl">The idea</div><p>${esc(w.concept || "—")}</p>
        <div class="lbl">Who it's for</div><p>${esc(w.audience || "—")}</p>
        <div class="lbl">Price</div><p>${money(p.listing?.price || w.target_price_usd)}</p>
        <div class="lbl">Search keywords</div><div class="tags">${(w.keywords || []).map((k) => `<span class="pill">${esc(k)}</span>`).join("") || "—"}</div>
      </div>
    </div>
    ${qa ? `<div class="sec"><h2>QA review</h2>
        <div class="verdict ${qa.passed ? "ok" : "no"}">${qa.passed ? "Approved for Etsy" : "Rejected: not good enough to list yet"}</div>
        ${qa.verdict ? `<p><i>${esc(qa.verdict)}</i></p>` : ""}
        ${Object.entries(qa.scores || {}).map(([k, v]) => `<div class="score ${COMMERCIAL.includes(k) ? "biz" : ""}"><span>${esc(SCORE_LABEL[k] || k)}</span><span class="track"><i style="width:${v * 10}%;background:${COMMERCIAL.includes(k) ? (v >= 8 ? "var(--good)" : "var(--bad)") : scoreColor(v)}"></i></span><b>${esc(v)}</b></div>`).join("")}
        ${qa.improvements?.length ? `<div class="lbl">How to make it better</div><ul>${qa.improvements.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${qa.reasons.length ? `<details><summary class="lbl" style="cursor:pointer">QA's full notes</summary><ol>${qa.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ol></details>` : ""}
        ${p.file_check ? `<p class="small" style="color:${p.file_check.ok ? "var(--good)" : "var(--bad)"}">${p.file_check.ok ? "✓ File check passed" : "File check failed: " + esc((p.file_check.errors || []).slice(0, 2).join("; "))}</p>` : ""}
      </div>` : ""}
    ${onEtsy ? `<div class="sec"><h2>Changes</h2><p class="muted">This is on Etsy now, so make changes in the Etsy app.</p></div>` : feedbackBlock(p)}
    <div class="dgrid" style="margin-top:16px">
      <div class="sec"><h2>Files</h2>
        ${p.files.length ? `<div class="files">${p.files.map((f) => `<a href="${esc(f.url)}" target="_blank" rel="noopener"><span class="kind">${esc(f.kind)}</span>${esc(f.name)}</a>`).join("")}</div>` : `<p class="muted">No files yet.</p>`}
        ${design.palette ? `<div class="lbl">Colours</div><div class="swatches">${design.palette.map((c) => `<span title="${esc(c)}" style="background:${esc(c)}"></span>`).join("")}</div>` : ""}
        ${design.planner_spec || design.spreadsheet_spec ? `<details><summary class="lbl" style="cursor:pointer">Design plan</summary><pre class="spec">${esc(JSON.stringify(design.planner_spec || design.spreadsheet_spec, null, 2))}</pre></details>` : ""}
      </div>
      <div class="sec"><h2>Etsy listing</h2>
        ${p.listing?.title ? `<p><b>${esc(p.listing.title)}</b></p><div class="tags">${(p.listing.tags || []).map((t) => `<span class="pill">${esc(t)}</span>`).join("")}</div>
          <details><summary class="lbl" style="cursor:pointer">Description</summary><p style="white-space:pre-wrap">${esc(p.listing.description)}</p></details>`
          : `<p class="muted">Written after QA approves it and Etsy is connected.</p>`}
      </div>
    </div>
    <div class="sec"><details><summary style="cursor:pointer"><b>Market research for “${esc(p.niche || "")}”</b></summary>${marketBlock(p.market, p.market_summary)}</details></div>
    <div class="sec"><h2>History</h2><ul class="timeline">${(p.timeline || []).map((t) => `<li><time>${esc(when(t.ts))}</time>${esc(t.text)}</li>`).join("") || "<li class='muted'>—</li>"}</ul></div>`;

  const main = $(".gallery .main");
  $$(".gallery .thumbs button").forEach((b) => (b.onclick = () => {
    $$(".gallery .thumbs button").forEach((x) => x.classList.remove("on")); b.classList.add("on");
    main.style.backgroundImage = `url('${b.dataset.full}')`; main.dataset.full = b.dataset.full;
  }));
  if (main) main.onclick = () => { $("#lightbox img").src = main.dataset.full; $("#lightbox").hidden = false; };
  $("#go-feedback")?.addEventListener("click", () => { $("#feedback").scrollIntoView({ behavior: "smooth" }); $("#fb-text").focus({ preventScroll: true }); });
  $("#del-one")?.addEventListener("click", () => deleteProducts([p.id]));
  $("#fb-send")?.addEventListener("click", async () => {
    const text = $("#fb-text").value.trim();
    if (!text) return toast("Write what should change first.", true);
    const revise = $("input[name=fb]:checked")?.value === "rebuild";
    try {
      await send("POST", `/api/dashboard/product/${p.id}/comments`, { text, revise });
      toast(revise ? "Sent back to the Creator with your changes." : "Note saved.");
      openProduct(p.id);
      if (current === "products") loadProducts();
      refreshOverview().catch(() => {});
    } catch (e) { toast(e.message, true); }
  });
  openDrawer(`product/${id}`);
}
function openDrawer(hash) {
  const fresh = !$("#drawer").classList.contains("open");
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false"); $("#scrim").hidden = false;
  if (fresh) $("#drawer").scrollTop = 0;
  if (location.hash.slice(1) !== hash) history.pushState(null, "", `#${hash}`);
}
function closeDrawer(updateHash = true) {
  if (!$("#drawer").classList.contains("open")) return;
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true"); $("#scrim").hidden = true;
  if (updateHash && /^#(product|agent)\//.test(location.hash))
    history.replaceState(null, "", `#${current || "home"}${current === "products" && store.filter !== "all" ? "/" + store.filter : ""}`);
}
$("#close").onclick = () => closeDrawer();
$("#scrim").onclick = () => closeDrawer();
$("#lightbox").onclick = () => ($("#lightbox").hidden = true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#lightbox").hidden = true; closeDrawer(); } });

/* ================================================================ RESEARCH */
async function renderResearch(view) {
  const d = await api("/api/dashboard/research");
  const left = Math.max(0, d.weekly.cap - d.weekly.used);
  view.innerHTML = `
    <div class="page-head"><div><h1>Research</h1><p>Point the Researcher at what you want next, and see what every run found.</p></div></div>
    <div id="queue" class="queue"></div>
    <div class="panel planner">
      <div class="form">
        <h2 id="form-title">Brief the next research run</h2>
        <div><label class="lbl" for="focus">What should it look for?</label>
          <textarea id="focus" rows="3" maxlength="2000" placeholder="e.g. Halloween decor that's cute rather than scary: porch-sign printables, kids' shirts, mugs for teachers"></textarea></div>
        <div><span class="lbl">Product types</span><div class="chips" id="types">${d.types.map((t) => `<button class="chip" data-t="${esc(t.key)}">${esc(t.label)}</button>`).join("")}</div>
          <div class="muted small" style="margin-top:6px">Pick none to let it choose.</div></div>
        <div class="row"><label class="lbl" for="count" style="margin:0">How many ideas</label>
          <select id="count">${[3, 6, 10, 15, 20].map((n) => `<option ${n === 6 ? "selected" : ""}>${n}</option>`).join("")}</select>
          <span class="muted small">${left} of ${d.weekly.cap} left this week</span></div>
        <div class="row"><button class="btn primary" id="queue-brief">Queue for the next run</button>
          <button class="btn" id="cancel-edit" hidden>Cancel editing</button></div>
      </div>
      <div class="explain"><b style="color:var(--ink)">How briefs work</b><ol>
        <li>Your brief waits for the next research run: <b style="color:var(--ink)">${esc(until(d.next_run))}</b>.</li>
        <li>That run works only on your brief, makes up to the number of ideas you chose, and is then done with it. It doesn't carry over to later runs.</li>
        <li>The ideas appear below and in Products, with the market research behind them.</li>
        <li>With no brief waiting, the Researcher works through its usual niche list.</li></ol>
        ${d.run_now_available ? "" : `<p class="small" style="margin:12px 0 0">Want it sooner? “Run now” works once RAILWAY_API_TOKEN and RAILWAY_SERVICE_IDS are set in Railway.</p>`}</div>
    </div>
    <div class="section-title"><h2>Past runs</h2></div>
    <div class="runs" id="runs"></div>`;
  const chosen = new Set();
  $$("#types .chip").forEach((c) => (c.onclick = () => { chosen.has(c.dataset.t) ? chosen.delete(c.dataset.t) : chosen.add(c.dataset.t); c.classList.toggle("on"); }));
  let editing = null;
  const resetForm = () => { editing = null; $("#focus").value = ""; chosen.clear(); $$("#types .chip").forEach((c) => c.classList.remove("on"));
    $("#count").value = "6"; $("#form-title").textContent = "Brief the next research run"; $("#queue-brief").textContent = "Queue for the next run"; $("#cancel-edit").hidden = true; };
  $("#cancel-edit").onclick = resetForm;
  $("#queue-brief").onclick = async () => {
    const focus = $("#focus").value.trim();
    if (focus.length < 3) return toast("Describe what the run should look for.", true);
    const body = { focus, product_types: [...chosen], idea_count: +$("#count").value };
    try {
      await (editing ? send("PATCH", `/api/dashboard/research/briefs/${editing}`, body) : send("POST", "/api/dashboard/research/briefs", body));
      toast(editing ? "Brief updated." : "Queued. The next research run will work on it.");
      await renderResearch(view); refreshOverview().catch(() => {});
    } catch (e) { toast(e.message, true); }
  };

  $("#queue").innerHTML = d.queued.map((b, i) => `
    <div class="panel qbrief" data-id="${b.id}">
      <div><div class="meta" style="margin-bottom:6px"><span class="st ${esc(b.status)}">${b.status === "running" ? "Researching now" : i === 0 ? "Up next" : `In line (#${i + 1})`}</span>
        <span>${b.status === "running" ? "started " + esc(ago(b.started_at)) : i === 0 ? `used by the research run ${esc(until(d.next_run))}` : `used by a later run, after the ${i === 1 ? "brief above" : `${i} briefs above`}`}</span></div>
        <div class="focus">${esc(b.focus)}</div>
        <div class="muted small" style="margin-top:6px">${b.idea_count} ideas · ${b.product_types.length ? b.product_types.map((t) => esc(d.types.find((x) => x.key === t)?.label || t)).join(", ") : "any product type"}${b.note ? ` · ${esc(b.note)}` : ""}</div></div>
      <div class="acts">${b.status === "queued" ? `
        ${i === 0 && d.run_now_available ? `<button class="btn small primary" data-a="run">Run now</button>` : ""}
        <button class="btn small" data-a="edit">Edit</button><button class="btn small danger" data-a="cancel">Cancel</button>` : ""}</div>
    </div>`).join("");
  $$("#queue .qbrief").forEach((el) => {
    const b = d.queued.find((x) => x.id === +el.dataset.id);
    $$("[data-a]", el).forEach((btn) => (btn.onclick = async () => {
      if (btn.dataset.a === "edit") {
        editing = b.id; $("#focus").value = b.focus; $("#count").value = String(b.idea_count);
        chosen.clear(); b.product_types.forEach((t) => chosen.add(t));
        $$("#types .chip").forEach((c) => c.classList.toggle("on", chosen.has(c.dataset.t)));
        $("#form-title").textContent = "Edit this brief"; $("#queue-brief").textContent = "Save changes"; $("#cancel-edit").hidden = false;
        $("#focus").focus(); return;
      }
      if (btn.dataset.a === "cancel") {
        if (!(await confirmBox({ title: "Cancel this brief?", body: "The next run will use the next brief in line, or the usual niche list.", ok: "Cancel brief", danger: true }))) return;
        try { await send("DELETE", `/api/dashboard/research/briefs/${b.id}`); toast("Brief cancelled."); await renderResearch(view); refreshOverview().catch(() => {}); } catch (e) { toast(e.message, true); }
      }
      if (btn.dataset.a === "run") {
        try { await send("POST", "/api/dashboard/research/run-now"); toast("Research run started. Ideas appear here in a few minutes."); } catch (e) { toast(e.message, true); }
      }
    }));
  });

  $("#runs").innerHTML = d.runs.map((r) => `
    <div class="panel run">
      <div class="run-head"><h2>${esc(when(r.started_at))}</h2>
        <span class="meta"><span class="st ${r.status === "success" ? "done" : r.status === "running" ? "running" : "failed"}">${r.status === "success" ? "Done" : r.status === "running" ? "Running" : r.status === "cap_reached" ? "Stopped at the weekly limit" : "Problem"}</span>
          <span>${plural(r.ideas.length, "idea")} · ${money(r.cost_usd)}</span></span></div>
      ${r.brief ? `<div class="run-brief"><b>Your brief:</b> ${esc(r.brief.focus)}${r.brief.note ? `<div class="muted small">${esc(r.brief.note)}</div>` : ""}</div>`
        : `<div class="run-brief standard">No brief: worked through the usual niche list.</div>`}
      ${r.errors.length && r.status !== "success" ? `<p class="small" style="color:var(--bad)">${esc(r.errors[0])}</p>` : ""}
      ${r.ideas.length ? `<div class="mini">${r.ideas.map((p) => `<button data-id="${p.id}"><span class="th" style="${thumb(p)}"></span><span><span class="tt">${esc(p.title)}</span><span class="st ${esc(p.state)}" style="font-size:11px">${esc(p.stage)}</span></span></button>`).join("")}</div>` : ""}
      ${r.markets.length ? `<details class="market"><summary>Market research behind it (${plural(r.markets.length, "niche")})</summary>
        ${r.markets.map((m) => `<div class="mkt"><h3>${esc(m.niche)}</h3>${marketBlock(m)}</div>`).join("")}</details>` : ""}
    </div>`).join("") || `<div class="panel pad muted">No research runs yet. The first one runs ${esc(until(d.next_run))}.</div>`;
  $$("#runs .mini button").forEach((b) => (b.onclick = () => (location.hash = `product/${b.dataset.id}`)));
}

/* ================================================================ ORDERS */
async function renderOrders(view) {
  const d = await api("/api/print/orders?view=all");
  const c = d.counts || {};
  const LABEL = { new: "New", needs_review: "Check first", ready: "Ready to print", printing: "Printing", shipped: "Shipped", cancelled: "Cancelled" };
  const addr = (a) => [a.name, [a.city, a.state].filter(Boolean).join(", ")].filter(Boolean).join(" · ");
  const rows = (list) => list.map((o) => `
    <div class="panel order" data-id="${o.id}">
      <span class="th" style="${o.proof_url ? `background-image:url('${esc(o.proof_url)}')` : ""}"></span>
      <div><div class="meta"><span class="st ${o.status === "shipped" ? "done" : o.status === "needs_review" ? "failed" : "queued"}">${esc(LABEL[o.status] || o.status)}</span>
          <span>Order #${esc(o.receipt_id)} · ${esc(ago(o.created_at))}</span></div>
        <b>${esc(o.item)}</b> <span class="muted">${[o.size, o.color, `×${o.quantity}`].filter(Boolean).map(esc).join(" · ")}</span>
        ${Object.keys(o.personalization || {}).length ? `<div class="small">Prints: <b>${Object.values(o.personalization).map(esc).join(" · ")}</b>${o.personalization_raw ? ` <span class="muted">(buyer typed “${esc(o.personalization_raw)}”)</span>` : ""}</div>` : ""}
        ${o.review_reason ? `<div class="small" style="color:var(--warn)">${esc(o.review_reason)}</div>` : ""}
        <div class="muted small">${esc(addr(o.ship_to || {}))}${o.tracking_code ? ` · ${esc((o.carrier || "").toUpperCase())} ${esc(o.tracking_code)}` : ""}</div></div>
      <div class="acts">${o.status === "needs_review" ? `<button class="btn small" data-a="edit">Fix text</button> <button class="btn small primary" data-a="ok">Print as is</button>` : ""}</div>
    </div>`).join("");
  const open = d.orders.filter((o) => !["shipped", "cancelled"].includes(o.status));
  const done = d.orders.filter((o) => ["shipped", "cancelled"].includes(o.status));
  view.innerHTML = `
    <div class="page-head"><div><h1>Orders</h1><p>Shirts, sweatshirts, hoodies and mugs printed by the print shop. Digital downloads are delivered by Etsy automatically.</p></div>
      <a class="btn" href="/print" target="_blank" rel="noopener">Print shop's view ↗</a></div>
    <div class="flow" style="margin:0 0 8px">
      ${[["needs_review", "To check"], ["ready", "Ready to print"], ["printing", "Printing"], ["shipped", "Shipped"]].map(([k, l]) => `<button disabled><span class="n">${c[k] || 0}</span><span class="l">${l}</span></button>`).join("")}
      <button disabled><span class="n">${money(d.ledger.owed_unpaid)}</span><span class="l">Owed to the shop</span></button></div>
    <div class="section-title"><h2>In progress</h2></div>
    <div class="orders">${rows(open) || `<div class="panel pad muted">No open orders. When someone buys a shirt or mug, it shows up here and in the print shop's queue within the hour.</div>`}</div>
    ${done.length ? `<div class="section-title"><h2>Shipped</h2></div><div class="orders">${rows(done.slice(0, 30))}</div>` : ""}
    <div class="modal" id="fix" hidden><div class="modal-card"><h2>What should be printed?</h2><p>Use exactly what the buyer meant. The print file is redone when you save.</p>
      <div id="fix-fields" style="display:grid;gap:10px;margin-bottom:16px"></div>
      <div class="row"><button class="btn" id="fix-cancel">Cancel</button><button class="btn primary" id="fix-save">Save</button></div></div></div>`;
  $$(".order [data-a]").forEach((b) => (b.onclick = async () => {
    const o = d.orders.find((x) => x.id === +b.closest(".order").dataset.id);
    if (b.dataset.a === "ok") {
      try { await send("POST", `/api/print/orders/${o.id}/status`, { status: "ready" }); toast("Sent to the print shop."); renderOrders(view); refreshOverview().catch(() => {}); }
      catch (e) { toast(e.message, true); }
      return;
    }
    $("#fix-fields").innerHTML = o.fields.map((f) => `<label class="small"><b>${esc(f.label || f.key)}</b> (max ${esc(f.max_chars)})
      <input type="text" data-key="${esc(f.key)}" value="${esc((o.personalization || {})[f.key] || "")}" style="width:100%;margin-top:4px"></label>`).join("");
    $("#fix").hidden = false;
    $("#fix-cancel").onclick = () => ($("#fix").hidden = true);
    $("#fix-save").onclick = async () => {
      const values = Object.fromEntries($$("#fix-fields input").map((i) => [i.dataset.key, i.value]));
      try {
        const r = await send("POST", `/api/print/orders/${o.id}/personalization`, { values });
        $("#fix").hidden = true;
        toast(r.problems.length ? r.problems.join("; ") : "Fixed. It's ready to print.", !!r.problems.length);
        renderOrders(view); refreshOverview().catch(() => {});
      } catch (e) { toast(e.message, true); }
    };
  }));
}

/* ================================================================ TEAM */
async function renderTeam(view) {
  const [v, o] = await Promise.all([api("/api/village/state"), refreshOverview()]);
  const nextFor = (name) => o.schedule.find((s) => s.key === (["qa", "lister"].includes(name) ? "creator" : name));
  view.innerHTML = `
    <div class="page-head"><div><h1>Team</h1><p>Six agents, each with one job. Click one to see its recent runs and any problems.</p></div></div>
    <div class="team">${v.agents.map((a) => {
      const last = a.last_run, nx = nextFor(a.name);
      return `<div class="panel agent" data-name="${esc(a.name)}" tabindex="0" role="button">
        <div class="who"><span class="av" style="background:${AGENT_COLOR[a.name] || "#ccc"}">${esc(a.title[0])}</span>
          <div><b>${esc(a.title)}</b><div class="muted small">${esc(a.role)}</div></div>
          <span class="state ${esc(a.state)}">${esc(STATE_WORD[a.state] || a.state)}</span></div>
        <div class="small">${esc(a.state_reason)}${a.current_item ? `: <b>${esc(a.current_item)}</b>` : ""}</div>
        <div class="muted small">${last ? `Last run ${esc(ago(last.ended_at || last.started_at))}: ${plural(last.items, "item")}, ${money(last.cost_usd)}${last.errors.length ? `, <span style="color:var(--bad)">${plural(last.errors.length, "problem")}</span>` : ""}` : "Hasn't run yet"}${nx ? ` · next ${esc(until(nx.at))}` : ""}</div>
      </div>`; }).join("")}</div>
    <div class="section-title"><h2>Everything that happened</h2></div>
    <div class="panel pad"><ul class="feed">${activityHTML(o.activity, 60)}</ul></div>`;
  $$(".agent").forEach((el) => { el.onclick = () => openAgent(el.dataset.name); el.onkeydown = (e) => { if (e.key === "Enter") openAgent(el.dataset.name); }; });
}
async function openAgent(name) {
  const a = await api(`/api/village/agent/${name}`);
  const pill = (s) => `<span class="st ${s === "success" ? "done" : s === "running" ? "running" : "failed"}">${esc(s === "success" ? "Done" : s === "cap_reached" ? "Hit a limit" : s)}</span>`;
  $("#detail").innerHTML = `
    <div class="dhead"><div class="meta"><span class="state ${esc(a.state)}" style="margin:0">${esc(STATE_WORD[a.state] || a.state)}</span><span>${esc(a.schedule)}</span></div>
      <h1><span style="display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;font-size:18px;vertical-align:middle;margin-right:8px;color:#0e1016;background:${AGENT_COLOR[name] || "#ccc"}">${esc(a.title[0])}</span>${esc(a.title)}</h1>
      <p class="muted" style="margin:0">${esc(a.role)}</p>
      <div class="dactions"><button class="btn" id="run-now" ${a.run_now_available ? "" : "disabled"}>Run now</button></div>
      ${a.run_now_available ? "" : `<p class="muted small" style="margin:0">“Run now” needs RAILWAY_API_TOKEN and RAILWAY_SERVICE_IDS on the web service.</p>`}</div>
    <div class="sec"><h2>Right now</h2><p>${esc(a.state_reason)}${a.current_item ? `: <b>${esc(a.current_item)}</b>` : ""}</p>
      <p class="muted small">Today: ${plural(a.runs_today, "run")}, ${plural(a.items_today, "item")}, ${money(a.cost_today)} · ${a.input_count} waiting for it</p></div>
    <div class="sec"><h2>Recent runs</h2><ul class="timeline">${a.runs.map((r) => `<li>${pill(r.status)} <time>${esc(when(r.started_at))}</time>${plural(r.items, "item")} · ${money(r.cost_usd)}
      ${r.errors.length ? `<pre class="spec">${esc(r.errors.join("\n"))}</pre>` : ""}</li>`).join("") || "<li class='muted'>No runs yet.</li>"}</ul></div>`;
  $("#run-now").onclick = async (e) => {
    e.target.disabled = true;
    try { const r = await fetch(`/api/village/run/${name}`, { method: "POST" }); toast(r.ok ? `${a.title} started.` : `Couldn't start it (${r.status}).`, !r.ok); }
    catch { toast("Couldn't start it.", true); }
  };
  openDrawer(`agent/${name}`);
}

/* ---------------------------------------------------------------- boot */
route();
