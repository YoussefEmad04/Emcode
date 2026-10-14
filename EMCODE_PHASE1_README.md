# EMCODE — Geospatial Analytics PoC (Phase 1)

> **README starter for the Codex-generated repository.** This describes the intended project contract. Have Codex update it to match the files and commands it actually implements and verifies. The application source code is **not** included in this standalone README.

A simple local demo that combines **PostgreSQL + PostGIS**, synthetic **batch and live geospatial events**, and **Apache Superset** dashboards. No customer data is required.

## What the demo proves

- Load seven days of historical GPS and camera-event data **once** (batch).
- Simulate new vehicle positions and camera events every few seconds (continuous insert).
- Store both in PostGIS and analyze which zone each event falls into.
- Display separate **Historical Batch** and **Live Monitoring** dashboards in Superset.
- Refresh the live dashboard automatically by periodic querying.
- Compare vehicle activity and camera-event counts by zone and time window.

All vehicle IDs, camera events, positions and detections are **synthetic**. This is a local PoC, **not production-ready**.

## Architecture

```text
             Synthetic event generator (Python)
                             |
                +------------+------------+
                |                         |
          7-day batch load         live events (every 3s)
                |                         |
                +------------+------------+
                             |
                  PostgreSQL + PostGIS
               zones / vehicles / cameras
                             |
                    spatial SQL views
                             |
                     Apache Superset
                   /                \
          Historical Batch       Live Monitoring
                                   refresh ~10s
```

There are only **three long-running Docker services**: `postgis`, `superset`, and `simulator`. Superset uses a separate persisted SQLite file for its **own dashboard metadata** in this local demo. The event data itself lives in PostgreSQL.

**OpenStreetMap is a basemap, not the source of events.** The map tiles come from a public tile service when your browser has internet access. Give attribution to © OpenStreetMap contributors. PostGIS provides geospatial storage and spatial SQL functions; it does not generate GIS data by itself.

## Prerequisites

- Docker Desktop (with Compose v2) or Docker Engine + Compose plugin.
- A browser and internet access initially to pull images, and for map tiles.
- Suggested available resources: at least 4 GB RAM for containers, preferably more for Superset.
- On Apple Silicon Macs, some `postgis/postgis` tags are AMD64-only. If Docker warns about architecture, follow the generated project's verified Compose platform configuration or the troubleshooting section.

## Start the demo

From the generated repository directory:

```bash
cp .env.example .env
```

Open `.env` and review the local demo database/admin credentials and secret key. Do **not** use these development settings for a publicly accessible deployment.

Then:

```bash
docker compose up -d --build
```

The first launch may take a few minutes while images download, PostGIS initializes and Superset runs migrations and creates the dashboards. Check:

```bash
docker compose ps
docker compose logs -f simulator
```

Open: **http://localhost:8088**

Log in using the `SUPERSET_ADMIN_USERNAME` and `SUPERSET_ADMIN_PASSWORD` values in `.env`.

The implementation prompt asks Codex to provision these automatically:

1. **EMCODE | Historical Batch** — historical chart, event counts, activity by zone, and geographic view.
2. **EMCODE | Live Monitoring** — latest vehicle points, recent-event KPI, recent activity, and a zone comparison.

If provisioning couldn't be fully automated in the chosen Superset version, Codex must document the actual remaining UI steps here and label the limitation clearly.

## Batch versus live

- **Batch:** the initializer generates reproducible historical events once. Restarting the demo must not insert a second copy.
- **Live:** `simulator` appends events with fresh UTC timestamps while it is running. This makes the dataset grow continuously.
- **Dashboard:** Superset queries PostgreSQL. Its auto-refresh mechanism is *polling / near real-time*, not a websocket stream.

To enable periodic dashboard refresh if it wasn't reliably persisted by Codex's provisioning script:

1. Open **EMCODE | Live Monitoring**.
2. Click the dashboard **⋮** menu.
3. Choose **Set auto-refresh interval** → **10 seconds**.
4. Leave the tab open and watch the values change.

