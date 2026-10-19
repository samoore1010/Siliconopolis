"use strict";
// Owner dashboard: plain-English view of the agents' work and the products they made.

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null || v === "" ? "—" : "$" + Number(v).toFixed(2));
const STAGE_KEYS = { brief: "Ideas", created: "Built", approved: "Approved", drafted: "Etsy drafts", published: "Live", rejected: "Rejected" };

async function api(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function ago(iso) {
  if (!iso) return "";
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");

/* ---------------- tabs ---------------- */
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  if (name === "products") loadProducts();
  if (name === "research") loadResearch();
  history.replaceState(null, "", "#" + name);
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ---------------- overview ---------------- */
async function loadOverview() {
  const d = await api("/api/dashboard/overview");
  $("#env").innerHTML = [
    `<span class="pill">${esc(d.settings.app_env)}</span>`,
    `<span class="pill">Etsy: ${esc(d.settings.etsy)}</span>`,
    `<span class="pill">Images: ${esc(d.settings.images)}</span>`,
  ].join("");

  $("#attention").innerHTML = d.attention.length
    ? d.attention.map((a) => `<div class="alert ${esc(a.level)}"><b>${a.level === "action" ? "Your turn" : a.level === "warn" ? "Heads up" : "Note"}</b><span>${esc(a.text)}</span></div>`).join("")
    : `<div class="alert info"><b>All clear</b><span>Nothing needs you right now.</span></div>`;

  $("#stages").innerHTML = d.stages.map((s) => `
    <button class="stage ${esc(s.state)}" data-state="${esc(s.state)}">
      <div class="n">${s.count}</div><div class="l">${esc(s.label)}</div><div class="d">${esc(s.help)}</div>
    </button>`).join("");
  document.querySelectorAll(".stage").forEach((b) => b.addEventListener("click", () => { state.filter = b.dataset.state; showTab("products"); }));

  $("#activity").innerHTML = d.activity.length ? d.activity.map((a) => {
    const cls = a.kind === "error" ? "err" : a.to_state === "approved" ? "ok" : a.to_state === "rejected" ? "err" : "";
    const text = esc(a.text);
    const body = a.product_id ? `<a data-product="${a.product_id}">${text}</a>` : text;
    return `<li><time title="${esc(when(a.ts))}">${esc(ago(a.ts))}</time><span class="${cls}">${body}</span></li>`;
  }).join("") : `<li><span class="hint">No activity yet.</span></li>`;
  document.querySelectorAll("#activity a[data-product]").forEach((a) => a.addEventListener("click", () => openProduct(a.dataset.product)));

  const pct = (a, b) => Math.min(100, b ? (a / b) * 100 : 0);
  $("#spend").innerHTML = `
    <span>AI spend today</span><span class="v">${money(d.spend.today)} / ${money(d.spend.daily_limit)}</span>
    <div class="bar"><i style="width:${pct(d.spend.today, d.spend.daily_limit)}%"></i></div>
    <span>Last 7 days</span><span class="v">${money(d.spend.week)}</span>
    <span>All time</span><span class="v">${money(d.spend.total)}</span>
    <span>Ideas this week</span><span class="v">${d.limits.ideas_this_week} / ${d.limits.ideas_cap}</span>
    <div class="bar"><i style="width:${pct(d.limits.ideas_this_week, d.limits.ideas_cap)}%"></i></div>
    <span>Etsy drafts today</span><span class="v">${d.limits.drafts_today} / ${d.limits.drafts_cap}</span>`;

  const names = { researcher: "Researcher", creator: "Creator", qa: "QA", lister: "Lister", analyst: "Analyst" };
  const roles = { researcher: "studies the market, proposes ideas", creator: "designs the files and images", qa: "reviews every product", lister: "writes the Etsy listing", analyst: "reads sales, guides research" };
  $("#agents").innerHTML = Object.keys(names).map((k) => {
    const r = d.last_runs[k];
    const st = !r ? `<span class="hint">not run yet</span>`
      : r.status === "failed" ? `<span class="status-bad">failed</span>`
      : r.status === "cap_reached" ? `<span class="status-warn">stopped at limit</span>`
      : r.status === "running" ? `<span class="status-warn">running…</span>`
      : `<span class="status-ok">ok</span>`;
    const detail = r ? `${ago(r.at)} · ${r.items} item${r.items === 1 ? "" : "s"} · ${money(r.cost_usd)}` : "";
    return `<tr><td><b>${names[k]}</b><br><span class="hint small">${roles[k]}</span></td><td>${st}<br><span class="hint small">${esc(detail)}</span></td></tr>`;
  }).join("");
}

/* ---------------- products ---------------- */
const state = { filter: null, q: "", products: [] };

async function loadProducts() {
  const d = await api("/api/dashboard/products");
  state.products = d.products;
  renderProducts();
}

function renderProducts() {
  const counts = {};
  state.products.forEach((p) => (counts[p.state] = (counts[p.state] || 0) + 1));
  $("#stage-chips").innerHTML = [`<button class="chip ${state.filter ? "" : "on"}" data-s="">All (${state.products.length})</button>`]
    .concat(Object.entries(STAGE_KEYS).map(([k, l]) => `<button class="chip ${state.filter === k ? "on" : ""}" data-s="${k}">${l} (${counts[k] || 0})</button>`)).join("");
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { state.filter = c.dataset.s || null; renderProducts(); }));

  const q = state.q.toLowerCase();
  const list = state.products.filter((p) => (!state.filter || p.state === state.filter)
    && (!q || (p.title + " " + (p.niche || "") + " " + p.one_liner).toLowerCase().includes(q)));
  $("#cards-empty").hidden = list.length > 0;
  $("#cards").innerHTML = list.map((p) => `
    <button class="card" data-id="${p.id}">
      <div class="img" style="${p.thumb ? `background-image:url('${esc(p.thumb)}')` : ""}">${p.thumb ? "" : "Not built yet"}</div>
      <div class="body">
        <div class="meta"><span class="st ${esc(p.state)}">${esc(p.stage)}</span><span>${esc(p.type_label)}</span>${p.price ? `<span>· ${money(p.price)}</span>` : ""}</div>
        <div class="t">${esc(p.title)}</div>
        <div class="o">${esc(p.one_liner)}</div>
        ${p.top_reason ? `<div class="why-bad">QA: ${esc(p.top_reason)}</div>` : ""}
        <div class="meta">${esc(p.niche || "")} · ${esc(ago(p.updated_at))}</div>
      </div>
    </button>`).join("");
  document.querySelectorAll(".card").forEach((c) => c.addEventListener("click", () => openProduct(c.dataset.id)));
}
$("#search").addEventListener("input", (e) => { state.q = e.target.value; renderProducts(); });

