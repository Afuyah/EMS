# Employee Management System (EMS)

An internal HR platform for a mid-size organisation. It manages employees,
biometric attendance, rule-based leave with a suggestion engine, disciplinary
actions, notifications and reporting.

Employees punch in and out on biometric devices (ZKTeco, Hikvision, or anything
that can POST JSON). HR and admins work in a server-rendered web UI. Leave rules
are enforced automatically, and the system suggests the best window for a
requested leave based on the department calendar, the employee's balance and the
admin-configured constraints.

---

## Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12+ |
| Web | Flask 3 (application factory) |
| ORM / migrations | Flask-SQLAlchemy 3.1, Flask-Migrate 4 (Alembic) |
| Auth | Flask-JWT-Extended (access + server-side rotated refresh tokens) |
| Validation | Marshmallow |
| Database | PostgreSQL 14+ |
| Jobs | Celery 5 + Redis, Celery Beat |
| UI | Jinja2, Tailwind (CDN), HTMX, Alpine.js |
| Passwords | argon2id |

---

## Architecture

Feature-first, layered. The dependency chain is never violated:

```
Blueprint/View → Controller → Service → Repository → Model
                                 │
                                 ├→ Engine / State machine (pure)
                                 ├→ Adapter (external systems)
                                 └→ Other feature's Service
```

```
app/
  config.py               BaseConfig / Development / Testing / Production
  core/                   database, security, exceptions, error_handlers,
                          permissions, responses, time, logging
  common/                 base_repository, pagination
  features/
    auth/                 users, refresh_tokens, JWT callbacks
    employees/            departments, positions, employees
    biometrics/           devices, mappings, append-only raw logs, adapters/
    attendance/           daily rollups, shift policies, punch resolver engine
    leave/                requests, balances, pure engine + state machine
    rules/                colour ladder, disciplinary + leave rules, evaluators
    notifications/        inbox, pluggable senders, dispatcher
    reports/              single-query aggregations
    exports/              CSV output + bulk balance import
    docs/                 OpenAPI 3.0 document + Swagger UI
  views/                  HTML blueprints (/app/*)
  jobs/                   Celery tasks, each with a pure *_impl
  seed/                   idempotent seed data
  cli.py                  flask commands
  templates/ static/      Jinja2 templates and assets
migrations/               Alembic
tests/                    pytest suite
```

**Rules that hold throughout the codebase**

- Views and blueprints never import repositories or models; they call services.
- Services never import Flask HTTP primitives.
- Repositories are the only place with SQL.
- Engines and state machines are pure Python — no database, no Flask.
- Cross-feature reads go through the other feature's **service**.
- **Transaction boundary:** controllers, views and jobs commit; services only
  flush.

---

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env        # then edit DATABASE_URL / secrets

# 3. Create the database
createdb ems_dev

# 4. Apply migrations and seed
flask --app wsgi db upgrade
flask --app wsgi seed

# 5. Run
flask --app wsgi run --debug
celery -A celery_worker.celery worker -l info -B
```

> **Windows workers:** Celery's default *prefork* pool uses billiard, which
> passes messages between processes over named pipes. In a confined/sandboxed
> environment that raises `PermissionError: [WinError 5]`, so run the worker
> with the threads pool instead:
>
> ```bash
> celery -A celery_worker.celery worker -l info -B --pool=threads
> ```
>
> Linux/macOS hosts and containers should keep the default prefork pool.
> Celery only queues tasks here; it never executes them in-process.

Then open:

| URL | What |
| --- | --- |
| http://127.0.0.1:5000/app/login | Web UI |
| http://127.0.0.1:5000/app/rules | Lateness &amp; absenteeism rules (HR/Admin) |
| http://127.0.0.1:5000/api/docs/ui | Swagger UI |
| http://127.0.0.1:5000/api/docs/openapi.json | OpenAPI document |
| http://127.0.0.1:5000/api/v1/health | Health probe |

Seeded logins:

| User | Password | Role |
| --- | --- | --- |
| `admin@ems.local` | `ChangeMe123!` | ADMIN |
| `alice@ems.local` | `Password123!` | EMPLOYEE |
| `carol@ems.local` | `Password123!` | MANAGER |
| `david@ems.local` | `Password123!` | HR |

---

## Configuration

Read from `.env`; `FLASK_CONFIG` selects the class (default `development`).

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Flask session key |
| `JWT_SECRET_KEY` | JWT signing key (≥ 32 bytes) |
| `DATABASE_URL` | `postgresql+psycopg2://…` |
| `TEST_DATABASE_URL` | Database used by pytest |
| `REDIS_URL` | Celery broker / result backend |
| `RATELIMIT_STORAGE_URI` | `memory://` in dev, Redis in prod |
| `CORS_ORIGINS` | `*` in dev, an explicit list in prod |
| `JWT_COOKIE_SECURE` | `false` in dev, `true` in prod |
| `SMTP_*`, `SMS_ENABLED` | Notification transports |
| `EMAIL_BACKEND` | `console` (log only), `smtp` (real), `null` (tests) |
| `EMAIL_ENABLED` | Master switch for outbound email |
| `EMAIL_MAX_ATTEMPTS` | Retries before an email is parked as failed |
| `APP_BASE_URL` | Base URL used to build links inside emails |
| `DEFAULT_TIMEZONE` | Default employee timezone |

