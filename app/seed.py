from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg2.extras import execute_values

from app.db import connect_with_retry

BATCH_VEHICLE_ROWS = 20_000
BATCH_CAMERA_ROWS = 2_000
RANDOM_SEED = 20261013
BATCH_SEED_VERSION = "phase1-historical-dashboard-v2"

# Intentionally artificial weights create an easy-to-read demo pattern. They do
# not model real Dubai traffic or camera activity.
VEHICLE_ZONE_WEIGHTS = (34, 25, 18, 14, 9)
CAMERA_ZONE_WEIGHTS = (17, 13, 31, 11, 28)
VEHICLE_DAY_WEIGHTS = (0.62, 0.88, 1.18, 1.48, 1.24, 0.78, 0.52)
CAMERA_DAY_WEIGHTS = (0.70, 0.96, 1.08, 1.31, 1.52, 0.82, 0.61)
HOURLY_WEIGHTS = (
    0.20, 0.16, 0.14, 0.13, 0.16, 0.30, 0.65, 1.25,
    1.55, 1.20, 0.92, 0.85, 0.82, 0.88, 0.96, 1.12,
    1.42, 1.66, 1.48, 1.05, 0.78, 0.55, 0.38, 0.27,
)
HOTSPOTS = ((0.28, 0.31), (0.68, 0.66), (0.52, 0.43))

# Deliberately fictitious rectangles near Dubai; points are generated strictly inside them.
ZONES = (
    (1, "Demo Marina", 55.1250, 25.0750, 55.1550, 25.1000),
    (2, "Demo Downtown", 55.2650, 25.1850, 55.2950, 25.2100),
    (3, "Demo Creek", 55.3250, 25.2200, 55.3550, 25.2450),
    (4, "Demo Silicon", 55.3700, 25.1050, 55.4000, 25.1300),
    (5, "Demo Jebel", 55.0200, 24.9850, 55.0500, 25.0100),
)


def _read_sql(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "sql" / name).read_text(encoding="utf-8")


def _point_in_zone(rng: random.Random, zone: tuple) -> tuple[float, float]:
    _, _, min_lon, min_lat, max_lon, max_lat = zone
    center_x, center_y = rng.choices(HOTSPOTS, weights=(5, 3, 2), k=1)[0]
    fraction_x = min(0.97, max(0.03, rng.gauss(center_x, 0.09)))
    fraction_y = min(0.97, max(0.03, rng.gauss(center_y, 0.09)))
    longitude = min_lon + (max_lon - min_lon) * fraction_x
    latitude = min_lat + (max_lat - min_lat) * fraction_y
    return longitude, latitude


def _weighted_event_time(
    rng: random.Random,
    start: datetime,
    day_weights: tuple[float, ...],
) -> datetime:
    day = rng.choices(range(7), weights=day_weights, k=1)[0]
    hour = rng.choices(range(24), weights=HOURLY_WEIGHTS, k=1)[0]
    return start + timedelta(
        days=day,
        hours=hour,
        minutes=rng.randrange(60),
        seconds=rng.randrange(60),
    )


def initialize_schema(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_read_sql("001_schema.sql"))
        zone_rows = [
            (
                zone_id,
                name,
                f"POLYGON(({min_lon} {min_lat},{max_lon} {min_lat},{max_lon} {max_lat},{min_lon} {max_lat},{min_lon} {min_lat}))",
            )
            for zone_id, name, min_lon, min_lat, max_lon, max_lat in ZONES
        ]
        execute_values(
            cursor,
            """
            INSERT INTO geo_zones (zone_id, zone_name, geom)
            VALUES %s
            ON CONFLICT (zone_id) DO UPDATE
            SET zone_name = EXCLUDED.zone_name, geom = EXCLUDED.geom
            """,
            zone_rows,
            template="(%s, %s, ST_GeomFromText(%s, 4326))",
        )
        cursor.execute(_read_sql("002_views.sql"))
    connection.commit()


