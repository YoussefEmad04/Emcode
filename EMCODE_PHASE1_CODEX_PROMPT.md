# Codex implementation prompt — EMCODE Geospatial Phase 1

You are a senior Python/data engineer. Implement a COMPLETE, WORKING, local-only proof of concept in the CURRENT repository, rather than giving me a plan or pseudocode. Use the simplest architecture that delivers the requirements and verify the actual end-to-end flow. Do not implement Phase 2 / What-if Analysis.

## Purpose

Demonstrate an EMCODE-like unified geospatial analytics workflow **without any customer data**:

1. Generate reproducible, explicitly synthetic geographic data around Dubai.
2. Load historical data once as a **batch** into PostgreSQL with PostGIS.
3. Continuously insert a separate **live simulation** stream into the same database.
4. Query and compare historical versus near-real-time data through **Apache Superset** dashboards with maps, KPIs and charts.
5. Demonstrate a simple cross-source correlation between vehicle positions and camera event *counts by shared zone and time window*, without inventing identity matching.

This is a local technical demo, not a production platform. Everything must be runnable with `docker compose up -d --build` (after copying `.env.example` to `.env`) and explained in a clear README.

## Hard constraints: no overengineering

- Exactly **three long-running Compose services**: `postgis`, `superset`, and `simulator`. Short-lived initialization inside existing containers is fine, but do not add permanent infrastructure.
- Technology: Docker Compose, PostGIS/PostgreSQL, Python (standard library plus psycopg2 / SQLAlchemy if needed), and Apache Superset.
- No Kafka, Airflow, Spark, Redis, Celery, Nginx, Kubernetes, separate FastAPI or Streamlit app, frontend, authentication service, or cloud dependency.
- Do not require a Mapbox key. Prefer an OpenStreetMap (OSM) basemap with Superset's supported map tiles; the tiles require browser internet access. Clearly credit OpenStreetMap contributors. **OSM supplies only the basemap**; it is NOT the data generator and PostGIS is NOT a data source. Do not scrape or bulk-download OSM tiles.
- No real people, surveillance videos, real vehicle IDs, or PII. Camera event types and positions are fictitious demo records.
- Pin known-working Docker image tags, not `latest`. Prefer a real `postgis/postgis:16-3.5` image and a tested released `apache/superset:6.1.0` image; inspect installed drivers/CLI and adjust to a pinned compatible version if necessary. Check Apple Silicon support; if the PostGIS image is AMD64-only, document emulation or pick a verified safe alternative.
- This is for local demo use only. Bind the Superset port to `127.0.0.1:8088`, keep the Postgres port internal unless a host port is truly required, and persist DB and Superset metadata with named volumes.
- Use SQLite for **Superset metadata only** to avoid a fourth service; document that it is acceptable for a local sandbox, not production. All analytical records live in PostGIS.

## Data and SQL requirements

Create an idempotent PostGIS schema (enable `CREATE EXTENSION IF NOT EXISTS postgis;`) and at least these tables:

- `geo_zones`: zone ID, readable name, `geometry(Polygon, 4326)`; generate five small non-overlapping demo zone polygons near Dubai.
- `vehicle_positions`: unique event ID, synthetic vehicle ID, event time, ingestion time, ingestion mode (`batch` or `stream`), speed, `geometry(Point, 4326)`.
- `camera_events`: unique event ID, synthetic camera ID, event time, ingestion time, ingestion mode, simple event category, `geometry(Point, 4326)`.

Include appropriate GIST spatial and timestamp indexes. Use SRID 4326, longitude then latitude, and avoid mixing degree distances with meter-based distances. Use `ST_Contains` / `ST_Intersects` for zone membership and `::geography` if meter distances are needed.

Batch seed: deterministic random seed, 7 days of historical timestamps up to roughly startup time, at least 20,000 vehicle positions and 2,000 camera events, 50 synthetic vehicles, 10 cameras, and five zones. Generate geographically plausible point values *inside* the zone polygons and make some matching zone/time bins across vehicle and camera events to prove correlation. Keep batch rows static once generated; restarting the stack must not duplicate batch records.

Live simulator: after initialization, append 5-10 new positions and 1-3 camera events every 3 seconds (configurable via `.env`) using **current UTC timestamps** and `ingestion_mode='stream'`. Reuse the same entity and zone space. Restarting the container must not truncate existing rows. Make logs show inserted counts and failures clearly. Prefer direct parameterized INSERTs and small batches; this is simulated continuous ingestion, not Kafka streaming.

Build clear SQL views for Superset:

- `v_batch_vehicle_activity` (batch-only, chart-friendly dimensions).
- `v_live_vehicle_latest` (latest live point per vehicle; expose numeric `latitude`/`longitude` using `ST_Y` and `ST_X`).
- `v_live_events_recent` (live events for recent time periods).
- `v_zone_activity_5m` (aggregate vehicle and camera **counts independently first** by 5-minute UTC window and zone, then join these aggregate groups; avoid inflated counts from many-to-many raw joins). Include rows from both modes only if modes are clearly separated.

Keep queries modest and easy to read. Include at least one spatial test verifying that generated points fall within the intended zones.

## Superset setup and visualization requirements

