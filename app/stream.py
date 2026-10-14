from __future__ import annotations

import os
import random
import signal
import time
import uuid
from datetime import datetime, timezone

from psycopg2.extras import execute_values

from app.db import connect_with_retry
from app.seed import ZONES, _point_in_zone, _synthetic_speed, initialize_schema, seed_batch
from app.ml import train_and_score

RUNNING = True


def _stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def _integer_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def load_vehicle_state(connection, rng: random.Random) -> dict[str, tuple]:
    """Reuse last committed location after restart, so vehicles do not teleport."""
    state = {}
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT DISTINCT ON (vehicle_id) vehicle_id, zone_id,
                   ST_X(geom), ST_Y(geom)
            FROM vehicle_positions WHERE ingestion_mode='stream'
            ORDER BY vehicle_id, event_time DESC, event_id DESC
        """)
        for vehicle_id, zone_id, lon, lat in cursor.fetchall():
            zone = next(z for z in ZONES if z[0] == zone_id)
            state[vehicle_id] = (zone, lon, lat, rng.uniform(0, 6.2831853))
    return state


def insert_cycle(connection, rng: random.Random, vehicle_count: int,
                 camera_count: int, state: dict[str, tuple], interval: float) -> None:
    import math

    now = datetime.now(timezone.utc)
    vehicles = []
    available_ids = [f"SYN-V{i:03d}" for i in range(1, 51)]
    for vehicle_id in rng.sample(available_ids, k=min(vehicle_count, 50)):
        if vehicle_id not in state:
            index = int(vehicle_id[-3:]) - 1
            zone = ZONES[index % len(ZONES)]
            lon, lat = _point_in_zone(rng, zone)
            state[vehicle_id] = (zone, lon, lat, rng.uniform(0, 2 * math.pi))
        zone, longitude, latitude, heading = state[vehicle_id]
        speed = _synthetic_speed(rng, zone[0], now)
        distance_m = speed / 3.6 * interval
        dlat = math.sin(heading) * distance_m / 111_000
        dlon = math.cos(heading) * distance_m / (111_000 * math.cos(math.radians(latitude)))
        next_lon, next_lat = longitude + dlon, latitude + dlat
        # Bounce off fictional zone bounds rather than teleport to other zones.
        if not (zone[2] + 0.0002 <= next_lon <= zone[4] - 0.0002):
            heading = math.pi - heading
        if not (zone[3] + 0.0002 <= next_lat <= zone[5] - 0.0002):
            heading = -heading
        next_lon = min(zone[4] - 0.0002, max(zone[2] + 0.0002, next_lon))
        next_lat = min(zone[5] - 0.0002, max(zone[3] + 0.0002, next_lat))
        vehicles.append(
            (f"stream-vp-{uuid.uuid4()}", vehicle_id, zone[0], now, now,
             "stream", speed, next_lon, next_lat)
        )
        state[vehicle_id] = (zone, next_lon, next_lat, heading)
    cameras = []
    categories = ("traffic_flow", "stopped_vehicle", "lane_congestion")
    for _ in range(camera_count):
        zone = rng.choice(ZONES)
        longitude, latitude = _point_in_zone(rng, zone)
        cameras.append(
            (f"stream-ce-{uuid.uuid4()}", f"SYN-C{rng.randrange(1, 11):02d}",
             zone[0], now, now, "stream", rng.choice(categories), longitude, latitude)
        )
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            """INSERT INTO vehicle_positions
               (event_id, vehicle_id, zone_id, event_time, ingestion_time,
                ingestion_mode, speed_kph, geom) VALUES %s""",
            vehicles,
            template="(%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
        )
        execute_values(
            cursor,
            """INSERT INTO camera_events
               (event_id, camera_id, zone_id, event_time, ingestion_time,
                ingestion_mode, event_category, geom) VALUES %s""",
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
        train_and_score(connection)
        vehicle_state = load_vehicle_state(connection, rng)
        while RUNNING:
            vehicle_count = rng.randint(vehicle_min, vehicle_max)
            camera_count = rng.randint(camera_min, camera_max)
            try:
                if connection.closed:
                    connection = connect_with_retry()
                insert_cycle(connection, rng, vehicle_count, camera_count, vehicle_state, interval)
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

