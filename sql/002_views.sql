CREATE OR REPLACE VIEW v_batch_vehicle_activity AS
SELECT
    p.event_id,
    p.vehicle_id,
    p.event_time,
    p.ingestion_time,
    p.ingestion_mode,
    p.speed_kph,
    z.zone_id,
    z.zone_name,
    ST_Y(p.geom) AS latitude,
    ST_X(p.geom) AS longitude
FROM vehicle_positions p
JOIN geo_zones z ON ST_Contains(z.geom, p.geom)
WHERE p.ingestion_mode = 'batch';

CREATE OR REPLACE VIEW v_batch_camera_activity AS
SELECT
    e.event_id,
    e.camera_id,
    e.event_time,
    e.ingestion_time,
    e.ingestion_mode,
    e.event_category,
    z.zone_id,
    z.zone_name,
    ST_Y(e.geom) AS latitude,
    ST_X(e.geom) AS longitude
FROM camera_events e
JOIN geo_zones z ON ST_Contains(z.geom, e.geom)
WHERE e.ingestion_mode = 'batch';

CREATE OR REPLACE VIEW v_batch_daily_zone_activity AS
WITH gps_daily AS (
    SELECT
        date_trunc('day', p.event_time) AS event_time,
        z.zone_id,
        z.zone_name,
        count(*)::bigint AS gps_record_count
    FROM vehicle_positions p
    JOIN geo_zones z ON ST_Contains(z.geom, p.geom)
    WHERE p.ingestion_mode = 'batch'
    GROUP BY date_trunc('day', p.event_time), z.zone_id, z.zone_name
),
camera_daily AS (
    SELECT
        date_trunc('day', e.event_time) AS event_time,
        z.zone_id,
        z.zone_name,
        count(*)::bigint AS camera_event_count
    FROM camera_events e
    JOIN geo_zones z ON ST_Contains(z.geom, e.geom)
    WHERE e.ingestion_mode = 'batch'
    GROUP BY date_trunc('day', e.event_time), z.zone_id, z.zone_name
)
SELECT
    COALESCE(g.event_time, c.event_time) AS event_time,
    COALESCE(g.zone_id, c.zone_id) AS zone_id,
    COALESCE(g.zone_name, c.zone_name) AS zone_name,
    COALESCE(g.gps_record_count, 0)::bigint AS gps_record_count,
    COALESCE(c.camera_event_count, 0)::bigint AS camera_event_count
FROM gps_daily g
FULL OUTER JOIN camera_daily c
  ON c.event_time = g.event_time
 AND c.zone_id = g.zone_id;

-- Per-day aggregate at each demo zone centroid. Superset combines dates
-- after applying dashboard filters: five readable proportional markers.
CREATE OR REPLACE VIEW v_batch_spatial_activity AS
SELECT
    date_trunc('day', p.event_time) AS event_time,
    z.zone_id,
    z.zone_name,
    ST_Y(ST_Centroid(z.geom))::double precision AS latitude,
    ST_X(ST_Centroid(z.geom))::double precision AS longitude,
    count(*)::bigint AS gps_record_count
FROM vehicle_positions p
JOIN geo_zones z ON ST_Contains(z.geom, p.geom)
WHERE p.ingestion_mode='batch'
GROUP BY date_trunc('day', p.event_time), z.zone_id, z.zone_name, z.geom;

-- One row for each zone/hour, built without multiplying GPS/camera rows.
CREATE OR REPLACE VIEW v_batch_hourly_zone_activity AS
WITH gps AS (
    SELECT
        date_trunc('hour', p.event_time) AS event_time,
        z.zone_id,
        z.zone_name,
        count(*)::bigint AS gps_record_count,
        avg(p.speed_kph)::double precision AS avg_speed_kph,
        avg(CASE WHEN p.speed_kph < 30 THEN 1.0 ELSE 0.0 END)::double precision AS slow_fraction
    FROM vehicle_positions p
    JOIN geo_zones z ON ST_Contains(z.geom, p.geom)
    WHERE p.ingestion_mode='batch'
    GROUP BY date_trunc('hour', p.event_time), z.zone_id, z.zone_name
),
camera AS (
    SELECT date_trunc('hour', e.event_time) AS event_time,
           z.zone_id, count(*)::bigint AS camera_event_count
    FROM camera_events e
    JOIN geo_zones z ON ST_Contains(z.geom, e.geom)
    WHERE e.ingestion_mode='batch'
    GROUP BY date_trunc('hour', e.event_time), z.zone_id
)
SELECT
    g.*,
    coalesce(c.camera_event_count, 0)::bigint AS camera_event_count,
    extract(hour FROM g.event_time AT TIME ZONE 'Asia/Dubai')::integer AS local_hour,
    extract(isodow FROM g.event_time AT TIME ZONE 'Asia/Dubai')::integer AS local_weekday
