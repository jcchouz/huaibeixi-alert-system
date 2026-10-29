# Alert Analysis System

Bridge structural health monitoring alert and analytics platform. The backend
uses FastAPI, PostgreSQL/TimescaleDB and a modular service architecture.

## Backend Layout

Detailed folder-by-folder and file-by-file documentation is available in
[后端架构说明.md](后端架构说明.md).

Detection startup boundaries, one-time rule loading, per-alert rule snapshots
and the 10,000 rows/second benchmark are documented in
[detection-state-and-10k-benchmark.md](docs/detection-state-and-10k-benchmark.md).

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

2. Create `backend/.env` and configure the database. Local development and
   production both use this file. Set `STATION_NAME` to the station displayed
   when the database station metadata has no name, for example
   `STATION_NAME=玉环站`. `backend/.env` is ignored by Git.

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

### Threshold-only debugging

`backend/.env` currently enables all five alert types so full-chain capacity
tests can run. To debug only the threshold chain, stop the backend, set the
other four `DETECTION_ALERT_*_ENABLED` switches to `false`, restart it, then
inject samples:

```powershell
cd backend
.\run_inject_threshold.bat --level 3 --seconds 20
```

The injector (`backend/tools/inject_threshold.py`) only writes one channel to
`t_monitor`. See
[单通道阈值预警调试指南.md](docs/单通道阈值预警调试指南.md) for the full
breakpoint sequence, verification SQL and troubleshooting notes.

Production only needs the backend service. It scans newly arrived raw samples
from `t_monitor` for alerts every second by default and writes one-minute and
ten-minute statistics to `t_stat_record`. The service is designed for a single backend
instance. On startup it creates a database-time boundary and only processes
samples and complete statistics windows after that boundary; historical
watermarks and catch-up processing are not used. Configure the realtime alert interval
with `DETECTION_INTERVAL_SECONDS` and the statistics windows with
`DETECTION_WINDOW_MINUTES` (comma-separated, default `1,10`). Each alert loop delays scanning by one
`DETECTION_INTERVAL_SECONDS` window to allow external samples to be committed;
with the default one-second interval it processes `[now-2s, now-1s)`. Each
loop advances at most one interval, and if a window takes longer than that,
`alertBacklogSeconds` grows and the backend is not keeping up with ingestion.
Continuous conditions use
`DETECTION_CONFIRM_SECONDS`, `DETECTION_STUCK_SECONDS` and
`DETECTION_OFFLINE_SECONDS` for threshold, out-of-range, stuck and offline
detection. Threshold, out-of-range and stuck confirmation convert their
configured reference seconds to consecutive samples using
`t_sensor.frequency`, with a minimum of three samples. For the default
3-second threshold and out-of-range window, that means 3 samples at 1 Hz, 150
samples at 50 Hz, and 3 samples at 1/60 Hz. Channel offline detection runs only
when the current scan contains no sample for that channel; any sample in the
current scan marks the channel online regardless of gaps between samples or
windows. The offline timeout is at least five sampling periods: with a
5-second base, offline detection stays at 5 seconds for 1 Hz and 50 Hz and
extends to 300 seconds at 1/60 Hz. The
realtime scan cursor advances from the previous `window_end`, so backlog is
processed window by window instead of skipping to the current time. Each
alert scenario can be disabled independently with
`DETECTION_ALERT_THRESHOLD_ENABLED`,
`DETECTION_ALERT_OFFLINE_ENABLED`, `DETECTION_ALERT_DAQ_OFFLINE_ENABLED`,
`DETECTION_ALERT_STUCK_ENABLED` and `DETECTION_ALERT_OOR_ENABLED` (all default
to `true`); disabling a scenario only stops writing that alert type, while
sampling and statistics keep running. Set `DETECTION_ENABLED=false` only when
detection should run in a separate deployment.

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

For a compliant all-alert capacity test that uses only existing real sensors,
channels and thresholds:

```powershell
cd backend
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode preflight
.\.venv\Scripts\python.exe tools/stress_real_alerts.py --mode all `
  --allow-write --confirm-database huaibeixi_db
```

The all-alert test requires a running backend with all five alert switches
enabled. It verifies threshold, out-of-range, stuck, channel offline and
collector offline alerts plus one-minute and ten-minute statistics, and keeps
its test data.

## Docker

Configure the database credentials in `backend/.env`, then run:

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
