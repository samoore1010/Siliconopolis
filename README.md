# Etsy Agent Pipeline

A near-autonomous, five-agent system that researches, creates, quality-checks and **drafts** digital
products on an Etsy shop. One human checkpoint: the owner reviews draft listings in the Etsy app and
publishes the good ones. The system never publishes on its own.

```
Researcher ──► [brief] ──► Creator ──► [created] ──► QA ──► [approved | rejected]
                                                              │
                                                              ▼
Analyst ◄── sales/stats ◄── Etsy ◄── [drafted] ◄────────── Lister
   │
   └──► research_notes (feeds next Researcher run)
```

Agents talk only through the `products` table (a queue keyed by `state`). Every agent run is an
independent process that claims rows with a compare-and-swap lock, does its work, records a `runs`
row (counts, errors, token/image cost) and exits — which is exactly what Railway cron services need.

Spec: [`etsy-agent-pipeline-instructions_1.md`](etsy-agent-pipeline-instructions_1.md).
Deployment: [`DEPLOY.md`](DEPLOY.md).

## Status

| Milestone | State |
|---|---|
| 1 — Skeleton (config, DB + Alembic, CLI, logging, `runs`, mock adapters) | done |
| 2 — End-to-end in mock mode (5 agents, `run-all`, `status`) | done |
| 2.5 — Railway staging (Dockerfile, IaC, S3 storage, `smoke`) | code done; deploy per `DEPLOY.md` |
| 3 — Live-ready (live Etsy adapter, OAuth helper + token table, `DRY_RUN`) | Etsy live client + OAuth + token table built, **unverified against live API**; image provider: mock only |
| 4 — Printify / POD | interface + mock only |

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
| `python -m pipeline run <agent>` | one agent: `researcher`, `creator`, `qa`, `lister`, `analyst` | 0 ok, 1 failed, 3 cap reached |
| `python -m pipeline run daily` | `creator -> qa -> lister` (the daily cron) | worst of the three |
| `python -m pipeline run-all` | all five in order | worst |
| `python -m pipeline status [--json]` | counts by state, recent runs, spend | 0 |
| `python -m pipeline smoke [--json]` | DB, storage, Anthropic, Etsy, Printify (live only), image provider | 0 pass / 1 fail |
| `python -m pipeline web` | FastAPI: `/health`, `/status`, `/oauth/etsy/start`, `/oauth/etsy/callback` | — |
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
| `MODEL_RESEARCH` / `MODEL_WORKER` / `MODEL_CHEAP` | Claude models per role |
| `LLM_PROVIDER` | `anthropic` or `mock`; defaults to anthropic when a key is set |
| `DAILY_DRAFT_CAP`, `WEEKLY_BRIEF_CAP`, `MAX_DAILY_SPEND_USD` | hard caps; hitting one stops the run (exit 3) |

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
  adapters/images      base, mock placeholder provider
  agents/              researcher, creator, qa, lister, analyst (+ base Run bookkeeping)
  generators/          pdf (reportlab), xlsx (openpyxl), mockups (Pillow)
  guardrails/          blocklist, caps
  commands/            status, smoke
  web/                 FastAPI app
alembic/               migrations
prompts/               agent prompts
scripts/etsy_oauth.py  one-time OAuth PKCE helper
.railway/railway.ts    Railway Infrastructure as Code (services, crons, postgres, bucket)
Dockerfile             single image for every service
tests/                 unit, contract, draft-only, end-to-end (mock)
```

## Schedule (Railway, UTC) — Phoenix is UTC-7 year-round

| Service | Cron (UTC) | Phoenix | Command |
|---|---|---|---|
| `researcher` | `0 13 * * 1` | Mon 6:00 AM | `python -m pipeline run researcher` |
| `daily-pipeline` | `0 14 * * *` | daily 7:00 AM | `python -m pipeline run daily` |
| `analyst` | `0 3 * * 1` | Sun 8:00 PM | `python -m pipeline run analyst` |
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
