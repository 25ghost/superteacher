# SuperTeacher backend — API image.
# Interpreter matches the test suite: local .venv runs Python 3.12.
FROM python:3.12-slim

# No .pyc files; stdout/stderr unbuffered so logs reach the collector as emitted.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Which proxy IPs may set X-Forwarded-* headers (uvicorn --forwarded-allow-ips).
# The 127.0.0.1 default is ONLY correct with no reverse proxy in front.
# Behind a reverse proxy you MUST set this to the proxy's address (comma
# separated for several), otherwise FastAPI sees every request as coming from
# the proxy and ALL users share one rate-limit bucket.
#   docker run ... -e FORWARDED_ALLOW_IPS=<proxy-ip> ...
ENV FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app

# Dependencies first so this layer stays cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# The API only — the image deliberately runs NO migrations and NO seeding on
# start. Apply them as explicit deploy steps (see README "Deploy"):
#   python -m alembic upgrade head
#   python scripts/seed_reference_data.py
#   python scripts/create_admin.py --email ...
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY scripts/ ./scripts/

RUN useradd --system --uid 10001 --create-home appuser
USER appuser

EXPOSE 8000

# Exactly ONE worker is required: RATE_LIMIT_STORAGE_URI=memory:// (the
# default) keeps rate-limit counters per process, so extra workers would
# multiply every configured limit. Shell form expands ${FORWARDED_ALLOW_IPS}
# at container start.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips ${FORWARDED_ALLOW_IPS}"]