/* ---------------- product detail ---------------- */
function scoreColor(v) { return v >= 8 ? "var(--good)" : v >= 6 ? "var(--warn)" : "var(--bad)"; }
const SCORE_LABEL = { quality: "Visual quality", legibility: "Legibility", artifacts: "No glitches", matches_brief: "Matches the idea" };

function marketBlock(m, ref) {
  if (!m && !ref) return `<p class="hint">No market analysis was saved for this product (it was researched before this feature existed).</p>`;
  if (!m) return `<p>${esc(ref.summary || "")}</p>`;
  const a = m.analysis || {};
  const list = (xs) => (xs && xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="hint">—</p>`);
  return `
    <p>${esc(a.summary || "")}</p>
    <div class="lbl">Demand</div><p>${esc(a.demand || "—")}</p>
    <div class="lbl">Competition</div><p>${esc(a.competition || "—")}</p>
    <div class="lbl">Typical prices</div><p>${esc(a.price_range || "—")}</p>
    <div class="lbl">What buyers want</div>${list(a.what_buyers_want)}
    <div class="lbl">Gaps this shop can fill</div>${list(a.gaps)}
    <div class="lbl">Evidence</div><p class="hint">${esc(a.data_basis || "")} ${m.etsy_data ? `(${m.signals_count} Etsy listings sampled)` : "(no live Etsy data yet)"}</p>`;
}

async function openProduct(id) {
  const p = await api(`/api/dashboard/product/${id}`);
  const w = p.why || {};
  const imgs = p.images || [];
  const qa = p.qa;
  const design = p.design || {};
  $("#detail").innerHTML = `
    <div class="meta"><span class="st ${esc(p.state)}">${esc(p.stage)}</span><span>${esc(p.type_label)}</span><span>· ${esc(p.niche || "")}</span><span>· cost so far ${money(p.cost_usd)}</span></div>
    <h1>${esc(p.title)}</h1>
    <div class="dgrid">
      <div class="gallery">
        ${imgs.length ? `<div class="main" style="background-image:url('${esc(imgs[0].full)}')" data-full="${esc(imgs[0].full)}"></div>
          <div class="thumbs">${imgs.map((im, i) => `<button class="${i ? "" : "on"}" style="background-image:url('${esc(im.thumb)}')" data-full="${esc(im.full)}" aria-label="Image ${i + 1}"></button>`).join("")}</div>`
        : `<div class="sec"><p class="hint">No images yet: this product hasn't been built.</p>${p.errors.length ? `<p class="status-bad">Last build error: ${esc(p.errors[0])}</p>` : ""}</div>`}
      </div>
      <div>
        <div class="sec" style="margin-top:0">
          <h2>Why the Researcher picked this</h2>
          <p>${esc(w.rationale || "—")}</p>
          <div class="lbl">The idea</div><p>${esc(w.concept || "—")}</p>
          <div class="lbl">Who it's for</div><p>${esc(w.audience || "—")}</p>
          <div class="lbl">Target price</div><p>${money(w.target_price_usd)}</p>
          <div class="lbl">Search keywords</div><div class="tags">${(w.keywords || []).map((k) => `<span class="pill">${esc(k)}</span>`).join("") || "—"}</div>
        </div>
        ${qa ? `<div class="sec">
          <h2>QA review</h2>
          <div class="verdict ${qa.passed ? "ok" : "no"}">${qa.passed ? "Passed: approved for Etsy" : "Rejected: not good enough to list"}</div>
          ${Object.entries(qa.scores || {}).map(([k, v]) => `<div class="score"><span>${esc(SCORE_LABEL[k] || k)}</span><span class="track"><i style="width:${v * 10}%;background:${scoreColor(v)}"></i></span><b>${esc(v)}</b></div>`).join("")}
          ${qa.reasons.length ? `<div class="lbl">QA's notes</div><ol class="reasons">${qa.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ol>` : ""}
          ${qa.ip_concerns.length ? `<div class="lbl">Copyright / trademark concerns</div><ul>${qa.ip_concerns.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        </div>` : `<div class="sec"><h2>QA review</h2><p class="hint">Not reviewed yet.</p></div>`}
      </div>
    </div>
    <div class="sec"><h2>Market analysis for “${esc(p.niche || "")}”</h2>${marketBlock(p.market, p.market_summary)}</div>
    <div class="dgrid">
      <div class="sec">
        <h2>What was built</h2>
        ${p.files.length ? `<div class="files">${p.files.map((f) => `<a href="${esc(f.url)}" target="_blank" rel="noopener"><span class="kind">${esc(f.kind)}</span>${esc(f.name)}</a>`).join("")}</div>` : `<p class="hint">No files yet.</p>`}
        ${design.palette ? `<div class="lbl">Colours</div><div class="swatches">${design.palette.map((c) => `<span title="${esc(c)}" style="background:${esc(c)}"></span>`).join("")}</div>` : ""}
        ${design.image_prompt && p.product_type === "wall_art" ? `<div class="lbl">Image prompt</div><p class="hint">${esc(design.image_prompt)}</p>` : ""}
        ${design.planner_spec || design.spreadsheet_spec ? `<details><summary class="lbl" style="cursor:pointer">Design plan</summary><pre class="spec">${esc(JSON.stringify(design.planner_spec || design.spreadsheet_spec, null, 2))}</pre></details>` : ""}
      </div>
      <div class="sec">
        <h2>Etsy listing</h2>
        ${p.listing && p.listing.title ? `<p><b>${esc(p.listing.title)}</b></p><p>${money(p.listing.price)}</p>
          <div class="tags">${(p.listing.tags || []).map((t) => `<span class="pill">${esc(t)}</span>`).join("")}</div>
          <details><summary class="lbl" style="cursor:pointer">Description</summary><p style="white-space:pre-wrap">${esc(p.listing.description)}</p></details>
          ${p.listing.etsy_url ? `<p><a href="${esc(p.listing.etsy_url)}" target="_blank" rel="noopener">Open on Etsy ↗</a></p>` : ""}`
        : `<p class="hint">The listing is written after QA approves the product and Etsy is connected.</p>`}
      </div>
    </div>
    <div class="sec"><h2>Timeline</h2><ul class="timeline">${(p.timeline || []).map((t) => `<li><time>${esc(when(t.ts))}</time>${esc(t.text)}</li>`).join("") || "<li class='hint'>—</li>"}</ul></div>`;

  const main = $(".gallery .main");
  document.querySelectorAll(".gallery .thumbs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".gallery .thumbs button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    main.style.backgroundImage = `url('${b.dataset.full}')`;
    main.dataset.full = b.dataset.full;
  }));
  if (main) main.addEventListener("click", () => { $("#lightbox img").src = main.dataset.full; $("#lightbox").hidden = false; });
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
  $("#drawer").scrollTop = 0;
  history.replaceState(null, "", `#product-${id}`);
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
  history.replaceState(null, "", "#" + ($(".tabs button.on")?.dataset.tab || "overview"));
}
$("#close").addEventListener("click", closeDrawer);
$("#scrim").addEventListener("click", closeDrawer);
$("#lightbox").addEventListener("click", () => ($("#lightbox").hidden = true));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#lightbox").hidden = true; closeDrawer(); } });

/* ---------------- research ---------------- */
async function loadResearch() {
  const d = await api("/api/dashboard/research");
  $("#market-empty").hidden = d.market.length > 0;
  $("#market").innerHTML = d.market.map((m) => {
    const a = m.analysis || {};
    const list = (xs) => (xs && xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "<p class='hint'>—</p>");
    const sig = (m.signals_sample || []);
    return `<div class="mkt">
      <div class="meta">${esc(when(m.created_at))} · ${m.etsy_data ? `${m.signals_count} Etsy listings sampled` : "no live Etsy data yet (keywords only)"} · ${m.ideas_in_niche} idea(s) in this niche</div>
      <h3>${esc(m.niche)}</h3>
      <p>${esc(a.summary || "")}</p>
      <div class="grid3">
        <div><div class="k">Demand</div><p>${esc(a.demand || "—")}</p></div>
        <div><div class="k">Competition</div><p>${esc(a.competition || "—")}</p></div>
        <div><div class="k">Typical prices</div><p>${esc(a.price_range || "—")}</p></div>
      </div>
      <div class="grid3">
        <div><div class="k">What buyers want</div>${list(a.what_buyers_want)}</div>
        <div><div class="k">Gaps to fill</div>${list(a.gaps)}</div>
        <div><div class="k">Keywords studied</div><div class="tags">${(m.keywords || []).map((k) => `<span class="pill">${esc(k)}</span>`).join("")}</div></div>
      </div>
      ${sig.length ? `<details><summary class="k" style="cursor:pointer">Top competitor listings used as demand signals</summary>
        <table class="sig"><tr><th>Listing</th><th>Price</th><th>Favorites</th><th>Views</th></tr>
        ${sig.map((s) => `<tr><td>${esc(s.title)}</td><td class="n">${money(s.price_usd)}</td><td class="n">${esc(s.num_favorers)}</td><td class="n">${esc(s.views)}</td></tr>`).join("")}</table></details>` : ""}
      <div class="basis">${esc(a.data_basis || "")} <a data-niche="${esc(m.niche)}" style="cursor:pointer">See the products from this niche →</a></div>
    </div>`;
  }).join("");
  document.querySelectorAll("#market a[data-niche]").forEach((a) => a.addEventListener("click", () => {
    state.filter = null; state.q = a.dataset.niche; $("#search").value = a.dataset.niche; showTab("products");
  }));
}

/* ---------------- boot ---------------- */
async function boot() {
  try { await loadOverview(); } catch (e) { $("#attention").innerHTML = `<div class="alert warn"><b>Error</b><span>${esc(e.message)}</span></div>`; }
  const h = location.hash.slice(1);
  if (h.startsWith("product-")) { showTab("products"); openProduct(h.slice(8)); }
  else if (["products", "research"].includes(h)) showTab(h);
  setInterval(() => { if ($("#tab-overview").classList.contains("on")) loadOverview().catch(() => {}); }, 30000);
}
boot();
