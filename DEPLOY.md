# Deploying to Railway

One Railway project, two environments (`staging`, `production`). Each environment holds:

| Service | Type | Start command / purpose |
|---|---|---|
| `postgres` | Railway Postgres | queue, state, runs, OAuth tokens |
| `storage` | Railway Bucket (S3-compatible) | product files + listing images |
| `web` | web service | image default (`python -m pipeline migrate && python -m pipeline web`); IaC also sets **pre-deploy** `alembic upgrade head` |
| `researcher` | cron | `python -m pipeline run researcher` — `0 13 * * 1` (Mon 6:00 AM Phoenix) |
| `daily-pipeline` | cron | `python -m pipeline run daily` — `0 14 * * *` (7:00 AM Phoenix) |
| `analyst` | cron | `python -m pipeline run analyst` — `0 3 * * 1` (Sun 8:00 PM Phoenix) |

All services build from the same `Dockerfile`; only the start command differs. Cron services run to
completion and exit (nonzero on failure, `3` when a cap is hit) and close their DB pool on exit, as
Railway requires. Railway skips a cron tick if the previous run is still active, and the row locks
make overlapping or manual "run now" executions safe anyway.

> **`railway.json` is deprecated.** Railway's Config as Code (`railway.json`/`railway.toml`) can no
> longer be enabled for new services and stops being read on 2026-12-01. This repo therefore ships
> **Infrastructure as Code** in [`.railway/railway.ts`](.railway/railway.ts), which declares every
> service, cron schedule, pre-deploy command, Postgres, bucket and variable wiring. If you prefer
> clicking, Section B below lists the equivalent dashboard settings.

## Already have a single service from the dashboard?

If you created the project by pointing Railway at the repo (one service, no database), you have the
web service only. It serves `/` and `/health`, but agents cannot run and nothing persists:
`APP_ENV=local` with SQLite is wiped on every redeploy. To finish the setup, either run Section A
(IaC adds Postgres, the bucket and the three cron services next to your existing service — rename
the existing service to `web` first so the plan updates it instead of creating a second one) or
follow Section B steps 2–7 in the dashboard. Then set `APP_ENV=staging` or `production`.

## Prerequisites

