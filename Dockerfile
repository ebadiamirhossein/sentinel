FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 1000 sentinel

COPY pyproject.toml README.md ./
COPY sentinel ./sentinel
COPY alembic.ini ./
COPY alembic ./alembic
COPY config.yaml ./

RUN pip install --no-cache-dir .

USER sentinel
EXPOSE 8000

# Migrations run at boot, then the app. Both are idempotent and safe to restart.
CMD ["sh", "-c", "alembic upgrade head && python -m sentinel.main"]