`ProductionConfig.validate()` fails fast when a production-critical setting is
missing or weak. CSRF protection is **off** in development (HTMX convenience)
and **on** in production, where the base template injects `X-CSRF-TOKEN` through
`hx-headers`.

---

## CLI

```bash
flask --app wsgi seed                       # idempotent full seed
flask --app wsgi seed-minimal               # admin + rules only
flask --app wsgi reset-demo                 # clear transactional data
flask --app wsgi allocate-balances --year 2026
flask --app wsgi balance-adjust --employee-no EMP-0001 --leave-type annual --entitled 25

flask --app wsgi device add --sn ZK-001 --location "Main door" --vendor zkteco
flask --app wsgi device rotate --sn ZK-001
flask --app wsgi device map --sn ZK-001 --employee-no EMP-0001 --device-user-id 42
flask --app wsgi device list

flask --app wsgi jobs                       # list jobs + beat schedule
flask --app wsgi run-job notifications_dispatcher
flask --app wsgi run-job nightly_attendance --arg work_date=2026-05-04
flask --app wsgi routes                     # print every URL rule
flask --app wsgi check                      # config + database diagnostics
```

Every job exposes a pure `*_impl` function and a `@celery.task` wrapper, so jobs
run from the CLI without a broker.

### Beat schedule

| When | Job |
| --- | --- |
| every minute | `notifications_dispatcher` |
| every 5 minutes | `device_health` |
| 02:00 daily | `nightly_attendance` |
| 02:30 daily | `color_recompute` |
| 03:00 daily | `disciplinary_evaluator` |
| Jan 1 00:05 | `allocate_balances` |

---

## Domain notes

### Biometrics

Two ways in, because hardware and software callers need different things.

**Physical terminals — the ZKTeco ADMS ("iClock") protocol.** Terminals that
support ADMS push to a server from their own menu. The MB20-VL lists ADMS as a
standard function. Point the device at your domain:

