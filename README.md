# Huaibeixi Alert System

Bridge structural health monitoring alert and analytics platform. The backend
uses FastAPI, PostgreSQL/TimescaleDB and a modular service architecture.

## Backend Layout

Detailed folder-by-folder and file-by-file documentation is available in
[后端架构说明.md](后端架构说明.md).

```text
backend/
  app/
    api/
      app.py                 # application factory, middleware, lifespan
      deps.py                # dependency wiring
      routers/               # alerts, thresholds and stats
    core/
      config.py              # environment-backed settings
      exceptions.py          # stable API error envelope
      logging.py             # JSON logging
    domain/engine.py         # alert state machine and analytics engine
    infrastructure/
      database.py            # connection pool and transactions
      middleware.py          # request IDs, latency, Prometheus metrics
    repositories/            # SQL and persistence boundaries
    schemas/                 # Pydantic request/response contracts
    services/                # application use cases
  migrations/                # Alembic database migrations
  tests/                     # unit and contract tests
```

## Local Development

1. Install backend dependencies:

   ```powershell
   cd backend
   .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
   ```

2. Copy `backend/.env.example` to `backend/.env` and configure the database.
   `backend/.env` is ignored by Git.

3. Start the backend, frontend and continuous data simulator:

   ```powershell
   .\start_all.bat
   ```

The backend is available at `http://localhost:8090`, Swagger at
`http://localhost:8090/docs`, and the frontend at
`http://localhost:5174`. The local simulator only writes monitoring samples to
`t_monitor`; the backend independently detects alerts and writes
`t_alert_record` and `t_stat_record`.

To start only the backend and frontend:

```powershell
.\start_all.bat --no-simulator
```

Production only needs the backend service. It continuously consumes the latest
completed average window from `t_monitor` (one minute by default), runs all alert
detection in-process, and survives multiple backend instances through a
PostgreSQL advisory leader lock. Configure the window with
`DETECTION_WINDOW_MINUTES`. Sampling interval is derived from
`t_sensor.frequency`; threshold/out-of-range, stuck and offline detection use
`DETECTION_CONFIRM_POINTS`, `DETECTION_STUCK_POINTS` and
`DETECTION_OFFLINE_POINTS`. Set `DETECTION_ENABLED=false` only when detection
should run in a separate deployment.

## Alert Categories

`t_alert_record.alert_category` classifies every alert:

| Value | Category | Alert types |
| --- | --- | --- |
| `0` | 数据预警 | 阈值超限、传感器死值、超物理量程 |
| `1` | 设备预警 | 传感器离线、采集仪离线 |

The backend derives the category from `alert_type`; API clients do not need to
send it when creating alerts.

## Access Model

The application opens directly without login, user accounts, roles or
permissions. Every REST endpoint is available anonymously.

Alert popups and the alert management table refresh through the API every 5
seconds. Alert acknowledgement is sent directly through the REST API and is
recorded with the internal audit actor `web`.

## Database Migrations

For a fresh environment:

```powershell
cd backend
alembic upgrade head
```

For an existing database that already contains the documented schema, verify the
schema first and then mark the baseline as applied:

```powershell
cd backend
alembic stamp 20261019_0001
```

Always back up production data before applying migrations.

## Verification

```powershell
cd backend
.\.venv\Scripts\ruff.exe check app tests
.\.venv\Scripts\python.exe -m pytest --cov=app
.\.venv\Scripts\alembic.exe upgrade head --sql

cd ..\frontend
npm ci
npm run build
```

Live database validation tools are in `backend/tools/`.

## Docker

Create `backend/.env` from `backend/.env.production.example`, fill in the database
credentials, then run:

```powershell
docker compose up --build -d
```

The frontend is exposed on `http://localhost:5174`. Nginx proxies `/api` and
`/metrics` to the backend.

## Operations

- `GET /api/health` is a liveness endpoint.
- `GET /api/ready` checks database connectivity.
- `GET /metrics` exposes Prometheus metrics.
- The frontend polls alert APIs every 5 seconds and acknowledges alerts through
  the REST API.
- Request IDs and latency are included in structured logs.
