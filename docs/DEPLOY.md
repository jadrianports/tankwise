# Deploying TankWise

An operator runbook for provisioning TankWise's free-tier production stack: Neon
(Postgres), then Upstash (Redis), then the Render Blueprint, then live
verification. This is the exact order the four services depend on each other in
— Render's Blueprint needs both connection strings in hand before it can even
prompt you for them.

Follow the sections top to bottom, in one sitting if you can. Each checkbox is a
real step; tick it as you go.

## Read this first

Three facts change how you should treat this deploy, all specific to a
brand-new service's *first* deploy:

- **There is nothing to roll back to.** Render's zero-downtime rollback keeps
  routing traffic to the previous instance if a new deploy's health check
  fails — but that protection only exists starting with the *second* deploy.
  On the first deploy there is no previous instance, so a failed health check
  simply leaves nothing running. Recovery here is "fix the value in the
  dashboard and trigger a redeploy," not "traffic quietly stays on an older
  working version."
- **Render gives the health check up to 15 minutes before it gives up.** Once
  the container starts, Render polls `/api/ready` for up to 15 minutes before
  it cancels the deploy. The batched first-boot station seed (Task 15-01)
  completes in well under a minute, so there's wide safety margin here even
  against a slow Neon cold start — this is a generous budget, not a ticking
  clock. *(Updated 2026-08-09: the seed now runs on every boot, not only the
  first (plan 22-09) — measured steady-state median 2.115s, range
  1.915s–2.805s, against the combined post-import dataset. Still a small
  fraction of the 15-minute window.)*
- **A deploy that looks idle right after you push isn't stuck — it's waiting
  on CI.** `render.yaml` sets `autoDeployTrigger: checksPass`, so a push to
  `main` only starts a Render deploy once every GitHub Actions check on that
  commit reports green. If you push and the Render dashboard shows nothing
  happening for a couple of minutes, that's CI running, not a hung deploy.

**One more thing that isn't a show-stopper on Render's side, but is a
precondition for all of it:** Render deploys whatever is on `origin/main` on
GitHub, not your local working tree. If your local `main` has commits that
haven't been pushed yet, push them first — the CI-gated auto-deploy above
only ever sees, and only ever waits on checks for, the commit that's actually
on GitHub. The pre-flight block appended to the end of this document records
whether that was true at the time this runbook was prepared.

**Absolute rule for this whole document:** it names environment variables and
where their values come from. It never contains a credential value — no
connection string, password, token, or secret key — not even transiently
while debugging a failed deploy. If you paste a real value into a terminal or
a support thread while troubleshooting, don't paste it back into this file
afterward.

---

## Section 1 — Neon (Postgres)

1. [ ] Create a free Neon project (neon.com). Any region is fine; Render's
       `region: oregon` in `render.yaml` is independent of Neon's region —
       there's a small latency cost to a cross-region pair, not a
       correctness one.
2. [ ] On the project's Connection Details panel, Neon shows **two** distinct
       connection strings for the same database. This is the first show-stopper
       to get right:

   | Which string | Hostname shape | Goes into |
   |---|---|---|
   | **Pooled** (default/most prominent in the dashboard) | `ep-<name>-<id>-pooler.<region>.aws.neon.tech` — note the `-pooler` token | `DB_HOST` |
   | **Direct / unpooled** | `ep-<name>-<id>.<region>.aws.neon.tech` — same hostname, no `-pooler` token | `DB_MIGRATE_HOST` |

   The `-pooler` token in the hostname is the one thing to look for. Neon's
   pooled endpoint runs PgBouncer in transaction mode, which drops
   session-level features (`SET`, `SQL PREPARE`, advisory locks) that
   `manage.py migrate` depends on — `entrypoint.sh` already runs `migrate`
   against `DB_MIGRATE_HOST` specifically so it never touches the pooler. Get
   this backwards (both env vars pointed at the `-pooler` host) and
   migrations can partially succeed, then fail later on a more
   session-feature-dependent migration, on some deploy that isn't
   necessarily the first one.
3. [ ] Copy each connection string **exactly as Neon displays it** — don't
       hand-retype or reconstruct it. The generated string can carry
       parameters beyond `sslmode=require` (e.g. `channel_binding=require`)
       that the driver expects to see.
4. [ ] `render.yaml` takes the connection as four separate env vars, not one
       URL. Split the string Neon gave you like this (using the pooled
       string's own example shape — yours will have real values in every
       `<...>` slot):

   ```
   postgresql://<user>:<password>@<host>/<dbname>?sslmode=require
   ```

   | Piece of the string | Env var |
   |---|---|
   | `<host>` (the `-pooler` one) | `DB_HOST` |
   | `<host>` (the direct one, from the *other* connection string) | `DB_MIGRATE_HOST` |
   | `<dbname>` | `DB_NAME` |
   | `<user>` | `DB_USER` |
   | `<password>` | `DB_PASSWORD` |

   `DB_PORT` (`5432`) and `DB_SSLMODE`-equivalent behavior are already
   committed, non-secret values in `render.yaml` — you won't be prompted for
   them.

