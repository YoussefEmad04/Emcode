# EMCODE — Geospatial Analytics PoC (Phase 1)

A small, local demonstration of synthetic batch and near-real-time geospatial analytics with PostgreSQL/PostGIS, Python, and Apache Superset 6.1.0. It uses no customer data, real vehicle identities, surveillance footage, or PII.

This is a local proof of concept, **not production-ready**. Phase 2 / What-if Analysis is deliberately not included.

## What it demonstrates

- A reproducible one-time batch of 40,000 synthetic GPS position records and 4,000 fictitious camera events across the previous seven days.
- Fifty synthetic vehicle IDs, ten synthetic camera IDs, and five non-overlapping demonstration zones near Dubai.
- New UTC-timestamped data every three seconds: 5–10 vehicle positions and 1–3 camera events per cycle by default.
- PostGIS point/polygon storage, GIST indexes, UTC timestamp indexes, spatial zone membership, and independent five-minute vehicle/camera aggregation.
- Three automatically provisioned Superset dashboards backed by saved datasets and charts, including a four-KPI historical view with date and zone filters and a synthetic congestion diagnostics view.

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
        /              |                \
Historical Batch  Live Monitoring  Congestion Diagnostics
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

From this repository (copy the example only when `.env` does not already exist, so local credentials are preserved):

```bash
test -f .env || cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f simulator
```

Review `.env` before starting. Its credentials are intentionally simple local-demo credentials and must never be used for a public deployment. The first launch can take several minutes because it pulls images, initializes PostGIS, runs Superset migrations, creates the admin account, tests the analytical database connection, and provisions all assets. Superset exits instead of hiding a provisioning error.