| Device menu field | Value |
| --- | --- |
| Server Address | the domain, without `https://` |
| Server Port | `443` |
| Comm Key | the device secret, shown once at registration |

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/iclock/cdata?SN=…` | Handshake, then the device pushes |
| POST | `/iclock/cdata?SN=…&table=ATTLOG` | Attendance records |
| GET | `/iclock/getrequest?SN=…` | Command polling |
| GET | `/iclock/ping` | Reachability probe (no auth, exposes nothing) |

Replies are plain text: `OK` means stored, anything else makes the device retry.
These routes exist as a separate endpoint because a device posts
form-encoded data, **cannot set custom headers**, and cannot read an HTML or JSON
error page — so the JSON webhook below is unusable for real hardware.

**Software callers — the JSON webhook.** Devices and bridges that *can* set
headers authenticate with `X-Device-SN` + `X-Device-Secret` (the secret is stored
hashed, salted with the serial) and post JSON to `/hooks/biometrics/punch`.
Vendor payloads are normalised by a pluggable adapter — ZKTeco, Hikvision and a
generic shape ship in `features/biometrics/adapters/`.

Every accepted punch from either path is written to `biometric_raw_logs`, which
is **append-only** and never updated or deleted. Idempotency uses

```
sha256(device_sn | device_user_id | punch_time_iso | punch_type)
```

inserted with `ON CONFLICT DO NOTHING`, so a device retrying after a network
blip cannot create a second attendance row and receives `duplicate: true`.

Punches for unmapped device users are acknowledged (HTTP 200) and audited, so
the device does not queue and retry forever.

### Leave (rule-based, with suggestions)

Rules are configured per department or globally: `max_per_year`,
`max_per_month`, `max_consecutive_days`, `max_employees_off_per_dept`,
`min_notice_days`, and `default_days` for special leave.

For a normal request:

1. The rule and balance are resolved.
2. `DeptCalendar` is built using **exactly two queries** (employees in the
   department, then active leaves in the window).
3. `engine.find_best_windows()` runs a pure constraint solver over candidate
   windows in the next 120 days, scoring each on earlier start, midweek start,
   department clarity and weekend padding.
4. The top 3 windows are stored in `leave_requests.suggestions` (JSONB) and the
   best one becomes the request's window.

The one-denial rule lives in `state_machine.py`, not in the database:

- **Accept** → `approved`, and the balance reservation moves into `used`.
- **Deny (1st)** → re-suggest the next-best window, skipping the declined one.
- **Deny (2nd)** → the denial is spent, so the current window is
  **force-approved** (`approved_forced`) and the employee is told.

Special leave (sick, maternity, paternity) skips the engine, goes straight to
`pending`, and is charged in **calendar** days as configured.

Every transition writes a row to `leave_request_history`, and notifications are
enqueued best-effort — a notification failure never poisons the leave
transaction.

### Attendance resolution

The first punch of the day sets `clock_in`; a second, later punch sets
`clock_out`; extra punches are ignored. `late_minutes`,
`early_leave_minutes` and `worked_minutes` are recomputed against the
department's shift policy (department override, else the global default). All
timestamps are stored in UTC and converted to the employee's timezone at the
edges.

---

## Testing

```bash
createdb ems_test
python -m pytest                 # everything
python -m pytest -m unit         # pure logic, no database
python -m pytest -m integration  # repositories/services against PostgreSQL
python -m pytest -m endpoint     # Flask test client
```

Unit tests cover the leave engine, the leave state machine, the attendance
resolver and both rule evaluators — all without touching a database. Integration
tests run against `TEST_DATABASE_URL`; the `clean_db` fixture truncates every
table before and after each test.

---

## Migrations

```bash
flask --app wsgi db migrate -m "describe the change"
flask --app wsgi db upgrade
flask --app wsgi db downgrade
```

---

## Gender and parental leave

Employees have a recorded **gender**, which exists to answer one question: does
**maternity** or **paternity** leave apply?

| Recorded gender | Maternity | Paternity |
| --- | --- | --- |
| Female | ✅ offered | ❌ refused |
| Male | ❌ refused | ✅ offered |
| Not recorded | ❌ refused | ❌ refused |

A female employee is never offered paternity and a male employee is never offered
maternity. When gender is **not recorded**, neither is available — the system has
no basis for choosing between them — and the request form says so and tells the
employee to speak to HR.

### Where the rule is enforced

Three places, and the middle one is what actually guarantees it:

1. **The form** only lists the leave types that apply, and shows a *"Not available
   to you"* panel explaining anything it withheld, so the option never silently
   disappears.
2. **The service** refuses a mismatched request on submission
   (`assert_leave_type_allowed`). A form can be bypassed, so this is the real gate.
3. **The planner** works from the same eligibility, so it never allocates a leave
   type the employee cannot take.

### Recording it

* **New employees** must have a gender: it is a required choice on the create form.
* **Existing employees** show a **"Record gender"** button on the employees list,
  opening a one-pass screen that lists everyone still missing one and saves in
  batches. Anything left blank is skipped and stays on the list.

At the service layer gender is *validated but optional*, so the seed and the API
can create a record first and have HR set it afterwards; that is what the
one-pass screen is for. The create **form** is what makes it required in practice.

---

## Reports

**Reports** (`/app/reports`, HR/Admin/Manager) has six views over any date range
and department, each with a matching CSV download:

| Tab | Shows |
| --- | --- |
| Attendance summary | Per-department people, records, late/absent/leave days, hours, plus totals |
| Attendance health | Badge distribution across the workforce |
| Daily headcount | Headcount, clocked-in and on-leave per day |
| Top late | Employees ranked by late days, then minutes |
| Leave utilisation | Entitlement versus usage by leave type |
| Balance ledger | Per-employee balances for one department |

**No department filter means every department.** The page used to default to the
first department in the list, which silently showed an empty report whenever
that department happened to have no staff, with no way to clear the filter. There
is now an explicit *All departments* option, and an unknown id falls back to all
rather than to nothing.

CSVs are served from the JSON API (`/api/v1/exports/...`). They authenticate
from the browser session cookie, and CSRF protection applies only to mutating
methods, so linking to them from the page works.

---

## Correcting attendance

**Attendance &rarr; Correct a record** records or amends one employee's day.

It is addressed by **employee and date**, not by an existing row id. That matters:
the days most in need of correcting are the ones with *no punch at all* — a missed
clock-in, a device that was down, a forgotten clock-out. The picker therefore
lists **every active employee** for that day, marking who has no record yet, and
saving creates the row on demand.

Corrections reuse the same recompute path as a real punch, so a manual entry
produces identical late/worked figures. Rules still apply: an explicit status
overrides the computed one, a clock-out before the clock-in is rejected, and a
day covered by **approved leave cannot be silently overwritten** (HR can still
set it to *on leave* deliberately).

---

## Deploying to Railway

**Full walkthrough: [DEPLOYMENT.md](DEPLOYMENT.md).** The essentials:

Railway needs **four services from the same image**, differing only in command.
One container cannot be both the web app and the worker, and Beat must not be
duplicated:

| Service | Command | Replicas |
| --- | --- | --- |
| `web` | `/app/docker-entrypoint.sh web` | 1+ |
| `worker` | `/app/docker-entrypoint.sh worker` | 1+ |
| `beat` | `/app/docker-entrypoint.sh beat` | **exactly 1** |
| *(release)* | `/app/docker-entrypoint.sh release` | once per deploy |

**Celery Beat must never be scaled past one replica.** Two schedulers mean every
nightly job fires twice — the attendance sweep would run twice and balances could
double-allocate. Scale `worker` instead.

The `Dockerfile`, `railway.toml`, `.dockerignore` and `docker-entrypoint.sh` are
all in the repo, so there is nothing to write by hand.

### Environment variables

Every service needs its **own** copy. A worker without `REDIS_URL` consumes
nothing; a worker without `APP_BASE_URL` sends emails with localhost links.

| Variable | Value |
| --- | --- |
| `FLASK_CONFIG` | `production` |
| `SECRET_KEY` | 48+ random chars |
| `JWT_SECRET_KEY` | 48+ random chars (**different** from `SECRET_KEY`) |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` |
| `RATELIMIT_STORAGE_URI` | the same Redis URL — **not** `memory://` |
| `REDIS_SSL_CERT_REQS` | `none` if Railway gives you a `rediss://` URL |
| `CORS_ORIGINS` | your exact public origin, e.g. `https://ems.up.railway.app` |
| `APP_BASE_URL` | the same public origin — **see below** |
| `APP_NAME` / `HR_CONTACT` | used in emails |
| `JWT_COOKIE_SECURE` | `true` |
| `EMAIL_BACKEND` | `smtp` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | your provider |
| `SMTP_TLS` | `true` for port 587, `SMTP_SSL=true` for 465 |
| `DEFAULT_TIMEZONE` | e.g. `Africa/Nairobi` |

