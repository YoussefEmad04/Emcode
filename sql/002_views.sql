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

-- Dashboard-friendly aggregates. Every source is counted before any cross-source
-- join, and historical views are explicitly restricted to the batch partition.
CREATE OR REPLACE VIEW v_batch_hourly_activity AS
SELECT date_trunc('hour', p.event_time) AS event_time,
       extract(hour FROM p.event_time AT TIME ZONE 'Asia/Dubai')::integer AS local_hour,
       z.zone_id, z.zone_name,
       count(*)::bigint AS gps_record_count,
       avg(p.speed_kph)::double precision AS avg_speed_kph
FROM vehicle_positions p
JOIN geo_zones z ON z.zone_id=p.zone_id
WHERE p.ingestion_mode='batch'
GROUP BY date_trunc('hour', p.event_time),
         extract(hour FROM p.event_time AT TIME ZONE 'Asia/Dubai'), z.zone_id, z.zone_name;

CREATE OR REPLACE VIEW v_batch_camera_rate_by_zone AS
WITH gps AS (
    SELECT date_trunc('day', p.event_time) AS event_time, z.zone_id, z.zone_name,
           count(*)::bigint AS gps_record_count
    FROM vehicle_positions p JOIN geo_zones z ON z.zone_id=p.zone_id
    WHERE p.ingestion_mode='batch'
    GROUP BY 1,2,3
), camera AS (
    SELECT date_trunc('day', e.event_time) AS event_time, z.zone_id, z.zone_name,
           count(*)::bigint AS camera_event_count
    FROM camera_events e JOIN geo_zones z ON z.zone_id=e.zone_id
    WHERE e.ingestion_mode='batch'
    GROUP BY 1,2,3
)
SELECT g.event_time, g.zone_id, g.zone_name, g.gps_record_count,
       coalesce(c.camera_event_count,0)::bigint AS camera_event_count,
       CASE WHEN g.gps_record_count=0 THEN NULL
            ELSE 1000.0 * coalesce(c.camera_event_count,0) / g.gps_record_count
       END::double precision AS camera_events_per_1000_gps
FROM gps g LEFT JOIN camera c USING (event_time, zone_id, zone_name);

CREATE OR REPLACE VIEW v_live_kpis AS
WITH gps AS (
    SELECT count(*) FILTER (WHERE event_time >= now()-interval '15 minutes')::bigint AS gps_15m,
           count(DISTINCT vehicle_id) FILTER (WHERE event_time >= now()-interval '10 minutes')::bigint AS vehicles_10m,
           max(ingestion_time) AS latest_gps_received
    FROM vehicle_positions WHERE ingestion_mode='stream'
), camera AS (
    SELECT count(*) FILTER (WHERE event_time >= now()-interval '15 minutes')::bigint AS camera_15m,
           max(ingestion_time) AS latest_camera_received
    FROM camera_events WHERE ingestion_mode='stream'
)
SELECT gps.gps_15m, gps.vehicles_10m, camera.camera_15m,
       greatest(gps.latest_gps_received,camera.latest_camera_received) AS latest_event_received_at
FROM gps CROSS JOIN camera;

CREATE OR REPLACE VIEW v_live_gps_5m AS
WITH bins AS (
    SELECT generate_series(
        date_bin(interval '5 minutes', now()-interval '25 minutes', timestamptz '2000-01-01 00:00:00+00'),
        date_bin(interval '5 minutes', now(), timestamptz '2000-01-01 00:00:00+00'),
        interval '5 minutes') AS window_start
), counts AS (
    SELECT date_bin(interval '5 minutes', event_time, timestamptz '2000-01-01 00:00:00+00') AS window_start,
           count(*)::bigint AS gps_record_count
    FROM vehicle_positions
    WHERE ingestion_mode='stream' AND event_time >= now()-interval '30 minutes'
    GROUP BY 1
)
SELECT bins.window_start, coalesce(counts.gps_record_count,0)::bigint AS gps_record_count
FROM bins LEFT JOIN counts USING (window_start);

CREATE OR REPLACE VIEW v_live_camera_5m AS
WITH bins AS (
    SELECT generate_series(
        date_bin(interval '5 minutes', now()-interval '25 minutes', timestamptz '2000-01-01 00:00:00+00'),
        date_bin(interval '5 minutes', now(), timestamptz '2000-01-01 00:00:00+00'),
        interval '5 minutes') AS window_start
), counts AS (
    SELECT date_bin(interval '5 minutes', event_time, timestamptz '2000-01-01 00:00:00+00') AS window_start,
           count(*)::bigint AS camera_event_count
    FROM camera_events
    WHERE ingestion_mode='stream' AND event_time >= now()-interval '30 minutes'
    GROUP BY 1
)
SELECT bins.window_start, coalesce(counts.camera_event_count,0)::bigint AS camera_event_count
FROM bins LEFT JOIN counts USING (window_start);

