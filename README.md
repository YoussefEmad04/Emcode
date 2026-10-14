# EMCODE — Geospatial Analytics PoC (Phase 1)

A small, local demonstration of synthetic batch and near-real-time geospatial analytics with PostgreSQL/PostGIS, Python, and Apache Superset 6.1.0. It uses no customer data, real vehicle identities, surveillance footage, or PII.

This is a local proof of concept, **not production-ready**. Phase 2 / What-if Analysis is deliberately not included.

## What it demonstrates

- A reproducible one-time batch of 20,000 synthetic GPS position records and 2,000 fictitious camera events across the previous seven days.
- Fifty synthetic vehicle IDs, ten synthetic camera IDs, and five non-overlapping demonstration zones near Dubai.
- New UTC-timestamped data every three seconds: 5–10 vehicle positions and 1–3 camera events per cycle by default.
- PostGIS point/polygon storage, GIST indexes, UTC timestamp indexes, spatial zone membership, and independent five-minute vehicle/camera aggregation.
- Two automatically provisioned Superset dashboards backed by saved datasets and charts, including a four-KPI historical view with date and zone filters.

## Architecture

```text
             simulator (Python)
          batch seed + live inserts
                    |
                    v
      PostgreSQL 16 + PostGIS 3.5
   zones / positions / camera events
                    |
              spatial views
                    |
                    v
           Apache Superset 6.1.0
        /                         \
 Historical Batch          Live Monitoring
                         (10 s query polling)
```

There are exactly three long-running Compose services: `postgis`, `superset`, and `simulator`. Superset stores only its own users/dashboard metadata in a persistent SQLite file. All analytical data is in PostGIS.

OpenStreetMap provides the browser-visible basemap only. It does not generate demo data, and PostGIS is a storage/query engine rather than a data source. The configured tiles are from OpenStreetMap and require browser internet access. Map attribution: © OpenStreetMap contributors.

## Prerequisites

- Docker Desktop with Compose v2, or Docker Engine plus the Compose plugin.
- A browser; internet access is needed to pull images and display OSM map tiles.
- At least 4 GB of memory available to Docker; more is preferable while Superset starts.

The pinned `postgis/postgis:16-3.5` manifest is AMD64-only. `compose.yaml` therefore uses `platform: linux/amd64`; Docker Desktop runs it under emulation on Apple Silicon. Superset 6.1.0 and the simulator are native multi-architecture images. The PostGIS emulation warning and slower first startup are expected on ARM Macs.

## First startup

