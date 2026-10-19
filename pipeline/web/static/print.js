"use strict";
// Print queue for the print shop: what to print, the file, where to ship it.
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => "$" + Number(v || 0).toFixed(2);
const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
const LABEL = { new: "New", needs_review: "Check first", ready: "Ready to print", printing: "Printing", shipped: "Shipped", cancelled: "Cancelled" };
let view = "open", data = null;

async function send(method, path, body) {
  const r = await fetch(path, { method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Siliconopolis": "1" }, body: JSON.stringify(body || {}) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : `${r.status}`);
  return d;
}

function address(a) {
  return [a.name, a.first_line, a.second_line, [a.city, a.state, a.zip].filter(Boolean).join(", ").replace(/, (\d)/, " $1"), a.country_iso]
    .filter(Boolean).join("\n");
}

function card(o) {
  const pers = Object.entries(o.personalization || {});
  const fields = o.fields || [];
  const reviewing = o.status === "needs_review";
  return `<article class="order ${esc(o.status)}" data-id="${o.id}">
    <button class="proof" style="${o.proof_url ? `background-image:url('${esc(o.proof_url)}')` : ""}" data-proof="${esc(o.proof_url || "")}" aria-label="View proof"></button>
    <div>
      <div class="meta"><span class="st ${esc(o.status)}">${esc(LABEL[o.status] || o.status)}</span>
        <span>Order #${esc(o.receipt_id)}</span><span>· ${esc(when(o.created_at))}</span></div>
      <h2>${esc(o.item)}</h2>
      <div class="chips">
        ${o.size ? `<span class="chip">Size ${esc(o.size)}</span>` : ""}
        ${o.color ? `<span class="chip"><span class="sw" style="background:${esc(o.color_hex || "#ccc")}"></span>${esc(o.color)}</span>` : ""}
        <span class="chip">Qty ${esc(o.quantity)}</span>
        <span class="chip">${esc(o.method)} · ${esc(o.print_area)}</span>
      </div>
      ${pers.length ? `<div class="printtext">Prints: ${pers.map(([k, v]) => `<b>${esc(v)}</b>`).join(" · ")}</div>` : ""}
      ${o.personalization_raw ? `<div class="note">Buyer typed: “${esc(o.personalization_raw)}”</div>` : ""}
      ${o.buyer_note ? `<div class="note">Note from buyer: ${esc(o.buyer_note)}</div>` : ""}
      ${reviewing ? `<div class="review"><b>Check before printing:</b> ${esc(o.review_reason || "")}</div>
        ${fields.length ? `<div class="fields">${fields.map((f) => `<label>${esc(f.label || f.key)} (max ${esc(f.max_chars)})
          <input data-key="${esc(f.key)}" value="${esc((o.personalization || {})[f.key] || "")}" maxlength="${Number(f.max_chars || 40) + 10}"></label>`).join("")}</div>` : ""}` : ""}
      <div class="addr">${esc(address(o.ship_to || {}))}</div>
      ${o.tracking_code ? `<div class="note">Tracking: ${esc((o.carrier || "").toUpperCase())} ${esc(o.tracking_code)} ${o.etsy_synced ? "· sent to Etsy ✓" : "· sending to Etsy…"}</div>` : ""}
      ${o.notes ? `<div class="note">${esc(o.notes)}</div>` : ""}
      <div class="actions">
        ${o.print_url ? `<a class="btn" href="${esc(o.print_url)}">⬇ Print file</a>` : ""}
        <button class="btn" data-act="copy">Copy address</button>
        ${reviewing && fields.length ? `<button class="btn" data-act="fix">Save &amp; redo file</button>` : ""}
        ${reviewing ? `<button class="btn primary" data-act="ready">Looks fine, print it</button>` : ""}
        ${o.status === "ready" ? `<button class="btn primary" data-act="printing">Start printing</button>` : ""}
        ${["ready", "printing"].includes(o.status) ? `<button class="btn good" data-act="ship">Mark shipped…</button>` : ""}
        ${o.status !== "shipped" && o.status !== "cancelled" ? `<button class="btn" data-act="cancel">Cancel</button>` : ""}
        <span class="msg"></span>
      </div>
      <div class="ship" hidden>
        <select aria-label="Carrier">${(data.carriers || []).map((c) => `<option value="${c}">${c.toUpperCase()}</option>`).join("")}</select>
        <input placeholder="Tracking number" aria-label="Tracking number">
        <button class="btn good" data-act="shipped">Save</button>
      </div>
    </div>
  </article>`;
}

async function load() {
  data = await (await fetch(`/api/print/orders?view=${view}`, { credentials: "same-origin" })).json();
  const c = data.counts || {};
  const open = (c.new || 0) + (c.needs_review || 0) + (c.ready || 0) + (c.printing || 0);
  $("#n-open").textContent = open ? `(${open})` : "";
  $("#n-shipped").textContent = c.shipped ? `(${c.shipped})` : "";
  const L = data.ledger || {};
  $("#ledger").innerHTML = `
    <div class="stat"><b>${c.ready || 0}</b>ready to print</div>
    <div class="stat"><b>${c.needs_review || 0}</b>need a check</div>
    <div class="stat"><b>${c.printing || 0}</b>printing</div>
    <div class="stat"><b>${money(L.owed_unpaid)}</b>owed to the shop (unpaid)</div>
    ${data.role === "owner" && L.owed_unpaid > 0 ? `<button class="btn" id="paid">Mark ${money(L.owed_unpaid)} paid</button>` : ""}`;
  $("#paid")?.addEventListener("click", async (e) => { e.target.disabled = true; await send("POST", "/api/print/ledger/paid"); load(); });
  $("#orders").innerHTML = data.orders.map(card).join("");
  $("#empty").hidden = data.orders.length > 0;
  wire();
}

function wire() {
  document.querySelectorAll(".order").forEach((el) => {
    const id = el.dataset.id, msg = $(".msg", el);
    const say = (t, bad) => { msg.textContent = t; msg.className = "msg " + (bad ? "bad" : "ok"); };
    const o = data.orders.find((x) => String(x.id) === id);
    el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
      const act = b.dataset.act;
      try {
        if (act === "copy") { await navigator.clipboard.writeText(address(o.ship_to || {})); return say("Address copied"); }
        if (act === "ship") { $(".ship", el).hidden = false; $(".ship input", el).focus(); return; }
        if (act === "fix") {
          const values = {}; el.querySelectorAll("[data-key]").forEach((i) => (values[i.dataset.key] = i.value));
          const r = await send("POST", `/api/print/orders/${id}/personalization`, { values });
          return r.problems.length ? (say(r.problems.join("; "), true), setTimeout(load, 1500)) : load();
        }
        if (act === "cancel" && !b.dataset.armed) { b.dataset.armed = 1; b.textContent = "Tap again to cancel"; return; }
        const body = { status: act === "cancel" ? "cancelled" : act };
        if (act === "shipped") { body.carrier = $(".ship select", el).value; body.tracking_code = $(".ship input", el).value; }
        await send("POST", `/api/print/orders/${id}/status`, body);
        load();
      } catch (e) { say(e.message, true); }
    }));
    $(".proof", el).addEventListener("click", (e) => { const u = e.currentTarget.dataset.proof; if (u) { $("#lightbox img").src = u; $("#lightbox").hidden = false; } });
  });
}

document.querySelectorAll(".views button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".views button").forEach((x) => x.classList.toggle("on", x === b));
  view = b.dataset.view; load();
}));
$("#lightbox").addEventListener("click", () => ($("#lightbox").hidden = true));
load();
setInterval(() => { if (!document.querySelector(".ship:not([hidden])") && !document.activeElement?.matches("input")) load(); }, 60000);
