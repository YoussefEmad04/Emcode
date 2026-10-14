import os
import time

import psycopg2


def connection_parameters() -> dict[str, object]:
    return {
        "host": os.getenv("POSTGRES_HOST", "postgis"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DB", "emcode"),
        "user": os.getenv("POSTGRES_USER", "emcode"),
        "password": os.getenv("POSTGRES_PASSWORD", "emcode_local_only"),
        "connect_timeout": 5,
    }


def connect_with_retry(attempts: int = 60, delay_seconds: float = 2.0):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            connection = psycopg2.connect(**connection_parameters())
            connection.autocommit = False
            return connection
        except psycopg2.Error as exc:
            last_error = exc
            print(f"Database connection attempt {attempt}/{attempts} failed: {exc}", flush=True)
            time.sleep(delay_seconds)
    raise RuntimeError(f"Database did not become ready: {last_error}")

