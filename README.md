# Siliconopolis — Etsy Agent Pipeline

A near-autonomous, seven-agent system that researches, creates, polishes, quality-checks and
**drafts** digital products (and in-house printed shirts/mugs) on an Etsy shop. One human
checkpoint: the owner reviews draft listings in the Etsy app and publishes the good ones. The
system never publishes on its own.

```
Researcher ──► [brief] ──► Creator ──► [created] ──► QA ──► [approved | rejected]
                                          ▲                   │
                 you: "Polish it" ──► [editing] ──► Editor ───┘ (back through QA)
                                                              │
                                                              ▼
Analyst ◄── sales/stats ◄── Etsy ◄── [drafted] ◄────────── Lister
   │                           │
   └──► research_notes         └──► orders ──► Fulfiller ──► print shop (/print)
```

- **Researcher** (weekly) proposes ideas from niches, your run briefs and the Analyst's notes.
- **Creator** (daily) builds first drafts: Claude-designed planner pages, verified spreadsheets,
  AI wall art, shirt/mug prints, plus listing images.
- **Editor** (owner-triggered, starts within a minute) is a senior designer on a stronger model (Opus by
  default). You send it a built, approved or rejected product with an optional note; QA's
  improvement suggestions go along. It edits rather than rebuilds, keeps what works, raises the
  polish, and sends the result back through QA, which checks each requested change was made. It
  has its own queue (reorderable), Stop button, and a per-product cost cap. Nothing goes to the
  Editor automatically. The Creator's "start over with these changes" stays for a fresh take.
- **QA** reviews the real files and listing images, enforces IP safety, near-duplicate and
  sellability bars.
- **Lister** writes the copy and saves an Etsy **draft** (never publishes).
- **Analyst** (weekly) reads stats and sales back into research notes.
- **Fulfiller** (hourly) turns shirt/mug orders into print jobs for the family print shop.

Agents talk only through the `products` table (a queue keyed by `state`). Every agent run claims
rows with a compare-and-swap lock, does its work and records a `runs` row (counts, errors,
token/image cost). The web service runs them through an in-app worker (`pipeline/worker.py`): QA,
the Editor and the Lister start within seconds of work reaching their queue, the Researcher /
Creator / Analyst / Fulfiller keep their schedule, and every "Run now" button just queues a job.
Each agent is also runnable as a one-shot process (`python -m pipeline run <agent>`), so Railway
cron services remain an optional backstop.

Spec: [`etsy-agent-pipeline-instructions_1.md`](etsy-agent-pipeline-instructions_1.md) and the
village UI addendum [`village-addendum.md`](village-addendum.md). Deployment: [`DEPLOY.md`](DEPLOY.md).

## The app and the village

`/dashboard` (the root URL redirects there) is the owner's app: Home (what needs you, the live
office, schedule, spend), Products (every product with images, files, why it was chosen, the
market analysis, QA's verdict, your feedback; the Creator's build queue and the Editor's polish
queue, both reorderable with Stop), Research (brief the next run), Orders, Team.

`/village` (embedded on Home) is a live isometric pixel office rendered from real database state:
a desk with a character per agent, the Brain (the `products` queue) in the middle, a conveyor
path, an Etsy outbox for drafts waiting on you, a reject bin, and a sidebar with spend/draft/brief
meters and a live event feed. Characters show **working / idle / sleeping /
needs you / cap reached / error / stale lock**, derived from the `runs`, `products` and `events`
tables (`pipeline/web/village_state.py`). Click a desk, the Brain, the outbox, the bin or a feed
item for details. Protected by HTTP basic auth (`VILLAGE_USER` / `VILLAGE_PASSWORD`); `/health`
stays public. Art is original and procedural: see `pipeline/web/static/ASSETS.md`.

To watch it move locally: `python -m pipeline web` in one terminal, `python -m pipeline demo` in
another (mock mode only; refuses to run in production).

## Status

| Milestone | State |
|---|---|
| 1 — Skeleton (config, DB + Alembic, CLI, logging, `runs`, mock adapters) | done |
| 2 — End-to-end in mock mode (5 agents, `run-all`, `status`) | done |
| 2.5 — Railway staging (Dockerfile, IaC, S3 storage, `smoke`) | code done; deploy per `DEPLOY.md` |
| 3 — Live-ready (live Etsy adapter, OAuth helper + token table, `DRY_RUN`) | Etsy live client + OAuth + token table built, **unverified against live API**; image provider: OpenAI (`gpt-image-1.5`) |
| 4 — Printify / POD | interface + mock only |
| 5 — Village UI (events table, authenticated API, `/village` scene, `demo`) | done |
| 7 — Owner dashboard (`/dashboard`: products with images/files, why chosen, market analysis, QA verdicts, plain-English activity) | done |
| 6 — Real product design (Claude-designed HTML planners → Chromium PDFs, typed spreadsheets, AI lifestyle mockups) | done |
| 8 — In-house merch (shirts/mugs by the family print shop, `/print` queue, Fulfiller) | done |
| 9 — Owner controls (run briefs, build queue + Stop, rebuild with changes) and the Editor (owner-triggered polish on a stronger model, own queue, back through QA) | done |

