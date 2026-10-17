# Deploying the EMS to Railway

This guide takes the project from a git repo to a running production system with
**PostgreSQL, Redis, the web app, a Celery worker and Celery Beat** — which is
what makes the nightly attendance sweep actually run.

Follow it in order. Everything here has been checked against the code in this
repository; where a step is easy to get wrong it is called out explicitly.

---

## 1. How the deployment is shaped

Railway runs **one service per process**. A single container cannot be the web
app and the worker at once, and Beat must never be duplicated.

You will create **four services from the same repository and the same image**.
They differ only in the command they run:

| Service | Command | Replicas | Purpose |
| --- | --- | --- | --- |
| `web` | `/app/docker-entrypoint.sh web` | 1+ | Gunicorn serving HTTP |
| `worker` | `/app/docker-entrypoint.sh worker` | 1+ | Runs queued jobs |
| `beat` | `/app/docker-entrypoint.sh beat` | **exactly 1** | Schedules the nightly jobs |
| *(release)* | `/app/docker-entrypoint.sh release` | — | Migrations, runs once per deploy |

```
                    ┌──────────────┐
   Internet ───────▶│     web      │──┐
                    └──────────────┘  │
                    ┌──────────────┐  │   ┌───────────────┐
                    │    beat      │──┼──▶│     Redis     │
                    └──────────────┘  │   │ broker + queue│
                    ┌──────────────┐  │   └───────┬───────┘
                    │   worker     │◀─┘           │
                    └──────┬───────┘              │
                           │                      │
                           ▼                      ▼
                    ┌──────────────────────────────────┐
                    │           PostgreSQL             │
                    └──────────────────────────────────┘
```

**Why the release step is separate.** Migrations run in `preDeployCommand`, once,
before the new container receives traffic. If migrations lived in the web start
command instead, every replica would run them simultaneously on each deploy.

---

## 2. Prerequisites

* The project pushed to GitHub (Railway deploys from a repo).
* A Railway account.

> **Before anything else: commit the project.** `git log` currently has **zero
> commits**, so there is nothing for Railway to build. See §3.

---

## 3. Commit the project

```bash
cd ems
git add -A
git commit -m "EMS: initial deployable state"
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main
```

`.env` is git-ignored and must stay that way — it holds your database password.
`.dockerignore` also excludes it from the image, so local secrets can never leak
into a build.

---

## 4. Create the project and add the databases

1. Railway → **New Project** → **Deploy from GitHub repo** → pick the repo.
2. Cancel the first build if it starts (the service has no variables yet).
3. **New** → **Database** → **Add PostgreSQL**.
4. **New** → **Database** → **Add Redis**.

Two variables now exist inside the project: `DATABASE_URL` (from Postgres) and
`REDIS_URL` (from Redis). You will reference them, not copy them, so credentials
rotate with the plugin.

---

## 5. Generate the secrets

Run this twice — `SECRET_KEY` and `JWT_SECRET_KEY` **must differ**:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The app refuses to start in production if either is under 32 characters, if
`DATABASE_URL` is not PostgreSQL, if `CORS_ORIGINS` is missing or contains `*`,
or if `JWT_COOKIE_SECURE` is false. That failure is deliberate: it is far better
than running with a weak secret.

---

## 6. Configure the `web` service

Open the service that Railway created from your repo and rename it `web`.

### Variables

Use **Variable Reference** (`${{Postgres.DATABASE_URL}}`) rather than pasting
values, so credentials stay managed by Railway.

| Variable | Value |
| --- | --- |
| `FLASK_CONFIG` | `production` |
| `SECRET_KEY` | first generated secret |
| `JWT_SECRET_KEY` | second generated secret |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `RATELIMIT_STORAGE_URI` | `${{Redis.REDIS_URL}}` |
| `REDIS_SSL_CERT_REQS` | `none` |
| `CORS_ORIGINS` | your public URL, e.g. `https://ems-production.up.railway.app` |
| `APP_BASE_URL` | the **same** public URL |
| `APP_NAME` | your organisation's name |
| `HR_CONTACT` | e.g. `the HR office` |
| `JWT_COOKIE_SECURE` | `true` |
| `EMAIL_ENABLED` | `true` |
| `EMAIL_BACKEND` | `smtp` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your SMTP username |
| `SMTP_PASSWORD` | your SMTP password / app password |
| `SMTP_FROM` | e.g. `no-reply@yourdomain.com` |
| `SMTP_TLS` | `true` |
| `DEFAULT_TIMEZONE` | `Africa/Nairobi` |

