# Siliconopolis — Village UI Addendum (Milestone 5)

Add this to the repo next to the existing spec. Build on top of what exists; do not change agent logic, guardrails, or the draft-only rule.

## 1. Objective

Turn the web service into a live, game-style "village" where each agent is a pixel character at a desk, work visibly moves between desks, and the owner can see at a glance who is working, who is stuck, what it costs, and what is waiting for approval.

- It is the primary dashboard for the system, not decoration. Anything shown must reflect real database state.
- Off-the-shelf visualizers (Pixel Agents, pixtuoid, Claude Office) watch Claude Code terminals and **will not work** here, because our agents are Python cron jobs calling the Anthropic API. Build a custom view inside the existing FastAPI `web` service.

## 2. Scene layout

Top-down or isometric pixel office (pick one; isometric preferred, like the reference "Agents Office" style):

- **Five desks / pods**, one per agent: Researcher, Creator, QA, Lister, Analyst. Each has a character, nameplate, and a small stat card (runs today, items processed, cost today).
- **"The Brain"** in the center: represents the `products` queue. Shows counts by state (`brief`, `created`, `approved`, `rejected`, `drafted`, `published`).
- **Conveyor / paths** between desks in pipeline order: Researcher → Creator → QA → Lister → (Etsy outbox) → Analyst → back to Researcher. Products appear as small paper/box tokens that travel along the path when their state changes.
- **Etsy outbox / mailbox** after the Lister: shows drafted count with a "waiting for approval" badge (the human step).
- **Reject bin** next to QA: rejected count; clicking shows rejection reasons.
- **Right sidebar:** live task feed (newest first), spend meter vs `MAX_DAILY_SPEND_USD`, draft cap vs `DAILY_DRAFT_CAP`, environment / mode / dry-run badge.

## 3. Character states (derived from real data)

| State | Visual | Source |
|---|---|---|
| Working | typing animation, speech bubble with current item title | `runs` row with `started_at` set and `ended_at` null for that agent; locked `products` rows (`locked_by`) |
| Idle | sitting, occasional idle animation | no open run |
| Sleeping | "zzz" | no run in > 1.5× the agent's schedule interval and nothing in its input state |
| Waiting on human | "?" bubble over Lister / outbox | drafted products not yet published |
| Cap reached | stop sign | last run exit status = cap reached (exit 3) |
| Error | red "!" | last run `status = failed`; click shows `errors_json` |
| Stale lock | yellow clock | locked rows older than the stale-lock threshold |

## 4. Data needed

Most data exists. Add one table so movement can be animated:

**events** — `id`, `ts`, `agent`, `product_id` (nullable), `from_state`, `to_state`, `message`, `cost_usd`
- Write an event in the base agent on every state transition, run start, run end, cap hit, and error.
- Alembic migration required. Keep it append-only; add a retention job or cap (e.g. keep 30 days).

New read-only endpoints:
- `GET /api/village/state` — snapshot: agents (state, current item, today's counts/cost), queue counts, caps, spend, env badges
- `GET /api/village/events?since=<id>` — incremental events for animation
- `GET /api/village/agent/<name>` — last 20 runs, errors, prompt file name
- `GET /api/village/product/<id>` — brief, QA result, listing copy, image thumbnails, history

Use polling every 3–5 seconds (simplest on Railway) or SSE. No WebSockets required.

## 5. Interactions

- Click an agent: panel with recent runs, errors, cost, and which `prompts/<agent>.md` it uses.
- Click a product token or feed item: panel with its full history and thumbnails.
- Click the outbox: list of drafts awaiting review with links to the Etsy listing (live mode) or the mock shop state (mock mode).
- **Optional "Run now" button per agent:** triggers the corresponding Railway cron service through Railway's API. Disabled unless `RAILWAY_API_TOKEN` is set and the viewer is authenticated. Must respect caps and locking exactly like a scheduled run.
- **No button may publish a listing.** Draft-only stays absolute.

## 6. Tech and assets

- Frontend served by the existing FastAPI app at `/village` (make `/` redirect there). One self-contained page or a small static bundle; no separate frontend service.
- Rendering: PixiJS or Phaser (pinned CDN or vendored), or plain Canvas. Pixel-perfect scaling, works on desktop and phone.
- **Art must be original or explicitly CC0/permissively licensed.** Do not copy sprites or tilesets from Pixel Agents, pixtuoid, Claude Office, or any paid tileset. Record each asset's source and license in `pipeline/web/static/ASSETS.md`. Simple generated or hand-made pixel sprites are fine.
- Each agent gets a distinct character and desk color; keep a consistent palette and dark theme.

## 7. Security

The Railway web URL is public. Before exposing the village:
- Add authentication (HTTP basic auth via `VILLAGE_USER` / `VILLAGE_PASSWORD` env vars is enough for v1) to `/village`, `/api/village/*`, `/status`, and the OAuth start route. `/health` stays public for Railway health checks.
- Never expose tokens, keys, or full prompts' secret contents in API responses.

## 8. Demo mode

Add `python -m pipeline demo` that, in mock mode only, seeds the database and runs agents in a loop with short sleeps so the village can be watched animating locally and on staging. It must refuse to run when `APP_ENV=production`.

## 9. Testing

- Unit tests for state derivation (working / idle / sleeping / error / cap / stale lock) from fixture `runs` and `products` rows.
- API tests for each `/api/village/*` endpoint, including auth required.
- Test that events are written on every transition.
- Test that no village endpoint or button can change a listing to active.

## 10. Deliverables

1. `events` table + migration + event writes in the base agent
2. Village API endpoints with auth
3. `/village` page with the scene, character states, moving product tokens, sidebar, and click panels
4. `demo` command
5. `ASSETS.md` with asset licenses
6. Update `README.md` and `DEPLOY.md` (new env vars: `VILLAGE_USER`, `VILLAGE_PASSWORD`, optional `RAILWAY_API_TOKEN`)

Work in that order and summarize at the end what is real data vs placeholder.