From this repository:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f simulator
```

Review `.env` before starting. Its credentials are intentionally simple local-demo credentials and must never be used for a public deployment. The first launch can take several minutes because it pulls images, initializes PostGIS, runs Superset migrations, creates the admin account, tests the analytical database connection, and provisions all assets. Superset exits instead of hiding a provisioning error.

Open [http://localhost:8088](http://localhost:8088) and sign in with `SUPERSET_ADMIN_USERNAME` / `SUPERSET_ADMIN_PASSWORD` from `.env` (the defaults are `admin` / `admin`). The host binding is restricted to `127.0.0.1`; PostgreSQL has no host port.

## Dashboards

- [EMCODE | Historical Batch](http://localhost:8088/superset/dashboard/emcode-historical-batch/) — a synthetic-data note, four KPIs, daily GPS bars, a descending horizontal zone ranking, an aggregated GPS activity map, and separately labeled GPS/camera counts by zone.
- [EMCODE | Live Monitoring](http://localhost:8088/superset/dashboard/emcode-live-monitoring/) — latest vehicle points, camera events over the last 15 minutes, live event timeline, camera events by zone, and five-minute vehicle/camera activity.

Superset refresh is database query polling, not push streaming. In each browser tab, open the live dashboard menu (`⋮`), choose **Set auto-refresh interval**, and select **10 seconds**. Superset 6.1.0 treats this as per-session UI state, so this is the only manual dashboard step. Query-result caching is disabled in this local sandbox so each poll can observe new rows.

The historical dashboard labels the source as **SYNTHETIC DEMO DATA**. Its KPI definitions are explicit: **Total GPS Records** counts position observations, **Distinct Vehicles** counts unique synthetic vehicle IDs, **Total Camera Events** counts fictitious camera-event rows, and **Monitored Zones** counts represented zones. These are separate from the live stream. The deterministic batch deliberately varies activity by day, zone, hour, and spatial hotspot to make the visuals readable. Those patterns are artificial and do not represent real traffic measurements.

## Batch and live behavior

On its first run, `simulator` applies the idempotent schema/views and saves a batch anchor timestamp in `demo_metadata`. Batch IDs, random values, spatial hotspots, activity weights, and the seven-day window are reproducible from the fixed seed and saved anchor. A versioned one-time refresh updates an existing Phase 1 batch to the current deterministic pattern; later restarts or seed reruns change no historical rows. `ON CONFLICT` protects every batch event.

After seeding, the same container enters the continuous loop. It uses parameterized small-batch inserts and never truncates tables. Change the interval/count bounds in `.env`, then recreate `simulator`, if a different rate is useful.

## Verification

Run the requested direct checks:

```bash
docker compose exec postgis psql -U emcode -d emcode -c 'SELECT PostGIS_Version();'
docker compose exec postgis psql -U emcode -d emcode -c "SELECT ingestion_mode, COUNT(*) FROM vehicle_positions GROUP BY ingestion_mode ORDER BY ingestion_mode;"
docker compose exec simulator python -m app.verify
```

Or use:

```bash
./scripts/verify.sh
```

The verification program exits nonzero on failure. It checks PostGIS/SRID, five non-overlapping zones, batch totals and idempotence, correct historical KPI totals and distinct counts, that all historical views exclude stream rows, day/zone activity variation, all points inside their intended polygons, independently aggregated common zone/time bins, growth during a ten-second wait, Superset admin authentication, a real SQL Lab query, seven datasets, two dashboards, all thirteen saved chart queries, filter definitions, the sorted horizontal bar configuration, the aggregated spatial map configuration, and its latitude/longitude bindings.

For a visual check, sign into Superset and open both dashboard links above. The historical dashboard has preconfigured Date range and Zone native filters; selecting a zone and clicking **Apply filters** should update its KPIs and all four main charts. The required aggregate totals and synthetic pattern checks also run in `app.verify`.

Useful inspection commands:

```bash
docker compose ps
docker compose logs --tail=100 postgis
docker compose logs --tail=100 superset
docker compose logs --tail=100 simulator

docker compose exec postgis psql -U emcode -d emcode -c \
  "SELECT ingestion_mode, COUNT(*) FROM camera_events GROUP BY ingestion_mode ORDER BY ingestion_mode;"

docker compose exec postgis psql -U emcode -d emcode -c \
  "SELECT * FROM v_zone_activity_5m WHERE vehicle_position_count > 0 AND camera_event_count > 0 ORDER BY window_start DESC LIMIT 10;"