- Railway account + [Railway CLI](https://docs.railway.com/cli) (`npm i -g @railway/cli` or brew),
  `railway login`.
- The repo pushed to GitHub and the Railway GitHub app granted access to it.
- Node (only to evaluate the IaC file): `npm install railway` in the repo root.

## A. Create the project with IaC (recommended)

```bash
railway init                     # create project "etsy-agent-pipeline" (or railway link an existing one)
railway environment new staging  # production exists by default; create staging
railway environment staging      # select it
npm install railway
railway config plan              # review: +postgres, +storage bucket, +web, +researcher, +daily-pipeline, +analyst
railway config apply
```

Edit `REPO`/`BRANCH` at the top of `.railway/railway.ts` if they differ from your repo, and pick the
bucket `region` (immutable once created). The file renders staging vs production differences from
`ctx.environment` — caps, `MODE`, `DRY_RUN` — see the table in Section D.

Then set the secrets (they are `preserve()`d in the file, i.e. never stored in git). Either in the
dashboard (each service → Variables) or via the CLI, per environment:

```bash
railway variables --service web --set ANTHROPIC_API_KEY=sk-ant-...   # repeat per service, or use a shared variable
```

Tip: create them once as **shared variables** on the environment, then reference them from each
service as `${{shared.ANTHROPIC_API_KEY}}` — or edit `.railway/railway.ts` to use
`ctx.shared.ANTHROPIC_API_KEY` instead of `preserve()`.

Repeat `railway environment production && railway config plan && railway config apply` for
production.

## B. Create the project by hand (dashboard)

1. **New project** → name it. Create environment `staging` (Settings → Environments).
2. **Add Postgres**: Create → Database → PostgreSQL. It exposes `DATABASE_URL`.
3. **Add Bucket**: Create → Bucket, name `storage`, choose a region. Its Credentials tab shows
   `ENDPOINT`, `BUCKET`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`, `REGION`.
4. **Create four services from the GitHub repo** (Create → GitHub Repo), named `web`,
   `researcher`, `daily-pipeline`, `analyst`. Railway detects the Dockerfile automatically.
5. Per service → Settings:
   - **Start command** as in the table above.
   - `web`: **Pre-deploy command** `alembic upgrade head`; **Healthcheck path** `/health`;
     generate a public domain (Networking) — needed for the Etsy OAuth callback.
   - cron services: **Cron schedule** as in the table; **Restart policy** Never.
6. Per service → Variables: add the variables from Section D. For Postgres/bucket use references:
   `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `S3_ENDPOINT=${{storage.ENDPOINT}}`,
   `S3_BUCKET=${{storage.BUCKET}}`, `S3_REGION=${{storage.REGION}}`,
   `S3_ACCESS_KEY_ID=${{storage.ACCESS_KEY_ID}}`, `S3_SECRET_ACCESS_KEY=${{storage.SECRET_ACCESS_KEY}}`.
   Easiest: define everything once as shared variables and reference `${{shared.NAME}}`.
7. Deploy. Duplicate the environment for `production` and change the values per Section D.

## C. First deploy checklist (per environment)

1. `web` deploys → pre-deploy runs `alembic upgrade head` → `/health` returns `{"ok": true}`.
2. Open the `web` service shell (or `railway run --service web`) and run:
   ```bash
   python -m pipeline smoke      # DB, bucket, Anthropic, Etsy adapter, image provider
   ```
3. Trigger the crons manually once — `railway cron run` or the service's **Run now** button — in this
   order and watch logs (JSON, one line per event, `agent`/`run_id` on every line):
   `researcher` → `daily-pipeline` → `analyst`.
4. `python -m pipeline status` (or GET `/status`) should show products in `drafted` (mock drafts
   live in the bucket under `mock_etsy/shop_state.json`).
5. Redeploy `web` and confirm counts persist (Postgres) and files are still listed in the bucket.

## D. Environment variables

| Variable | staging | production | Notes |
|---|---|---|---|
| `APP_ENV` | `staging` | `production` | non-local refuses SQLite/local storage |
| `MODE` | `mock` → `live` | `live` | flip staging to live at ladder stage 3 |
| `DRY_RUN` | `true` (once live) | `false` | live reads, no external writes |
| `DAILY_DRAFT_CAP` | `3` | `3`, raise later | |
| `WEEKLY_BRIEF_CAP` | `10` | `20` | |
| `MAX_DAILY_SPEND_USD` | `3` | `10` | |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | same | injected by Railway |
| `STORAGE_BACKEND` | `s3` | `s3` | |
| `S3_*` | bucket references | same | see Section B.6 |
| `MODEL_RESEARCH` / `MODEL_WORKER` / `MODEL_CHEAP` | `claude-opus-5-5` / `claude-sonnet-5-5` / `claude-haiku-5-5` | same | |
| `IMAGE_PROVIDER` | `mock` or `openai` | `openai` | `openai` enables wall art + AI lifestyle mockups |
| `IMAGE_MODEL` / `IMAGE_QUALITY` | `gpt-image-1.5` / `high` | same | wall art quality (~$0.20/image at high) |
| `MOCKUP_SCENES` / `MOCKUP_QUALITY` | `true` / `medium` | same | backgrounds generated once per scene, then reused |
| `VILLAGE_USER`, `VILLAGE_PASSWORD` | ✔ | ✔ | basic auth for the dashboard; without them the village returns 503 off-local |
| `RAILWAY_API_TOKEN`, `RAILWAY_SERVICE_IDS` | optional | optional | enables "Run now" buttons (Section H) |
| `LOG_FORMAT` | `json` | `json` | Railway log explorer parses JSON lines |
| **secrets** | | | never in the repo |
| `ANTHROPIC_API_KEY` | ✔ | ✔ | |
| `ETSY_API_KEY`, `ETSY_SHARED_SECRET`, `ETSY_SHOP_ID`, `ETSY_REDIRECT_URI` | when live | ✔ | redirect = `https://<web-domain>/oauth/etsy/callback` |
| `ETSY_OAUTH_ACCESS_TOKEN`, `ETSY_OAUTH_REFRESH_TOKEN` | optional seed | optional seed | only seed the `oauth_tokens` table on first run |
| `IMAGE_API_KEY` | when `openai` | ✔ | OpenAI API key |
| `PRINTIFY_API_TOKEN`, `PRINTIFY_SHOP_ID` | Phase 2 | Phase 2 | |

## E. Etsy OAuth (once per environment, when going live)

Option 1 — locally, then seed Railway:
```bash
ETSY_API_KEY=... ETSY_REDIRECT_URI=https://<web-domain>/oauth/etsy/callback python scripts/etsy_oauth.py --no-db
```
Paste the printed access/refresh tokens into `ETSY_OAUTH_ACCESS_TOKEN` / `ETSY_OAUTH_REFRESH_TOKEN`
on Railway. The first live run copies them into the `oauth_tokens` table; afterwards the table is the
source of truth (Etsy rotates refresh tokens) and the env vars can be deleted.

Option 2 — through the web service: GET `https://<web-domain>/oauth/etsy/start`, open the returned
`authorize_url`, approve; the callback stores tokens directly in the table. (The PKCE verifier is
kept in process memory, so finish the flow before the web service redeploys.)

Register the exact redirect URI in the Etsy developer app first.

## F. Testing ladder

| Stage | Where | Settings | Pass criteria |
|---|---|---|---|
| 1 | local | `MODE=mock`, SQLite | `pytest` green; `run-all` reaches `drafted` |
| 2 | Railway staging | `MODE=mock`, Postgres, bucket | crons fire, data persists across redeploy, logs readable, `smoke` passes |
| 3 | Railway staging | `MODE=live`, `DRY_RUN=true` | real Etsy reads + real images; no writes; review quality/cost in `status` |
| 4 | Railway production | `MODE=live`, `DRY_RUN=false`, `DAILY_DRAFT_CAP=2-3` | real drafts appear in the Etsy shop; owner publishes manually |

Moving between stages is a variable change only; run `python -m pipeline smoke` after each deploy.

## G. Operations

- **Village**: open `https://<web-domain>/` (redirects to `/village`), sign in with
  `VILLAGE_USER`/`VILLAGE_PASSWORD`. Everything shown is read from Postgres; it polls every 4s.
- **Demo on staging**: in the `web` service shell run `python -m pipeline demo --cycles 2` while
  watching the village. Refuses when `APP_ENV=production` or any adapter is live.

- **Logs**: each service's Logs tab; filter by `"agent":"qa"` or `"run_id":123`.
- **Run now**: service → Deployments → ⋯ → *Run now* (or `railway cron run`). Safe at any time.
- **Caps hit**: the run exits `3` and logs `CAP REACHED`; the `runs` row has `status=cap_reached`.
- **Stuck rows**: a crashed run leaves `locked_by` set; locks older than `LOCK_TIMEOUT_MINUTES`
  (120) are reclaimed automatically. `status` shows `locked rows`.
- **Rollback**: redeploy a previous deployment; migrations are additive (downgrade exists for 0001).

## H. Optional "Run now" buttons

Each agent panel has a Run-now button that asks Railway to re-run the agent's cron service. It is
disabled until the `web` service has:

- `RAILWAY_API_TOKEN` — a Railway project or account token with access to this project.
- `RAILWAY_SERVICE_IDS` — JSON mapping `{"researcher": "<serviceId>", "daily": "<serviceId>", "analyst": "<serviceId>"}`
  (Creator, QA and Lister all map to `daily`). Service ids are in each service's Settings page.
- `RAILWAY_ENVIRONMENT_ID` — injected by Railway automatically.

The button only starts a normal run, so caps, locks and the draft-only rule apply exactly as on a
schedule. The GraphQL mutation used (`serviceInstanceRedeploy`) is marked `TODO(owner)` to verify
against Railway's current API before relying on it.
