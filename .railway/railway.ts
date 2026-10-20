/**
 * Railway Infrastructure as Code for the Etsy agent pipeline.
 *
 * One Railway project, two environments (staging, production). One web service runs the dashboard
 * AND the agents (in-app worker); optional cron services are built from the same Dockerfile.
 *
 * Usage:
 *   npm install railway             # IaC SDK (see .railway/README.md)
 *   railway link                    # pick project + environment
 *   railway config plan             # preview
 *   railway config apply            # create/update services, postgres, bucket, variables
 *
 * NOTE: `railway.json` (Config as Code) is deprecated and new services cannot use it, so this file
 * is the source of truth. Secrets are NEVER written here: they are `preserve()`d, meaning "keep the
 * value already set in the Railway dashboard".
 *
 * All cron schedules are UTC. Phoenix (America/Phoenix, UTC-7, no DST) equivalents in comments.
 */
import { bucket, defineRailway, github, group, postgres, preserve, project, service } from "railway/iac";

// TODO(owner): set to your GitHub repo and deploy branch.
const REPO = "samoore1010/Siliconopolis";
const BRANCH = "main";

export default defineRailway((ctx) => {
  const prod = ctx.environment === "production";

  const db = postgres("postgres");
  const storage = bucket("storage", { region: "iad" }); // TODO(owner): pick region near you; immutable later

  // Shared environment for every pipeline service.
  const env = {
    APP_ENV: prod ? "production" : "staging",
    MODE: prod ? "live" : "mock", // staging flips to "live" with DRY_RUN=true at testing-ladder stage 3
    DRY_RUN: prod ? "false" : "true",
    LOG_FORMAT: "json",
    LOG_LEVEL: "INFO",

    DAILY_DRAFT_CAP: "3", // production: start at 3, raise after a week of acceptable quality
    WEEKLY_BRIEF_CAP: prod ? "20" : "10",
    MAX_DAILY_SPEND_USD: prod ? "10" : "3",

    MODEL_RESEARCH: "claude-opus-5-5",
    MODEL_WORKER: "claude-sonnet-5-5",
    MODEL_CHEAP: "claude-haiku-5-5",
    MODEL_EDITOR: "claude-opus-5-5", // the Editor polishes with a stronger model than the Creator
    EDITOR_MAX_COST_PER_PRODUCT_USD: prod ? "3" : "1",

    DATABASE_URL: db.env.DATABASE_URL,

    STORAGE_BACKEND: "s3",
    S3_ENDPOINT: storage.env.ENDPOINT,
    S3_BUCKET: storage.env.BUCKET,
    S3_REGION: storage.env.REGION,
    S3_ACCESS_KEY_ID: storage.env.ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY: storage.env.SECRET_ACCESS_KEY,

    IMAGE_PROVIDER: prod ? "openai" : "mock", // openai needs IMAGE_API_KEY (OpenAI key)
    IMAGE_MODEL: "gpt-image-1.5",
    IMAGE_QUALITY: "high",

    // Secrets: set once in the Railway dashboard per environment; never in the repo.
    ANTHROPIC_API_KEY: preserve(),
    ETSY_API_KEY: preserve(),
    ETSY_SHARED_SECRET: preserve(),
    ETSY_SHOP_ID: preserve(),
    ETSY_OAUTH_ACCESS_TOKEN: preserve(),
    ETSY_OAUTH_REFRESH_TOKEN: preserve(),
    ETSY_REDIRECT_URI: preserve(),
    PRINTIFY_API_TOKEN: preserve(),
    PRINTIFY_SHOP_ID: preserve(),
    IMAGE_API_KEY: preserve(),
    // Village dashboard basic auth (required: the web URL is public).
    VILLAGE_USER: preserve(),
    VILLAGE_PASSWORD: preserve(),
    // The in-app worker (default on): the web service runs the agents itself.
    WORKER_ENABLED: "true",
  };

  const source = github(REPO, { branch: BRANCH });

  // Runs `alembic upgrade head` before the web container starts (pre-deploy = the "migrate" step).
  // The web service is always on, so it is the natural owner of migrations; cron services only
  // start on schedule and would otherwise race each other.
  const web = service("web", {
    source,
    start: "python -m pipeline web",
    preDeploy: "alembic upgrade head",
    healthcheck: "/health",
    healthcheckTimeout: 120,
    env,
  });

  // The web service also runs the agents (pipeline/worker.py): QA, the Editor and the Lister react
  // to their queues within seconds, the Researcher/Creator/Analyst/Fulfiller keep their schedule, and
  // "Run now" in the dashboard needs no Railway token. Separate cron services are therefore optional.
  // Set OPTIONAL_CRONS=1 when applying if you still want them as a belt-and-braces backstop (their
  // runs are harmless duplicates: whichever process claims a row first does the work).
  const crons = process.env.OPTIONAL_CRONS
    ? [
        service("researcher", { source, start: "python -m pipeline run researcher", deploy: { cronSchedule: "0 13 * * 1", restartPolicyType: "NEVER" }, env }), // Mon 6:00 AM Phoenix
        service("daily-pipeline", { source, start: "python -m pipeline run daily", deploy: { cronSchedule: "0 14 * * *", restartPolicyType: "NEVER" }, env }), // 7:00 AM Phoenix
        service("editor", { source, start: "python -m pipeline run editor", deploy: { cronSchedule: "30 * * * *", restartPolicyType: "NEVER" }, env }),
        service("analyst", { source, start: "python -m pipeline run analyst", deploy: { cronSchedule: "0 3 * * 1", restartPolicyType: "NEVER" }, env }), // Sun 8:00 PM Phoenix
        service("fulfiller", { source, start: "python -m pipeline run fulfiller", deploy: { cronSchedule: "0 * * * *", restartPolicyType: "NEVER" }, env }),
      ]
    : [];

  const data = group("Data", [db, storage]);
  const resources = crons.length ? [data, web, group("Optional crons", crons)] : [data, web];

  return project("etsy-agent-pipeline", { resources });
});