FROM gps g
LEFT JOIN camera c ON c.event_time=g.event_time AND c.zone_id=g.zone_id;

CREATE OR REPLACE VIEW v_ml_congestion_forecast AS
SELECT p.target_hour AS event_time, p.feature_hour, p.zone_id, z.zone_name,
       p.congestion_probability * 100 AS congestion_risk_pct,
       p.predicted_congestion, p.gps_record_count,
       p.camera_event_count, p.avg_speed_kph, p.model_version
FROM ml_congestion_predictions p
JOIN geo_zones z ON z.zone_id=p.zone_id
WHERE p.forecast_kind='forecast';

CREATE OR REPLACE VIEW v_ml_congestion_holdout AS
SELECT p.target_hour AS event_time, p.feature_hour, p.zone_id, z.zone_name,
       p.congestion_probability * 100 AS congestion_risk_pct,
       CASE WHEN p.predicted_congestion THEN 1 ELSE 0 END AS predicted_congested,
       CASE WHEN p.actual_congestion THEN 1 ELSE 0 END AS observed_congested,
       p.model_version
FROM ml_congestion_predictions p
JOIN geo_zones z ON z.zone_id=p.zone_id
WHERE p.forecast_kind='holdout';

CREATE OR REPLACE VIEW v_live_vehicle_latest AS
SELECT event_id, vehicle_id, event_time, ingestion_time, ingestion_mode,
       speed_kph, zone_id, zone_name, latitude, longitude
FROM (
    SELECT
        p.event_id,
        p.vehicle_id,
        p.event_time,
        p.ingestion_time,
        p.ingestion_mode,
        p.speed_kph,
        z.zone_id,
        z.zone_name,
        ST_Y(p.geom) AS latitude,
        ST_X(p.geom) AS longitude,
        row_number() OVER (PARTITION BY p.vehicle_id ORDER BY p.event_time DESC, p.event_id DESC) AS rn
    FROM vehicle_positions p
    JOIN geo_zones z ON ST_Contains(z.geom, p.geom)
    WHERE p.ingestion_mode = 'stream'
) ranked
WHERE rn = 1 AND event_time >= now() - interval '10 minutes';

CREATE OR REPLACE VIEW v_live_events_recent AS
SELECT
    e.event_id,
    e.camera_id,
    e.event_time,
    e.ingestion_time,
    e.ingestion_mode,
    e.event_category,
    z.zone_id,
    z.zone_name,
    ST_Y(e.geom) AS latitude,
    ST_X(e.geom) AS longitude
FROM camera_events e
JOIN geo_zones z ON ST_Contains(z.geom, e.geom)
WHERE e.ingestion_mode = 'stream'
  AND e.event_time >= now() - interval '24 hours';

CREATE OR REPLACE VIEW v_zone_activity_5m AS
WITH vehicle_counts AS (
    SELECT
        p.ingestion_mode,
        date_bin(interval '5 minutes', p.event_time, timestamptz '2000-01-01 00:00:00+00') AS window_start,
        z.zone_id,
        z.zone_name,
        count(*) AS vehicle_position_count
    FROM vehicle_positions p
    JOIN geo_zones z ON ST_Contains(z.geom, p.geom)
    GROUP BY p.ingestion_mode, window_start, z.zone_id, z.zone_name
),
camera_counts AS (
    SELECT
        e.ingestion_mode,
        date_bin(interval '5 minutes', e.event_time, timestamptz '2000-01-01 00:00:00+00') AS window_start,
        z.zone_id,
        z.zone_name,
        count(*) AS camera_event_count
    FROM camera_events e
    JOIN geo_zones z ON ST_Contains(z.geom, e.geom)
    GROUP BY e.ingestion_mode, window_start, z.zone_id, z.zone_name
)
SELECT
    COALESCE(v.ingestion_mode, c.ingestion_mode) AS ingestion_mode,
    COALESCE(v.window_start, c.window_start) AS window_start,
    COALESCE(v.zone_id, c.zone_id) AS zone_id,
    COALESCE(v.zone_name, c.zone_name) AS zone_name,
    COALESCE(v.vehicle_position_count, 0)::bigint AS vehicle_position_count,
    COALESCE(c.camera_event_count, 0)::bigint AS camera_event_count
FROM vehicle_counts v
FULL OUTER JOIN camera_counts c
  ON c.ingestion_mode = v.ingestion_mode
 AND c.window_start = v.window_start
 AND c.zone_id = v.zone_id;

