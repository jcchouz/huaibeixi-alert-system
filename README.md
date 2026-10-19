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
      routers/               # authentication, alerts, thresholds, stats, engine
    core/
      config.py              # environment-backed settings
      security.py            # JWT authentication and RBAC
      exceptions.py          # stable API error envelope
      logging.py             # JSON logging
    domain/engine.py         # alert state machine and analytics engine
    infrastructure/
      database.py            # connection pool and transactions
      sse.py                 # local + Redis cross-instance SSE fan-out
      workers.py             # replay worker with leader lock
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

3. Run the backend and frontend:

   ```powershell
   .\start_all.bat
   ```

The backend is available at `http://localhost:8090`, Swagger at
`http://localhost:8090/docs`, and the frontend at
`http://localhost:5174`.

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
| `admin` | All operations, including threshold changes and replay control |

The frontend attaches the bearer token to REST calls and passes the token to the
SSE connection. Acknowledgement identity is always taken from the authenticated
principal, not from request body text.

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
`/metrics` to the backend and disables response buffering for SSE.

## Operations

- `GET /api/health` is a liveness endpoint.
- `GET /api/ready` checks database connectivity.
- `GET /metrics` exposes Prometheus metrics.
- Replay workers use a PostgreSQL advisory lock, preventing duplicate execution
  when the backend is scaled horizontally.
- Set `REDIS_URL` when running multiple backend workers or containers so SSE
  events reach clients connected to every instance.
- Runtime identity, request IDs and latency are included in structured logs.