Open [http://localhost:8088](http://localhost:8088) and sign in with `SUPERSET_ADMIN_USERNAME` / `SUPERSET_ADMIN_PASSWORD` from `.env` (the defaults are `admin` / `admin`). The host binding is restricted to `127.0.0.1`; PostgreSQL has no host port.

## Dashboards

- [EMCODE | Historical Batch](http://localhost:8088/superset/dashboard/emcode-historical-batch/) — **SYNTHETIC DEMO DATA**, four KPIs, daily GPS activity, sorted zone activity, an aggregated zone-centroid map, and camera events per 1,000 GPS records. Supporting analysis adds 24-hour Dubai-local GPS activity, actual historical camera event categories, and a full-width per-zone GPS/camera/rate report. Date and zone filters scope the supporting charts.
- [EMCODE | Live Monitoring](http://localhost:8088/superset/dashboard/emcode-live-monitoring/) — four stream-only KPIs, latest non-stale vehicle locations, five-minute GPS/camera timelines, zone activity, plus a separate ML section for risk, observed-vs-predicted speed, one-zone prediction history, and per-zone freshness/status.
- [EMCODE | Congestion Diagnostics](http://localhost:8088/superset/dashboard/emcode-congestion-diagnostics/) — historical synthetic risk/speed overview, chronological held-out classification metrics and confusion matrix, plus regression MAE/RMSE/R², persistence baseline, actual-vs-predicted zone speeds, and model metadata.

Superset refresh is database query polling, not push streaming. In each browser tab, open the live dashboard menu (`⋮`), choose **Set auto-refresh interval**, and select **10 seconds**. Superset 6.1.0 treats this as per-session UI state, so this is the only manual dashboard step. Query-result caching is disabled in this local sandbox so each poll can observe new rows.

The historical KPI labels are precise: **Total GPS Records** counts batch position observations; **Unique Vehicles** counts distinct synthetic IDs in the filtered rows; **Total Camera Events** counts batch camera records; and **Monitored Zones** counts zones represented in the filtered GPS data. These are separate from the live stream. The deterministic generator varies activity by day, zone, hour, and spatial hotspot for demonstration; the patterns are artificial, not measurements.

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

The verification program exits nonzero on failure. It checks PostGIS/SRID, zone geometry, exact batch totals and idempotence, correct historical KPIs, batch/live separation, variation, aggregate totals, continuous streaming, purged chronological classifier/regressor metrics and artifact persistence, two fresh successful five-zone inference cycles, model/window/version fields, Superset authentication and SQL connectivity, all 25 saved datasets and 38 chart queries, dashboard layouts, stored-coordinate maps, and absence of replaced chart titles. A clean simulator restart causes a cold-start interval; verification waits until the stream has refilled the full feature window and two inference cycles have completed.

For a visual check, sign into Superset and open the dashboard links above. The historical dashboard has preconfigured Date range and Zone native filters; selecting a zone and clicking **Apply filters** updates its KPIs and four main charts. The required aggregate totals and synthetic pattern checks also run in `app.verify`.

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
| `v_batch_spatial_activity` | Batch-only GPS counts aggregated by day and zone centroid for the map. |
| `v_batch_hourly_activity` | Batch GPS counts and speed per hour/zone for filterable hourly charts. |
| `v_batch_camera_rate_by_zone` | Separately aggregated batch GPS/camera counts and safe events-per-1,000 ratio. |
| `v_live_vehicle_latest` | Latest streamed point per vehicle, limited to event times within ten minutes. |
| `v_live_kpis` | Live-only recently seen vehicles, 15-minute source counts, and latest ingestion time. |
| `v_live_events_recent` | Streamed camera events from the last 24 hours; chart time ranges narrow further. |
| `v_live_gps_5m`, `v_live_camera_5m` | Independent five-minute stream counts for the last 30 minutes. |
| `v_live_vehicles_by_zone_30m`, `v_live_camera_by_zone_30m` | Separate live vehicle/camera counts by zone. |
| `ml_model_metadata`, `ml_regression_holdout` | Saved model schemas, metrics, and historical held-out predictions for classification and speed regression. |
| `ml_live_predictions`, `ml_inference_runs` | Per-zone near-real-time predictions, explicit insufficient/error statuses, and inference-cycle diagnostics. |
| `v_ml_live_latest`, `v_ml_live_latest_status`, `v_ml_live_history` | Latest valid live forecasts, latest per-zone status (including cold-start/data gaps), and recent valid inference history. |
| `v_ml_regression_metrics`, `v_ml_regression_holdout` | Regression metrics/baselines and held-out actual/predicted speed data for diagnostics. |
| `v_zone_activity_5m` | Vehicle and camera counts by mode/zone/five-minute UTC window, independently aggregated before joining. |
| `v_ml_report_metrics`, `v_ml_confusion_matrix` | Stored model metrics/baseline/context and actual held-out confusion counts. |

The historical KPI and chart datasets read batch-only views. The map uses actual PostGIS zone centroids and scales markers by aggregated GPS counts. Daily bars use UTC calendar days; the first and last covered days are partial. The camera-event rate uses separate source aggregates and divides safely when GPS count is zero. Source counts remain available in a compact table. Date and zone filters target each relevant historical dataset. Live charts query stream-only data. The congestion risk is a historical-batch forecast and will be stale after its target hour; it is not live inference.

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

**Superset starts but dashboards are absent:** `docker compose logs superset` should end with provisioning of all three dashboards. A real provisioning failure terminates the container, after which Compose restarts it. Fix the reported error; do not treat a health page alone as successful provisioning.

**Map markers appear without a basemap:** the coordinates come from PostGIS, while map tiles come directly to the browser from `https://tile.openstreetmap.org`. Check browser internet access, content blocking, and the browser console. Do not scrape or bulk-download OSM tiles.

**Live counts do not change:** confirm inserts in `docker compose logs -f simulator`, rerun the stream count SQL, and reselect the per-tab 10-second dashboard auto-refresh. A dashboard tab does not update when polling is off.

**Superset metadata migration fails:** retain the log before resetting. For a disposable local demo only, a clean `docker compose down -v` removes the SQLite metadata volume and PostGIS data so initialization can start over.

## Scope and limitations

- Local sandbox security and SQLite Superset metadata are unsuitable for production.
- OSM map tiles depend on browser internet access and the public tile service's usage policy.
- Dashboard updates are polled, not pushed, and the 10-second selection is per browser session.
- The zone/time comparison is an aggregate correlation only. It intentionally makes no claim that a camera event identifies or matches a vehicle.
- There is no alerting or What-if Analysis, Kafka, Airflow, Redis, Celery, separate API/frontend, cloud service, or real EMCODE/customer data.


## Synthetic congestion model

The simulator trains a `RandomForestClassifier` and `RandomForestRegressor` from historical batch data, using a shared trailing 30-minute feature pipeline. Each target covers the following 60 minutes. UTC is used for storage/windows; hour and weekday features are derived in `Asia/Dubai`. The congestion proxy is next-hour mean speed below 43 km/h with at least five future GPS observations. This remains synthetic and is not validated for real traffic.

Training splits chronologically into train/validation/test partitions. A 90-minute purge separates partitions to keep overlapping 30-minute feature and 60-minute target windows apart. The classifier threshold is selected on validation data only. Test metrics include accuracy, balanced accuracy, precision, recall, F1 and confusion matrix, compared with a majority-class baseline. Regression reports MAE, RMSE and R² against a persistence baseline (future speed equals current observed speed). Test predictions and model metrics are stored in PostgreSQL.

Artifacts and feature schema are saved under `/models` on the persistent `simulator_models` Compose volume and reused on restart. Set `ML_RETRAIN_ON_START=1` in `.env` to explicitly retrain. Otherwise training runs only if the compatible artifact is missing. Inference runs independently of the approximately 3-second ingestion cycle, every 60 seconds, using live-only records in the previous 30 minutes; no batch rows are substituted. During cold start or after an ingestion gap, each zone gets an `insufficient_data` status until its live feature window is fully covered and has at least 20 positions from three vehicles. Prediction cycles, per-zone results, and errors/status are persisted in `ml_inference_runs` and `ml_live_predictions`.

The Live Monitoring dashboard adds a distinct Near Real-Time ML Predictions section: latest valid congestion risk, current versus predicted speed, selected-zone prediction history, and per-zone status/forecast intervals. Congestion Diagnostics retains historical held-out classification evaluation and adds regression metrics, persistence baselines, and actual-versus-predicted test speed by zone. These are synthetic proxy demonstrations only, not real-world forecasts. Superset auto-refresh is browser polling (select 10 seconds from the dashboard menu), not push streaming.

Important ML objects: `ml_model_metadata`, `ml_congestion_predictions` (historical holdout only), `ml_regression_holdout`, `ml_live_predictions`, `ml_inference_runs`, `v_ml_live_latest`, `v_ml_live_latest_status`, `v_ml_live_history`, `v_ml_regression_metrics`, and `v_ml_regression_holdout`.

### Verified local run (2026-10-14)

The rebuilt stack has 40,000 batch GPS records and 4,000 batch camera events, 50 synthetic vehicles, and five zones. Seeding on restart added no batch rows. Continuous stream insertion was observed across a 10-second verification interval. All 20 datasets and 29 saved chart queries executed successfully. In the browser, the Historical Batch dashboard rendered all four KPIs and charts; selecting Demo Creek changed GPS/camera totals to 7,169/1,229, then adding Last week changed them to 6,510/1,100. Live Monitoring rendered all four KPIs, the latest-vehicle map, and separated activity charts; after setting 10-second polling, the 15-minute GPS and camera counts changed on refresh. Congestion Diagnostics rendered its synthetic-data warning, sorted risk chart, evaluation metrics, and held-out confusion table. The dashboard URLs remained stable.

The earlier verified local run predates this live-prediction upgrade; rerun the commands below for current test evidence. Never interpret synthetic evaluation metrics as real-world traffic performance.

Rebuild and verify:
```bash
docker compose up -d --build
docker compose exec simulator python -m app.verify
docker compose exec postgis psql -U emcode -d emcode -c "SELECT model_version, metrics_json FROM ml_model_metadata"
```

The initial upgraded batch updates existing seeded records using the stored anchor and does not require deleting the database volumes.