### How products are made (Milestone 6)

- **Planners/trackers:** the Creator writes a design brief, then Claude designs every page as HTML/CSS
  (`prompts/creator_page.md`). Headless Chromium checks the layout at Letter and A4 (overflow, clipped
  text) and sends problems back to Claude to fix (up to 2 rounds), then renders print-ready PDFs.
  Rendering is offline: network blocked, scripts stripped.
- **Spreadsheets:** typed columns (date, currency, checkbox, formula…), totals and cross-sheet formulas;
  the listing preview is rendered from the same spec with sample rows.
- **Wall art:** generated by `IMAGE_PROVIDER=openai`; refused on Railway while the provider is `mock`.
- **Listing images:** real rendered product first, AI lifestyle scene (desk or wall, generated once per
  scene variant and cached in `scenes/`), page spread or detail, and a "What's included" card.
- A brief that fails creation twice is rejected instead of retried forever.

Everything external is behind an adapter with a mock implementation. Without `ANTHROPIC_API_KEY`
the LLM is mocked too, so `python -m pipeline run-all` works on a fresh clone with no credentials.

## Quick start (local, mock mode)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # defaults = mock mode, SQLite, local ./storage
alembic upgrade head            # create tables
python -m pipeline smoke        # connectivity checks
python -m pipeline run-all      # researcher -> creator -> qa -> lister -> analyst
python -m pipeline status
pytest
```

Add `ANTHROPIC_API_KEY=...` to `.env` to use real Claude calls (everything else stays mocked).
Mock Etsy drafts are written to `storage/mock_etsy/shop_state.json`; product files and listing
images to `storage/products/<id>-<slug>/`.

## CLI

| Command | What it does | Exit code |
|---|---|---|
| `python -m pipeline run <agent>` | one agent: `researcher`, `creator`, `editor`, `qa`, `lister`, `analyst`, `fulfiller` | 0 ok, 1 failed, 3 cap reached |
| `python -m pipeline run daily` | `creator -> qa -> lister` (the daily cron) | worst of the three |
| `python -m pipeline run-all` | every agent once, in pipeline order | worst |
| `python -m pipeline status [--json]` | counts by state, recent runs, spend | 0 |
| `python -m pipeline smoke [--json]` | DB, storage, Anthropic, Etsy, Printify (live only), image provider | 0 pass / 1 fail |
| `python -m pipeline web` | the app (`/dashboard`, `/village`, `/print`, APIs, `/status`, `/health`, Etsy OAuth) **and the in-app worker** | — |
| `python -m pipeline worker` | the worker loop alone (if you prefer a separate always-on process) | — |
| `python -m pipeline demo [--cycles N --sleep S --reset]` | mock-only loop that animates the village; refuses in production | 0 / 2 refused |
| `python -m pipeline migrate` | `alembic upgrade head` | 0 |
| `--dry-run` (global flag) | live reads, no external writes | — |

Every run is idempotent: rows are claimed per run (`locked_by`), released on success/failure, and
stale locks (default 120 min) are reclaimable, so "run now" at any time is safe.

## Configuration

All settings come from environment variables (or `.env` locally) via `pipeline/config.py`.
See [`.env.example`](.env.example) for the full list. Key ones:

| Variable | Purpose |
|---|---|
| `MODE` | `mock` (default) or `live`; `ETSY_MODE` / `PRINTIFY_MODE` override per adapter |
| `APP_ENV` | `local` / `staging` / `production`. Non-local **refuses** SQLite and local storage |
| `DRY_RUN` | `true` = live reads, skip all external writes |
| `DATABASE_URL` | Railway injects Postgres; `postgres://` is normalised to `postgresql+psycopg://` |
| `STORAGE_BACKEND` | `local` or `s3` (+ `S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`) |
| `MODEL_RESEARCH` / `MODEL_WORKER` / `MODEL_CHEAP` | Claude models per role (Researcher/Analyst, Creator/QA/Lister, order checks) |
| `MODEL_EDITOR`, `EDITOR_MAX_COST_PER_PRODUCT_USD` | the Editor's (stronger) model and its budget per polish |
| `QA_MIN_SCORE`, `QA_MIN_COMMERCIAL_SCORE` | QA pass bars (default 6 and 7 of 10); QA judges the product, and keeps remarks about the auto-generated listing images separate |
| `LLM_PROVIDER` | `anthropic` or `mock`; defaults to anthropic when a key is set |
| `DAILY_DRAFT_CAP`, `WEEKLY_BRIEF_CAP`, `MAX_DAILY_SPEND_USD` | hard caps; hitting one stops the run (exit 3) |
| `VILLAGE_USER`, `VILLAGE_PASSWORD` | basic auth for the dashboard, status and OAuth start routes (required off-local) |
| `WORKER_ENABLED`, `WORKER_POLL_SECONDS`, `WORKER_RETRY_MINUTES` | the in-app worker (on by default) |
| `RAILWAY_API_TOKEN`, `RAILWAY_SERVICE_IDS` | legacy "Run now" through Railway cron services, only when the worker is off |
| `PRINT_USER`, `PRINT_PASSWORD`, `ETSY_SHIPPING_PROFILE_ID`, `ETSY_PRODUCTION_PARTNER_ID` | in-house merch: the print shop's login and Etsy ids (see `DEPLOY.md`) |

