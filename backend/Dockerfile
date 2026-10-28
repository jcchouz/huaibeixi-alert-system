FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY alembic.ini pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY tools ./tools

RUN mkdir -p /app/logs && chown app:app /app/logs

USER app

EXPOSE 8090

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8090 --workers ${WEB_CONCURRENCY:-1}"]