def seed_batch(connection) -> tuple[int, int]:
    rng = random.Random(RANDOM_SEED)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(454_001)")
        cursor.execute("SELECT value FROM demo_metadata WHERE key = 'batch_anchor_utc'")
        row = cursor.fetchone()
        if row:
            anchor = datetime.fromisoformat(row[0])
        else:
            anchor = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            cursor.execute(
                "INSERT INTO demo_metadata (key, value) VALUES ('batch_anchor_utc', %s)",
                (anchor.isoformat(),),
            )
        start = anchor - timedelta(days=7)
        cursor.execute("SELECT value FROM demo_metadata WHERE key = 'batch_seed_version'")
        version_row = cursor.fetchone()
        refresh_existing = not version_row or version_row[0] != BATCH_SEED_VERSION
        conflict_clause = (
            "DO UPDATE SET vehicle_id=EXCLUDED.vehicle_id, zone_id=EXCLUDED.zone_id, "
            "event_time=EXCLUDED.event_time, ingestion_time=EXCLUDED.ingestion_time, "
            "ingestion_mode=EXCLUDED.ingestion_mode, speed_kph=EXCLUDED.speed_kph, geom=EXCLUDED.geom"
            if refresh_existing
            else "DO NOTHING"
        )

        vehicle_rows = []
        for index in range(BATCH_VEHICLE_ROWS):
            zone = rng.choices(ZONES, weights=VEHICLE_ZONE_WEIGHTS, k=1)[0]
            longitude, latitude = _point_in_zone(rng, zone)
            event_time = _weighted_event_time(rng, start, VEHICLE_DAY_WEIGHTS)
            vehicle_rows.append(
                (
                    f"batch-vp-{index:05d}",
                    f"SYN-V{index % 50 + 1:03d}",
                    zone[0],
                    event_time,
                    anchor,
                    "batch",
                    round(rng.uniform(0, 115), 2),
                    longitude,
                    latitude,
                )
            )
        changed_vehicle_rows = execute_values(
            cursor,
            f"""
            INSERT INTO vehicle_positions
              (event_id, vehicle_id, zone_id, event_time, ingestion_time, ingestion_mode, speed_kph, geom)
            VALUES %s ON CONFLICT (event_id) {conflict_clause}
            RETURNING event_id
            """,
            vehicle_rows,
            template="(%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
            page_size=2_000,
            fetch=True,
        )
        vehicle_changed = len(changed_vehicle_rows)

        categories = ("traffic_flow", "stopped_vehicle", "lane_congestion")
        camera_rows = []
        for index in range(BATCH_CAMERA_ROWS):
            zone = rng.choices(ZONES, weights=CAMERA_ZONE_WEIGHTS, k=1)[0]
            longitude, latitude = _point_in_zone(rng, zone)
            event_time = _weighted_event_time(rng, start, CAMERA_DAY_WEIGHTS)
            camera_rows.append(
                (
                    f"batch-ce-{index:05d}",
                    f"SYN-C{index % 10 + 1:02d}",
                    zone[0],
                    event_time,
                    anchor,
                    "batch",
                    categories[index % len(categories)],
                    longitude,
                    latitude,
                )
            )
        camera_conflict_clause = (
            "DO UPDATE SET camera_id=EXCLUDED.camera_id, zone_id=EXCLUDED.zone_id, "
            "event_time=EXCLUDED.event_time, ingestion_time=EXCLUDED.ingestion_time, "
            "ingestion_mode=EXCLUDED.ingestion_mode, event_category=EXCLUDED.event_category, geom=EXCLUDED.geom"
            if refresh_existing
            else "DO NOTHING"
        )
        changed_camera_rows = execute_values(
            cursor,
            f"""
            INSERT INTO camera_events
              (event_id, camera_id, zone_id, event_time, ingestion_time, ingestion_mode, event_category, geom)
            VALUES %s ON CONFLICT (event_id) {camera_conflict_clause}
            RETURNING event_id
            """,
            camera_rows,
            template="(%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
            page_size=1_000,
            fetch=True,
        )
        camera_changed = len(changed_camera_rows)
        if refresh_existing:
            cursor.execute(
                """INSERT INTO demo_metadata (key, value)
                   VALUES ('batch_seed_version', %s)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""",
                (BATCH_SEED_VERSION,),
            )
    connection.commit()
    return vehicle_changed, camera_changed


def main() -> None:
    connection = connect_with_retry()
    try:
        initialize_schema(connection)
        vehicles, cameras = seed_batch(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                  count(*) FILTER (WHERE ingestion_mode='batch') AS vehicles,
                  (SELECT count(*) FROM camera_events WHERE ingestion_mode='batch') AS cameras
                FROM vehicle_positions
                """
            )
            total_vehicles, total_cameras = cursor.fetchone()
        print(
            f"Batch seed complete: inserted vehicles={vehicles}, cameras={cameras}; "
            f"stored batch vehicles={total_vehicles}, cameras={total_cameras}",
            flush=True,
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()

