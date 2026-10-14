from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone

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
            metrics = row[0] if row else None
            if isinstance(metrics, str):
                metrics = json.loads(metrics)
            classification = metrics.get("classification", {}) if isinstance(metrics, dict) else {}
            test_metrics = classification.get("test", {})
            confusion = test_metrics.get("confusion_matrix", [])
            confusion_valid = (
                len(confusion) == 2
                and all(len(line) == 2 for line in confusion)
                and sum(sum(line) for line in confusion) == metrics.get("split", {}).get("test_size")
            )
            result.check(
                "Chronological holdout metrics and class balance",
                bool(metrics)
                and metrics.get("split", {}).get("train_size", 0) > 0
                and metrics.get("split", {}).get("validation_size", 0) > 0
                and 0 < classification.get("positive_class_rate", 0) < 1
                and classification.get("positive_support", 0) > 0
                and all(key in test_metrics for key in ("accuracy", "balanced_accuracy", "precision", "recall", "f1"))
                and 0 < classification.get("threshold", 0) < 1
                and "majority_baseline" in classification
                and confusion_valid,
                f"split={metrics.get('split') if metrics else None}, "
                f"test_positive_rate={classification.get('positive_class_rate')}, "
                f"test_confusion_matrix={confusion}, threshold={classification.get('threshold')}, "
                f"baseline={classification.get('majority_baseline')}",
            )
            regression = metrics.get("regression", {}) if isinstance(metrics, dict) else {}
            regression_test = regression.get("test", {})
            regression_base = regression.get("persistence_baseline", {})
            result.check("Random Forest regressor metrics and persistence baseline",
                         all(math.isfinite(regression_test.get(k, float("nan"))) for k in ("mae", "rmse", "r2"))
                         and regression_test.get("test_size") == metrics.get("split", {}).get("test_size")
                         and all(k in regression_base for k in ("mae", "rmse", "r2")),
                         f"test={regression_test}, persistence={regression_base}")
            artifact_path = __import__("pathlib").Path(__import__("os").getenv("MODEL_DIR", "/models")) / f"{metrics.get('model_version')}.pkl"
            result.check("Persisted classifier and regressor artifact exists", artifact_path.exists(), str(artifact_path))
            cursor.execute(
                """SELECT count(*) FILTER (WHERE actual_congestion),
                          count(*) FILTER (WHERE NOT actual_congestion),
                          count(*) FILTER (WHERE predicted_congestion),
                          count(*) FILTER (WHERE NOT predicted_congestion)
                   FROM ml_congestion_predictions WHERE forecast_kind='holdout'"""
            )
            positive_actual, negative_actual, positive_predicted, negative_predicted = cursor.fetchone()
            result.check(
                "Persisted holdout predictions include both classes",
                positive_actual > 0 and negative_actual > 0
                and positive_predicted > 0 and negative_predicted > 0,
                f"actual_positive={positive_actual}, actual_negative={negative_actual}, "
                f"predicted_positive={positive_predicted}, predicted_negative={negative_predicted}",
            )
            cursor.execute("SELECT sum(predicted_not_congested), sum(predicted_congested), sum(row_total) FROM v_ml_confusion_matrix")
            cm_not, cm_yes, cm_total = cursor.fetchone()
            result.check("Stored confusion view matches held-out counts",
                         cm_not == confusion[0][0] + confusion[1][0]
                         and cm_yes == confusion[0][1] + confusion[1][1]
                         and cm_total == metrics.get("split", {}).get("test_size"),
                         f"predicted_negative={cm_not}, predicted_positive={cm_yes}, total={cm_total}")
            result.check(
                "Batch row counts",
                batch_vehicles == BATCH_VEHICLE_ROWS and batch_cameras == BATCH_CAMERA_ROWS,
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


def verify_live_ml_cycles(result: Verification) -> None:
    """Wait for two complete post-start inference cycles, not stale table contents."""
    connection = connect_with_retry()
    started = datetime.now(timezone.utc)
    deadline = time.monotonic() + 2400
    try:
        successful_times = []
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT prediction_time FROM ml_inference_runs
                                 WHERE prediction_time>%s AND status='ok' AND successful_zone_count=5
                                 ORDER BY prediction_time""", (started,))
                successful_times = [row[0] for row in cursor.fetchall()]
            connection.commit()
            if len(successful_times) >= 2:
                break
            time.sleep(5)
        result.check("Two new successful live inference cycles", len(successful_times) >= 2,
                     f"cycles={successful_times}; waited_from={started.isoformat()}")
        if len(successful_times) < 2:
            return
        cadence_seconds = (successful_times[1] - successful_times[0]).total_seconds()
        result.check("Live inference cadence is approximately 60 seconds",
                     45 <= cadence_seconds <= 75, f"seconds_between_cycles={cadence_seconds}")
        with connection.cursor() as cursor:
            cursor.execute("""SELECT count(*), count(DISTINCT zone_id),
                       bool_and(congestion_probability BETWEEN 0 AND 1),
                       bool_and(predicted_avg_speed_kph >= 0 AND predicted_avg_speed_kph < 'Infinity'::float8),
                       bool_and(feature_window_end=prediction_time),
                       bool_and(feature_window_start=prediction_time-interval '30 minutes'),
                       bool_and(target_start=prediction_time AND target_end=prediction_time+interval '1 hour'),
                       count(DISTINCT classifier_version), count(DISTINCT regressor_version)
                       FROM ml_live_predictions WHERE prediction_time=ANY(%s) AND prediction_status='ok'""",
                       (successful_times[:2],))
            row = cursor.fetchone()
        result.check("Live prediction records cover five zones with valid windows/models",
                     row[0] == 10 and row[1] == 5 and all(row[2:7]) and row[7] == 1 and row[8] == 1,
                     f"rows/zones={row[0]}/{row[1]}, valid_flags={row[2:7]}, versions={row[7:9]}")
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM v_ml_live_latest_status WHERE prediction_status='insufficient_data'")
            insufficient = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM v_ml_live_latest")
            latest_valid = cursor.fetchone()[0]
        result.check("Live status and valid-prediction views are explicit",
                     insufficient == 0 and latest_valid == 5,
                     f"insufficient_latest={insufficient}, latest_valid_zones={latest_valid}")
    finally:
        connection.close()


def verify_superset(result: Verification) -> None:
    api = SupersetAPI()
    api.login()
    result.check("Superset admin login", True, f"authenticated at {api.base_url}")
    database = api.find_one("database", "database_name", "EMCODE PostGIS")
    result.check("Superset database connection exists", bool(database), f"database_id={database.get('id') if database else None}")
    if not database:
        return
    _, sql_response = api.request("POST", "/api/v1/sqllab/execute/", {
        "database_id": database["id"], "sql": "SELECT COUNT(*) AS vehicle_count FROM vehicle_positions",
        "schema": "public", "runAsync": False, "expand_data": True,
    })
    sql_data = sql_response.get("data") or []
    sql_count = sql_data[0].get("vehicle_count") if sql_data else None
    result.check("Superset executes analytical SQL", isinstance(sql_count, int) and sql_count >= BATCH_VEHICLE_ROWS,
                 f"SELECT COUNT(*) returned {sql_count}")

    expected_datasets = {
        "v_batch_vehicle_activity", "v_batch_camera_activity", "v_batch_daily_zone_activity", "v_batch_spatial_activity",
        "v_batch_hourly_zone_activity", "v_batch_hourly_activity", "v_batch_camera_rate_by_zone",
        "v_live_vehicle_latest", "v_live_events_recent", "v_live_kpis", "v_live_gps_5m", "v_live_camera_5m",
        "v_live_vehicles_by_zone_30m", "v_live_camera_by_zone_30m", "v_zone_activity_5m",
        "v_ml_congestion_forecast", "v_ml_congestion_holdout", "v_ml_report_metrics", "v_ml_model_summary", "v_ml_confusion_matrix",
        "v_ml_live_latest", "v_ml_live_latest_status", "v_ml_live_history", "v_ml_regression_metrics", "v_ml_regression_holdout",
    }
    actual_datasets = {row.get("table_name") for row in api.list_all("dataset")}
    result.check("Expected Superset datasets", expected_datasets <= actual_datasets,
                 f"found={len(expected_datasets & actual_datasets)}/{len(expected_datasets)}")

    expected_historical = {
        "Total GPS Records", "Unique Vehicles", "Total Camera Events", "Monitored Zones",
        "Daily GPS Records", "GPS Records by Zone", "Historical GPS Activity Map",
        "Camera Events per 1,000 GPS Records by Zone", "Hourly GPS Activity", "Camera Events by Type",
        "GPS and Camera Detailed Zone Report",
    }
    expected_live = {
        "Live | Recently Seen Vehicles", "Live | Camera Events — Last 15 Minutes", "Live | GPS Records — Last 15 Minutes",
        "Live | Latest Event Age (seconds)", "Live | Latest Vehicle Locations", "Live | GPS Position Events Over Time",
        "Live | Camera Events Over Time", "Live | Vehicle Activity by Zone", "Live | Camera Events by Type",
        "Live | Camera Events by Zone — Last 30 Minutes",
        "ML | Live Congestion Risk by Zone", "ML | Current vs Predicted Speed (km/h)",
        "ML | Live Prediction History", "ML | Latest Prediction Status by Zone",
    }
    expected_ml = {
        "Next-hour Congestion Risk by Zone", "Average Speed by Hour of Day", "ML | Accuracy", "ML | Precision",
        "ML | Recall", "ML | F1-score", "ML | Test-set Confusion Matrix", "ML | Model Evaluation Summary",
        "ML | MAE (km/h)", "ML | RMSE (km/h)", "ML | R²", "ML | Actual vs Predicted Speed by Zone",
        "ML | Regression Baseline and Model Summary",
    }
    expected = expected_historical | expected_live | expected_ml
    charts = api.list_all("chart")
    provisioned = [chart for chart in charts if chart.get("slice_name") in expected]
    result.check("All expected dashboards/charts are provisioned", {c.get("slice_name") for c in provisioned} == expected,
                 f"found={len(provisioned)}/{len(expected)}")
    obsolete = {"GPS vs Camera Records by Zone", "Zone Activity Breakdown", "GPS and Camera Source Counts",
                "Live | Zone Vehicle and Camera Activity",
                "Test-set Predictions vs Proxy Labels by Zone"}
    result.check("Obsolete noisy charts removed", not (obsolete & {c.get("slice_name") for c in charts}),
                 f"remaining={obsolete & {c.get('slice_name') for c in charts}}")

    dashboards = api.list_all("dashboard")
    expected_dashboards = {"EMCODE | Historical Batch", "EMCODE | Live Monitoring", "EMCODE | Congestion Diagnostics"}
    result.check("Three dashboards provisioned", expected_dashboards <= {d.get("dashboard_title") for d in dashboards},
                 f"found={sorted(expected_dashboards & {d.get('dashboard_title') for d in dashboards})}")
    by_title = {chart.get("slice_name"): chart for chart in provisioned}
    for title, warning_terms, expected_filter_names in (
        ("EMCODE | Historical Batch", ("SYNTHETIC DEMO DATA", "partial batch-window days"), {"Date range", "Zone"}),
        ("EMCODE | Live Monitoring", ("SYNTHETIC LIVE DEMO", "10 seconds"), set()),
        ("EMCODE | Congestion Diagnostics", ("SYNTHETIC DEMO DATA", "not live inference"), {"Zone"}),
    ):
        dashboard = next((d for d in dashboards if d.get("dashboard_title") == title), None)
        if not dashboard:
            continue
        _, response = api.request("GET", f"/api/v1/dashboard/{dashboard['id']}")
        detail = response.get("result", response)
        metadata = json.loads(detail.get("json_metadata") or "{}")
        layout = json.loads(detail.get("position_json") or "{}")
        content = " ".join(str(item.get("meta", {}).get("code", "")) for item in layout.values()
                            if isinstance(item, dict) and item.get("type") == "MARKDOWN")
        names = {item.get("name") for item in metadata.get("native_filter_configuration", [])}
        ids = {item.get("meta", {}).get("chartId") for item in layout.values()
               if isinstance(item, dict) and item.get("type") == "CHART"}
        should_be = {by_title[name]["id"] for name in expected_historical} if title == "EMCODE | Historical Batch" else (
            {by_title[name]["id"] for name in expected_live} if title == "EMCODE | Live Monitoring" else
            {by_title[name]["id"] for name in expected_ml})
        result.check(f"{title} layout and audience note", ids == should_be and all(term in content for term in warning_terms),
                     f"charts={len(ids)}, note_ok={all(term in content for term in warning_terms)}")
        result.check(f"{title} filter configuration", names == expected_filter_names, f"filters={sorted(names)}")
        if title == "EMCODE | Historical Batch":
            zone_filter = next((item for item in metadata.get("native_filter_configuration", []) if item.get("name") == "Zone"), {})
            date_filter = next((item for item in metadata.get("native_filter_configuration", []) if item.get("name") == "Date range"), {})
            result.check("Historical zone filter scopes all relevant datasets",
                         len(zone_filter.get("targets", [])) >= 7, f"targets={len(zone_filter.get('targets', []))}")
            support_dataset_ids = {
                by_title[name].get("datasource_id")
                for name in ("Hourly GPS Activity", "Camera Events by Type", "GPS and Camera Detailed Zone Report")
            }
            zone_dataset_ids = {target.get("datasetId") for target in zone_filter.get("targets", [])}
            result.check("Historical date and zone filters include each supporting dataset",
                         bool(support_dataset_ids) and support_dataset_ids <= zone_dataset_ids
                         and bool(date_filter.get("targets")) and date_filter.get("scope", {}).get("excluded") == [],
                         f"support_dataset_ids={sorted(support_dataset_ids)}, zone_targets={sorted(zone_dataset_ids)}, date_scope={date_filter.get('scope')}")
            layout_charts = {
                item.get("meta", {}).get("chartId"): item
                for item in layout.values()
                if isinstance(item, dict) and item.get("type") == "CHART"
            }
            support_layout = {
                name: layout_charts.get(by_title[name]["id"], {}).get("meta", {})
                for name in ("Hourly GPS Activity", "Camera Events by Type", "GPS and Camera Detailed Zone Report")
            }
            hourly_parent = layout_charts.get(by_title["Hourly GPS Activity"]["id"], {}).get("parents", [])
            type_parent = layout_charts.get(by_title["Camera Events by Type"]["id"], {}).get("parents", [])
            report_parent = layout_charts.get(by_title["GPS and Camera Detailed Zone Report"]["id"], {}).get("parents", [])
            result.check("Historical supporting layout is 50/50 then full-width",
                         support_layout["Hourly GPS Activity"].get("width") == 6
                         and support_layout["Camera Events by Type"].get("width") == 6
                         and support_layout["GPS and Camera Detailed Zone Report"].get("width") == 12
                         and hourly_parent[-1:] == type_parent[-1:]
                         and report_parent[-1:] != hourly_parent[-1:], json.dumps(support_layout))

    executed, failed, map_count = 0, [], 0
    kpi_values = {}
    chart_rows: dict[str, list[dict]] = {}
    chart_params: dict[str, dict] = {}
    for chart in provisioned:
        chart_id, title = chart["id"], chart["slice_name"]
        try:
            _, detail = api.request("GET", f"/api/v1/chart/{chart_id}")
            chart_detail = detail.get("result", detail)
            params = json.loads(chart_detail.get("params") or "{}")
            chart_params[title] = params
            if chart_detail.get("viz_type") == "deck_scatter":
                map_count += 1
                spatial = params.get("spatial", {})
                result.check(f"{title} stored-coordinate bindings",
                             spatial.get("latCol") == "latitude" and spatial.get("lonCol") == "longitude",
                             str(spatial))
                if title == "Historical GPS Activity Map":
                    radius = params.get("point_radius_fixed", {})
                    result.check("Historical map aggregates by zone centroid",
                                 radius.get("type") == "metric" and params.get("dimension") == "zone_name",
                                 f"radius={radius.get('type')}, dimension={params.get('dimension')}")
                if title == "Live | Latest Vehicle Locations":
                    result.check("Live map freshness and tooltip fields",
                                 params.get("tooltip_columns") == ["vehicle_id", "event_time", "speed_kph"]
                                 and params.get("dimension") == "zone_name", json.dumps(params.get("tooltip_columns")))
            if title == "GPS Records by Zone":
                result.check("Zone chart uses descending horizontal bars",
                             params.get("orientation") == "horizontal" and params.get("x_axis_sort_asc") is True,
                             f"orientation={params.get('orientation')}, highest-at-top={params.get('x_axis_sort_asc') is True}")
            if title == "Camera Events per 1,000 GPS Records by Zone":
                sql = params.get("metrics", [{}])[0].get("sqlExpression", "")
                result.check("Camera rate is safe and separately aggregated", "NULLIF" in sql and "/ 1000" not in sql,
                             sql)
                result.check("Camera rate chart uses descending horizontal bars",
                             params.get("orientation") == "horizontal" and params.get("x_axis_sort_asc") is True,
                             f"orientation={params.get('orientation')}, highest-at-top={params.get('x_axis_sort_asc') is True}")
            if title == "Next-hour Congestion Risk by Zone":
                result.check("Congestion risk chart sorts highest risk first",
                             params.get("orientation") == "horizontal" and params.get("x_axis_sort_asc") is True,
                             f"orientation={params.get('orientation')}, highest-at-top={params.get('x_axis_sort_asc') is True}")
            _, data = api.request("GET", f"/api/v1/chart/{chart_id}/data/")
            executed += 1
            rows = (data.get("result") or [{}])[0].get("data", [])
            chart_rows[title] = rows
            if title in {"Total GPS Records", "Unique Vehicles", "Total Camera Events", "Monitored Zones"} and rows:
                kpi_values[title] = next(iter(rows[0].values()))
            if title == "Live | Latest Event Age (seconds)" and rows:
                age = next(iter(rows[0].values()))
                result.check("Live freshness KPI is a readable age in seconds", isinstance(age, (int, float)) and 0 <= age < 600,
                             f"age_seconds={age}")
            if not rows and title not in {"Live | Camera Events Over Time", "Live | Camera Events by Type", "Live | Camera Events by Zone — Last 30 Minutes"}:
                failed.append(f"{title}: query returned no rows")
        except Exception as exc:
            failed.append(f"{title}: {type(exc).__name__}: {exc}")
    expected_kpis = {"Total GPS Records": BATCH_VEHICLE_ROWS, "Unique Vehicles": 50,
                     "Total Camera Events": BATCH_CAMERA_ROWS, "Monitored Zones": 5}
    result.check("Historical KPI values through Superset", kpi_values == expected_kpis, f"values={kpi_values}")
    result.check("All saved chart queries execute", executed == len(expected),
                 f"executed={executed}/{len(expected)}, query_failures={failed}")
    for title, wanted in (("ML | Live Congestion Risk by Zone", 5),
                          ("ML | Current vs Predicted Speed (km/h)", 5),
                          ("ML | Latest Prediction Status by Zone", 5),
                          ("ML | Actual vs Predicted Speed by Zone", 5)):
        rows = chart_rows.get(title, [])
        result.check(f"{title} displays database-backed zone results", len(rows) == wanted,
                     f"rows={len(rows)} expected={wanted}")
    hist_rows = chart_rows.get("ML | Live Prediction History", [])
    result.check("Live prediction history includes multiple cycles", len(hist_rows) >= 2,
                 f"rows={len(hist_rows)}")
    hourly_rows = chart_rows.get("Hourly GPS Activity", [])
    hourly_hours = [row.get("local_hour") for row in hourly_rows]
    hourly_total = sum((row.get("GPS records") or 0) for row in hourly_rows)
    hourly_params = chart_params.get("Hourly GPS Activity", {})
    result.check("Historical hourly line uses Dubai local-hour buckets",
                 len(hourly_rows) == 24 and hourly_hours == list(range(24)) and hourly_total == BATCH_VEHICLE_ROWS
                 and by_title["Hourly GPS Activity"].get("viz_type") == "echarts_timeseries_line"
                 and hourly_params.get("show_value") is False,
                 f"buckets={len(hourly_rows)}, hours={hourly_hours}, gps_total={hourly_total}, labels={hourly_params.get('show_value')}")
    camera_type_rows = chart_rows.get("Camera Events by Type", [])
    camera_type_values = {row.get("event_category") for row in camera_type_rows}
    try:
        from app.db import connect_with_retry
        category_conn = connect_with_retry()
        try:
            with category_conn.cursor() as cursor:
                cursor.execute("SELECT DISTINCT event_category FROM camera_events WHERE ingestion_mode='batch'")
                actual_categories = {row[0] for row in cursor.fetchall()}
        finally:
            category_conn.close()
    except Exception:
        actual_categories = set()
    camera_type_total = sum((row.get("Camera events") or 0) for row in camera_type_rows)
    camera_type_params = chart_params.get("Camera Events by Type", {})
    camera_type_counts = [row.get("Camera events", 0) for row in camera_type_rows]
    result.check("Historical camera type chart uses actual categories and counts",
                 bool(actual_categories) and camera_type_values == actual_categories
                 and camera_type_total == BATCH_CAMERA_ROWS
                 and camera_type_params.get("orientation") == "horizontal"
                 and camera_type_counts == sorted(camera_type_counts, reverse=True),
                 f"categories={sorted(camera_type_values)}, actual={sorted(actual_categories)}, total={camera_type_total}, sorted_counts={camera_type_counts}")
    report_rows = chart_rows.get("GPS and Camera Detailed Zone Report", [])
    report_gps = sum((row.get("GPS Records") or 0) for row in report_rows)
    report_cameras = sum((row.get("Camera Events") or 0) for row in report_rows)
    report_meta = chart_params.get("GPS and Camera Detailed Zone Report", {})
    gps_counts = [row.get("GPS Records", 0) for row in report_rows]
    result.check("Historical detailed zone report counts and formatting",
                 len(report_rows) == 5 and report_gps == BATCH_VEHICLE_ROWS and report_cameras == BATCH_CAMERA_ROWS
                 and gps_counts == sorted(gps_counts, reverse=True)
                 and report_meta.get("show_cell_bars") is False
                 and "Camera Events per 1,000 GPS Records" in [metric.get("label") for metric in report_meta.get("metrics", [])],
                 f"zones={len(report_rows)}, gps={report_gps}, cameras={report_cameras}, sorted={gps_counts}")
    result.check("Geospatial charts use stored coordinates", map_count == 2, f"map_charts={map_count}")


def main() -> None:
    result = Verification()
    result.guarded("Database verification", lambda: verify_database(result))
    result.guarded("Live ML inference-cycle verification", lambda: verify_live_ml_cycles(result))
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