### Two settings that are easy to miss

**`APP_BASE_URL` is critical.** Every sign-in link in the welcome email is built
from it. Leave it as the default and **every new employee receives a link to
localhost** and cannot sign in.

**`REDIS_SSL_CERT_REQS=none`** is only needed if Railway gives you a `rediss://`
(TLS) URL. Celery's Redis client otherwise demands a local CA file and fails with
a certificate error. It is harmless to set even for a plain `redis://` URL, and
it only relaxes certificate *verification for the Redis connection inside
Railway's private network* — it does not affect HTTPS to your users.

**`DATABASE_URL` needs no editing.** Railway provides a bare
`postgresql://...` with no driver, which SQLAlchemy 2.x would resolve to
psycopg 3 — a package this project does not install, causing
`ModuleNotFoundError: No module named 'psycopg'` at startup. The config
normalises any PostgreSQL URL (`postgres://`, `postgresql://`, or an explicit
driver) onto the `psycopg2` driver that `psycopg2-binary` provides, so the value
Railway gives you works as-is. `python tools/check_database_url.py` verifies it.

### Build and deploy settings

The repo already contains `railway.toml`, which sets the Dockerfile builder, the
healthcheck path (`/health`), the start command and the release command. Leave
**Settings → Build → Builder** on *Dockerfile*.

Confirm **Settings → Networking → Public Networking** is enabled and note the
generated domain — it is what goes in `CORS_ORIGINS` and `APP_BASE_URL`.

---

## 7. Create the `worker` service

1. **New** → **GitHub Repo** → same repo. Rename it `worker`.
2. Variables: **copy every variable from `web`** except the networking ones
   (`CORS_ORIGINS` is not used by a worker, but leaving it is harmless).
3. Settings → Deploy → **Custom Start Command**:

   ```
   /app/docker-entrypoint.sh worker
   ```

4. Set `CELERY_CONCURRENCY` (default `4`). Each worker process holds a database
   connection, so keep `CELERY_CONCURRENCY` × replicas well under your Postgres
   connection limit.

---

## 8. Create the `beat` service

1. **New** → **GitHub Repo** → same repo. Rename it `beat`.
2. Variables: the same again.
3. **Custom Start Command**:

   ```
   /app/docker-entrypoint.sh beat
   ```

4. **Leave replicas at exactly 1.**

> ### This is the step that breaks the nightly job if you get it wrong
>
> Celery Beat is the scheduler. If **two** Beat instances run, every scheduled
> job fires **twice** — the nightly attendance sweep would run twice, the email
> retry would run twice, and the balance allocation could double-allocate.
>
> Never scale `beat` beyond one replica. If you ever need redundancy, run one
> Beat that dispatches to many workers — which is exactly this layout: **one
> beat, many workers.** Scale `worker` instead.

---

## 9. Deploy

Trigger a deploy (push a commit, or **Deploy** in the dashboard). The order
Railway follows:

1. Build the Docker image.
2. Run `preDeployCommand` → `flask --app wsgi db upgrade` (migrations).
3. Start the container.
4. Poll `/health` until it returns 200.

Migrations are idempotent: Alembic records the revision and skips applied ones,
so this is safe on every deploy.

---

## 10. First-run bootstrap (do this before creating any account)

A brand-new database is **empty**. Migrations must run before anything else,
and the default rule ladders must exist before attendance or leave will work.
If you skip this you will see `relation "users" does not exist`.

Normally the `preDeployCommand` runs migrations for you. If it did not — or you
want to be certain — run all three steps in the **web** service shell:

```bash
flask --app wsgi db upgrade        # 1. create the schema
flask --app wsgi seed-defaults     # 2. shift policy + rule ladders (no accounts)
flask --app wsgi create-admin --email you@yourdomain.com   # 3. your account
```

That is the entire bootstrap. The last command prompts for a password twice
(hidden). To confirm it worked:

```bash
curl -s localhost:$PORT/health     # {"status":"ok","database":"up"}
flask --app wsgi check-schema      # must print "Schema matches the models"
```

### After EVERY deploy: `db upgrade`

A deploy that lands before its migration is the most confusing failure this app
can produce. Every query touching a new column fails, and the error shows up far
from its cause — a punch that is simply "not recorded", or *"The correction could
not be saved."* — with nothing pointing at the database.

So after any deploy that includes a migration:

```bash
flask --app wsgi db upgrade
flask --app wsgi check-schema
```

`check-schema` compares every model column against the database and names what is
missing:

