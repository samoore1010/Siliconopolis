# Etsy Agent Pipeline — Build Instructions for Claude Code

## 1. Objective

Build a near-autonomous, multi-agent system that researches, creates, quality-checks, and lists products on an Etsy shop, with one human checkpoint.

- **Goal of v1:** prove the agent architecture works end to end and can run on a schedule with minimal intervention. Revenue is secondary at this stage; reliability, observability, and safety come first.
- **Human role:** the owner reviews Etsy **draft** listings and publishes the good ones (via the Etsy app). The system must never publish a listing to "active" on its own in v1.
- **Credentials are not available yet.** The owner will set up the Etsy shop, Etsy developer app, Printify account, and image-generation API key later. Build everything now against **mock adapters**, so real credentials can be dropped in via environment variables with no code changes.

## 2. Product scope

**Phase 1 (build now): digital downloads on Etsy**
- Printable wall art (PNG/PDF at print resolutions)
- Simple printables: planners, trackers, checklists (PDF), and spreadsheet templates (XLSX)
- Etsy delivers digital files automatically, so there is no fulfillment step.

**Phase 2 (stub interfaces now, implement later): print-on-demand via Printify**
- Posters, stickers, mugs, shirts. Printify is linked to the Etsy shop and handles printing and shipping.

**Phase 3 (optional, stub only):** Adobe Stock / microstock uploads reusing the image pipeline.

## 3. Architecture

Five agents communicate only through a shared queue (database). Each agent is an independently runnable job with a role prompt, a narrow set of tools, and clear input/output states.

```
Researcher ──► [brief] ──► Creator ──► [created] ──► QA ──► [approved | rejected]
                                                              │
                                                              ▼
Analyst ◄── sales/stats ◄── Etsy ◄── [drafted] ◄────────── Lister
   │
   └──► research_notes (feeds next Researcher run)
```

### Agents

| Agent | Schedule | Input | Output | Notes |
|---|---|---|---|---|
| **Researcher** | Weekly | `niches.yaml`, `research_notes`, Etsy listing search | 10–20 product briefs → state `brief` | Uses Etsy public listing search (API only, no HTML scraping). Briefs describe an *original* product inspired by demand signals, never a copy. |
| **Creator** | Daily | `brief` rows | Product files + mockup images → state `created` | Image products via image-gen adapter. Planners/trackers generated in code (PDF via reportlab or similar; XLSX via openpyxl). Generate 3–5 listing images per product (product + simple mockups). |
| **QA** | Daily | `created` rows | state `approved` or `rejected` with reasons | Vision check with Claude (quality, artifacts, legibility, matches brief). Blocklist check for trademarks, brands, characters, celebrities, artist names. Near-duplicate check against our own catalog (perceptual hash). Reject anything uncertain. |
| **Lister** | Daily | `approved` rows | Etsy **draft** listing → state `drafted` | Writes title (≤140 chars), 13 tags, description with AI-use disclosure. Creates draft, uploads images and digital files. Never sets state to active. |
| **Analyst** | Weekly | Etsy receipts, listing stats, our DB | `research_notes` + weekly summary | Ranks what is selling/favorited, writes guidance for the Researcher, flags dead listings. |

### Orchestration
- Single CLI entry point: `python -m pipeline run <agent>` and `python -m pipeline run-all`.
- Each run is idempotent and safe to re-run. Use row-level locking or status transitions to avoid double processing.
- Scheduling: provide a cron config suitable for Railway (or any cron host). Document the schedule in the README.

## 4. Tech stack

- **Language:** Python 3.12+
- **LLM:** Anthropic Python SDK. Model names must be configurable via env vars. Suggested defaults:
  - Researcher, Analyst: `claude-opus-5-5`
  - Creator prompts, Lister copy, QA vision: `claude-sonnet-5-5`
  - Cheap classification/tag tasks: `claude-haiku-5-5`
- **Deployment target:** Railway (see Section 13). Build for it from the start.
- **Queue/state:** Postgres via SQLAlchemy + Alembic migrations (`DATABASE_URL`). SQLite allowed for local dev only — never on Railway, since the container filesystem is wiped on redeploy.
- **Files:** storage interface with two backends: local `./storage/` (dev) and S3-compatible object storage (Railway bucket) for staging/production. Selected by `STORAGE_BACKEND=local|s3`.
- **Image generation:** provider-agnostic adapter (`ImageProvider` interface) with a mock implementation that returns placeholder images. Real providers (OpenAI, Google, Ideogram) added later behind the same interface.
- **Config:** `.env` (via pydantic-settings) + `niches.yaml` + `blocklist.txt`.
- **Logging:** structured JSON logs per agent run, plus a `runs` table recording start/end, counts, errors, and token/image costs.

## 5. Data model (minimum)

**products**
- `id`, `created_at`, `updated_at`
- `state`: `brief | created | approved | rejected | drafted | published | retired`
- `niche`, `product_type` (`wall_art | planner | tracker | spreadsheet | pod_*`)
- `brief_json` (concept, style, audience, keywords, target price, rationale)
- `file_paths` (deliverable files), `image_paths` (listing images)
- `qa_result_json` (pass/fail, reasons, scores)
- `listing_json` (title, tags, description, price, taxonomy_id)
- `etsy_listing_id`, `printify_product_id` (nullable)
- `cost_usd` (accumulated API costs)

