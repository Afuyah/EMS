#!/usr/bin/env bash
#
# Container entrypoint for the EMS.
#
# One image serves every role, selected by the first argument, so the web, the
# Celery worker and Celery beat all run the exact same code:
#
#   web      -> gunicorn (default)
#   worker   -> celery worker
#   beat     -> celery beat scheduler (exactly ONE instance, ever)
#   release  -> database migrations, run before a new deploy takes traffic
#
set -euo pipefail

ROLE="${1:-web}"
shift || true

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------------------
# Preflight. Every service needs these, and a missing one otherwise surfaces as
# a confusing SQLAlchemy or Celery traceback deep inside startup.
# ---------------------------------------------------------------------------
missing=""
for var in FLASK_CONFIG DATABASE_URL; do
  if [ -z "${!var:-}" ]; then
    missing="${missing} ${var}"
  fi
done
if [ -n "$missing" ]; then
  log "FATAL: missing required environment variable(s):${missing}"
  log "Set them on THIS Railway service (each service has its own variables)."
  exit 1
fi
if [ "${FLASK_CONFIG}" != "production" ]; then
  log "WARNING: FLASK_CONFIG=${FLASK_CONFIG}; production expects 'production'."
fi

# The Celery roles additionally need the broker.
if [ "$ROLE" = "worker" ] || [ "$ROLE" = "beat" ]; then
  if [ -z "${REDIS_URL:-}" ]; then
    log "FATAL: REDIS_URL is not set on this service, so there is no broker."
    exit 1
  fi
  log "broker configured (host hidden)"
fi

# ---------------------------------------------------------------------------
# Release task: apply migrations. Safe to run on every deploy because Alembic
# records the revision and skips anything already applied.
# ---------------------------------------------------------------------------
if [ "$ROLE" = "release" ]; then
  log "applying database migrations"
  exec python -m flask --app wsgi db upgrade
fi

# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
if [ "$ROLE" = "web" ]; then
  PORT="${PORT:-8000}"
  WORKERS="${WEB_CONCURRENCY:-4}"
  TIMEOUT="${WEB_TIMEOUT:-120}"
  log "starting gunicorn on 0.0.0.0:${PORT} with ${WORKERS} worker(s)"
  exec gunicorn \
      --bind "0.0.0.0:${PORT}" \
      --workers "${WORKERS}" \
      --worker-class gthread \
      --threads "${WEB_THREADS:-4}" \
      --timeout "${TIMEOUT}" \
      --graceful-timeout 30 \
      --keep-alive 5 \
      --access-logfile - \
      --error-logfile - \
      --log-level "${GUNICORN_LOG_LEVEL:-info}" \
      --forwarded-allow-ips '*' \
      wsgi:app
fi

# ---------------------------------------------------------------------------
# Celery worker
#
# The default prefork pool is correct on Linux (the Windows note about
# --pool=threads in the README does not apply here).
# ---------------------------------------------------------------------------
if [ "$ROLE" = "worker" ]; then
  CONCURRENCY="${CELERY_CONCURRENCY:-4}"
  log "starting celery worker (concurrency=${CONCURRENCY})"
  exec celery -A celery_worker.celery worker \
      --loglevel="${CELERY_LOG_LEVEL:-info}" \
      --concurrency="${CONCURRENCY}" \
      --max-tasks-per-child=200 \
      --without-gossip \
      --without-mingle
fi

# ---------------------------------------------------------------------------
# Celery beat
#
# MUST run as exactly one replica. Two beat schedulers mean every scheduled job
# fires twice, which would double-run the nightly attendance sweep.
#
# The schedule state file lives on an ephemeral filesystem, so a redeploy resets
# it. That is acceptable because every scheduled job is written to be
# idempotent, and we pass --pidfile= to avoid a stale-pid refusal on restart.
# ---------------------------------------------------------------------------
if [ "$ROLE" = "beat" ]; then
  SCHEDULE="${CELERYBEAT_SCHEDULE_FILE:-/tmp/celerybeat-schedule}"
  log "starting celery beat (schedule=${SCHEDULE})"
  exec celery -A celery_worker.celery beat \
      --loglevel="${CELERY_LOG_LEVEL:-info}" \
      --schedule "${SCHEDULE}" \
      --pidfile= \
      --max-interval "${CELERYBEAT_MAX_INTERVAL:-60}"
fi

log "unknown role '${ROLE}' (expected: web | worker | beat | release)"
exit 1
