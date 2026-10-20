# Deploying to Railway

One Railway project, two environments (`staging`, `production`). Each environment holds:

| Service | Type | Start command / purpose |
|---|---|---|
| `postgres` | Railway Postgres | queue, state, runs, OAuth tokens |
| `storage` | Railway Bucket (S3-compatible) | product files + listing images |
| `web` | web service | image default (`python -m pipeline migrate && python -m pipeline web`): the dashboard **and the agents**. The in-app worker (`pipeline/worker.py`, `WORKER_ENABLED=true`) runs QA, the Editor and the Lister within seconds of work reaching their queue, keeps the Researcher (Mon 6:00 AM Phoenix), Creator (daily 7:00 AM), Analyst (Sun 8:00 PM) and Fulfiller (hourly) on schedule, and serves every "Run now" button. |

That is the whole deployment. Cron services are **optional**: `.railway/railway.ts` only creates them
with `OPTIONAL_CRONS=1`, and if you already have `researcher` / `daily-pipeline` / `editor` /
`analyst` services you can delete them (or keep them: their runs are harmless duplicates, since row
locks mean whichever process claims a product first does the work). The web service needs enough
memory for Chromium and LibreOffice renders (1-2 GB is comfortable).

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
(IaC adds Postgres, the bucket and the cron services next to your existing service — rename
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
railway config plan              # review: +postgres, +storage bucket, +web, +researcher, +daily-pipeline, +editor, +analyst, +fulfiller
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
4. **Create six services from the GitHub repo** (Create → GitHub Repo), named `web`,
   `researcher`, `daily-pipeline`, `editor`, `analyst`, `fulfiller`. Railway detects the Dockerfile
   automatically.
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
| `MODEL_EDITOR` | `claude-opus-5-5` | same | the Editor's model; stronger than the Creator's by design |
| `EDITOR_MAX_COST_PER_PRODUCT_USD` | `1` | `3` | budget per polish (LLM + images); over it, the product is handed back unchanged |
| `IMAGE_PROVIDER` | `mock` or `openai` | `openai` | `openai` enables wall art + AI lifestyle mockups |
| `IMAGE_MODEL` / `IMAGE_EDIT_MODEL` | `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst` | same | generation vs edits (photo mockups, Editor artwork edits); Sunburst keeps the input design exact, at ~2x the latency |
| `IMAGE_QUALITY` | `high` | same | low / medium / high / xhigh / max; ~$0.05 per image at high on GPT Image 2.5, ~$0.21 at max |
| `MOCKUP_SCENES` / `MOCKUP_QUALITY` | `true` / `medium` | same | backgrounds generated once per scene, then reused |
| `VILLAGE_USER`, `VILLAGE_PASSWORD` | ✔ | ✔ | basic auth for the dashboard; without them the village returns 503 off-local |
| `PRINT_USER`, `PRINT_PASSWORD` | ✔ | ✔ | login for the print shop's `/print` queue (sees only orders) |
| `ETSY_SHIPPING_PROFILE_ID` | ✔ for merch | ✔ for merch | shirts/mugs are not drafted until this is set |
| `ETSY_PRODUCTION_PARTNER_ID` | ✔ for merch | ✔ for merch | the print shop, set up in Etsy › Settings › Production partners |
| `ETSY_READINESS_STATE_ID`, `ETSY_RETURN_POLICY_ID` | optional | optional | processing profile / return policy, if your shop requires them |
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

## H. "Run now" buttons

Every agent has a Run-now button (Team page, agent panel, build/polish queues, Research). With the
in-app worker it queues a `job_requests` row the worker picks up on its next poll (15 s), no token
needed. Only when `WORKER_ENABLED=false` does it fall back to asking Railway to re-run a cron
service, which then needs:

- `RAILWAY_API_TOKEN` — a Railway project or account token with access to this project.
- `RAILWAY_SERVICE_IDS` — JSON mapping `{"researcher": "<serviceId>", "daily": "<serviceId>", "editor": "<serviceId>",
  "analyst": "<serviceId>", "fulfiller": "<serviceId>"}` (Creator, QA and Lister all map to `daily`). Service
  ids are in each service's Settings page.
- `RAILWAY_ENVIRONMENT_ID` — injected by Railway automatically.

The button only starts a normal run, so caps, locks and the draft-only rule apply exactly as on a
schedule. The GraphQL mutation used (`serviceInstanceRedeploy`) is marked `TODO(owner)` to verify
against Railway's current API before relying on it.


## I. The Editor (polishing existing products)

Nothing reaches the Editor on its own. On any built, approved or rejected product (never an Etsy
draft or a live listing) choose **Polish it** in the app, optionally with a note; QA's improvement
suggestions go along with it. The Editor starts from the product's current design (page HTML,
spreadsheet spec, print design or artwork), applies every requested change, keeps the rest, raises
the finish, re-runs the same layout/formula checks, and hands the result back to QA, which checks
each requested change was actually made. The version it started from is kept for comparison.

- Separate cron service (`editor`, hourly at :30) so polishes never wait behind the daily build.
  Add it like the other crons (Section A does it; Section B: one more service with that schedule).
- `MODEL_EDITOR` (default `claude-opus-5-5`) and `EDITOR_MAX_COST_PER_PRODUCT_USD` (default 3) cap
  quality and cost; the daily `MAX_DAILY_SPEND_USD` still applies on top.
- The polish queue is at Products → *Polish queue*: reorder it, take a product back before the
  Editor starts, or press Stop while it is running (it finishes the product in hand).

## J. In-house shirts, sweatshirts, hoodies and mugs (family print shop)

1. **Catalog.** Edit `catalog.yaml`: blanks, colours (name → hex), sizes, print area, the shop's
   cost per item (`unit_cost`), postage estimate and price. The agents only design what is listed.
2. **Etsy setup** (Shop Manager): add the shop as a *production partner*; create a *shipping
   profile* (and processing profile / return policy if asked). Put their ids in
   `ETSY_PRODUCTION_PARTNER_ID`, `ETSY_SHIPPING_PROFILE_ID` (and optional `ETSY_READINESS_STATE_ID`,
   `ETSY_RETURN_POLICY_ID`).
3. **OAuth scope.** Order handling needs `transactions_w` (sending tracking). If Etsy was authorized
   before this change, re-run `/oauth/etsy/start` once.
4. **Print shop login.** Set `PRINT_USER` / `PRINT_PASSWORD` on `web`; the shop opens
   `https://<web-domain>/print` (works on a phone). It shows each order's proof, the 300 dpi print
   file to download, size/colour/qty, personalisation, ship-to address, and buttons for
   *Start printing* and *Mark shipped* (carrier + tracking). Orders with unclear personalisation are
   flagged *Check first* and can be corrected there before printing.
5. **Fulfiller cron.** Add the `fulfiller` service (hourly, see the table above).
6. **Money.** The print queue shows what is owed to the shop (`unit_cost` × qty for printed orders);
   the owner login can mark it paid.