```

## SQL objects

| Object | Purpose |
|---|---|
| `geo_zones` | Five synthetic `geometry(Polygon, 4326)` zones. |
| `vehicle_positions` | Historical and live point events with synthetic vehicle ID, timestamps, mode, speed, and `geometry(Point, 4326)`. |
| `camera_events` | Historical and live fictitious camera events with category and point geometry. |
| `demo_metadata` | Persists the original deterministic batch anchor. |
| `v_batch_vehicle_activity` | Chart-friendly batch positions with spatially derived zone and numeric coordinates. |
| `v_batch_camera_activity` | Batch-only fictitious camera records with zone and numeric coordinates. |
| `v_batch_daily_zone_activity` | Independent batch GPS and camera counts by UTC calendar day and zone; the two source aggregates are joined only after counting. |
| `v_batch_spatial_activity` | Batch-only GPS counts grouped into deterministic spatial grid cells by zone/day, with cell-centroid coordinates for the activity map. |
| `v_live_vehicle_latest` | Latest streamed point per vehicle with numeric latitude/longitude. |
| `v_live_events_recent` | Streamed camera events from the last 24 hours. |
| `v_zone_activity_5m` | Vehicle and camera counts aggregated independently by mode, zone, and five-minute UTC window before joining. |

The four historical KPIs read batch-only views. The day and zone charts use `v_batch_daily_zone_activity`; the cross-source chart has separate `GPS Records` and `Camera Events` metrics. The map draws aggregated grid-cell centroids and scales marks by GPS record count rather than drawing vehicle IDs. Date and zone filters target the corresponding time and zone fields across the historical dashboard.

Longitude is passed before latitude when points are created. Spatial membership uses `ST_Contains`; no distance calculation mixes degrees and meters.

## Persistence, stopping, and reset

Stop while retaining analytical data and Superset metadata:

```bash
docker compose down
```

Start again:

```bash
docker compose up -d --build
```

Delete both named volumes and rebuild from an empty sandbox:

```bash
docker compose down -v
docker compose up -d --build
```

**Warning:** `docker compose down -v` permanently deletes all generated rows and saved Superset metadata for this Compose project.

## Troubleshooting

**First launch is slow or services are still starting:** inspect `docker compose ps` and `docker compose logs -f postgis superset simulator`. On Apple Silicon, PostGIS emulation makes initialization slower. Superset has health-aware startup retries.

**PostGIS architecture error:** ensure Docker Desktop's x86/AMD64 emulation is enabled. The Compose platform pin is required for this exact PostGIS tag on ARM64. On an AMD64 host it runs natively.

**Superset database connection fails:** its SQLAlchemy URI must use Docker hostname `postgis:5432`, never `localhost`. The custom Superset image installs `psycopg2-binary`; verify it with `docker compose exec superset python -c 'import psycopg2; print(psycopg2.__version__)'`.

**Superset starts but dashboards are absent:** `docker compose logs superset` should end with `Provisioned 2 dashboards...`. A real provisioning failure terminates the container, after which Compose restarts it. Fix the reported error; do not treat a health page alone as successful provisioning.

**Map markers appear without a basemap:** the coordinates come from PostGIS, while map tiles come directly to the browser from `https://tile.openstreetmap.org`. Check browser internet access, content blocking, and the browser console. Do not scrape or bulk-download OSM tiles.

**Live counts do not change:** confirm inserts in `docker compose logs -f simulator`, rerun the stream count SQL, and reselect the per-tab 10-second dashboard auto-refresh. A dashboard tab does not update when polling is off.

**Superset metadata migration fails:** retain the log before resetting. For a disposable local demo only, a clean `docker compose down -v` removes the SQLite metadata volume and PostGIS data so initialization can start over.

## Scope and limitations

- Local sandbox security and SQLite Superset metadata are unsuitable for production.
- OSM map tiles depend on browser internet access and the public tile service's usage policy.
- Dashboard updates are polled, not pushed, and the 10-second selection is per browser session.
- The zone/time comparison is an aggregate correlation only. It intentionally makes no claim that a camera event identifies or matches a vehicle.
- There is no alerting, prediction, What-if Analysis, Kafka, Airflow, Redis, Celery, separate API/frontend, cloud service, or real EMCODE/customer data.


## Phase 1 extension: synthetic congestion diagnostics

This branch adds a **demonstration-only** classification model, not a genuine traffic forecasting service. The proxy target is next-hour synthetic mean speed <43 km/h with at least 5 GPS records. Random Forest trains on current-hour counts, camera activity, speed, slow fraction, time and zone. No next-hour features are used for prediction. It uses a chronological 65/17/18 train/validation/test split. Test metrics are stored in `ml_model_metadata`. Do not interpret metrics as performance on real EMCODE data.

- Batch data: **40,000 GPS records** and **4,000 camera events**.
- Live updates are bounded gradual movements in synthetic zones, not real road routing; live points >10 minutes old are excluded.
- Historical map is a proportional **zone-level activity map** (not ML clustering or a heatmap).
- Open [EMCODE | Congestion Diagnostics](http://localhost:8088/superset/dashboard/emcode-congestion-diagnostics/) for three focused report charts: zone risk, hourly mean speed, and held-out predictions versus proxy labels.
- The model retrains at **simulator startup**; its latest forecast is tied to historical synthetic data, **not the current live stream**. Display that limitation whenever demonstrating it.

Rebuild and check:
```bash
docker compose up -d --build
docker compose exec simulator python -m app.verify
docker compose exec postgis psql -U emcode -d emcode -c "SELECT model_version, metrics_json FROM ml_model_metadata"
```

The initial upgraded batch updates existing seeded records using the stored anchor and does not require deleting the database volumes.