Generate the secrets with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The app refuses to start with an unsafe production configuration, so a missing
setting fails at boot rather than in front of your users. Run
`python tools/check_production_config.py` to see every rule locally.

### APP_BASE_URL is the one people forget

Every sign-in link in a welcome email is built from `APP_BASE_URL`. If it is left
at the default, **every employee receives a link to localhost** and cannot sign
in. Set it to the public Railway domain before creating any real accounts.

### Creating the first administrator

**Do not run `flask seed` in production.** It creates `admin@ems.local` with the
published default password `ChangeMe123!`, so it now refuses to run when
`FLASK_CONFIG=production`. Use:

```bash
flask --app wsgi create-admin --email you@yourdomain.com
```

It prompts for the password securely, enforces the strength rules, and flags the
account to change it at first sign-in.

### Migrations

Wired to `preDeployCommand`, which runs **once per deploy before the new
container takes traffic** — not in the start command, which would run them in
every replica at once. They are idempotent, so re-running is harmless.

### Verification after deploying

```bash
curl https://<your-domain>/health              # {"status":"ok","database":"up"}
python tools/check_celery_config.py            # broker + nightly schedule
flask --app wsgi run-job nightly_attendance    # prove the sweep runs now
```

The nightly sweep is scheduled for **02:00** in `DEFAULT_TIMEZONE`, followed by
the colour recompute at 02:30 and the disciplinary evaluator at 03:00.