CREATE OR REPLACE VIEW v_live_vehicles_by_zone_30m AS
SELECT z.zone_name, count(DISTINCT p.vehicle_id)::bigint AS unique_vehicles,
       count(*)::bigint AS gps_position_records
FROM vehicle_positions p JOIN geo_zones z ON z.zone_id=p.zone_id
WHERE p.ingestion_mode='stream' AND p.event_time >= now()-interval '30 minutes'
GROUP BY z.zone_name;

CREATE OR REPLACE VIEW v_live_camera_by_zone_30m AS
SELECT z.zone_name, count(*)::bigint AS camera_events
FROM camera_events e JOIN geo_zones z ON z.zone_id=e.zone_id
WHERE e.ingestion_mode='stream' AND e.event_time >= now()-interval '30 minutes'
GROUP BY z.zone_name;

CREATE OR REPLACE VIEW v_ml_report_metrics AS
SELECT m.model_version,
       'Random Forest classifier'::text AS model_type,
       m.trained_at,
       coalesce((j.metrics->'split'->>'train_size')::integer,(j.metrics->>'train_size')::integer) AS train_size,
       coalesce((j.metrics->'split'->>'validation_size')::integer,(j.metrics->>'validation_size')::integer) AS validation_size,
       coalesce((j.metrics->'split'->>'test_size')::integer,(j.metrics->>'test_size')::integer) AS test_size,
       coalesce((j.metrics->'classification'->>'positive_class_rate')::double precision,
                (j.metrics->'test'->>'positive_rate')::double precision) AS positive_class_rate,
       coalesce((j.metrics->'classification'->'test'->>'accuracy')::double precision,
                (j.metrics->'test'->>'accuracy')::double precision) AS accuracy,
       coalesce((j.metrics->'classification'->'test'->>'precision')::double precision,
                (j.metrics->'test'->>'precision')::double precision) AS precision,
       coalesce((j.metrics->'classification'->'test'->>'recall')::double precision,
                (j.metrics->'test'->>'recall')::double precision) AS recall,
       coalesce((j.metrics->'classification'->'test'->>'f1')::double precision,
                (j.metrics->'test'->>'f1')::double precision) AS f1,
       coalesce((j.metrics->'classification'->>'threshold')::double precision,
                (j.metrics->>'decision_threshold')::double precision) AS decision_threshold,
       coalesce((j.metrics->'classification'->'majority_baseline'->>'accuracy')::double precision,
                (j.metrics->'majority_baseline'->>'accuracy')::double precision) AS baseline_accuracy,
       coalesce((j.metrics->'classification'->'majority_baseline'->>'precision')::double precision,
                (j.metrics->'majority_baseline'->>'precision')::double precision) AS baseline_precision,
       coalesce((j.metrics->'classification'->'majority_baseline'->>'recall')::double precision,
                (j.metrics->'majority_baseline'->>'recall')::double precision) AS baseline_recall,
       coalesce((j.metrics->'classification'->'majority_baseline'->>'f1')::double precision,
                (j.metrics->'majority_baseline'->>'f1')::double precision) AS baseline_f1,
       coalesce(j.metrics->>'label_definition','next-hour average speed <43 km/h and >=5 GPS samples') AS label_definition,
       f.latest_forecast_target,
       CASE WHEN f.latest_forecast_target < now() THEN 'Stale — historical batch forecast'
            ELSE 'Upcoming — historical batch forecast' END AS forecast_freshness
FROM ml_model_metadata m
CROSS JOIN LATERAL (SELECT m.metrics_json::jsonb AS metrics) j
CROSS JOIN LATERAL (
    SELECT max(target_hour) AS latest_forecast_target
    FROM ml_congestion_predictions WHERE forecast_kind='forecast'
) f
WHERE j.metrics ? 'classification'
  AND m.model_version=(SELECT model_version FROM ml_model_metadata ORDER BY trained_at DESC LIMIT 1);

CREATE OR REPLACE VIEW v_ml_model_summary AS
SELECT * FROM v_ml_report_metrics;

