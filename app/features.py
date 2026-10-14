"""Shared UTC 30-minute feature window for historical training and live inference."""
from __future__ import annotations

FEATURE_NAMES = (
    "zone_id", "local_hour", "local_weekday", "is_weekend", "is_rush_hour",
    "gps_count", "vehicle_count", "avg_speed_kph", "min_speed_kph",
    "max_speed_kph", "speed_stddev_kph", "slow_fraction", "camera_count",
    "traffic_flow_count", "stopped_vehicle_count", "lane_congestion_count",
    "gps_source_available", "camera_source_available",
)

FEATURE_SQL = """
SELECT z.zone_id, z.zone_name, %s::timestamptz AS window_end,
       %s::timestamptz - interval '30 minutes' AS window_start,
       extract(hour FROM (%s::timestamptz AT TIME ZONE 'Asia/Dubai'))::int AS local_hour,
       extract(isodow FROM (%s::timestamptz AT TIME ZONE 'Asia/Dubai'))::int AS local_weekday,
       CASE WHEN extract(isodow FROM (%s::timestamptz AT TIME ZONE 'Asia/Dubai')) IN (6,7) THEN 1 ELSE 0 END AS is_weekend,
       CASE WHEN extract(hour FROM (%s::timestamptz AT TIME ZONE 'Asia/Dubai')) BETWEEN 7 AND 9
                  OR extract(hour FROM (%s::timestamptz AT TIME ZONE 'Asia/Dubai')) BETWEEN 16 AND 19 THEN 1 ELSE 0 END AS is_rush_hour,
       coalesce(g.gps_count,0)::int AS gps_count, coalesce(g.vehicle_count,0)::int AS vehicle_count,
       g.avg_speed_kph, g.min_speed_kph, g.max_speed_kph, coalesce(g.speed_stddev_kph,0)::double precision AS speed_stddev_kph,
       g.window_head_event_time,
       g.slow_fraction, coalesce(c.camera_count,0)::int AS camera_count,
       coalesce(c.traffic_flow_count,0)::int AS traffic_flow_count,
       coalesce(c.stopped_vehicle_count,0)::int AS stopped_vehicle_count,
       coalesce(c.lane_congestion_count,0)::int AS lane_congestion_count,
       (g.gps_count IS NOT NULL)::int AS gps_source_available,
       EXISTS(SELECT 1 FROM camera_events ce WHERE ce.ingestion_mode=%s
              AND ce.event_time > %s::timestamptz - interval '30 minutes'
              AND ce.event_time <= %s::timestamptz)::int AS camera_source_available
FROM geo_zones z
LEFT JOIN LATERAL (
  SELECT count(*)::int gps_count, count(DISTINCT vehicle_id)::int vehicle_count,
         avg(speed_kph)::double precision avg_speed_kph, min(speed_kph)::double precision min_speed_kph,
         max(speed_kph)::double precision max_speed_kph, stddev_samp(speed_kph)::double precision speed_stddev_kph,
         min(event_time) FILTER (WHERE event_time <= %s::timestamptz - interval '25 minutes') AS window_head_event_time,
         avg((speed_kph < 30)::int)::double precision slow_fraction
  FROM vehicle_positions p WHERE p.zone_id=z.zone_id AND p.ingestion_mode=%s
    AND p.event_time > %s::timestamptz - interval '30 minutes' AND p.event_time <= %s::timestamptz
) g ON true
LEFT JOIN LATERAL (
  SELECT count(*)::int camera_count,
         count(*) FILTER (WHERE event_category='traffic_flow')::int traffic_flow_count,
         count(*) FILTER (WHERE event_category='stopped_vehicle')::int stopped_vehicle_count,
         count(*) FILTER (WHERE event_category='lane_congestion')::int lane_congestion_count
  FROM camera_events e WHERE e.zone_id=z.zone_id AND e.ingestion_mode=%s
    AND e.event_time > %s::timestamptz - interval '30 minutes' AND e.event_time <= %s::timestamptz
) c ON true
ORDER BY z.zone_id
"""


def feature_values(row: dict) -> list[float]:
    """Exact ordered model vector; missing GPS statistics use explicit neutral values."""
    return [float(row.get(name) if row.get(name) is not None else 0.0) for name in FEATURE_NAMES]
