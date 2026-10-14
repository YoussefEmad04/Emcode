from __future__ import annotations

import os
import random
import signal
import time
import uuid
from datetime import datetime, timezone

from psycopg2.extras import execute_values

from app.db import connect_with_retry
from app.seed import ZONES, _point_in_zone, initialize_schema, seed_batch

RUNNING = True


def _stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def _integer_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def insert_cycle(connection, rng: random.Random, vehicle_count: int, camera_count: int) -> None:
    now = datetime.now(timezone.utc)
    vehicles = []
    for _ in range(vehicle_count):
        zone = rng.choice(ZONES)
        longitude, latitude = _point_in_zone(rng, zone)
        vehicles.append(
            (
                f"stream-vp-{uuid.uuid4()}",
                f"SYN-V{rng.randrange(1, 51):03d}",
                zone[0],
                now,
                now,
                "stream",
                round(rng.uniform(0, 115), 2),
                longitude,
                latitude,
            )
        )
    cameras = []
    categories = ("traffic_flow", "stopped_vehicle", "lane_congestion")
    for _ in range(camera_count):
        zone = rng.choice(ZONES)
        longitude, latitude = _point_in_zone(rng, zone)
        cameras.append(
            (
                f"stream-ce-{uuid.uuid4()}",
                f"SYN-C{rng.randrange(1, 11):02d}",
                zone[0],
                now,
                now,
                "stream",
                rng.choice(categories),
                longitude,
                latitude,
            )
        )
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            """INSERT INTO vehicle_positions
               (event_id, vehicle_id, zone_id, event_time, ingestion_time, ingestion_mode, speed_kph, geom)
               VALUES %s""",
            vehicles,
            template="(%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
        )
        execute_values(
            cursor,
            """INSERT INTO camera_events
               (event_id, camera_id, zone_id, event_time, ingestion_time, ingestion_mode, event_category, geom)
               VALUES %s""",
            cameras,
            template="(%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
        )
    connection.commit()


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = float(os.getenv("STREAM_INTERVAL_SECONDS", "3"))
    vehicle_min = _integer_setting("STREAM_VEHICLE_MIN", 5)
    vehicle_max = _integer_setting("STREAM_VEHICLE_MAX", 10)
    camera_min = _integer_setting("STREAM_CAMERA_MIN", 1)
    camera_max = _integer_setting("STREAM_CAMERA_MAX", 3)
    if vehicle_min > vehicle_max or camera_min > camera_max or interval <= 0:
        raise ValueError("Stream min/max values and interval are invalid")

    connection = connect_with_retry()
    rng = random.Random()
    try:
        initialize_schema(connection)
        inserted_vehicles, inserted_cameras = seed_batch(connection)
        print(
            f"Initialization complete; deterministic batch rows inserted or refreshed this run: "
            f"vehicles={inserted_vehicles}, cameras={inserted_cameras}",
            flush=True,
        )
        while RUNNING:
            vehicle_count = rng.randint(vehicle_min, vehicle_max)
            camera_count = rng.randint(camera_min, camera_max)
            try:
                if connection.closed:
                    connection = connect_with_retry()
                insert_cycle(connection, rng, vehicle_count, camera_count)
                print(
                    f"{datetime.now(timezone.utc).isoformat()} inserted "
                    f"vehicle_positions={vehicle_count}, camera_events={camera_count}",
                    flush=True,
                )
            except Exception as exc:
                connection.rollback()
                print(f"Stream insert failed: {type(exc).__name__}: {exc}", flush=True)
            deadline = time.monotonic() + interval
            while RUNNING and time.monotonic() < deadline:
                time.sleep(min(0.25, deadline - time.monotonic()))
    finally:
        connection.close()
        print("Simulator stopped cleanly", flush=True)


if __name__ == "__main__":
    main()

