# Huaibeixi Alert System

Bridge structural health monitoring alert and analytics platform. The backend
uses FastAPI, PostgreSQL/TimescaleDB and a modular service architecture.

## Backend Layout

```text
backend/
  app/
    api/
      app.py                 # application factory, middleware, lifespan
      deps.py                # dependency wiring and role guards
      routers/               # authentication, alerts, thresholds and stats
    core/
      config.py              # environment-backed settings
      security.py            # JWT authentication and RBAC
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
`http://localhost:5174`. The simulator writes monitoring samples to
`t_monitor` and runs the same alert detection path used by the demo.

To start only the backend and frontend:

```powershell
.\start_all.bat --no-simulator
```

## Alert Categories

`t_alert_record.alert_category` classifies every alert:

| Value | Category | Alert types |
| --- | --- | --- |
| `0` | 数据预警 | 阈值超限、传感器死值、超物理量程 |
| `1` | 设备预警 | 传感器离线、采集仪离线 |

The backend derives the category from `alert_type`; API clients do not need to
send it when creating alerts.

## Authentication And Roles

Authentication is disabled only for local development. Production startup
rejects wildcard CORS and missing JWT configuration.

Generate a password hash:

```powershell
cd backend
.\.venv\Scripts\python.exe tools\hash_password.py "change-me" --username admin --role admin
```

Put the JSON output in `AUTH_USERS_JSON`, set `AUTH_ENABLED=true`, and provide a
random `JWT_SECRET` of at least 32 characters.

Roles:

| Role | Access |
| --- | --- |
| `viewer` | Read dashboards and alert history |
| `operator` | Viewer access plus alert acknowledgement and threshold simulation |
| `admin` | All operations, including threshold changes |

The frontend attaches the bearer token to every REST request. Alert popups and
the alert management table refresh through the API every 5 seconds. Alert
acknowledgement is also sent directly through the REST API, and the
acknowledgement identity is taken from the authenticated principal.

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

Live database validation tools are in `backend/tools/`. Set `AUTH_TOKEN` when
authentication is enabled.

## Docker

Create `backend/.env` from `backend/.env.production.example`, fill in the database
credentials, JWT secret and `AUTH_USERS_JSON`, then run:

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
- Runtime identity, request IDs and latency are included in structured logs.
