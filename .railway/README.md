# Railway IaC

`railway.ts` declares the whole project (Postgres, bucket, web + 3 cron services) for the linked
environment. Run from the repo root:

```bash
npm install railway        # SDK that evaluates this file (no runtime dependency)
railway login && railway link
railway config plan        # preview per environment
railway config apply
```

Switch environments with `railway environment` and re-run plan/apply; the file renders different
caps/modes for `production` vs anything else (treated as staging).

Secrets are `preserve()`d: set them once in the dashboard (Variables tab of each service, or as
shared variables). See `../DEPLOY.md` for the full walkthrough and the `railway.json` deprecation
note.