```
alembic revision : 8b1f4c7a2d90
MISSING COLUMNS — the code expects these
  attendance_daily.sessions
The database is behind the code. Run:
  flask --app wsgi db upgrade
```

The app also logs this **at startup**, so a stale schema is visible in the deploy
log rather than only when a user hits the broken screen:

```
ERROR  DATABASE IS BEHIND THE CODE — missing_columns=['attendance_daily.sessions']
```

It never refuses to start: a running process that can serve sign-in and the
health probe is more useful than a crash loop.

### If punches were stranded by an earlier failure

Punches are now committed **before** attendance is resolved, so a later failure
no longer discards them. Replay anything left unprocessed:

```bash
flask --app wsgi reprocess-punches
```

Idempotent, so running it repeatedly is safe.

### Why step 2 matters

`seed-defaults` creates the configuration the app assumes exists:

| Creates | Without it |
| --- | --- |
| Global shift policy (09:00–17:00, 10 min grace) | Attendance is never classified as late or on time |
| Leave rules (annual 21, sick 14, maternity 90, paternity 14, unpaid) | Every leave request is rejected for a missing rule |
| Attendance colour ladder (green/yellow/orange/red) | The badge never escalates |
| Disciplinary rules | No warnings or suspensions are ever raised |

It creates **no accounts**, is safe in production, and is idempotent — running it
again changes nothing. `flask seed` and `flask seed-minimal` still refuse to run
in production because they create `admin@ems.local` with a published password.

> **A fresh deployment therefore needs exactly three commands**: `db upgrade`,
> `seed-defaults`, `create-admin`. Everything else is optional.

### If migrations never ran automatically

Check the deploy log for the `release` step. Two common causes:

1. **`preDeployCommand` was not honoured.** It runs as a separate step before the
   container starts. Confirm it appears in the deploy log; if not, run
   `flask --app wsgi db upgrade` manually as above.
2. **An invalid `railway.toml`.** Railway ignores the whole file if any key is
   wrong, silently dropping `preDeployCommand`. The committed file is valid;
   note that replica counts are deliberately *not* set there (set them in the
   dashboard) because `[deploy.resources] replicas` is not part of the schema.

Since v1.0 the container also **self-heals**: on start, `web` and `worker` check
for the `alembic_version` table and run migrations themselves if the schema is
missing entirely. So a truly empty database now bootstraps on first deploy
rather than failing every request. `seed-defaults` is still a separate step.

---

## 11. Create the first administrator

**Do not run `flask seed` in production.** It creates `admin@ems.local` with the
published default password `ChangeMe123!`. For that reason
`seed`/`seed-minimal` **refuse to run when `FLASK_CONFIG=production`**.

Create a real administrator instead (step 3 above):

```bash
flask --app wsgi create-admin --email you@yourdomain.com
```

It prompts for the password twice, hidden, and enforces the app's strength rules.
The account is flagged `must_change_password`, so the first sign-in forces a
change anyway. Pass `--role HR` to create an HR user rather than an admin.

Re-running the command for an existing email resets that password (and flags a
forced change), which doubles as an account-recovery path.

### Migrating existing data

If you already have local data worth keeping:

```bash
# locally
pg_dump "$LOCAL_DATABASE_URL" --no-owner --no-acl -f ems.sql
```

Upload `ems.sql` to the web service shell and run:

```bash
psql "$DATABASE_URL" -f ems.sql
```

If you import real employee data, **record each employee's gender** on
**Employees → Record gender** — until then neither maternity nor paternity leave
is available to them.

---

## 12. Verify the deployment

### The app answers

```bash
curl https://<your-domain>/health
# {"status":"ok","database":"up","version":"1.0.0"}
```

A `503` here means the database is unreachable — check `DATABASE_URL`.

### The bare domain works

Open `https://<your-domain>/` in a browser. It should redirect to the sign-in
page — or straight to your dashboard if already signed in. If it 404s, the
running revision predates the entry-point routes.

The short forms `/login` and `/logout` also work, so a link to the obvious URL is
never a dead end.

### The worker is consuming the queue

In the **worker** service logs you should see:

```
[entrypoint] starting celery worker (concurrency=4)
celery@... ready.
```

Then trigger a task from the **web** shell:

```bash
flask --app wsgi run-job email_dispatch
```

That runs in-process. To prove the *broker* works, queue something from the web
shell and watch it appear in the worker logs:

```bash
python -c "
from celery_worker import celery
celery.send_task('app.jobs.notifications_dispatcher.dispatch_pending')
print('queued')
"
```