---

## Section 2 — Upstash (Redis)

1. [ ] Create a free Upstash Redis database (upstash.com). Same
       region-independence note as Neon applies here.
2. [ ] Upstash's database page shows **two** credential pairs for the same
       database — the second show-stopper:
       - A **REST API** URL + token pair, for HTTP/edge access.
       - A **Redis** (TCP) connection string, for the standard Redis wire
         protocol.

   `django-redis` (this app's cache backend) speaks the TCP protocol only.
   Open the **"Redis"** connection tab — not "REST API" — and copy the
   string that starts `rediss://` (double-`s`: TLS, which Upstash cannot
   disable). It has this shape:

   ```
   rediss://default:<password>@<endpoint>:<port>
   ```

3. [ ] That whole string is the value for `REDIS_URL`. Do not use the REST
       URL or its token anywhere in this deploy.
4. [ ] If you get this wrong, the symptom is specific: `/api/ready`'s
       database check passes but its cache check fails, because the cache
       backend can't parse a REST URL as a Redis connection string at all.

---

## Section 3 — Render Blueprint

1. [ ] Push any unpushed local commits to `origin/main` first (see "Read this
       first" above) — Render's Blueprint reads from GitHub, not from a local
       checkout.
2. [ ] In the Render dashboard, connect the GitHub repository and create a
       new Blueprint from `render.yaml`. Render parses the file and shows you
       one prompt per `sync: false` key, in the order they appear in the
       file.
3. [ ] Enter each secret below. This table is generated from `render.yaml`'s
       own `envVars` list, in that file's own order, so a future edit to the
       Blueprint can't silently drift from this checklist without this table
       also changing:

   | Env var | Where the value comes from | Warning |
   |---|---|---|
   | `DJANGO_SECRET_KEY` | Generate a fresh random secret (e.g. `python -c "import secrets; print(secrets.token_urlsafe(50))"`) — never reuse the local dev fallback. | — |
   | `DB_HOST` | Neon, **pooled** connection string's host (the `-pooler` one). | Section 1, step 2. |
   | `DB_MIGRATE_HOST` | Neon, **direct/unpooled** connection string's host (no `-pooler` token). | Section 1, step 2 — this is the pairing most likely to be reversed by accident. |
   | `DB_NAME` | Neon connection string's database name. | — |
   | `DB_USER` | Neon connection string's username. | — |
   | `DB_PASSWORD` | Neon connection string's password. | — |
   | `REDIS_URL` | Upstash, the `rediss://` **TCP** string from the "Redis" tab. | Section 2 — never the REST API URL/token. |
   | `MAPBOX_TOKEN` | A **secret** Mapbox token (`sk.*`, or a default `pk.*` scoped with the right APIs) with Directions + Geocoding access, from mapbox.com. Server-side only. | Missing this makes every route request 502. |
   | `MAPBOX_PUBLIC_TOKEN` | A **public** Mapbox token that starts `pk.`, created without URL restrictions (this app's `map_url` is opened as a direct navigation with no `Referer` header — see README's "Mapbox tokens" section). | Both this and `MAPBOX_TOKEN` above must be set together, or `/api/route` 502s with "Service misconfigured." This exact pairing has bitten this project's own local dev before. |

4. [ ] `render.yaml` also carries twelve **non-secret** values (settings
       module, DB engine/port, cache backend, proxy count, throttle rates,
       gunicorn tuning, HSTS seconds) that are already committed in the
       Blueprint. You will not be prompted for these — if the dashboard
       shows fewer prompts than the nine secrets above, that's expected, not
       a sign something is missing.
5. [ ] `EIA_API_KEY` is **not** in the list above because it isn't in
       `render.yaml`'s `envVars` at all — it's optional. Leaving it unset is
       a valid choice: the app runs in frozen-snapshot pricing mode (the
       original 2024/2025 dataset prices, unindexed) and says so on
       `manage.py check` and in the API's own price-vintage disclaimer. Add
       it later as a Render environment variable (Environment tab, not the
       Blueprint's `sync: false` prompt flow) if you want live weekly EIA
       indexing; a free key is instant at eia.gov/opendata.
6. [ ] Create the Blueprint. This kicks off the first deploy once GitHub
       Actions checks on the pushed commit report green (see "Read this
       first").

---

## Section 4 — Watching the first deploy

Render's deploy log follows `entrypoint.sh`'s start sequence in order. Here's
what each stage looks like and what to do if it fails there.

1. [ ] **`migrate --noinput`.** The log line shows the command running.
       Don't just check for a "migrations applied" success message — that can
       appear even if `DB_MIGRATE_HOST` was accidentally set to the pooled
       host (some migrations succeed against the pooler before a
       session-feature-dependent one fails). If you can, confirm which host
       the migrate step actually targeted.
       - *If it fails here:* almost always a Neon credential problem (wrong
         host, wrong password) or the pooled/direct mixup from Section 1.
         Fix the value in the Render dashboard's Environment tab and trigger
         a manual redeploy — no code change needed.
2. [ ] **Station seed.** The log shows the seed step running on every boot —
       not only a genuinely first boot. This step is the batched
       `bulk_create`/`bulk_update` upsert from plan 15-01 — a handful of
       queries, not one per CSV row — so it should finish in well under a
       minute even against Neon's cold-start latency.
       - *If it fails here:* a Neon connectivity issue that migrate itself
         didn't catch, or (much less likely, since this path is
         idempotent-by-`opis_id`) a data problem in a committed CSV.
       - *(Updated 2026-08-09: this step previously ran only on a genuinely
         first boot, behind an empty-table guard that printed
         `Station table empty -- seeding from committed CSV...`. Plan 22-09
         removed that guard — the seed is now unconditional, on every boot,
         so a committed dataset change is always applied without a manual
         step. Measured steady-state cost: 2.115s median, range
         1.915s–2.805s, against the combined OPIS + Overture dataset.)*
3. [ ] **`exec gunicorn` binds the port.** The log shows gunicorn starting
       its configured worker count. This is the point where `/api/ready`
       first becomes reachable at all — nothing before this line can be
       polled by Render's health check.
4. [ ] **Render's health check turns green.** Render polls `/api/ready`
       (healthCheckPath in `render.yaml`) until it returns 200, for up to 15
       minutes, before marking the deploy live.
       - *If it never turns green:* check `/api/ready`'s response body
         directly (see Section 5) rather than guessing from the dashboard
         alone — a non-200 there almost always means one of `MAPBOX_TOKEN`
         / `MAPBOX_PUBLIC_TOKEN` is missing or malformed (the `tokens` check
         requires the public one to start with `pk.`), or a Redis
         credential problem from Section 2.

---

## Section 5 — Live verification

This section is what plan 15-08 executes once the service is live. Three legs,
covering API, infra, and UI:

1. [ ] **Bruno collection against the live base URL.** Point `bruno/`'s
       environment at `https://<your-render-host>` instead of
       `http://localhost` and run the existing collection
       (`bruno/*.bru`) to confirm the API contract holds against real
       infrastructure, not just CI's SQLite/Postgres-parity jobs.
2. [ ] **`GET /api/ready`, read the body, not just the status code.** A bare
       `200` is not sufficient proof — the response body's `checks` object
       reports `db`, `cache`, and `tokens` independently:

   ```json
   {"status": "ready", "checks": {"db": true, "cache": true, "tokens": true}, "station_count": 6738}
   ```

   A service that's technically up but pointed at bad credentials can still
   answer HTTP requests; `/api/ready` is deliberately the one place that
   fails loudly (503, with the specific check(s) reporting `false`) instead
   of masking a broken dependency behind a 200. Read all three booleans
   before calling the deploy verified.
3. [ ] **One real browser route.** Open the live URL, plan one real trip
       (a demo chip is fine), and confirm the map renders and a fuel plan
       comes back.
4. [ ] **Confirm the CI gate is actually live, not just declared.** Push a
       trivial commit to `main` (a comment, a whitespace fix — anything
       that still passes CI) and watch the Render dashboard: the deploy
       should visibly wait until GitHub Actions reports all checks green on
       that commit before it starts. If Render starts building immediately
       on push instead of waiting, the `autoDeployTrigger: checksPass`
       field didn't take effect as expected and is worth a second look.

---

## Pre-flight verified (2026-07-25, 12:03–12:12 UTC)

Run immediately before opening the provisioning checkpoint, in the same
session, against the tree at commit `f44bd3a` (this doc's own runbook
commit). Re-run this block and re-date it if it's been more than ~30 minutes,
or if any push has landed on `main`, before the checkpoint is presented.

- [x] `manage.py test` — **405 tests, 0 failures**, exit 0.
- [x] `npm test --prefix frontend` — **257 tests / 34 files, 0 failures**,
      exit 0.
- [x] `npm run typecheck --prefix frontend` — exit 0, no errors.
- [x] `npm run lint --prefix frontend` (oxlint) — exit 0, no errors.
- [x] `npm run build --prefix frontend` — exit 0. `frontend/dist/og-card.png`
      exists; `frontend/dist/index.html` contains `twitter:card`.
- [x] `git status --short` — clean working tree.
- [x] `docker compose down -v && docker compose up --build -d` — a genuine
      cold boot from a destroyed SQLite volume, so the conditional
      first-boot seed path actually ran (not skipped by an already-populated
      table).
  - Container start (`docker inspect` `StartedAt`): `2026-07-25T12:10:53Z`.
  - `migrate` (13 migrations) + the batched 6,738-row `seed_stations` upsert
    completed and gunicorn began listening at `2026-07-25T12:11:03Z` —
    **~10 seconds** from container start to the port being bound and
    `/api/ready` first becoming reachable at all.
  - Docker Compose's own healthcheck (against `/api/health`, `start_period:
    30s`, polling every 10s) first reported the container `healthy` at
    **~53 seconds** wall-clock from the `docker compose up` invocation —
    that number reflects the healthcheck's own polling schedule, not app
    slowness; the app itself was ready to serve traffic roughly 43 seconds
    before Compose's healthcheck happened to notice.
  - Either number sits at a small fraction of Render's 15-minute health-check
    cancel window — this is the concrete evidence behind this doc's
    "wide safety margin" claim in "Read this first."
- [x] `GET /api/ready` against the cold stack — HTTP 200:

  ```json
  {"status": "ready", "checks": {"db": true, "cache": true, "tokens": true}, "station_count": 6738}
  ```

- [x] `POST /api/route` (Dallas → Los Angeles) against the cold stack —
      returned a solved plan: `total_route_mi: 1437`, `4` fuel stops,
      non-empty `total_cost`. Confirms Mapbox + solver + Redis cache all
      work end-to-end against a freshly-seeded database, not just the
      readiness probe's shallow checks.

### One real gap this pre-flight surfaced — read before opening the checkpoint

**Local `main` is 58 commits ahead of `origin/main`, all unpushed.** This
runbook's commit (`f44bd3a`) and everything back through the start of this
phase's work exist only in this local checkout. `origin/main`'s current tip
is `c90596a` (last pushed 2026-07-22), which has a confirmed green CI run —
but that SHA predates this entire phase and does not carry the complete v3
product (multi-stop, elevation, cold-start UX, meta tags, or this runbook
itself).

Render's Blueprint deploys from GitHub, and `autoDeployTrigger: checksPass`
only ever waits on checks for whatever commit is actually pushed. **Pushing
local `main` to `origin/main` is a required step before Section 3 can
produce a deploy of the complete product** — and it is a plain `git push`,
not an account-creation or dashboard action, so it does not itself cross the
D-04 provisioning boundary. It's called out here rather than silently
assumed because skipping it would mean the very first live deploy ships an
old, incomplete tree.

Once pushed, re-verify the freshness condition above (SHA + ~30 minute
window) against the *newly pushed* SHA and its CI run before treating this
pre-flight as still current — the tests above were run against the local
tree, not against a pushed-and-CI-checked commit.

---

## Deployed (2026-07-25, ~14:12 UTC)

The Render Blueprint went live. Deploy `dep-d9ic8c6rnols73f1rtfg`, assigned
hostname **`tankwise.onrender.com`** — the unsuffixed default (D-06).

- `GET https://tankwise.onrender.com/api/ready` — HTTP 200:

  ```json
  {"status": "ready", "checks": {"db": true, "cache": true, "tokens": true}, "station_count": 6738}
  ```

- EIA weekly price indexing came up live in production on first boot (the
  operator set `EIA_API_KEY` in the Render dashboard, outside the Blueprint's
  `sync: false` prompt flow, exactly as Section 3 step 5 describes):
  `price_index_status: "current"`, `eia_week: "2026-07-20"`.
- No divergence was reported between the real Neon, Upstash, and Render
  dashboards and the sections above — nothing in this document needed
  correcting.
- Whether *this particular* first deploy waited on GitHub Actions checks
  before starting is inconclusive, and is left open rather than claimed:
  Blueprint creation itself triggers a service's first deploy, independent of
  `autoDeployTrigger: checksPass`, and CI on the deployed commit had already
  been green for the better part of an hour before the Blueprint was
  created. This one deploy can't prove the gate either way. Section 5 step 4
  — push a trivial commit and watch the dashboard hold for CI — is what
  actually exercises the gate, and remains to be done.

Plan 15-08 consumes `tankwise.onrender.com` as the literal hostname for
every remaining host-dependent value (`og:url`/`og:image`, the live Bruno
environment, the README badge).

