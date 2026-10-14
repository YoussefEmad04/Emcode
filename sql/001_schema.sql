CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS demo_metadata (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS geo_zones (
    zone_id smallint PRIMARY KEY,
    zone_name text NOT NULL UNIQUE,
    geom geometry(Polygon, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicle_positions (
    event_id text PRIMARY KEY,
    vehicle_id text NOT NULL,
    zone_id smallint NOT NULL REFERENCES geo_zones(zone_id),
    event_time timestamptz NOT NULL,
    ingestion_time timestamptz NOT NULL DEFAULT now(),
    ingestion_mode text NOT NULL CHECK (ingestion_mode IN ('batch', 'stream')),
    speed_kph numeric(5,2) NOT NULL CHECK (speed_kph >= 0),
    geom geometry(Point, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS camera_events (
    event_id text PRIMARY KEY,
    camera_id text NOT NULL,
    zone_id smallint NOT NULL REFERENCES geo_zones(zone_id),
    event_time timestamptz NOT NULL,
    ingestion_time timestamptz NOT NULL DEFAULT now(),
    ingestion_mode text NOT NULL CHECK (ingestion_mode IN ('batch', 'stream')),
    event_category text NOT NULL,
    geom geometry(Point, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS geo_zones_geom_gix ON geo_zones USING gist (geom);
CREATE INDEX IF NOT EXISTS vehicle_positions_geom_gix ON vehicle_positions USING gist (geom);
CREATE INDEX IF NOT EXISTS vehicle_positions_event_time_idx ON vehicle_positions (event_time DESC);
CREATE INDEX IF NOT EXISTS vehicle_positions_mode_time_idx ON vehicle_positions (ingestion_mode, event_time DESC);
CREATE INDEX IF NOT EXISTS camera_events_geom_gix ON camera_events USING gist (geom);
CREATE INDEX IF NOT EXISTS camera_events_event_time_idx ON camera_events (event_time DESC);
CREATE INDEX IF NOT EXISTS camera_events_mode_time_idx ON camera_events (ingestion_mode, event_time DESC);

