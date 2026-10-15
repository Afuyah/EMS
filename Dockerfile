# EMS production image.
#
# Pinned to Python 3.12 rather than 3.14: 3.14 is fine for local development but
# a number of C-extension wheels (psycopg2, Argon2) are still catching up, and a
# platform build should not depend on compiling those from source.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq is needed by psycopg2; the rest keeps the image small but functional.
# curl is used by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first so the dependency layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Never run as root. Railway does not require it and it limits blast radius.
RUN useradd --create-home --shell /bin/bash ems \
    && chown -R ems:ems /app
USER ems

# Entrypoint script dispatches on the first argument: web | worker | beat | release
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]

# Railway injects $PORT; default matches the healthcheck below.
ENV PORT=8000
EXPOSE 8000

# Web is the default role so a bare `docker run` still serves the app.
CMD ["web"]