**research_notes** — `id`, `created_at`, `source` (analyst/researcher), `content`

**runs** — `id`, `agent`, `started_at`, `ended_at`, `status`, `items_processed`, `errors_json`, `cost_usd`

## 6. External integrations (build as adapters with mocks)

Every integration gets an interface, a **mock** implementation (default), and a **real** implementation activated when credentials exist. `MODE=mock|live` env var controls this globally, with per-adapter overrides.

**Important:** API details below are a starting point. Before writing the live implementations, verify endpoints, auth headers, required fields, and allowed values against the current official Etsy Open API v3 and Printify API documentation.

### Etsy Open API v3
- Base: `https://openapi.etsy.com/v3/application`
- Auth: API key header (`x-api-key`) + OAuth 2.0 with PKCE (user access token + refresh token). Implement token refresh. Etsy issues a new refresh token on each refresh, so **persist tokens in a `oauth_tokens` database table**, not env vars. Env vars only seed the table on first run.
- Scopes likely needed: `listings_r`, `listings_w`, `listings_d`, `shops_r`, `transactions_r`
- Operations needed:
  - Search active listings (Researcher)
  - Get seller taxonomy nodes (Lister, to pick `taxonomy_id`)
  - Create draft listing (Lister)
  - Upload listing image (Lister)
  - Upload listing digital file (Lister)
  - Update listing (Lister — edits only; **never** set `state=active` in v1)
  - Get shop receipts / transactions and listing stats (Analyst)
- Respect rate limits (~10 req/sec, ~10,000/day). Add a client-side throttle and retry with backoff on 429/5xx.
- Set production attributes (who made / when made / AI disclosure) according to Etsy's current seller policy for AI-assisted "designed by seller" items. Make the mapping a config value and leave a TODO for the owner to confirm before go-live.

### Printify (Phase 2 — interface + mock only for now)
- Base: `https://api.printify.com/v1`, Bearer token auth
- Operations: list shops, list blueprints/print providers, upload image, create product, publish product to the linked Etsy shop

### Image generation
- Interface: `generate(prompt, size, style) -> image bytes + metadata (cost, model)`
- Mock returns a labeled placeholder image so the pipeline can run end to end.

## 7. Environment variables (placeholders — owner will supply values later)

```
MODE=mock
ANTHROPIC_API_KEY=
MODEL_RESEARCH=claude-opus-5-5
MODEL_WORKER=claude-sonnet-5-5
MODEL_CHEAP=claude-haiku-5-5

APP_ENV=local            # local | staging | production
DATABASE_URL=sqlite:///./pipeline.db   # Railway injects Postgres URL in staging/production
STORAGE_BACKEND=local    # local | s3
STORAGE_DIR=./storage
S3_ENDPOINT=
S3_BUCKET=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
DRY_RUN=false            # true = live reads, no external writes

ETSY_API_KEY=
ETSY_SHARED_SECRET=
ETSY_SHOP_ID=
ETSY_OAUTH_ACCESS_TOKEN=
ETSY_OAUTH_REFRESH_TOKEN=
ETSY_REDIRECT_URI=

PRINTIFY_API_TOKEN=
PRINTIFY_SHOP_ID=

IMAGE_PROVIDER=mock
IMAGE_API_KEY=

DAILY_DRAFT_CAP=10
WEEKLY_BRIEF_CAP=20
MAX_DAILY_SPEND_USD=10
```

Also include a small helper script (`scripts/etsy_oauth.py`) the owner can run once to complete the Etsy OAuth PKCE flow locally and print the access/refresh tokens.

## 8. Guardrails (non-negotiable)

- **Draft-only:** the system never publishes listings to active. Publishing is the human step.
- **Caps:** enforce `DAILY_DRAFT_CAP`, `WEEKLY_BRIEF_CAP`, and `MAX_DAILY_SPEND_USD`. Stop the run and log clearly when a cap is hit.
- **IP safety:** no brands, trademarks, logos, characters, celebrities, real people, or "in the style of [named artist]" in prompts or outputs. Maintain `blocklist.txt` and check prompts, titles, tags, and descriptions against it. QA rejects on doubt.
- **No copying:** the Researcher may use competitor listings only as demand signals (keywords, categories, price points, popularity). Briefs must describe original products. Never download or reuse competitor images.
- **No HTML scraping of Etsy.** API only.
- **Disclosure:** every listing description includes a clear statement that AI tools were used in the design.
- **Volume pacing:** avoid bulk near-identical listings. QA rejects products too similar to existing catalog items.
- **Secrets:** never log tokens or keys. `.env` in `.gitignore`. On Railway, all secrets live in Railway service variables, never in the repo.

## 9. Deliverables and milestones

**Milestone 1 — Skeleton (mock mode)**
- Repo structure, config, DB models and migrations, CLI, logging, `runs` table
- All adapters with mock implementations
- `README.md` with setup and run instructions

