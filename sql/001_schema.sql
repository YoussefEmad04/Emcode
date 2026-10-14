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


-- Lightweight ML result storage. Predictions use synthetic historical data only.
CREATE TABLE IF NOT EXISTS ml_congestion_predictions (
    forecast_kind text NOT NULL CHECK (forecast_kind IN ('holdout','forecast')),
    zone_id smallint NOT NULL REFERENCES geo_zones(zone_id),
    feature_hour timestamptz NOT NULL,
    target_hour timestamptz NOT NULL,
    congestion_probability double precision NOT NULL CHECK (congestion_probability BETWEEN 0 AND 1),
    predicted_congestion boolean NOT NULL,
    actual_congestion boolean,
    gps_record_count bigint NOT NULL,
    camera_event_count bigint NOT NULL,
    avg_speed_kph double precision NOT NULL,
    model_version text NOT NULL,
    PRIMARY KEY (forecast_kind, zone_id, feature_hour)
);

CREATE TABLE IF NOT EXISTS ml_model_metadata (
    model_version text PRIMARY KEY,
    trained_at timestamptz NOT NULL DEFAULT now(),
    metrics_json text NOT NULL
);

CREATE TABLE IF NOT EXISTS ml_live_predictions (
    zone_id smallint NOT NULL REFERENCES geo_zones(zone_id),
    prediction_time timestamptz NOT NULL,
    feature_window_start timestamptz NOT NULL,
    feature_window_end timestamptz NOT NULL,
    target_start timestamptz NOT NULL,
    target_end timestamptz NOT NULL,
    congestion_probability double precision CHECK (congestion_probability BETWEEN 0 AND 1),
    predicted_congested boolean,
    current_avg_speed_kph double precision,
    predicted_avg_speed_kph double precision,
    predicted_speed_change_pct double precision,
    classifier_version text NOT NULL,
    regressor_version text NOT NULL,
    prediction_status text NOT NULL CHECK (prediction_status IN ('ok','insufficient_data','error')),
    error_message text,
    gps_record_count integer NOT NULL DEFAULT 0,
    camera_event_count integer NOT NULL DEFAULT 0,
    PRIMARY KEY(zone_id,prediction_time),
    CHECK (feature_window_end >= feature_window_start),
    CHECK (target_end > target_start)
);
ALTER TABLE ml_live_predictions ADD COLUMN IF NOT EXISTS error_message text;
CREATE INDEX IF NOT EXISTS ml_live_predictions_time_idx ON ml_live_predictions(prediction_time DESC);

CREATE TABLE IF NOT EXISTS ml_inference_runs (
    prediction_time timestamptz PRIMARY KEY,
    feature_window_start timestamptz NOT NULL,
    target_end timestamptz NOT NULL,
    status text NOT NULL,
    prediction_count integer NOT NULL DEFAULT 0,
    successful_zone_count integer NOT NULL DEFAULT 0,
    insufficient_zone_count integer NOT NULL DEFAULT 0,
    failed_zone_count integer NOT NULL DEFAULT 0,
    error_message text,
    completed_at timestamptz
);
ALTER TABLE ml_inference_runs ADD COLUMN IF NOT EXISTS failed_zone_count integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS ml_regression_holdout (
    id bigserial PRIMARY KEY,
    zone_id smallint NOT NULL REFERENCES geo_zones(zone_id),
    observation_time timestamptz NOT NULL,
    target_start timestamptz NOT NULL,
    target_end timestamptz NOT NULL,
    actual_avg_speed_kph double precision NOT NULL,
    predicted_avg_speed_kph double precision NOT NULL,
    persistence_avg_speed_kph double precision NOT NULL,
    model_version text NOT NULL
);