CREATE OR REPLACE VIEW v_ml_confusion_matrix AS
SELECT CASE WHEN actual_congestion THEN 'Congested' ELSE 'Not congested' END AS actual_label,
       count(*) FILTER (WHERE NOT predicted_congestion)::bigint AS predicted_not_congested,
       count(*) FILTER (WHERE predicted_congestion)::bigint AS predicted_congested,
       count(*)::bigint AS row_total
FROM ml_congestion_predictions
WHERE forecast_kind='holdout' AND actual_congestion IS NOT NULL
GROUP BY actual_congestion
ORDER BY actual_congestion;

-- Live ML outputs are kept separate from historical holdout/backtest rows.
CREATE OR REPLACE VIEW v_ml_live_latest AS
SELECT DISTINCT ON (p.zone_id) p.zone_id,p.prediction_time,p.feature_window_start,p.feature_window_end,
       p.target_start,p.target_end,p.congestion_probability,p.predicted_congested,
       p.current_avg_speed_kph,p.predicted_avg_speed_kph,p.predicted_speed_change_pct,
       p.classifier_version,p.regressor_version,p.prediction_status,p.gps_record_count,
       p.camera_event_count,z.zone_name
FROM ml_live_predictions p JOIN geo_zones z USING (zone_id)
WHERE p.prediction_status='ok' AND p.prediction_time >= now()-interval '2 minutes'
ORDER BY p.zone_id,p.prediction_time DESC;

CREATE OR REPLACE VIEW v_ml_live_latest_status AS
SELECT DISTINCT ON (p.zone_id) p.zone_id,p.prediction_time,p.feature_window_start,p.feature_window_end,
       p.target_start,p.target_end,p.congestion_probability,p.predicted_congested,
       p.current_avg_speed_kph,p.predicted_avg_speed_kph,p.predicted_speed_change_pct,
       p.classifier_version,p.regressor_version,
       CASE WHEN p.prediction_time < now()-interval '2 minutes' THEN 'stale' ELSE p.prediction_status END AS prediction_status,
       p.gps_record_count,
       p.camera_event_count,z.zone_name
FROM ml_live_predictions p JOIN geo_zones z USING (zone_id)
ORDER BY p.zone_id,p.prediction_time DESC;

CREATE OR REPLACE VIEW v_ml_live_history AS
SELECT p.zone_id,p.prediction_time,p.feature_window_start,p.feature_window_end,p.target_start,p.target_end,
       p.congestion_probability,p.predicted_congested,p.current_avg_speed_kph,p.predicted_avg_speed_kph,
       p.predicted_speed_change_pct,p.classifier_version,p.regressor_version,p.prediction_status,
       p.gps_record_count,p.camera_event_count,z.zone_name
FROM ml_live_predictions p JOIN geo_zones z USING(zone_id)
WHERE p.prediction_status='ok' AND p.prediction_time >= now()-interval '24 hours';

CREATE OR REPLACE VIEW v_ml_regression_metrics AS
SELECT model_version, trained_at,
       (j.metrics->'split'->>'train_size')::int AS train_size,
       (j.metrics->'split'->>'validation_size')::int AS validation_size,
       (j.metrics->'split'->>'test_size')::int AS test_size,
       (j.metrics->'regression'->'test'->>'mae')::double precision AS mae,
       (j.metrics->'regression'->'test'->>'rmse')::double precision AS rmse,
       (j.metrics->'regression'->'test'->>'r2')::double precision AS r2,
       (j.metrics->'regression'->'persistence_baseline'->>'mae')::double precision AS baseline_mae,
       (j.metrics->'regression'->'persistence_baseline'->>'rmse')::double precision AS baseline_rmse,
       (j.metrics->'classification'->>'positive_class_rate')::double precision AS positive_class_rate,
       (j.metrics->'classification'->>'threshold')::double precision AS decision_threshold,
       j.metrics->>'label_definition' AS label_definition,
       (j.metrics->>'feature_window_minutes')::int AS feature_window_minutes,
       (j.metrics->>'prediction_horizon_minutes')::int AS prediction_horizon_minutes
FROM ml_model_metadata m CROSS JOIN LATERAL (SELECT m.metrics_json::jsonb AS metrics) j
WHERE j.metrics ? 'regression'
  AND m.model_version=(SELECT model_version FROM ml_model_metadata ORDER BY trained_at DESC LIMIT 1);

CREATE OR REPLACE VIEW v_ml_regression_holdout AS
SELECT h.*,z.zone_name FROM ml_regression_holdout h JOIN geo_zones z USING(zone_id);