---

## API

Authentication is JWT. Log in via `POST /api/v1/auth/login`, then send
`Authorization: Bearer <access_token>`. Browsers additionally receive both
tokens as HTTP-only cookies.

Refresh tokens are rotated server-side on every use: the previous session row is
revoked and replaying it is rejected, which also invalidates the access tokens
minted with that session.

The full endpoint list is in Swagger UI at `/api/docs/ui`.

---

## Shift &amp; lateness policies

Everything the system measures a punch against lives in **Attendance &rarr; Shift &amp; lateness**
(`/app/attendance/policies`), not in code. Each policy is one row, either the
global default (`department_id IS NULL`) or a department override; a department
override always wins and deleting it falls back to the global default.

| Setting | Meaning |
| --- | --- |
| Shift start | The arrival time the working day begins. |
| Shift end | The check-out time; leaving earlier counts as early leave. |
| Grace period | Free window after shift start. Arrivals inside it are on time. |
| Very late after | Minutes **past the grace period** before an arrival escalates. |
| Absent if no punch by | Optional intra-day cut-off; blank defers to the nightly job. |

The UI shows the resolved values per department, so an override is never
invisible, and it previews the cut-offs live as you type (`09:00` + `10` grace +
`60` very-late renders as *on time to 09:10, late to 10:10, very late after*).

### The threshold is exclusive

A policy of `very_late_after_minutes = 60` marks **exactly 60 minutes late as
`late`, and 61 as `very_late`** — matching the wording "very late *after*
60 minutes". This is covered by a boundary test so it cannot drift.

### Very late still counts everywhere it should

`late_minutes > 0` is what the colour ladder and the disciplinary thresholds
read, so a very-late day automatically counts towards those regardless of its
status label. `very_late` is deliberately included wherever lateness or presence
is counted (trend charts, per-employee stats, dashboard badges), so a very late
arrival never disappears from a report or inflates the present count.

### Validation

Configurations that would make attendance meaningless are rejected with a
reason: identical shift start/end, negative or excessive grace (>240 min), a
very-late threshold below 1 minute, and a zero absence cut-off.

---

## Self-service profile

Employees manage their own details at **My profile** (`/app/profile`), reachable
from the user menu.

**Editable:** first name, last name, email, timezone.

**Locked to HR:** employee number, phone number, department, position, hire
date. These are rendered read-only with an explanation rather than being silently
ignored, and a POST that tries to set one is refused loudly.

Because the email is also the sign-in identity, changing it updates the linked
`users` row in the same transaction — the employee table and the login can never
drift apart, and the new address works as a login immediately. The uniqueness
checks cover **both** tables, since each has its own unique email constraint.

Changing a password requires the current one (so a stolen session cannot lock the
real owner out), rejects reuse of the same password, enforces the strength rules,
and revokes every other session.

---

## Annual leave planning

**Annual plan** (`/app/leave/plan`, HR/Admin, plus read-only for managers) is the
whole-year roster: who is off, per department, month by month. It exists to
answer "in December, who is on leave?" in January.

### The one-person-per-department rule

`leave_rules.max_employees_off_per_dept` is the cap on how many people may be off
on the same day in one department. **Annual leave defaults to 1.**

It is enforced in **two** places, which is the point:

1. **When suggesting** — `score_window` marks a candidate blocked once a day
   reaches the cap, so the engine never offers a clashing window.
2. **When approving** — `LeaveService._enforce_capacity` runs on every path that
   commits days: HR approval, employee acceptance, and force-approval.