If `queued` prints and the worker log shows the task, **Redis is wired correctly**.

### The schedule is live

In the **beat** service logs:

```
[entrypoint] starting celery beat (schedule=/tmp/celerybeat-schedule)
Scheduler: Sending due task nightly-attendance ...
```

### The nightly sweep actually ran

The nightly job is scheduled for **02:00 in `DEFAULT_TIMEZONE`**. To avoid
waiting until then, run it on demand from the web shell:

```bash
flask --app wsgi run-job nightly_attendance
```

Expected output:

```
nightly_attendance complete.
```

Then confirm in the database:

```sql
SELECT work_date, status, count(*)
FROM attendance_daily
GROUP BY work_date, status
ORDER BY work_date DESC
LIMIT 20;
```

---

## 13. The nightly schedule, for reference
| Job | Schedule (`DEFAULT_TIMEZONE`) | What it does |
| --- | --- | --- |
| `notifications-dispatcher` | every 60s | Delivers in-app notifications |
| `email-dispatch` | every 60s | Retries the email outbox |
| `device-health` | every 5 min | Flags offline biometric devices |
| `nightly-attendance` | **02:00** | Marks absences, applies absence cut-offs |
| `color-recompute` | 02:30 | Recomputes attendance badges |
| `disciplinary-evaluator` | 03:00 | Raises disciplinary actions |
| `allocate-balances` | 1 Jan 00:05 | Allocates the year's leave balances |

The sweep runs at 02:00 for the *previous* day, in the employee's own timezone,
so a shift ending at midnight is still settled before the next morning.

---

## 14. Connecting a biometric terminal

ZKTeco terminals that support **ADMS** push their records to a server you
configure. The MB20-VL lists ADMS as a standard function, so it can talk to this
deployment directly — no local PC or SDK bridge.

### What to type into the device

Register the device first on **Devices** in the app. Registering (or rotating)
shows a **Comm Key** — a six-digit number. That number is the credential, and it
is shown **once**; it is stored hashed.

| Device menu field | Value |
| --- | --- |
| Server Address | your Railway domain, **without** `https://`, e.g. `kenems.up.railway.app` |
| Server Port | `443` (plain `80` also works) |
| Comm Key | the six-digit number shown when you registered the device |

The menu path varies by firmware — usually **Menu → Comm. → Cloud Server
Setting**, **ADMS**, or **Communication Password**. The device's own serial
number (Menu → Info → Device Serial Number) must match the one you registered
**exactly** — it is case-sensitive.

**Where the Comm Key comes from.** It is not issued by the device. It is a
shared secret this system generates when you register the device, and you type
the same value into both sides. It is deliberately **numeric and six digits**
because a terminal's keypad accepts digits only — a long alphanumeric token
could not be entered on the hardware.

**If you lose it:** the value cannot be recovered (only its hash is stored).
Rotate the device's secret in the app and set the new number on the device.

**If your device has no communication password set**, leave it at the factory
value. It will still be accepted — that is how most terminals ship, and refusing
it would mean no attendance data at all. To move a device off that default,
rotate the secret and type the new number in.

### Check reachability before touching the device

```bash
curl https://<your-domain>/iclock/ping
# OK
```

That endpoint is deliberately unauthenticated and exposes nothing, so you can
confirm the path from a browser on the same network as the device.

> **`/iclock/ping` returning OK does not mean the device is connected.** It only
> proves the server is reachable. A device shows as online only once it has
> successfully *authenticated* — a rejected device is never marked seen.

### Confirm the server side works before blaming the device

```bash
python tools/simulate_device.py https://<your-domain> <SERIAL> <COMM_KEY>
```

It makes the device's exact call sequence. If it reports `all calls succeeded`,
the endpoint and credentials are good and any remaining problem is the device's
own configuration. If it fails, the server or the credentials are at fault.

### Check whether the device is reaching us at all

Filter the **web** service logs for `ADMS`:

* `ADMS request refused SN=... path=/iclock/cdata` — the device **is** connecting;
  its serial or Comm Key is wrong.
* `ADMS handshake SN=...` — the device authenticated and will start pushing.
* Nothing at all — the device is not reaching the server.

### How it works