Superset documents this auto-refresh selection as **per session**. You may need to reselect it after closing the browser tab.

## Verify all connections

Run:

```bash
# All containers and their state
docker compose ps

# Check PostGIS is installed in the running PostgreSQL database
docker compose exec postgis psql -U emcode -d emcode -c 'SELECT PostGIS_Version();'

# Compare historical versus simulated events
docker compose exec postgis psql -U emcode -d emcode -c "SELECT ingestion_mode, COUNT(*) FROM vehicle_positions GROUP BY ingestion_mode ORDER BY ingestion_mode;"

# Run the integration checks implemented by Codex
docker compose exec simulator python -m app.verify
```

Wait about 10 seconds and run the counts again. The `stream` count should grow, while the `batch` count stays constant. The verification script should check the database, geometry, counts, Superset connection and dashboard assets, and exit nonzero if a required check fails.

### Expected SQL objects

| Name | Purpose |
|---|---|
| `geo_zones` | Five synthetic PostGIS polygon zones. |
| `vehicle_positions` | GPS points with batch/stream mode and timestamps. |
| `camera_events` | Synthetic geolocated camera events. |
| `v_batch_vehicle_activity` | Chart-friendly historical records. |
| `v_live_vehicle_latest` | Latest live point for each vehicle, with numeric latitude/longitude. |
| `v_live_events_recent` | Recent streaming events. |
| `v_zone_activity_5m` | Vehicle and camera event counts by five-minute window and zone. |

## Troubleshooting

**Superset opens but Postgres connection fails:** In Docker Compose, the database host must be **`postgis`**, not `localhost`. Check the Superset image includes a PostgreSQL driver and that the PostGIS service is healthy.

**Superset starts but dashboards do not appear:** Review `docker compose logs superset simulator`. The provisioning script should retry until Superset is initialized and then report exactly what did or did not get created. Do not confuse a successful container startup with a working dashboard.

**Map is blank:** Confirm latitude/longitude query columns contain values and OSM tiles are accessible from the browser. Verify the configured basemap and any content-security restrictions in Superset. A blank basemap does not necessarily mean data rows are missing.

**Live counts do not change:** Check `docker compose logs simulator` and the `stream` row count query. If inserts are occurring, recheck the dashboard's 10-second auto-refresh selection and data-cache settings.

**Apple Silicon image/platform warning:** Check your Docker platform and the chosen PostGIS image architectures. An AMD64-only PostGIS image may need `platform: linux/amd64` with emulation, which is slower. Have Codex verify the selected image rather than blindly ignoring platform errors.

**First launch takes time:** Wait for metadata migrations/initialization and inspect logs:

```bash
docker compose logs -f postgis superset simulator
```

## Persistence, stopping and resetting

Stop containers but keep data and Superset configuration:

```bash
docker compose down
```

Start again:

```bash
docker compose up -d --build
```

**Delete all demo database rows, dashboard definitions, and persisted volumes** and start fresh:

```bash
docker compose down -v
docker compose up -d --build
```

**Warning:** `down -v` deletes named volumes for this Compose project.

## Scope limits

This Phase 1 does **not** include: What-if Analysis, prediction models, a separate web app, message broker, Airflow, alert automation, production monitoring, hardened security, or real EMCODE data. Only after this small end-to-end flow works should further capabilities be considered.

## Useful references

- [PostGIS Docker image](https://hub.docker.com/r/postgis/postgis)
- [Apache Superset Docker Compose](https://superset.apache.org/docs/installation/docker-compose/)
- [Superset PostgreSQL connection](https://superset.apache.org/docs/databases/supported/postgresql/)
- [Superset map tiles](https://superset.apache.org/admin-docs/configuration/map-tiles/)
- [Superset dashboard refresh](https://superset.apache.org/user-docs/using-superset/creating-your-first-dashboard/)