**Milestone 2 — End-to-end in mock mode**
- All five agents implemented with real Claude calls (Anthropic key required; everything else mocked)
- One command produces products that move `brief → created → approved/rejected → drafted` with mock Etsy drafts
- A simple status report command: `python -m pipeline status` (counts by state, recent runs, spend)

**Milestone 2.5 — Railway staging (mock mode)**
- Dockerfile, `railway.json`, Alembic migrations, S3 storage backend
- Deploy to the Railway `staging` environment per Section 13 and confirm crons fire, Postgres and storage persist, and a full mock run completes

**Milestone 3 — Live-ready**
- Live Etsy adapter (draft creation, image/file upload, search, receipts) behind `MODE=live`
- Etsy OAuth helper script and token table
- Live image provider adapter (start with one provider)
- `DRY_RUN` / `--dry-run` that runs live reads but skips all external writes

**Milestone 4 — Phase 2 prep**
- Printify live adapter and POD product types (posters, stickers first)

## 10. Testing

- Unit tests for each agent's state transitions and guardrails (caps, blocklist, draft-only).
- Contract tests for adapters using recorded fixtures.
- A full pipeline test in mock mode that must pass in CI.
- A test that fails if any code path can set an Etsy listing to `active`.

## 11. What the owner will provide later

- Etsy shop ID, Etsy app API key and shared secret, OAuth redirect URI (then run the OAuth helper)
- Image-generation provider choice and API key
- Printify API token and shop ID (Phase 2)
- Initial niches for `niches.yaml` (seed the file with 3 clearly marked placeholder niches for now, e.g. minimalist botanical wall art, ADHD-friendly daily planners, small-business bookkeeping spreadsheets)
- Confirmation of the Etsy production-attribute / AI-disclosure field mapping

## 12. Working style for Claude Code

- Start with Milestone 1 and confirm the structure before moving on.
- Keep each agent's prompt in its own file under `prompts/` so prompts can be tuned without code changes.
- Prefer small, readable modules over frameworks. No heavy agent frameworks unless clearly justified.
- When an API detail is uncertain, check the official docs; if still unclear, leave a clearly marked `TODO(owner)` rather than guessing.
- At the end of each milestone, summarize what was built, what is mocked, and what is needed from the owner.

## 13. Railway deployment

### Project layout
One Railway project with two environments: **staging** and **production**. Each environment contains:

| Service | Type | Purpose |
|---|---|---|
| `postgres` | Railway Postgres | Queue, state, runs, OAuth tokens |
| `storage` | Railway bucket (S3-compatible) | Product files and listing images |
| `migrate` | Pre-deploy command | `alembic upgrade head` before agents run |
| `researcher` | Cron service | Weekly, e.g. Monday 6:00 AM |
| `daily-pipeline` | Cron service | Daily: runs Creator → QA → Lister in sequence |
| `analyst` | Cron service | Weekly, e.g. Sunday 8:00 PM |
| `web` (optional) | Web service | `/health`, `/status` page (counts by state, recent runs, spend), and the Etsy OAuth callback route |

All cron schedules are in UTC on Railway; document the Phoenix-time equivalents (Arizona is UTC-7, no DST).

### Build and run requirements
- Single Docker image used by every service; each service differs only by start command (e.g. `python -m pipeline run researcher`).
- Provide `railway.json` (or per-service config) with build settings, start commands, and pre-deploy migration.
- Cron services must run to completion and exit with a nonzero code on failure. No long-running loops in cron services.
- Every agent must be safe to trigger manually ("run now") at any time without double-processing.
- Reference Railway-provided variables (e.g. Postgres `DATABASE_URL`) rather than hardcoding.
- Write a `DEPLOY.md` with step-by-step setup: create project, add Postgres and bucket, create services, set variables per environment, run first deploy, run OAuth helper.

### Environment defaults

| Variable | staging | production |
|---|---|---|
| `APP_ENV` | staging | production |
| `MODE` | mock → live | live |
| `DRY_RUN` | true (once live) | false |
| `DAILY_DRAFT_CAP` | 3 | start at 3, raise later |
| `MAX_DAILY_SPEND_USD` | 3 | 10 |

## 14. Testing ladder

Each stage must pass before moving to the next. Claude Code should make each stage runnable with a single documented command or Railway setting change.

1. **Local, mock mode** — everything runs locally; only Claude API calls are real. Used for prompt tuning. Full pipeline test must pass.
2. **Railway staging, mock mode** — same code deployed. Verifies crons fire, Postgres and bucket persist across redeploys, variables load, logs are readable in Railway.
3. **Railway staging, live dry-run** — real Etsy reads (search, taxonomy, receipts) and real image generation; no writes to Etsy or Printify. Review output quality and per-product cost.
4. **Railway production, live drafts** — real drafts created in the shop, capped at 2–3 per day. Owner reviews in the Etsy app and publishes manually. Raise caps only after a week of acceptable quality.

Add a `python -m pipeline smoke` command that checks connectivity (DB, storage, Anthropic, and Etsy/Printify/image provider when live) without creating anything, for use after each deploy.
