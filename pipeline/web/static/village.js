/* Siliconopolis village — plain Canvas, no dependencies. All sprites below are original pixel art
   defined as character grids (see ASSETS.md). Everything drawn reflects /api/village/state + /events. */
(() => {
"use strict";
const $ = (id) => document.getElementById(id);
const canvas = $("scene"), ctx = canvas.getContext("2d");
ctx.imageSmoothingEnabled = false;
const W = canvas.width, H = canvas.height;
const POLL_MS = 4000;

/* ---------------------------------------------------------------- isometric helpers */
const TW = 84, TH = 42, ORIGIN = { x: W / 2, y: 70 };
const iso = (gx, gy) => ({ x: ORIGIN.x + (gx - gy) * (TW / 2), y: ORIGIN.y + (gx + gy) * (TH / 2) });
const GRID = 10;

/* ---------------------------------------------------------------- palette & sprites (original) */
const PAL = {
  ".": null, k: "#1b1b24", w: "#f3efe6", s: "#f0c8a0", S: "#c9956a", h: "#3b2a22", H: "#d9b36b", g: "#6d6d7a",
  a: "#7cc4ff", b: "#6fd38a", c: "#f3c34f", d: "#ff8fa3", e: "#b48cff", o: "#2a2f3f", O: "#3a4157",
};
// 12x16 character; letters: k outline, s skin, h hair, X shirt (replaced per agent), w eyes, o pants
const BODY = [
  "....hhhh....",
  "...hhhhhh...",
  "...hsssshh..",
  "...swswss...",
  "...ssssss...",
  "....ssss....",
  "...XXXXXX...",
  "..sXXXXXXs..",
  "..sXXXXXXs..",
  "...XXXXXX...",
  "...oooooo...",
  "...oo..oo...",
  "...oo..oo...",
  "...kk..kk...",
  "...kk..kk...",
  "............",
];
const BODY_TYPING = BODY.map((r, i) => (i === 7 ? "..XXXXXXXX.." : i === 8 ? ".sXXXXXXXXs." : r));
const AGENTS = {
  researcher: { shirt: "a", hair: "#3b2a22", label: "Researcher", gx: 0.8, gy: 0.8, color: "#7cc4ff" },
  creator:    { shirt: "d", hair: "#8a4b2a", label: "Creator",    gx: 6.5, gy: 0.6, color: "#ff8fa3" },
  qa:         { shirt: "c", hair: "#1b1b24", label: "QA",         gx: 8.6, gy: 4.4, color: "#f3c34f" },
  lister:     { shirt: "b", hair: "#d9b36b", label: "Lister",     gx: 6.3, gy: 7.6, color: "#6fd38a" },
  analyst:    { shirt: "e", hair: "#5a3d6b", label: "Analyst",    gx: 0.6, gy: 6.0, color: "#b48cff" },
};
const BRAIN = { gx: 4.6, gy: 4.6 };
const OUTBOX = { gx: 8.8, gy: 7.6 };
const BIN = { gx: 9.2, gy: 2.0 };
const ORDER = ["researcher", "creator", "qa", "lister", "analyst"];

function drawSprite(rows, x, y, scale, overrides) {
  for (let r = 0; r < rows.length; r++) for (let c = 0; c < rows[r].length; c++) {
    const ch = rows[r][c]; if (ch === ".") continue;
    let col = overrides[ch] || PAL[ch]; if (!col) continue;
    ctx.fillStyle = col; ctx.fillRect(x + c * scale, y + r * scale, scale, scale);
  }
}

/* ---------------------------------------------------------------- scene primitives */
function floorTile(gx, gy, shade) {
  const p = iso(gx, gy); ctx.beginPath();
  ctx.moveTo(p.x, p.y); ctx.lineTo(p.x + TW / 2, p.y + TH / 2); ctx.lineTo(p.x, p.y + TH); ctx.lineTo(p.x - TW / 2, p.y + TH / 2);
  ctx.closePath(); ctx.fillStyle = shade; ctx.fill();
}
function isoBox(gx, gy, w, d, h, top, left, right) {
  // box with footprint w x d tiles and height h px, anchored at tile (gx,gy)
  const a = iso(gx, gy), b = iso(gx + w, gy), c = iso(gx + w, gy + d), e = iso(gx, gy + d);
  ctx.fillStyle = top; ctx.beginPath(); ctx.moveTo(a.x, a.y - h); ctx.lineTo(b.x, b.y - h); ctx.lineTo(c.x, c.y - h); ctx.lineTo(e.x, e.y - h); ctx.closePath(); ctx.fill();
  ctx.fillStyle = left; ctx.beginPath(); ctx.moveTo(e.x, e.y - h); ctx.lineTo(c.x, c.y - h); ctx.lineTo(c.x, c.y); ctx.lineTo(e.x, e.y); ctx.closePath(); ctx.fill();
  ctx.fillStyle = right; ctx.beginPath(); ctx.moveTo(c.x, c.y - h); ctx.lineTo(b.x, b.y - h); ctx.lineTo(b.x, b.y); ctx.lineTo(c.x, c.y); ctx.closePath(); ctx.fill();
}
function text(str, x, y, size, color, align = "center", mono = false) {
  ctx.font = `${mono ? "" : "600 "}${size}px ${mono ? "ui-monospace,Menlo,monospace" : "system-ui,sans-serif"}`;
  ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = "middle"; ctx.fillText(str, x, y);
}
function bubble(x, y, str, color = "#f3efe6", bg = "#1b2030", max = 34) {
  if (str.length > max) str = str.slice(0, max - 1) + "…";
  ctx.font = "12px system-ui,sans-serif"; const w = ctx.measureText(str).width + 16, h = 22;
  ctx.fillStyle = bg; roundRect(x - w / 2, y - h, w, h, 6); ctx.fill();
  ctx.beginPath(); ctx.moveTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.lineTo(x, y + 6); ctx.closePath(); ctx.fill();
  text(str, x, y - h / 2, 12, color);
}
function roundRect(x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }

/* ---------------------------------------------------------------- state */
let state = null, lastEventId = 0, tokens = [], feed = [], hit = [], t0 = performance.now();
const STATE_COLORS = { working: "#6fd38a", idle: "#8b93a7", sleeping: "#5a6480", waiting_human: "#7cc4ff", cap_reached: "#ff6b6b", error: "#ff6b6b", stale_lock: "#f3c34f" };
const STATE_LABEL = { working: "working", idle: "idle", sleeping: "sleeping", waiting_human: "needs you", cap_reached: "cap reached", error: "error", stale_lock: "stale lock" };

/* ---------------------------------------------------------------- drawing */
function drawFloor() {
  for (let gx = 0; gx < GRID; gx++) for (let gy = 0; gy < GRID; gy++) floorTile(gx, gy, (gx + gy) % 2 ? "#141826" : "#171c2c");
  // walls (back edges)
  ctx.strokeStyle = "#2a3148"; ctx.lineWidth = 2; ctx.beginPath();
  const a = iso(0, 0), b = iso(GRID, 0), c = iso(0, GRID); ctx.moveTo(c.x, c.y); ctx.lineTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
}
function drawPath() {
  // conveyor path in pipeline order, drawn as dashed tiles-centre lines
  const pts = pathPoints();
  ctx.setLineDash([6, 8]); ctx.lineDashOffset = -((performance.now() - t0) / 40) % 14;
  ctx.strokeStyle = "#2e3650"; ctx.lineWidth = 3; ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); ctx.stroke(); ctx.setLineDash([]);
}
function center(o) { const p = iso(o.gx + 0.5, o.gy + 0.5); return { x: p.x, y: p.y + TH / 2 }; }
function pathPoints() { return [AGENTS.researcher, AGENTS.creator, AGENTS.qa, AGENTS.lister, OUTBOX, AGENTS.analyst, AGENTS.researcher].map(center); }

function drawBrain() {
  const q = state ? state.queue : {}; const total = Object.values(q).reduce((a, b) => a + b, 0);
  const pulse = 1 + Math.sin((performance.now() - t0) / 500) * 0.04;
  const base = center(BRAIN);
  isoBox(BRAIN.gx, BRAIN.gy, 1.4, 1.4, 14, "#242b40", "#1a2033", "#1f2539");
  ctx.save(); ctx.translate(base.x, base.y - 46); ctx.scale(pulse, pulse);
  ctx.fillStyle = "#8ab4f8"; ctx.beginPath(); ctx.ellipse(0, 0, 26, 20, 0, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#b48cff"; ctx.beginPath(); ctx.ellipse(-8, -4, 12, 10, 0, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#0e1016"; ctx.fillRect(-1, -16, 2, 32);
  ctx.restore();
  text("THE BRAIN", base.x, base.y - 82, 11, "#8b93a7");
  text(`${total} products`, base.x, base.y - 70, 11, "#e8eaf0");
  hit.push({ kind: "brain", x: base.x - 70, y: base.y - 95, w: 140, h: 110 });
}

function drawDesk(name, a, i) {
  const now = performance.now() - t0;
  const info = state ? state.agents.find((x) => x.name === name) : null;
  const st = info ? info.state : "idle";
  const base = center(a);
  // desk
  isoBox(a.gx, a.gy + 0.55, 1.2, 0.5, 18, "#4a3b30", "#352a22", "#3e3128");
  // monitor
  const mx = base.x + 14, my = base.y - 36;
  ctx.fillStyle = "#1b1b24"; ctx.fillRect(mx - 12, my - 10, 24, 16);
  ctx.fillStyle = st === "working" ? (Math.floor(now / 250) % 2 ? "#6fd38a" : "#8ab4f8") : st === "sleeping" ? "#20242f" : "#2f3a55";
  ctx.fillRect(mx - 10, my - 8, 20, 12); ctx.fillStyle = "#1b1b24"; ctx.fillRect(mx - 2, my + 6, 4, 4);
  // desk color stripe
  ctx.fillStyle = a.color; ctx.fillRect(base.x - 34, base.y - 2, 68, 3);
  // character
  const bob = st === "working" ? 0 : Math.round(Math.sin(now / 700 + i) * 1);
  const rows = st === "working" && Math.floor(now / 180) % 2 ? BODY_TYPING : BODY;
  const sx = base.x - 18 - 10, sy = base.y - 70 + bob;
  drawSprite(rows, sx, sy, 3, { X: PAL[a.shirt], h: a.hair });
  // nameplate
  ctx.fillStyle = "#0b0d13"; roundRect(base.x - 44, base.y + 10, 88, 16, 4); ctx.fill();
  text(a.label, base.x, base.y + 18, 11, "#e8eaf0");
  // status pill
  ctx.fillStyle = STATE_COLORS[st]; roundRect(base.x - 36, base.y + 28, 72, 12, 6); ctx.fill();
  text(STATE_LABEL[st], base.x, base.y + 34, 9, "#0b0d13");
  // stat card
  if (info) {
    text(`${info.runs_today} run${info.runs_today === 1 ? "" : "s"} · ${info.items_today} items · $${info.cost_today.toFixed(2)}`, base.x, base.y + 48, 9, "#8b93a7", "center", true);
    if (info.input_count) text(`${info.input_count} waiting`, base.x, base.y + 59, 9, a.color, "center", true);
  }
  // overlays
  const ox = base.x - 10, oy = sy - 14;
  if (st === "working") bubble(base.x - 10, sy - 8, info?.current_item || "working…");
  else if (st === "sleeping") text("z z z", ox + 24, oy + Math.sin(now / 600) * 3, 12, "#5a6480");
  else if (st === "waiting_human") bubble(base.x - 10, sy - 8, "waiting for your review", "#7cc4ff");
  else if (st === "cap_reached") { ctx.fillStyle = "#ff6b6b"; octagon(ox + 24, oy, 9); text("STOP", ox + 24, oy, 7, "#fff"); }
  else if (st === "error") { ctx.fillStyle = "#ff6b6b"; roundRect(ox + 16, oy - 10, 16, 20, 3); ctx.fill(); text("!", ox + 24, oy, 14, "#fff"); }
  else if (st === "stale_lock") { ctx.fillStyle = "#f3c34f"; ctx.beginPath(); ctx.arc(ox + 24, oy, 8, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = "#0b0d13"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(ox + 24, oy); ctx.lineTo(ox + 24, oy - 5); ctx.lineTo(ox + 28, oy - 2); ctx.stroke(); }
  hit.push({ kind: "agent", name, x: base.x - 50, y: sy - 20, w: 100, h: 110 });
}
function octagon(x, y, r) { ctx.beginPath(); for (let i = 0; i < 8; i++) { const a = Math.PI / 8 + (i * Math.PI) / 4; ctx.lineTo(x + r * Math.cos(a), y + r * Math.sin(a)); } ctx.closePath(); ctx.fill(); }

function drawOutbox() {
  const base = center(OUTBOX); const n = state ? state.outbox.awaiting_approval : 0;
  isoBox(OUTBOX.gx, OUTBOX.gy, 0.9, 0.9, 22, "#2f6e4a", "#214d34", "#28603f");
  ctx.fillStyle = "#e8eaf0"; ctx.fillRect(base.x - 10, base.y - 36, 20, 12); ctx.fillStyle = "#2f6e4a"; ctx.fillRect(base.x - 8, base.y - 34, 16, 8);
  text("Etsy outbox", base.x, base.y + 14, 10, "#e8eaf0");
  if (n) { const label = `${n} waiting for approval`; ctx.font = "600 9px system-ui,sans-serif"; const bw = ctx.measureText(label).width + 14;
    ctx.fillStyle = "#7cc4ff"; roundRect(base.x - bw / 2, base.y + 22, bw, 13, 6); ctx.fill(); text(label, base.x, base.y + 28, 9, "#0b0d13"); }
  else text("no drafts waiting", base.x, base.y + 28, 9, "#8b93a7");
  hit.push({ kind: "outbox", x: base.x - 46, y: base.y - 50, w: 92, h: 90 });
}
function drawBin() {
  const base = center(BIN); const n = state ? state.reject_bin.count : 0;
  isoBox(BIN.gx, BIN.gy, 0.6, 0.6, 16, "#5a2626", "#3f1b1b", "#4a2020");
  text("reject bin", base.x, base.y + 12, 10, "#e8eaf0"); text(`${n}`, base.x, base.y - 24, 13, "#ff6b6b");
  hit.push({ kind: "rejects", x: base.x - 32, y: base.y - 40, w: 64, h: 64 });
}

/* tokens: little paper sheets travelling along a path segment */
function spawnToken(fromKey, toKey, color, label) {
  const from = center(POS[fromKey]), to = center(POS[toKey]);
  tokens.push({ from, to, t: 0, color, label, speed: 1 / 1800 });
  if (tokens.length > 40) tokens.shift();
}
const POS = { ...AGENTS, outbox: OUTBOX, bin: BIN, brain: BRAIN };
function drawTokens(dt) {
  tokens = tokens.filter((k) => k.t < 1);
  for (const k of tokens) {
    k.t += dt * k.speed; const e = k.t < 0.5 ? 2 * k.t * k.t : -1 + (4 - 2 * k.t) * k.t;
    const x = k.from.x + (k.to.x - k.from.x) * e, y = k.from.y + (k.to.y - k.from.y) * e - Math.sin(k.t * Math.PI) * 28 - 30;
    ctx.fillStyle = "#f3efe6"; ctx.fillRect(x - 6, y - 8, 12, 16); ctx.fillStyle = k.color; ctx.fillRect(x - 6, y - 8, 12, 3);
    ctx.fillStyle = "#8b93a7"; ctx.fillRect(x - 4, y - 2, 8, 1); ctx.fillRect(x - 4, y + 1, 8, 1); ctx.fillRect(x - 4, y + 4, 6, 1);
  }
}

let last = performance.now();
function frame(now) {
  const dt = now - last; last = now; hit = [];
  ctx.clearRect(0, 0, W, H);
  drawFloor(); drawPath(); drawBin(); drawBrain();
  // draw back-to-front by gy for overlap correctness
  const items = ORDER.map((n, i) => ({ n, i, gy: AGENTS[n].gy + AGENTS[n].gx })).sort((a, b) => a.gy - b.gy);
  for (const it of items) drawDesk(it.n, AGENTS[it.n], it.i);
  drawOutbox(); drawTokens(dt);
  if (!state) text("connecting…", W / 2, H - 20, 12, "#8b93a7");
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

/* ---------------------------------------------------------------- data */
async function getJSON(url) { const r = await fetch(url, { headers: { accept: "application/json" } }); if (!r.ok) throw new Error(`${r.status} ${url}`); return r.json(); }

function routeForEvent(ev) {
  const m = { "brief>created": ["researcher", "creator"], "created>approved": ["creator", "qa"], "created>rejected": ["qa", "bin"],
              "approved>drafted": ["qa", "lister"], "approved>rejected": ["lister", "bin"], "drafted>published": ["lister", "outbox"] };
  if (ev.kind === "transition") {
    const r = m[`${ev.from_state}>${ev.to_state}`]; if (r) return r;
    if (ev.to_state === "brief") return ["brain", "researcher"];
  }
  if (ev.kind === "run_finished" && ev.agent === "lister") return ["lister", "outbox"];
  if (ev.kind === "run_finished" && ev.agent === "analyst") return ["analyst", "researcher"];
  if (ev.kind === "run_finished" && ev.agent === "researcher") return ["researcher", "brain"];
  return null;
}
function colorForEvent(ev) { return ev.kind === "error" || ev.kind === "cap_reached" ? "#ff6b6b" : (AGENTS[ev.agent]?.color || "#8b93a7"); }

function pushFeed(ev, animate) {
  feed.unshift(ev); if (feed.length > 80) feed.pop();
  if (animate) { const r = routeForEvent(ev); if (r) spawnToken(r[0], r[1], colorForEvent(ev)); }
}
function renderFeed() {
  $("feed").innerHTML = feed.map((e) => `<li class="kind-${e.kind}" data-pid="${e.product_id ?? ""}" data-agent="${e.agent}">
    <time>${new Date(e.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
    <div><span class="who">${e.agent}</span> ${esc(e.message)}${e.cost_usd ? ` <span class="pill">$${e.cost_usd.toFixed(3)}</span>` : ""}</div></li>`).join("");
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function renderSidebar() {
  const c = state.caps, env = state.env;
  const spendPct = c.max_daily_spend_usd ? Math.min(100, (c.spend_today / c.max_daily_spend_usd) * 100) : 0;
  const draftPct = c.daily_draft_cap ? Math.min(100, (c.drafts_today / c.daily_draft_cap) * 100) : 0;
  const briefPct = c.weekly_brief_cap ? Math.min(100, (c.briefs_this_week / c.weekly_brief_cap) * 100) : 0;
  setBar("spend", spendPct, `$${c.spend_today.toFixed(2)} / $${c.max_daily_spend_usd}`);
  setBar("draft", draftPct, `${c.drafts_today} / ${c.daily_draft_cap}`);
  setBar("brief", briefPct, `${c.briefs_this_week} / ${c.weekly_brief_cap}`);
  $("queue").innerHTML = ["brief", "created", "approved", "rejected", "drafted", "published"].map((k) => `<div class="q"><b>${state.queue[k] ?? 0}</b><span>${k}</span></div>`).join("");
  $("badges").innerHTML = [
    `<span class="badge ${env.app_env === "production" ? "bad" : env.app_env === "staging" ? "warn" : ""}">${env.app_env}</span>`,
    `<span class="badge ${env.mode === "live" ? "warn" : "on"}">mode: ${env.mode}</span>`,
    `<span class="badge ${env.dry_run ? "on" : ""}">dry run: ${env.dry_run ? "on" : "off"}</span>`,
    `<span class="badge">llm: ${env.llm_provider}</span>`, `<span class="badge">etsy: ${env.etsy_adapter}</span>`,
    state.stale_locks ? `<span class="badge warn">${state.stale_locks} stale lock(s)</span>` : "",
  ].join("");
  $("hint").textContent = `updated ${new Date(state.generated_at).toLocaleTimeString()} · click a desk, the Brain, the outbox or the bin`;
  $("legend").innerHTML = Object.keys(STATE_LABEL).map((k) => `<span><i style="background:${STATE_COLORS[k]}"></i>${STATE_LABEL[k]}</span>`).join("");
}
function setBar(id, pct, label) { const b = $(`${id}-bar`); b.style.width = pct + "%"; b.className = pct >= 100 ? "bad" : pct >= 75 ? "warn" : ""; $(`${id}-label`).textContent = label; }

async function poll(first) {
  try {
    const [s, ev] = await Promise.all([getJSON("/api/village/state"), getJSON(`/api/village/events?since=${lastEventId}&limit=${first ? 60 : 100}`)]);
    state = s;
    for (const e of ev.events) pushFeed(e, !first);
    if (ev.events.length) lastEventId = ev.last_id;
    renderSidebar(); renderFeed();
  } catch (e) { $("hint").textContent = `offline: ${e.message}`; }
}
poll(true); setInterval(() => poll(false), POLL_MS);

/* ---------------------------------------------------------------- interactions */
canvas.addEventListener("click", (e) => {
  const r = canvas.getBoundingClientRect(); const x = ((e.clientX - r.left) / r.width) * W, y = ((e.clientY - r.top) / r.height) * H;
  const h = [...hit].reverse().find((b) => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
  if (!h) return;
  if (h.kind === "agent") openAgent(h.name); else if (h.kind === "outbox") openOutbox(); else if (h.kind === "rejects") openRejects(); else if (h.kind === "brain") openBrain();
});
$("feed").addEventListener("click", (e) => { const li = e.target.closest("li"); if (!li) return; if (li.dataset.pid) openProduct(+li.dataset.pid); else openAgent(li.dataset.agent); });
$("panel-close").onclick = closePanel; $("scrim").onclick = closePanel;
function openPanel(title, html) { $("panel-title").textContent = title; $("panel-body").innerHTML = html; $("panel").classList.remove("hidden"); $("scrim").classList.remove("hidden"); }
function closePanel() { $("panel").classList.add("hidden"); $("scrim").classList.add("hidden"); }
const kv = (obj) => `<div class="kv">${Object.entries(obj).map(([k, v]) => `<div>${esc(k)}</div><div>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div>`;
const pill = (s) => `<span class="pill ${s === "success" ? "ok" : s === "failed" || s === "cap_reached" ? "bad" : ""}">${esc(s)}</span>`;

async function openAgent(name) {
  const a = await getJSON(`/api/village/agent/${name}`);
  const runs = a.runs.map((r) => `<li>${pill(r.status)} #${r.id} · ${new Date(r.started_at).toLocaleString()} · ${r.items} items · $${r.cost_usd.toFixed(3)}${r.errors.length ? `<pre>${esc(r.errors.join("\n"))}</pre>` : ""}</li>`).join("") || "<li class='note'>no runs yet</li>";
  openPanel(`${a.title} — ${STATE_LABEL[a.state]}`, `
    <p class="note">${esc(a.role)} · ${esc(a.schedule)} · prompt: <code>${esc(a.prompt_file)}</code></p>
    <p>${esc(a.state_reason)}${a.current_item ? ` — <b>${esc(a.current_item)}</b>` : ""}</p>
    ${kv({ "runs today": a.runs_today, "items today": a.items_today, "cost today": `$${a.cost_today.toFixed(3)}`, "waiting in queue": `${a.input_count} (${a.input_state || "n/a"})`, "locked rows": a.locked_count })}
    <h4>Run now</h4>
    <button class="btn" id="run-now" ${a.run_now_available ? "" : "disabled"}>Run ${esc(a.title)} now</button>
    <p class="note">${a.run_now_available ? "Triggers the Railway cron service; caps, locks and draft-only apply as usual." : "Disabled: set RAILWAY_API_TOKEN and RAILWAY_SERVICE_IDS on the web service. No button can ever publish a listing."}</p>
    <h4>Recent runs</h4><ul class="list">${runs}</ul>`);
  const btn = $("run-now"); if (btn && !btn.disabled) btn.onclick = async () => { btn.disabled = true; btn.textContent = "triggering…"; try { const r = await fetch(`/api/village/run/${name}`, { method: "POST" }); btn.textContent = r.ok ? "triggered ✓" : `failed (${r.status})`; } catch { btn.textContent = "failed"; } };
}
async function openProduct(id) {
  const p = await getJSON(`/api/village/product/${id}`);
  openPanel(`#${p.id} ${p.title}`, `
    <p>${pill(p.state)} <span class="pill">${esc(p.product_type)}</span> <span class="pill">${esc(p.niche)}</span> <span class="pill">$${p.cost_usd.toFixed(3)}</span>${p.etsy_listing_id ? ` <span class="pill">etsy ${esc(p.etsy_listing_id)}</span>` : ""}</p>
    ${p.images.length ? `<div class="thumbs">${p.images.map((u) => `<img src="${u}" alt="listing image">`).join("")}</div>` : ""}
    <h4>Brief</h4><pre>${esc(JSON.stringify(p.brief, null, 1))}</pre>
    ${p.creator_plan?.image_prompt || p.creator_plan?.planner_spec || p.creator_plan?.spreadsheet_spec ? `<h4>Creator plan</h4><pre>${esc(JSON.stringify(p.creator_plan, null, 1))}</pre>` : ""}
    ${p.qa_result ? `<h4>QA result</h4><pre>${esc(JSON.stringify(p.qa_result, null, 1))}</pre>` : ""}
    ${p.listing && Object.keys(p.listing).length ? `<h4>Listing copy</h4><pre>${esc(JSON.stringify(p.listing, null, 1))}</pre>` : ""}
    ${p.files.length ? `<h4>Files</h4><p>${p.files.map(esc).join(", ")}</p>` : ""}
    <h4>History</h4><ul class="list">${p.history.map((e) => `<li>${new Date(e.ts).toLocaleString()} · <b>${esc(e.agent)}</b> ${esc(e.message)}</li>`).join("") || "<li class='note'>no events</li>"}</ul>`);
}
async function openOutbox() {
  const o = await getJSON("/api/village/outbox");
  openPanel(`Etsy outbox — ${o.drafts.length} draft(s)`, `
    <p class="note">Drafts wait here for <b>you</b> to review and publish in the Etsy app. The system never publishes.${o.mock_shop_state ? ` Mock shop state: <code>${esc(o.mock_shop_state)}</code> in storage.` : ""}</p>
    <ul class="list">${o.drafts.map((d) => `<li data-pid="${d.id}">${pill(d.state)} #${d.id} ${esc(d.title)} ${d.price ? `<span class="pill">$${d.price}</span>` : ""}${d.dry_run ? '<span class="pill warn">dry run</span>' : ""}
      ${d.url ? ` <a href="${esc(d.url)}" target="_blank" rel="noopener">open on Etsy ↗</a>` : d.mock ? ' <span class="pill">mock draft</span>' : ""}</li>`).join("") || "<li class='note'>nothing drafted yet</li>"}</ul>`);
  $("panel-body").querySelectorAll("li[data-pid]").forEach((li) => li.addEventListener("click", (e) => { if (e.target.tagName !== "A") openProduct(+li.dataset.pid); }));
}
async function openRejects() {
  const r = await getJSON("/api/village/rejects");
  openPanel(`Reject bin — ${r.rejects.length}`, `<ul class="list">${r.rejects.map((d) => `<li data-pid="${d.id}">#${d.id} ${esc(d.title)}<br><span class="note">${esc(d.reasons.join(" · "))}</span></li>`).join("") || "<li class='note'>nothing rejected</li>"}</ul>`);
  $("panel-body").querySelectorAll("li[data-pid]").forEach((li) => li.addEventListener("click", () => openProduct(+li.dataset.pid)));
}
function openBrain() {
  const q = state.queue;
  openPanel("The Brain — products queue", `<p class="note">Agents never talk to each other directly; every hand-off is a row in the <code>products</code> table changing state.</p>
    ${kv(q)}<h4>Pipeline</h4><pre>brief → created → approved | rejected
approved → drafted (Etsy draft) → published (by you) → retired</pre>`);
}
})();