Before the second check existed, the cap was bypassable: an employee could simply
supply explicit dates instead of accepting a suggestion, and two colleagues could
both be approved for the same days. That is now refused with the clashing days
named and the next free window offered.

Genuine exceptions need a deliberate, audited override: `decide(..., force=True)`
records the breach in the request's `reason`, so it stays visible afterwards.

### Generating a roster

`LeavePlanService.generate` drafts a plan for one department and year:

* every active employee's **remaining** annual entitlement is placed (already
  approved leave is subtracted, so the roster reflects reality);
* allocations are **round-robin** — everyone takes one block before anyone takes
  a second, so a scarce year is shared instead of consumed by the most senior
  people;
* placements stay **inside the calendar year**; anything that will not fit is
  reported as unplaced with the reason, never silently pushed into January;
* weekends and blackout dates are never charged.

Nothing is visible to employees until HR **publishes** the roster — a draft is
HR's working copy, so a half-finished plan can never be mistaken for a decision.
Publishing can be undone, and `create_requests_for_plan` turns allocations into
approved leave requests when HR is ready.

Materialising checks the **department concurrency cap** as well as the
employee's own existing leave. The roster is drafted against the plan's own
occupancy, so leave approved outside the plan can still clash with it; when that
happens the allocation is refused and reported to HR rather than written
straight to `approved`. Without that check a published roster could create an
overlap that `decide()` would have refused.

The engine is pure (`plan_annual_leave` in `features/leave/engine.py`), so the
whole allocation strategy is testable without a database.

### Where employees see it

* **Dashboard** — a banner with their allocated window and a countdown, shown the
  moment they sign in.
* **My leave** — the same allocation, plus a **department roster** for the year:
  when each colleague is away and whether that is now, upcoming, or past. That is
  what answers "who is covering in December?" without asking HR.

Neither appears until HR publishes.

### When capacity is genuinely short

Planning a year that has already started (or a department with many staff) can
leave people unplaced, because the cap makes this a real constraint. The planner
reports the shortfall per employee with the arithmetic — for example
"*Partly placed: 10 of 21 day(s) fitted... the year only had room for 50 of 105
requested day(s) at 1 person(s) off per day*" — rather than pretending to succeed.
Raising the cap for a department, or planning earlier in the year, is the answer.

---

## Recall and rescheduling

HR can **recall** an employee from approved leave (`leave_recall`, available on
the leave queue for anything approved):

* the days are **refunded** to the balance, not consumed;
* the request becomes `recalled` and keeps its history and the reason;
* a **replacement window is proposed** as a fresh suggested request for the
  employee to confirm — their approved dates are never silently rewritten;
* the department slot frees up immediately, so a colleague can take it.

`recalled` is terminal but not live: it stops counting against the department
calendar the moment it happens.

---

## Attendance integrity

`AttendanceService.apply_punch` is the single choke point for every punch,
including the device webhook, so the guard lives there:

* **an employee on approved leave cannot clock in** — the day stays `on_leave`
  instead of silently becoming `present`;
* **a suspended (deactivated) employee cannot clock in** at all.

`is_clockable()` is the non-raising form used by the UI to explain *why* rather
than just failing.

---

## Account provisioning

When HR creates an employee **with a login**, the password is generated by the
system and **emailed to the employee** — it is never shown to the administrator
and never displayed on screen. The welcome email contains:

* the sign-in page link, their email, and the temporary password;
* an instruction to change it immediately;
* a reminder to **see HR about biometric registration**, with their staff number,
  since their clock-ins are ignored until their fingerprint is enrolled.

### Why emailing a password is acceptable here

Only because the password is **single-use in practice**: the new account is
flagged `must_change_password`, so the HTML login route redirects them straight
to *My profile* and asks them to choose their own password before they can use
the system. The flag is cleared the moment they do. An administrator-initiated
reset sets the flag again, because the administrator then knows that password.

`must_change_password` is surfaced on the session profile
(`GET /api/v1/auth/me`) so API clients can enforce the same thing.

### If the email does not arrive

The employee detail page shows a **"Has not signed in yet"** badge while the
provisioned password is still unused, so HR can tell whether the hand-off worked
without asking. **Resend welcome email** issues a genuinely new password and
emails it again — the previous one stops working immediately, so a message
sitting in a wrong inbox never leaves a usable credential behind.