The device drives the protocol; the paths are its choice, not ours:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/iclock/cdata?SN=…` | Handshake — tells the device to start pushing |
| POST | `/iclock/cdata?SN=…&table=ATTLOG` | Attendance records |
| GET | `/iclock/getrequest?SN=…` | Device polling for commands |
| GET | `/iclock/ping` | Reachability probe |

Replies are **plain text**: `OK` means stored. Anything else makes the device
keep the record and retry — which is what happens on a failure. Ingestion is
idempotent per punch, so a retry cannot create duplicates.

### Why there is a separate ADMS endpoint

The JSON webhook at `/hooks/biometrics/punch` cannot serve real hardware:

* A device posts `application/x-www-form-urlencoded`, not JSON.
* A device **cannot set custom headers**, so it cannot send the
  `X-Device-SN` / `X-Device-Secret` pair that webhook requires. Its serial
  number arrives in the body and its key arrives as `CommKey`.
* It expects plain text and cannot read an HTML or JSON error page.

The `/iclock` routes exist for exactly these reasons and should be left open
(they authenticate on the CommKey).

### Troubleshooting

| Symptom | Cause |
| --- | --- |
| Device shows "connect failed" | No internet on the device's network, or the address still has `https://` in it |
| Device shows **offline** but `/iclock/ping` is OK | Ping needs no credentials and marks nothing seen. The device has not authenticated — check its Comm Key |
| `/iclock/ping` works but no data | The serial number on the device does not match the registered one |
| Punches arrive, no attendance rows | The device's user ID is not mapped — use **Devices → map** |
| Works on HTTP but not HTTPS | Older firmware only supports HTTP on port 80; plain `http://` on 80 also works here |
| Everything refused with 401 | Wrong Comm Key, or the device was deactivated |
| Lost the Comm Key | It cannot be recovered (only its hash is stored). Rotate the secret and set the new number on the device |

## 15. Troubleshooting

**`ModuleNotFoundError: No module named 'psycopg'`**

This was a real failure on first deploy. Railway supplies a bare
`postgresql://...` with no driver, and SQLAlchemy 2.x resolves that to **psycopg
3**, which this project does not install — it ships `psycopg2-binary`.

The config now normalises every PostgreSQL URL form onto `psycopg2`, so the
value Railway gives you works unchanged. If you still see this error, the
running revision predates the fix — redeploy. Verify locally with:

```bash
python tools/check_database_url.py
```

**`Invalid production configuration: SECRET_KEY must be set...`**
The app refuses to boot with a weak or missing secret. Generate one and redeploy.

**`/usr/bin/env: 'bash\r': No such file or directory`**
`docker-entrypoint.sh` was committed with CRLF line endings, so Linux reads the
shebang as `bash\r`. Fix it once and permanently:

```bash
printf '*.sh text eol=lf\n' >> .gitattributes
git add --renormalize .
git commit -m "Normalise shell script line endings"
git push
```

Check with `git ls-files --eol docker-entrypoint.sh` — you want `i/lf w/lf`.

**`CORS_ORIGINS cannot contain '*' in production`**
Deliberate. Set the exact public origin, including the scheme.

**`sslmode` / certificate error on Redis**
Add `REDIS_SSL_CERT_REQS=none` and set `REDIS_URL` to the plugin's `rediss://` URL.

**Deploy succeeds, then the healthcheck fails**
Check the logs. A 503 from `/health` means the database is unreachable; a 500
usually means a missing variable. Remember every service needs its own copy.

**The nightly job never runs**
1. Is the `beat` service running? Its logs should print `starting celery beat`.
2. Are `web`, `worker` and `beat` pointing at the **same** `REDIS_URL`? A worker
   on a different Redis consumes nothing.
3. Is `beat` at exactly 1 replica?

**The nightly job runs twice**
More than one `beat` replica. This is the failure mode described in §8 — reduce
it to one.

**Emails link to `http://127.0.0.1:5000`**
`APP_BASE_URL` was not set on the service that sends mail (the **worker** must
have it too, since email delivery happens there).

---

## 16. Scaling notes

* **Scale `web`** by raising replicas. Sessions are JWT cookies, so no sticky
  sessions are needed.
* **Scale `worker`** by raising replicas or `CELERY_CONCURRENCY`. Jobs are
  written to be idempotent and safe to retry.
* **Never scale `beat`.**
* Watch Postgres connections: each Gunicorn thread and each Celery process holds
  one. `WEB_CONCURRENCY × WEB_THREADS` plus `CELERY_CONCURRENCY × worker
  replicas` should stay below the plan's connection limit.
* Railway's filesystem is **ephemeral** — nothing is stored on disk between
  deploys, which is why Beat's schedule file lives in `/tmp` and all real state
  is in PostgreSQL.
