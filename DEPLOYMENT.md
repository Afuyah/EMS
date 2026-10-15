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

## 10. Create the first administrator

**Do not run `flask seed` in production.** It creates `admin@ems.local` with the
published default password `ChangeMe123!`. For that reason
`seed`/`seed-minimal` now **refuse to run when `FLASK_CONFIG=production`**.

Create a real administrator instead. Open the **web** service → **Shell**:

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

## 11. Verify the deployment

### The app answers

```bash
curl https://<your-domain>/health
# {"status":"ok","database":"up","version":"1.0.0"}
```

A `503` here means the database is unreachable — check `DATABASE_URL`.

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

## 12. The nightly schedule, for reference

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

## 13. Troubleshooting

**`Invalid production configuration: SECRET_KEY must be set...`**
The app refuses to boot with a weak or missing secret. Generate one and redeploy.

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

## 14. Scaling notes

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
