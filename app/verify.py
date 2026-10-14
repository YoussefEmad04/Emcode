from __future__ import annotations

import json
import sys
import time

from app.db import connect_with_retry
from app.seed import BATCH_CAMERA_ROWS, BATCH_VEHICLE_ROWS, seed_batch
from app.superset_api import SupersetAPI


class Verification:
    def __init__(self):
        self.failures: list[str] = []

    def check(self, name: str, condition: bool, detail: str) -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name}: {detail}", flush=True)
        if not condition:
            self.failures.append(f"{name}: {detail}")

    def guarded(self, name: str, callback) -> None:
        try:
            callback()
        except Exception as exc:
            self.check(name, False, f"{type(exc).__name__}: {exc}")


def verify_database(result: Verification) -> None:
    connection = connect_with_retry()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT PostGIS_Version()")
            version = cursor.fetchone()[0]
            result.check("PostGIS extension", bool(version), version)

            cursor.execute("SELECT count(*), min(ST_SRID(geom)), max(ST_SRID(geom)) FROM geo_zones")
            zone_count, min_srid, max_srid = cursor.fetchone()
            result.check(
                "Five SRID 4326 zones",
                zone_count == 5 and min_srid == 4326 and max_srid == 4326,
                f"zones={zone_count}, srid_range={min_srid}..{max_srid}",
            )

            cursor.execute(
                """SELECT count(*) FROM geo_zones a JOIN geo_zones b
                   ON a.zone_id < b.zone_id AND ST_Intersects(a.geom, b.geom)"""
            )
            overlap_count = cursor.fetchone()[0]
            result.check("Zones do not overlap", overlap_count == 0, f"overlapping_pairs={overlap_count}")

            cursor.execute(
                """SELECT
                     count(*) FILTER (WHERE ingestion_mode='batch'),
                     count(*) FILTER (WHERE ingestion_mode='stream')
                   FROM vehicle_positions"""
            )
            batch_vehicles, stream_vehicles = cursor.fetchone()
            cursor.execute(
                """SELECT
                     count(*) FILTER (WHERE ingestion_mode='batch'),
                     count(*) FILTER (WHERE ingestion_mode='stream')
                   FROM camera_events"""
            )
            batch_cameras, stream_cameras = cursor.fetchone()
            cursor.execute("SELECT count(*) FROM ml_congestion_predictions WHERE forecast_kind = 'forecast'")
            result.check("Synthetic ML forecast zones", cursor.fetchone()[0] == 5, "Five zone predictions")
            cursor.execute("SELECT metrics_json FROM ml_model_metadata ORDER BY trained_at DESC LIMIT 1")
            row = cursor.fetchone()
            result.check("Chronological holdout metrics", bool(row and row[0]), "Stored model diagnostics")
            result.check(
                "Batch row counts",
                batch_vehicles >= BATCH_VEHICLE_ROWS and batch_cameras >= BATCH_CAMERA_ROWS,
                f"vehicles={batch_vehicles}, cameras={batch_cameras}",
            )
            cursor.execute(
                """SELECT
                     (SELECT count(DISTINCT vehicle_id) FROM v_batch_vehicle_activity),
                     (SELECT count(DISTINCT camera_id) FROM v_batch_camera_activity),
                     (SELECT count(DISTINCT zone_id) FROM v_batch_vehicle_activity),
                     (SELECT sum(gps_record_count) FROM v_batch_daily_zone_activity),
                     (SELECT sum(camera_event_count) FROM v_batch_daily_zone_activity),
                     (SELECT sum(gps_record_count) FROM v_batch_spatial_activity)"""
            )
            distinct_vehicles, distinct_cameras, represented_zones, daily_gps, daily_cameras, map_gps = cursor.fetchone()
            result.check(
                "Historical KPI and aggregate view correctness",
                distinct_vehicles == 50
                and distinct_cameras == 10
                and represented_zones == 5
                and daily_gps == batch_vehicles
                and daily_cameras == batch_cameras
                and map_gps == batch_vehicles,
                f"vehicles={distinct_vehicles}, cameras={distinct_cameras}, zones={represented_zones}, "
                f"daily_gps={daily_gps}/{batch_vehicles}, daily_cameras={daily_cameras}/{batch_cameras}, "
                f"map_gps={map_gps}/{batch_vehicles}",
            )
            cursor.execute(
                """SELECT
                     (SELECT array_agg(DISTINCT ingestion_mode) FROM v_batch_vehicle_activity),
                     (SELECT array_agg(DISTINCT ingestion_mode) FROM v_batch_camera_activity)"""
            )
            gps_modes, camera_modes = cursor.fetchone()
            result.check(
                "Historical views exclude live inserts",
                gps_modes == ["batch"] and camera_modes == ["batch"],
                f"gps_modes={gps_modes}, camera_modes={camera_modes}",
            )
            cursor.execute(
                """WITH daily AS (
                       SELECT event_time, sum(gps_record_count) AS records
                       FROM v_batch_daily_zone_activity GROUP BY event_time
                   ), zonal AS (
                       SELECT zone_id, sum(gps_record_count) AS records
                       FROM v_batch_daily_zone_activity GROUP BY zone_id
                   )
                   SELECT
                       (SELECT min(records) FROM daily),
                       (SELECT max(records) FROM daily),
                       (SELECT min(records) FROM zonal),
                       (SELECT max(records) FROM zonal)"""
            )
            daily_min, daily_max, zone_min, zone_max = cursor.fetchone()
            result.check(
                "Synthetic daily and spatial variation",
                daily_max >= daily_min * 2 and zone_max >= zone_min * 2,
                f"daily_range={daily_min}..{daily_max}, zone_range={zone_min}..{zone_max}",
            )
            result.check(
                "Stream rows exist",
                stream_vehicles > 0 and stream_cameras > 0,
                f"vehicles={stream_vehicles}, cameras={stream_cameras}",
            )

            before = (batch_vehicles, batch_cameras)
            inserted = seed_batch(connection)
            cursor.execute("SELECT count(*) FROM vehicle_positions WHERE ingestion_mode='batch'")
            after_vehicles = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM camera_events WHERE ingestion_mode='batch'")
            after_cameras = cursor.fetchone()[0]
            result.check(
                "Batch seed idempotence",
                inserted == (0, 0) and before == (after_vehicles, after_cameras),
                f"inserted_on_rerun={inserted}, before={before}, after={(after_vehicles, after_cameras)}",
            )

            cursor.execute(
                """SELECT
                     (SELECT count(*) FROM vehicle_positions p
                       JOIN geo_zones z ON z.zone_id=p.zone_id
                       WHERE NOT ST_Contains(z.geom,p.geom)),
                     (SELECT count(*) FROM camera_events e
                       JOIN geo_zones z ON z.zone_id=e.zone_id
                       WHERE NOT ST_Contains(z.geom,e.geom))"""
            )
            outside_vehicles, outside_cameras = cursor.fetchone()
            result.check(
                "Points inside intended zones",
                outside_vehicles == 0 and outside_cameras == 0,
                f"outside_vehicle={outside_vehicles}, outside_camera={outside_cameras}",
            )

            cursor.execute(
                """SELECT count(*) FROM v_zone_activity_5m
                   WHERE vehicle_position_count > 0 AND camera_event_count > 0"""
            )
            common_bins = cursor.fetchone()[0]
            cursor.execute(
                """SELECT
                     sum(vehicle_position_count),
                     sum(camera_event_count),
                     (SELECT count(*) FROM vehicle_positions),
                     (SELECT count(*) FROM camera_events)
                   FROM v_zone_activity_5m"""
            )
            view_vehicles, view_cameras, raw_vehicles, raw_cameras = cursor.fetchone()
            result.check(
                "Independent zone/time correlation",
                common_bins > 0
                and view_vehicles == raw_vehicles
                and view_cameras == raw_cameras,
                f"common_bins={common_bins}, vehicle_sum={view_vehicles}/{raw_vehicles}, "
                f"camera_sum={view_cameras}/{raw_cameras}",
            )

            cursor.execute("SELECT count(*) FROM vehicle_positions WHERE ingestion_mode='stream'")
            stream_before = cursor.fetchone()[0]
        print("Waiting 10 seconds to confirm continuous insertion...", flush=True)
        time.sleep(10)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM vehicle_positions WHERE ingestion_mode='stream'")
            stream_after = cursor.fetchone()[0]
        result.check(
            "Stream count increases",
            stream_after > stream_before,
            f"before={stream_before}, after={stream_after}, delta={stream_after-stream_before}",
        )
    finally:
        connection.close()