A mail outage cannot undo the account: the email is sent through `send_safely`,
and `EMAIL_ENABLED=false` skips email entirely while still creating the login.

### Who gets mail, and how much

Deliberately limited to things a person must act on or be told about:

| Event | Recipient |
| --- | --- |
| Account created | the new employee |
| Leave requested | the employee (ack) + HR/Admin + department manager |
| Leave approved / denied / recalled / cancelled | the employee + HR |
| Attendance indicator worsened | the employee |
| Disciplinary action recorded or recommended | the employee |

Everything else (device offline, leave queued for HR's attention) stays in the
in-app notification centre. One leave request produces exactly **one email per
recipient** — verified by `tools/audit_email_volume.py`, so nobody is
double-mailed for the same action.

---

## Email notifications

Every leave transition fans out by email as well as to the in-app inbox.

| Event | Employee | HR / Admin | Department manager |
| --- | --- | --- | --- |
| Leave requested | "request received" ack | actionable alert | yes |
| Approved / denied | decision notice | record of the outcome | — |
| Force-approved / cancelled / recalled | notice | record of the outcome | — |
| Attendance colour escalation | warning | — | — |
| Disciplinary action | notice | — | — |

The last two are emitted by their jobs (`color_recompute`, `disciplinary_evaluator`)
and are deliberately best effort: the colour change and the disciplinary **action
row** are the records of truth, so a mail outage never rolls either back. A
recommendation that has not been applied carries **no reference number**, rather
than inventing one.

### Backends

`EMAIL_BACKEND` selects how mail leaves the system:

* **`console`** *(development default)* — the full message is written to the Flask
  log and the row is marked sent. Use this to see exactly what would be delivered
  without configuring a mail server.
* **`smtp`** — real delivery. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
  `SMTP_PASSWORD`, `SMTP_FROM` and either `SMTP_TLS` (587) or `SMTP_SSL` (465).
  For Gmail, use an **App Password**, not your account password.
* **`null`** — records rows without delivering. Used by the test suite.

### Why it is safe to fail

* The `email_logs` row is **committed before the transport runs**, so a crash or a
  slow SMTP server cannot lose the fact that an email was due.
* Business code calls `send_safely`, so a mail outage **never rolls back** a leave
  approval. The failure is recorded on the row instead.
* The `email_dispatch` job retries anything left `pending`, giving up after
  `EMAIL_MAX_ATTEMPTS` so it can never loop forever.

Inspect what was sent (and why anything failed):

```bash
flask --app wsgi run-job email_dispatch   # drain the outbox now
psql ems_dev -c "select status, event_type, to_email, last_error from email_logs order by id desc limit 20;"
```

---

## Administering lateness and absenteeism rules

`/app/rules` (HR and Admin) manages both rule families on one page. Rule writes
go through `RuleAdminService`; the page and its forms never touch a repository.

**Attendance-health ladder** (`color_rules`) — sets the employee badge colour.
A rule matches when **both** `min_lates` and `min_absences` are reached inside
`window_days`; the first match by descending `priority` wins, and ties break
toward the more severe colour. Green is the implicit fallback, so it is not
allowed to carry thresholds. Applied nightly by `color_recompute` at 02:30.

**Disciplinary ladder** (`disciplinary_rules`) — raises warnings and
suspensions. A rule matches when its single `trigger_type` counter reaches
`threshold` within `period` (week/month/quarter). The most serious matching
threshold is recorded, at most once per employee per day. `auto_apply` off means
the action is only recommended to HR — the safer setting for suspensions.
Applied nightly by `disciplinary_evaluator` at 03:00.

Both families are **scopeable**: leave the department blank for a global rule,
or pick one department to override it. Scope is fixed once a rule exists — to
move a rule to another department, delete it and recreate it.

Guards worth knowing:

* a duplicate scope + colour (or scope + trigger + threshold) is refused
* `threshold` must be at least 1, since a rule firing at zero would hit everyone
* suspension days are only accepted on the suspension action, and default to 1
* a rule that has already produced recorded actions **cannot be deleted** — it
  must be disabled instead, so the audit trail keeps its referent
