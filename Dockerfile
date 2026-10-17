FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic

# Render sets PORT. The worker service overrides this command with: python -m app.worker
# No --proxy-headers: app.security.client_ip reads X-Forwarded-For from the trusted end itself.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