def verify_superset(result: Verification) -> None:
    api = SupersetAPI()
    api.login()
    result.check("Superset admin login", True, f"authenticated at {api.base_url}")

    database = api.find_one("database", "database_name", "EMCODE PostGIS")
    result.check(
        "Superset database connection exists",
        bool(database),
        f"database_id={database.get('id') if database else None}",
    )
    if not database:
        return

    # This synchronous SQL Lab request crosses the actual Superset SQLAlchemy connection.
    _, sql_response = api.request(
        "POST",
        "/api/v1/sqllab/execute/",
        {
            "database_id": database["id"],
            "sql": "SELECT COUNT(*) AS vehicle_count FROM vehicle_positions",
            "schema": "public",
            "runAsync": False,
            "expand_data": True,
        },
    )
    sql_data = sql_response.get("data") or []
    sql_count = sql_data[0].get("vehicle_count") if sql_data else None
    result.check(
        "Superset executes analytical SQL",
        isinstance(sql_count, int) and sql_count >= BATCH_VEHICLE_ROWS,
        f"SELECT COUNT(*) returned {sql_count}",
    )

    expected_datasets = {
        "v_batch_hourly_zone_activity",
        "v_ml_congestion_forecast",
        "v_ml_congestion_holdout",
        "v_batch_vehicle_activity",
        "v_batch_camera_activity",
        "v_batch_daily_zone_activity",
        "v_batch_spatial_activity",
        "v_live_vehicle_latest",
        "v_live_events_recent",
        "v_zone_activity_5m",
    }
    actual_datasets = {row.get("table_name") for row in api.list_all("dataset")}
    result.check(
        "Expected Superset datasets",
        expected_datasets <= actual_datasets,
        f"found={sorted(expected_datasets & actual_datasets)}",
    )

    expected_dashboards = {"EMCODE | Historical Batch", "EMCODE | Live Monitoring", "EMCODE | Congestion Diagnostics"}
    dashboards = api.list_all("dashboard")
    dashboard_titles = {row.get("dashboard_title") for row in dashboards}
    result.check(
        "Two dashboards provisioned",
        expected_dashboards <= dashboard_titles,
        f"found={sorted(expected_dashboards & dashboard_titles)}",
    )

    expected_historical = {
        "Total GPS Records",
        "Distinct Vehicles",
        "Total Camera Events",
        "Monitored Zones",
        "Daily GPS Records",
        "GPS Records by Zone",
        "Historical GPS Activity Map",
        "GPS vs Camera Records by Zone",
    }
    expected_live = {
        "Live | Latest Vehicle Locations",
        "Live | Camera Events Last 15 Minutes",
        "Live | Camera Event Timeline",
        "Live | Camera Events by Zone",
        "Live | Zone Vehicle and Camera Activity",
    }
    expected_ml = {"Next-hour Congestion Risk by Zone", "Hourly Average Speed by Zone", "Test-set Predictions vs Proxy Labels by Zone"}
    expected_charts = expected_historical | expected_live | expected_ml
    charts = api.list_all("chart")
    emcode_charts = [chart for chart in charts if chart.get("slice_name") in expected_charts]
    result.check(
        "Expected dashboard charts provisioned",
        {chart.get("slice_name") for chart in emcode_charts} == expected_charts,
        f"found={len(emcode_charts)}/{len(expected_charts)}",
    )

    historical = next(
        (row for row in dashboards if row.get("dashboard_title") == "EMCODE | Historical Batch"),
        None,
    )
    if historical:
        _, dashboard_response = api.request("GET", f"/api/v1/dashboard/{historical['id']}")
        dashboard_detail = dashboard_response.get("result", dashboard_response)
        metadata = json.loads(dashboard_detail.get("json_metadata") or "{}")
        position = json.loads(dashboard_detail.get("position_json") or "{}")
        filter_names = {
            item.get("name") for item in metadata.get("native_filter_configuration", [])
        }
        markdown = str(position.get("MARKDOWN-SYNTHETIC", {}).get("meta", {}).get("code", ""))
        positioned_chart_ids = {
            item.get("meta", {}).get("chartId")
            for item in position.values()
            if isinstance(item, dict) and item.get("type") == "CHART"
        }
        historical_chart_ids = {
            chart["id"] for chart in emcode_charts if chart.get("slice_name") in expected_historical
        }
        result.check(
            "Historical dashboard layout and synthetic labeling",
            positioned_chart_ids == historical_chart_ids
            and "SYNTHETIC DEMO DATA" in markdown
            and "position observations, not vehicles" in markdown,
            f"charts={len(positioned_chart_ids)}, synthetic_label={'SYNTHETIC DEMO DATA' in markdown}",
        )
        result.check(
            "Historical dashboard native filters",
            filter_names == {"Date range", "Zone"},
            f"filters={sorted(name for name in filter_names if name)}",
        )

    executed = 0
    map_charts = 0
    kpi_values: dict[str, int] = {}
    for chart in emcode_charts:
        chart_id = chart["id"]
        _, detail = api.request("GET", f"/api/v1/chart/{chart_id}")
        chart_detail = detail.get("result", detail)
        params = json.loads(chart_detail.get("params") or "{}")
        if chart_detail.get("viz_type") == "deck_scatter":
            map_charts += 1
            spatial = params.get("spatial", {})
            result.check(
                f"Map coordinate binding #{chart_id}",
                spatial.get("latCol") == "latitude" and spatial.get("lonCol") == "longitude",
                f"spatial={spatial}",
            )
            if chart.get("slice_name") == "Historical GPS Activity Map":
                radius = params.get("point_radius_fixed", {})
                result.check(
                    "Historical map uses aggregated activity clusters",
                    radius.get("type") == "metric"
                    and params.get("dimension") == "zone_name"
                    and "vehicle_id" not in json.dumps(params),
                    f"radius_type={radius.get('type')}, dimension={params.get('dimension')}",
                )
        if chart.get("slice_name") == "GPS Records by Zone":
            result.check(
                "Zone chart is a sorted horizontal bar",
                chart_detail.get("viz_type") == "echarts_timeseries_bar"
                and params.get("orientation") == "horizontal"
                and params.get("x_axis_sort") == "GPS Records"
                and params.get("x_axis_sort_asc") is False,
                f"viz={chart_detail.get('viz_type')}, orientation={params.get('orientation')}, "
                f"sort={params.get('x_axis_sort')}, ascending={params.get('x_axis_sort_asc')}",
            )
        if chart.get("slice_name") == "GPS vs Camera Records by Zone":
            labels = {item.get("label") for item in params.get("metrics", [])}
            result.check(
                "Cross-source chart has separately labeled counts",
                labels == {"GPS Records", "Camera Events"},
                f"metrics={sorted(label for label in labels if label)}",
            )
        _, data_response = api.request("GET", f"/api/v1/chart/{chart_id}/data/")
        if data_response is not None:
            executed += 1
            title = chart.get("slice_name")
            if title in {"Total GPS Records", "Distinct Vehicles", "Total Camera Events", "Monitored Zones"}:
                data = data_response.get("result", [{}])[0].get("data", [])
                if data:
                    kpi_values[title] = data[0].get(title)
    result.check(
        "Historical KPI values through Superset",
        kpi_values
        == {
            "Total GPS Records": 20_000,
            "Distinct Vehicles": 50,
            "Total Camera Events": 2_000,
            "Monitored Zones": 5,
        },
        f"values={kpi_values}",
    )
    result.check(
        "All saved chart queries execute",
        executed == len(expected_charts),
        f"executed={executed}/{len(expected_charts)}",
    )
    result.check("Geospatial charts use stored coordinates", map_charts == 2, f"map_charts={map_charts}")


def main() -> None:
    result = Verification()
    result.guarded("Database verification", lambda: verify_database(result))
    result.guarded("Superset verification", lambda: verify_superset(result))
    print("\n=== EMCODE Phase 1 verification summary ===", flush=True)
    if result.failures:
        print(f"FAIL: {len(result.failures)} check(s) failed", flush=True)
        for failure in result.failures:
            print(f" - {failure}", flush=True)
        raise SystemExit(1)
    print("PASS: all end-to-end checks succeeded", flush=True)


if __name__ == "__main__":
    main()