- Launch a ready-to-use Superset instance with a persisted local metadata store and a `SUPERSET_SECRET_KEY` supplied through `.env`/`superset_config.py`.
- Initialize Superset migrations, admin user and roles **idempotently**. Fail loudly on real errors rather than suppressing errors with `|| true`.
- Ensure the PostgreSQL SQLAlchemy driver is present in the actual Superset image. From the **Superset container**, connect to `postgis:5432`, not to `localhost:5432`. Verify the real connection.
- Create the database connection and datasets, and provision TWO dashboards automatically on first startup via a supported/version-correct Superset REST API, CLI/import bundle, or other maintainable mechanism. Do not fake saved chart definitions or return only pictures. If a chart/plugin limitation prevents full automated provision, implement the maximum supported automated setup, clearly describe the *exact* remaining UI steps, and report the shortfall instead of claiming completion.
- Dashboard **EMCODE | Historical Batch**: event counts, a timestamp chart, events by zone, and a geospatial chart/map based on stored points or aggregated map coordinates.
- Dashboard **EMCODE | Live Monitoring**: latest vehicle locations on a real map, last-15-minute event counts, live time series, camera events by zone, and a simple zone-level vehicle/camera activity comparison.
- Configure OpenStreetMap tile selection correctly for the selected Superset version. Verify the visualization is available (and if browser testing is possible, confirm markers and tiles render). The map must use actual query results, not a hardcoded screenshot.
- Superset dashboard refresh is **query polling**, not push-based real-time updates. Use a 10-second refresh interval if the installed Superset version supports a *verified* saved default; otherwise document the required one-time-per-browser-tab step: dashboard menu -> Set auto-refresh interval -> 10 seconds. Do not invent persistence or claim true streaming. For live charts ensure query caching does not conceal new rows, then verify the visible data can change after inserts.
- Keep the whole first-run workflow simple and clearly observable.

## Suggested file layout (adjust only if necessary)

```
.
├── compose.yaml
├── .env.example
├── .gitignore
├── README.md
├── sql/
│   ├── 001_schema.sql
│   └── 002_views.sql
├── superset/
│   ├── superset_config.py
│   └── bootstrap.sh
├── app/
│   ├── __init__.py
│   ├── seed.py
│   ├── stream.py
│   ├── provision_superset.py
│   └── verify.py
├── simulator/
│   └── Dockerfile
└── scripts/
    └── verify.sh
```

Only add a custom Superset Dockerfile if required to install its Postgres driver. Avoid unnecessary wrappers or extra dependencies.

## Expected operator workflow

The following commands must work from repository root and be documented exactly as implemented:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f simulator
docker compose exec postgis psql -U emcode -d emcode -c 'SELECT PostGIS_Version();'
docker compose exec postgis psql -U emcode -d emcode -c "SELECT ingestion_mode, COUNT(*) FROM vehicle_positions GROUP BY ingestion_mode ORDER BY ingestion_mode;"
docker compose exec simulator python -m app.verify
```

Superset must be accessible at `http://localhost:8088`. Get login credentials from `.env` (local demo credentials only). Its DB connection must target the Docker service hostname `postgis`, and the two dashboards should be accessible after provisioning. If first startup takes time, implement health-aware retries/timeouts rather than race conditions.

For a clean reset use `docker compose down -v` (explicitly warn that this deletes saved demo data and Superset metadata), followed by `docker compose up -d --build`.

## Verification and quality bar

You must RUN and verify the following if Docker is available in your execution environment:

1. `docker compose config` succeeds.
2. Database becomes healthy; PostGIS extension exists and SRID 4326 spatial queries succeed.
3. Batch seed inserts at least the requested row counts; rerunning batch seed creates **no duplicates**.
4. Stream rows increase between checks approximately 10 seconds apart.
5. Superset responds at the expected local address; admin login succeeds.
6. Superset's database connection genuinely executes `SELECT COUNT(*) FROM vehicle_positions`.
7. Both dashboards, expected datasets and charts exist and execute queries; geospatial map uses real coordinate columns.
8. Correlation view has reasonable non-zero vehicle and camera counts for common zone/time bins, without inflated joins.
9. After `docker compose restart`, volumes persist and demo works again.
10. `python -m app.verify` prints a clear PASS/FAIL summary and exits nonzero on failure.

If Docker/browser/network access is unavailable in your environment, still create all files and run available local tests; clearly label which steps you **could not** execute and provide exact commands the user should run. Do not claim something was tested when it was not.

## README deliverable

Write a complete, accurate `README.md` in this repo containing: purpose/limitations; short ASCII data-flow diagram; prerequisites (Docker Desktop/Compose); exact commands for first startup; login URL; how the batch and live jobs differ; where the OSM basemap fits; how to open both dashboards; the 10-second auto-refresh UI step if needed; validation/SQL commands; view descriptions; troubleshooting (image architecture, Superset driver, failed DB connection from container, metadata init, map tiles, no live updates); logs; persistence/reset; and how to stop. Include a clear note that data is fully synthetic and this is **not production ready**. Keep README beginner-friendly and aligned to commands that were actually implemented.

## Final response I expect from you

After writing all files, briefly report: implemented file tree, precise startup commands, dashboard links or names, verification results with PASS/FAIL and commands run, any failures/limitations, and the only manual step if any. Prioritize delivering a WORKING end-to-end PoC over optional architecture polish.