Other config files: `niches.yaml` (seeded with three placeholder niches), `blocklist.txt` (IP safety
terms), `prompts/*.md` (one prompt per agent, editable without code changes).

## Guardrails

- **Draft-only.** Every Etsy write goes through `pipeline/adapters/etsy/guard.py`; the only allowed
  listing state is `draft`. `tests/test_draft_only.py` scans the source tree and fails if any code
  path could send `state=active`.
- **Caps.** Draft/day, briefs/week and spend/day are checked before work and before every LLM call.
- **IP safety.** `blocklist.txt` + built-in patterns ("in the style of", "official", ...) are checked
  on briefs, image prompts, titles, tags and descriptions. QA also rejects on any vision-flagged IP
  concern or any score below 6/10.
- **No copying.** The Researcher sees competitor titles/prices/tags only as demand signals; the
  prompt forbids copying and briefs must state their original elements.
- **No scraping.** Etsy is accessed via the Open API v3 only.
- **Disclosure.** The Lister appends an AI-use disclosure to every description programmatically.
- **Near-duplicates.** QA perceptual-hashes the primary image and rejects anything within distance 6
  of an approved/drafted/published product.
- **Secrets.** Never logged (`logging_setup.redact`), `.env` is git-ignored, Railway variables hold
  everything in hosted environments.

## Project layout

```
pipeline/
  __main__.py          CLI
  config.py            pydantic-settings
  logging_setup.py     JSON logs with agent/run context
  llm.py               Anthropic client + MockLLM + cost estimate
  prompts.py           loads prompts/*.md
  db/                  models, session, row locking
  storage/             local + S3 backends
  adapters/etsy        base, guard (draft-only), mock, live, tokens
  adapters/printify    base, mock, live stub (Phase 2)
  adapters/images      base, mock placeholder provider, OpenAI (gpt-image)
  agents/              researcher, creator, editor (subclasses creator: same production pipeline,
                       stronger model, edits an existing design), qa, lister, analyst, fulfiller
  generators/          render (HTML -> PDF/PNG via Chromium), xlsx (openpyxl + LibreOffice check),
                       mockups (listing images), merch (print mockups), office (LibreOffice), pdf
  guardrails/          blocklist, caps
  commands/            status, smoke, demo
  schedule.py          next-run times (mirrors the Railway cron schedules)
  web/                 FastAPI app, basic auth, dashboard API, village API + state, print queue API,
                       static dashboard / village / print pages
alembic/               migrations
prompts/               agent prompts
scripts/etsy_oauth.py  one-time OAuth PKCE helper
.railway/railway.ts    Railway Infrastructure as Code (services, crons, postgres, bucket)
Dockerfile             single image for every service
tests/                 unit, contract, draft-only, end-to-end (mock)
```

## Schedule — Phoenix is UTC-7 year-round

Kept by the in-app worker (`pipeline/schedule.py`); the cron lines are the optional Railway backstop.

| Service | Cron (UTC) | Phoenix | Command |
|---|---|---|---|
| `researcher` | `0 13 * * 1` | Mon 6:00 AM | `python -m pipeline run researcher` |
| `daily-pipeline` | `0 14 * * *` | daily 7:00 AM | `python -m pipeline run daily` |
| `analyst` | `0 3 * * 1` | Sun 8:00 PM | `python -m pipeline run analyst` |
| `fulfiller` | `0 * * * *` | hourly | `python -m pipeline run fulfiller` |
| `editor` | reacts to its queue | — | `python -m pipeline run editor` (optional cron: `30 * * * *`) |
| QA, Lister | react to their queues | — | part of `run daily` in the optional cron |
| `web` | always on | — | `python -m pipeline web` (pre-deploy: `alembic upgrade head`) |

## What is mocked / what the owner must supply

Mocked today: Etsy (search, taxonomy, drafts, uploads, receipts, stats), Printify, image generation,
and the LLM when no API key is present.

Owner to-do (see `TODO(owner)` markers in code):
1. `ANTHROPIC_API_KEY` (unlocks real agents immediately).
2. Etsy shop ID, app API key + shared secret, redirect URI → run `scripts/etsy_oauth.py`.
3. Verify the live Etsy endpoints/fields in `pipeline/adapters/etsy/live.py` against current docs,
   and confirm the production-attribute / AI-disclosure mapping in `pipeline/agents/lister.py`.
4. Image provider choice + key; implement it behind `pipeline/adapters/images/base.py`.
5. Real niches in `niches.yaml`; extend `blocklist.txt`.
6. Printify token + shop ID (Phase 2).
