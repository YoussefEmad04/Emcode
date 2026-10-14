from __future__ import annotations

import json
import os
import time
import urllib.parse

from app.db import connect_with_retry
from app.superset_api import SupersetAPI

DATABASE_NAME = "EMCODE PostGIS"
HISTORICAL_DASHBOARD = "EMCODE | Historical Batch"
LIVE_DASHBOARD = "EMCODE | Live Monitoring"
_PROVISION_LOCK_CONNECTION = None


def metric(label: str, expression: str = "COUNT(*)") -> dict:
    return {
        "aggregate": None,
        "column": None,
        "datasourceWarning": False,
        "expressionType": "SQL",
        "hasCustomLabel": True,
        "label": label,
        "optionName": f"metric_{label.lower().replace(' ', '_')}",
        "sqlExpression": expression,
    }


def query_context(dataset_id: int, params: dict) -> str:
    """Build the saved backend query used by dashboard loads and verification."""
    viz_type = params["viz_type"]
    # Only timestamp columns are SQL time granularity. Treating a categorical
    # x-axis (for example zone_name) as granularity makes Superset select it
    # twice when it is also used as a group-by dimension.
    granularity = params.get("granularity_sqla")
    columns: list[str] = []
    metrics: list[dict] = []
    filters: list[dict] = []
    is_timeseries = bool(
        viz_type.startswith("echarts_timeseries") and params.get("time_grain_sqla")
    )

    if viz_type == "deck_scatter":
        spatial = params["spatial"]
        columns = [spatial["latCol"], spatial["lonCol"]]
        if params.get("dimension"):
            columns.append(params["dimension"])
        columns.extend(params.get("tooltip_columns", []))
        radius = params.get("point_radius_fixed", {})
        if radius.get("type") == "metric" and isinstance(radius.get("value"), dict):
            metrics = [radius["value"]]
    elif viz_type == "pie":
        columns = list(params.get("groupby", []))
        metrics = [params["metric"]]
    elif viz_type == "big_number_total":
        metrics = [params["metric"]]
    elif viz_type == "table":
        columns = list(params.get("all_columns", []))
        metrics = list(params.get("metrics", []))
    elif viz_type.startswith("echarts_timeseries"):
        columns = list(params.get("groupby", []))
        x_axis = params.get("x_axis")
        if x_axis and x_axis != granularity and x_axis not in columns:
            columns.insert(0, x_axis)
        metrics = list(params.get("metrics", []))

    time_range = params.get("time_range", "No filter")
    for adhoc in params.get("adhoc_filters", []):
        if adhoc.get("expressionType") == "SIMPLE":
            filters.append(
                {
                    "col": adhoc["subject"],
                    "op": adhoc["operator"],
                    "val": adhoc.get("comparator"),
                }
            )

    query = {
        "annotation_layers": [],
        "applied_time_extras": {},
        "columns": columns,
        "extras": {
            "having": "",
            "where": "",
            "time_grain_sqla": params.get("time_grain_sqla"),
        },
        "filters": filters,
        "granularity": granularity,
        "is_timeseries": is_timeseries,
        "metrics": metrics,
        "orderby": (
            [[params["sort_dimension"], True]]
            if params.get("sort_dimension")
            else ([[params["sort_metric"], False]]
                  if isinstance(params.get("sort_metric"), dict) else [])
        ),
        "order_desc": True,
        "row_limit": params.get("row_limit", 10_000),
        "series_limit": 0,
        "time_range": time_range,
        "url_params": {},
    }
    context = {
        "datasource": {"id": dataset_id, "type": "table"},
        "force": False,
        "queries": [query],
        "form_data": params,
        "result_format": "json",
        "result_type": "full",
    }
    return json.dumps(context)


def wait_for_analytics_schema() -> None:
    global _PROVISION_LOCK_CONNECTION
    connection = connect_with_retry()
    try:
        # Hold the shared session lock through provisioning so simulator DDL
        # cannot replace views while Superset introspects datasets and charts.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(454002)")
        for attempt in range(1, 91):
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.v_batch_vehicle_activity')")
                if cursor.fetchone()[0]:
                    print("Analytics schema and seed views are ready", flush=True)
                    _PROVISION_LOCK_CONNECTION = connection
                    return
            time.sleep(2)
        raise RuntimeError("Timed out waiting for analytics views")
    except Exception:
        connection.close()
        raise


def ensure_database(api: SupersetAPI) -> int:
    password = urllib.parse.quote_plus(os.getenv("POSTGRES_PASSWORD", "emcode_local_only"))
    user = urllib.parse.quote_plus(os.getenv("POSTGRES_USER", "emcode"))
    database = urllib.parse.quote_plus(os.getenv("POSTGRES_DB", "emcode"))
    uri = f"postgresql+psycopg2://{user}:{password}@postgis:5432/{database}"
    payload = {
        "database_name": DATABASE_NAME,
        "sqlalchemy_uri": uri,
        "expose_in_sqllab": True,
        "allow_ctas": False,
        "allow_cvas": False,
        "allow_dml": False,
        "allow_file_upload": False,
        "extra": json.dumps({"engine_params": {"pool_pre_ping": True}}),
    }
    api.request(
        "POST",
        "/api/v1/database/test_connection/",
        {
            "database_name": DATABASE_NAME,
            "sqlalchemy_uri": uri,
            "extra": payload["extra"],
        },
    )
    current = api.find_one("database", "database_name", DATABASE_NAME)
    if current:
        database_id = current["id"]
        api.request("PUT", f"/api/v1/database/{database_id}", payload)
        print(f"Updated and tested Superset database id={database_id}", flush=True)
        return database_id
    _, response = api.request("POST", "/api/v1/database/", payload)
    database_id = response["id"]
    print(f"Created and tested Superset database id={database_id}", flush=True)
    return database_id


def ensure_dataset(api: SupersetAPI, database_id: int, table_name: str) -> int:
    current = api.find_one("dataset", "table_name", table_name)
    if current:
        dataset_id = current["id"]
        print(f"Dataset exists: {table_name} id={dataset_id}", flush=True)
        return dataset_id
    _, response = api.request(
        "POST",
        "/api/v1/dataset/",
        {"database": database_id, "schema": "public", "table_name": table_name},
    )
    dataset_id = response["id"]
    print(f"Created dataset: {table_name} id={dataset_id}", flush=True)
    return dataset_id


def ensure_dashboard(api: SupersetAPI, title: str, slug: str) -> int:
    current = api.find_one("dashboard", "dashboard_title", title)
    payload = {
        "dashboard_title": title,
        "slug": slug,
        "published": True,
        "json_metadata": json.dumps(
            {
                "color_scheme": "supersetColors",
                "default_filters": "{}",
                "timed_refresh_immune_slices": [],
            }
        ),
    }
    if current:
        dashboard_id = current["id"]
        api.request("PUT", f"/api/v1/dashboard/{dashboard_id}", payload)
        print(f"Updated dashboard shell: {title} id={dashboard_id}", flush=True)
        return dashboard_id
    _, response = api.request("POST", "/api/v1/dashboard/", payload)
    dashboard_id = response["id"]
    print(f"Created dashboard shell: {title} id={dashboard_id}", flush=True)
    return dashboard_id


def ensure_chart(
    api: SupersetAPI,
    dashboard_id: int,
    dataset_id: int,
    title: str,
    viz_type: str,
    params: dict,
    legacy_titles: tuple[str, ...] = (),
) -> int:
    params = {
        "datasource": f"{dataset_id}__table",
        "viz_type": viz_type,
        "adhoc_filters": [],
        "time_range": "No filter",
        **params,
    }
    payload = {
        "slice_name": title,
        "viz_type": viz_type,
        "datasource_id": dataset_id,
        "datasource_type": "table",
        "params": json.dumps(params),
        "query_context": query_context(dataset_id, params),
        "query_context_generation": True,
        "cache_timeout": -1,
        "dashboards": [dashboard_id],
    }
    current = api.find_one("chart", "slice_name", title)
    if not current:
        for legacy_title in legacy_titles:
            current = api.find_one("chart", "slice_name", legacy_title)
            if current:
                break
    if current:
        chart_id = current["id"]
        api.request("PUT", f"/api/v1/chart/{chart_id}", payload)
        print(f"Updated chart: {title} id={chart_id}", flush=True)
        return chart_id
    _, response = api.request("POST", "/api/v1/chart/", payload)
    chart_id = response["id"]
    print(f"Created chart: {title} id={chart_id}", flush=True)
    return chart_id


def dashboard_layout(chart_ids: list[int]) -> str:
    layout: dict[str, object] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
        "GRID_ID": {
            "id": "GRID_ID",
            "type": "GRID",
            "children": [],
            "parents": ["ROOT_ID"],
        },
    }
    rows = [chart_ids[index : index + 2] for index in range(0, len(chart_ids), 2)]
    for row_index, row_charts in enumerate(rows, 1):
        row_id = f"ROW-{row_index}"
        layout["GRID_ID"]["children"].append(row_id)  # type: ignore[index]
        layout[row_id] = {
            "id": row_id,
            "type": "ROW",
            "children": [f"CHART-{chart_id}" for chart_id in row_charts],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
        }
        for chart_id in row_charts:
            layout[f"CHART-{chart_id}"] = {
                "id": f"CHART-{chart_id}",
                "type": "CHART",
                "children": [],
                "meta": {"chartId": chart_id, "height": 50, "width": 6},
                "parents": ["ROOT_ID", "GRID_ID", row_id],
            }
    return json.dumps(layout)


def sectioned_dashboard_layout(
    kpi_ids: list[int], main_ids: list[int], support_ids: list[int],
    *, warning: str, support_title: str = "Supporting analysis",
    support_rows: list[tuple[list[int], int, int]] | None = None,
) -> str:
    """Compact layout: four KPI cards, four main charts, then focused detail."""
    layout: dict[str, object] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
        "GRID_ID": {
            "id": "GRID_ID",
            "type": "GRID",
            "children": ["ROW-INTRO", "ROW-KPIS", "ROW-MAIN-1", "ROW-MAIN-2", "ROW-SUPPORT-TITLE"],
            "parents": ["ROOT_ID"],
        },
        "ROW-INTRO": {
            "id": "ROW-INTRO",
            "type": "ROW",
            "children": ["MARKDOWN-SYNTHETIC"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
        },
        "MARKDOWN-SYNTHETIC": {
            "id": "MARKDOWN-SYNTHETIC",
            "type": "MARKDOWN",
            "children": [],
            "meta": {
                "width": 12,
                "height": 16,
                "background": "BACKGROUND_TRANSPARENT",
                "code": (
                    warning
                ),
            },
            "parents": ["ROOT_ID", "GRID_ID", "ROW-INTRO"],
        },
        "ROW-KPIS": {
            "id": "ROW-KPIS",
            "type": "ROW",
            "children": [f"CHART-{chart_id}" for chart_id in kpi_ids],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
        },
    }
    for chart_id in kpi_ids:
        layout[f"CHART-{chart_id}"] = {
            "id": f"CHART-{chart_id}",
            "type": "CHART",
            "children": [],
            "meta": {"chartId": chart_id, "height": 20, "width": 3},
            "parents": ["ROOT_ID", "GRID_ID", "ROW-KPIS"],
        }
    for row_number, row_charts in enumerate((main_ids[:2], main_ids[2:4]), 1):
        row_id = f"ROW-MAIN-{row_number}"
        layout[row_id] = {
            "id": row_id,
            "type": "ROW",
            "children": [f"CHART-{chart_id}" for chart_id in row_charts],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
        }
        for chart_id in row_charts:
            layout[f"CHART-{chart_id}"] = {
                "id": f"CHART-{chart_id}",
                "type": "CHART",
                "children": [],
                "meta": {"chartId": chart_id, "height": 40, "width": 6},
                "parents": ["ROOT_ID", "GRID_ID", row_id],
            }
    layout["ROW-SUPPORT-TITLE"] = {
        "id": "ROW-SUPPORT-TITLE", "type": "ROW", "children": ["MARKDOWN-SUPPORT-TITLE"],
        "meta": {"background": "BACKGROUND_TRANSPARENT"}, "parents": ["ROOT_ID", "GRID_ID"],
    }
    layout["MARKDOWN-SUPPORT-TITLE"] = {
        "id": "MARKDOWN-SUPPORT-TITLE", "type": "MARKDOWN", "children": [],
        "meta": {"width": 12, "height": 8, "background": "BACKGROUND_TRANSPARENT", "code": f"### {support_title}"},
        "parents": ["ROOT_ID", "GRID_ID", "ROW-SUPPORT-TITLE"],
    }
    custom_support_rows = support_rows is not None
    support_rows = support_rows or [(support_ids, max(3, 12 // max(1, len(support_ids))), 32)]
    for row_index, (row_charts, chart_width, chart_height) in enumerate(support_rows, 1):
        row_id = f"ROW-SUPPORT-{row_index}" if custom_support_rows else "ROW-SUPPORT"
        layout["GRID_ID"]["children"].append(row_id)  # type: ignore[index]
        layout[row_id] = {
            "id": row_id, "type": "ROW",
            "children": [f"CHART-{chart_id}" for chart_id in row_charts],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
        }
        for chart_id in row_charts:
            layout[f"CHART-{chart_id}"] = {
                "id": f"CHART-{chart_id}", "type": "CHART", "children": [],
                "meta": {"chartId": chart_id, "height": chart_height, "width": chart_width},
                "parents": ["ROOT_ID", "GRID_ID", row_id],
            }
    return json.dumps(layout)


def diagnostics_dashboard_layout(chart_ids: list[int]) -> str:
    risk_id, speed_id, *rest = chart_ids
    metric_ids = rest[:4]
    confusion_id, summary_id = rest[4:6]
    regression_metric_ids = rest[6:9]
    regression_chart_id, regression_summary_id = rest[9:11]
    layout: dict[str, object] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
        "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": ["ROW-SYNTHETIC", "ROW-PREDICTION", "ROW-CLASSIFICATION-HEADER", "ROW-METRICS", "ROW-EVALUATION", "ROW-REGRESSION-HEADER", "ROW-REGRESSION-METRICS", "ROW-REGRESSION-EVALUATION"], "parents": ["ROOT_ID"]},
    }
    layout["ROW-SYNTHETIC"] = {
        "id": "ROW-SYNTHETIC",
        "type": "ROW",
        "children": ["MARKDOWN-SYNTHETIC"],
        "meta": {"background": "BACKGROUND_TRANSPARENT"},
        "parents": ["ROOT_ID", "GRID_ID"],
    }
    layout["MARKDOWN-SYNTHETIC"] = {
        "id": "MARKDOWN-SYNTHETIC",
        "type": "MARKDOWN",
        "children": [],
        "meta": {
            "width": 12,
            "height": 10,
            "background": "BACKGROUND_TRANSPARENT",
            "code": (
                "## SYNTHETIC DEMO DATA — CONGESTION DIAGNOSTICS\n"
                "Congestion labels are artificial speed-and-sample proxies. Metrics are chronological held-out results on synthetic data only; they do not validate real-world traffic prediction. Forecasts are anchored to historical batch data, not live inference."
            ),
        },
        "parents": ["ROOT_ID", "GRID_ID", "ROW-SYNTHETIC"],
    }
    for row_id, ids, height, width in (
        ("ROW-PREDICTION", [risk_id, speed_id], 42, 6),
        ("ROW-METRICS", metric_ids, 20, 3),
        ("ROW-EVALUATION", [confusion_id, summary_id], 36, 6),
    ):
        layout[row_id] = {"id": row_id, "type": "ROW", "children": [f"CHART-{cid}" for cid in ids],
                          "meta": {"background": "BACKGROUND_TRANSPARENT"}, "parents": ["ROOT_ID", "GRID_ID"]}
        for chart_id in ids:
            layout[f"CHART-{chart_id}"] = {"id": f"CHART-{chart_id}", "type": "CHART", "children": [],
                "meta": {"chartId": chart_id, "height": height, "width": width},
                "parents": ["ROOT_ID", "GRID_ID", row_id]}
    for row_id, label in (("ROW-CLASSIFICATION-HEADER","### Classification Evaluation"),
                          ("ROW-REGRESSION-HEADER","### Regression Evaluation — held-out synthetic test data")):
        md_id = f"MARKDOWN-{row_id}"
        layout[row_id] = {"id":row_id,"type":"ROW","children":[md_id],
                          "meta":{"background":"BACKGROUND_TRANSPARENT"},"parents":["ROOT_ID","GRID_ID"]}
        layout[md_id] = {"id":md_id,"type":"MARKDOWN","children":[],
                         "meta":{"width":12,"height":8,"background":"BACKGROUND_TRANSPARENT","code":label},
                         "parents":["ROOT_ID","GRID_ID",row_id]}
    for row_id, ids in (("ROW-REGRESSION-METRICS", regression_metric_ids),
                        ("ROW-REGRESSION-EVALUATION", [regression_chart_id, regression_summary_id])):
        layout[row_id] = {"id": row_id, "type": "ROW", "children": [f"CHART-{cid}" for cid in ids],
                          "meta": {"background": "BACKGROUND_TRANSPARENT"}, "parents": ["ROOT_ID", "GRID_ID"]}
        for chart_id in ids:
            layout[f"CHART-{chart_id}"] = {"id": f"CHART-{chart_id}", "type": "CHART", "children": [],
                "meta": {"chartId": chart_id, "height": 34 if row_id.endswith("METRICS") else 38,
                         "width": 4 if row_id.endswith("METRICS") else 6},
                "parents": ["ROOT_ID", "GRID_ID", row_id]}
    return json.dumps(layout)


def live_predictions_layout(kpis, main, support, ml_charts, warning):
    """Keep operational support charts and add a visually distinct ML section."""
    layout = json.loads(sectioned_dashboard_layout(
        kpis, main, support, warning=warning, support_title="Supporting operational analysis"))
    layout["GRID_ID"]["children"].extend(["ROW-ML-TITLE", "ROW-ML-1", "ROW-ML-2"])
    layout["ROW-ML-TITLE"] = {"id":"ROW-ML-TITLE","type":"ROW","children":["MARKDOWN-ML"],
                              "meta":{"background":"BACKGROUND_TRANSPARENT"},"parents":["ROOT_ID","GRID_ID"]}
    layout["MARKDOWN-ML"] = {"id":"MARKDOWN-ML","type":"MARKDOWN","children":[],
        "meta":{"width":12,"height":10,"background":"BACKGROUND_TRANSPARENT",
                "code":"\n".join(("### Near Real-Time ML Predictions",
                    "Predictions use live-only trailing 30-minute features to forecast the next hour. Insufficient-data rows are excluded from valid-result charts."))},
        "parents":["ROOT_ID","GRID_ID","ROW-ML-TITLE"]}
    for row_id, ids, width in (("ROW-ML-1",ml_charts[:2],6),("ROW-ML-2",ml_charts[2:],6)):
        layout[row_id] = {"id":row_id,"type":"ROW","children":[f"CHART-{i}" for i in ids],
                          "meta":{"background":"BACKGROUND_TRANSPARENT"},"parents":["ROOT_ID","GRID_ID"]}
        for cid in ids:
            layout[f"CHART-{cid}"]={"id":f"CHART-{cid}","type":"CHART","children":[],
                "meta":{"chartId":cid,"height":38,"width":width},"parents":["ROOT_ID","GRID_ID",row_id]}
    return json.dumps(layout)


def historical_metadata(zone_dataset_ids: list[int]) -> str:
    base_filter = {
        "cascadeParentIds": [],
        "defaultDataMask": {"extraFormData": {}, "filterState": {}},
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        "type": "NATIVE_FILTER",
    }
    return json.dumps(
        {
            "color_scheme": "supersetColors",
            "default_filters": "{}",
            "timed_refresh_immune_slices": [],
            "cross_filters_enabled": True,
            "native_filter_configuration": [
                {
                    **base_filter,
                    "id": "NATIVE_FILTER-HISTORICAL-DATE",
                    "name": "Date range",
                    "filterType": "filter_time",
                    "targets": [{}],
                    "controlValues": {"enableEmptyFilter": False},
                },
                {
                    **base_filter,
                    "id": "NATIVE_FILTER-HISTORICAL-ZONE",
                    "name": "Zone",
                    "filterType": "filter_select",
                    "targets": [{"column": {"name": "zone_name"}, "datasetId": dataset_id}
                                for dataset_id in zone_dataset_ids],
                    "controlValues": {
                        "enableEmptyFilter": False,
                        "inverseSelection": False,
                        "multiSelect": True,
                        "searchAllOptions": False,
                    },
                },
            ],
        }
    )


def zone_filter_metadata(dataset_ids: list[int], filter_name: str = "Zone") -> str:
    base = {
        "cascadeParentIds": [],
        "defaultDataMask": {"extraFormData": {}, "filterState": {}},
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        "type": "NATIVE_FILTER",
    }
    return json.dumps({
        "color_scheme": "supersetColors", "default_filters": "{}",
        "timed_refresh_immune_slices": [],
        "native_filter_configuration": [{
            **base, "id": f"NATIVE_FILTER-{filter_name.upper().replace(' ', '-')}",
            "name": filter_name, "filterType": "filter_select",
            "targets": [{"column": {"name": "zone_name"}, "datasetId": dataset_id}
                        for dataset_id in dataset_ids],
            "controlValues": {"enableEmptyFilter": False, "inverseSelection": False,
                              "multiSelect": True, "searchAllOptions": False},
        }],
    })


def configure_dashboard(
    api: SupersetAPI,
    dashboard_id: int,
    chart_ids: list[int],
    *,
    position_json: str | None = None,
    json_metadata: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "position_json": position_json or dashboard_layout(chart_ids),
        "published": True,
    }
    if json_metadata is not None:
        payload["json_metadata"] = json_metadata
    api.request(
        "PUT",
        f"/api/v1/dashboard/{dashboard_id}",
        payload,
    )


def main() -> None:
    wait_for_analytics_schema()
    # Wait for synthetic model results before provisioning the report.
    conn = connect_with_retry()
    try:
        for attempt in range(90):
            with conn.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM ml_congestion_predictions WHERE forecast_kind='forecast'")
                if cursor.fetchone()[0] == 5:
                    break
            time.sleep(2)
        else:
            raise RuntimeError("Synthetic congestion model did not produce five forecasts")
    finally:
        conn.close()
    api = SupersetAPI()
    for attempt in range(1, 61):
        try:
            api.login()
            break
        except Exception as exc:
            print(f"Waiting for Superset login ({attempt}/60): {exc}", flush=True)
            time.sleep(2)
    else:
        raise RuntimeError("Could not authenticate to Superset")

    database_id = ensure_database(api)
    dataset_names = (
        "v_batch_vehicle_activity", "v_batch_camera_activity", "v_batch_daily_zone_activity",
        "v_batch_spatial_activity", "v_batch_hourly_zone_activity", "v_batch_hourly_activity",
        "v_batch_camera_rate_by_zone", "v_live_vehicle_latest", "v_live_events_recent",
        "v_live_kpis", "v_live_gps_5m", "v_live_camera_5m", "v_live_vehicles_by_zone_30m",
        "v_live_camera_by_zone_30m", "v_zone_activity_5m", "v_ml_congestion_forecast",
        "v_ml_congestion_holdout", "v_ml_report_metrics", "v_ml_model_summary", "v_ml_confusion_matrix",
        "v_ml_live_latest", "v_ml_live_latest_status", "v_ml_live_history", "v_ml_regression_metrics", "v_ml_regression_holdout",
    )
    datasets = {name: ensure_dataset(api, database_id, name) for name in dataset_names}

    historical_id = ensure_dashboard(api, HISTORICAL_DASHBOARD, "emcode-historical-batch")
    historical_kpis = [
        ensure_chart(api, historical_id, datasets["v_batch_vehicle_activity"], "Total GPS Records", "big_number_total",
                     {"metric": metric("GPS position records"), "granularity_sqla": "event_time", "y_axis_format": "SMART_NUMBER"},
                     legacy_titles=("Historical | Vehicle Position Count",)),
        ensure_chart(api, historical_id, datasets["v_batch_vehicle_activity"], "Unique Vehicles", "big_number_total",
                     {"metric": metric("Unique vehicles", "COUNT(DISTINCT vehicle_id)"), "granularity_sqla": "event_time", "y_axis_format": "SMART_NUMBER"},
                     legacy_titles=("Distinct Vehicles",)),
        ensure_chart(api, historical_id, datasets["v_batch_camera_activity"], "Total Camera Events", "big_number_total",
                     {"metric": metric("Camera event records"), "granularity_sqla": "event_time", "y_axis_format": "SMART_NUMBER"}),
        ensure_chart(api, historical_id, datasets["v_batch_vehicle_activity"], "Monitored Zones", "big_number_total",
                     {"metric": metric("Zones with GPS records", "COUNT(DISTINCT zone_id)"), "granularity_sqla": "event_time", "y_axis_format": "SMART_NUMBER"}),
    ]
    historical_main = [
        ensure_chart(api, historical_id, datasets["v_batch_daily_zone_activity"], "Daily GPS Records", "echarts_timeseries_bar",
                     {"x_axis": "event_time", "granularity_sqla": "event_time", "time_grain_sqla": "P1D",
                      "metrics": [metric("GPS records", "SUM(gps_record_count)")], "groupby": [],
                      "row_limit": 100, "show_legend": False, "show_value": True},
                     legacy_titles=("Historical | Positions Over Time",)),
        ensure_chart(api, historical_id, datasets["v_batch_daily_zone_activity"], "GPS Records by Zone", "echarts_timeseries_bar",
                     {"x_axis": "zone_name", "granularity_sqla": "event_time", "metrics": [metric("GPS records", "SUM(gps_record_count)")],
                      "groupby": [], "row_limit": 100, "orientation": "horizontal",
                      "sort_metric": metric("GPS records", "SUM(gps_record_count)"), "x_axis_sort": "GPS records",
                      "x_axis_sort_asc": True, "show_legend": False, "show_value": True},
                     legacy_titles=("Historical | Positions by Zone",)),
        ensure_chart(api, historical_id, datasets["v_batch_spatial_activity"], "Historical GPS Activity Map", "deck_scatter",
                     {"spatial": {"type": "latlong", "latCol": "latitude", "lonCol": "longitude"}, "dimension": "zone_name",
                      "granularity_sqla": "event_time", "row_limit": 5000,
                      "mapbox_style": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                      "viewport": {"longitude": 55.24, "latitude": 25.12, "zoom": 9, "bearing": 0, "pitch": 0},
                      "point_radius_fixed": {"type": "metric", "value": metric("GPS records", "SUM(gps_record_count)")},
                      "point_unit": "pixels", "min_radius": 5, "max_radius": 28, "multiplier": 1,
                      "color_scheme": "supersetColors"}, legacy_titles=("Historical | Position Map", "GPS Activity Clusters")),
        ensure_chart(api, historical_id, datasets["v_batch_camera_rate_by_zone"], "Camera Events per 1,000 GPS Records by Zone", "echarts_timeseries_bar",
                     {"x_axis": "zone_name", "granularity_sqla": "event_time",
                      "metrics": [metric("Camera events / 1,000 GPS", "1000.0 * SUM(camera_event_count) / NULLIF(SUM(gps_record_count), 0)")],
                      "groupby": [], "row_limit": 100, "orientation": "horizontal",
                      "sort_metric": metric("Camera events / 1,000 GPS", "1000.0 * SUM(camera_event_count) / NULLIF(SUM(gps_record_count), 0)"),
                      "x_axis_sort": "Camera events / 1,000 GPS", "x_axis_sort_asc": True,
                      "show_legend": False, "show_value": True, "y_axis_format": ".2f"},
                     legacy_titles=("GPS vs Camera Records by Zone",)),
    ]
    historical_support = [
        ensure_chart(api, historical_id, datasets["v_batch_hourly_activity"], "Hourly GPS Activity", "echarts_timeseries_line",
                     {"x_axis": "local_hour", "granularity_sqla": "event_time",
                      "metrics": [metric("GPS records", "SUM(gps_record_count)")],
                      "groupby": [], "row_limit": 24, "show_legend": False, "show_value": False,
                      "sort_dimension": "local_hour",
                      "x_axis_sort": "local_hour", "x_axis_sort_asc": True}),
        ensure_chart(api, historical_id, datasets["v_batch_camera_activity"], "Camera Events by Type", "echarts_timeseries_bar",
                     {"x_axis": "event_category", "granularity_sqla": "event_time",
                      "metrics": [metric("Camera events", "COUNT(*)")], "groupby": [],
                      "row_limit": 20, "orientation": "horizontal",
                      "sort_metric": metric("Camera events", "COUNT(*)"),
                      "x_axis_sort": "Camera events", "x_axis_sort_asc": True,
                      "show_legend": False, "show_value": True},
                     legacy_titles=("Zone Activity Breakdown",)),
        ensure_chart(api, historical_id, datasets["v_batch_camera_rate_by_zone"], "GPS and Camera Detailed Zone Report", "table",
                     {"all_columns": ["zone_name"],
                      "metrics": [metric("GPS Records", "SUM(gps_record_count)"),
                                  metric("Camera Events", "SUM(camera_event_count)"),
                                  metric("Camera Events per 1,000 GPS Records",
                                         "1000.0 * SUM(camera_event_count) / NULLIF(SUM(gps_record_count), 0)")],
                      "sort_metric": metric("GPS Records", "SUM(gps_record_count)"),
                      "show_cell_bars": False,
                      "column_config": {
                          "GPS Records": {"d3NumberFormat": ",.0f"},
                          "Camera Events": {"d3NumberFormat": ",.0f"},
                          "Camera Events per 1,000 GPS Records": {"d3NumberFormat": ",.2f"},
                      }, "row_limit": 20},
                     legacy_titles=("GPS and Camera Source Counts",)),
    ]
    historical_warning = (
        "## SYNTHETIC DEMO DATA\nArtificial, reproducible records for demonstration only — not real traffic or surveillance data. "
        "**Total GPS Records** counts position observations; **Unique Vehicles** counts distinct synthetic IDs; "
        "**Total Camera Events** counts event records; **Monitored Zones** counts zones represented by GPS records. "
        "The start and end calendar days are partial batch-window days. Camera-event rate is descriptive, not causal or incident probability."
    )
    historical_all = historical_kpis + historical_main + historical_support
    configure_dashboard(api, historical_id, historical_all,
        position_json=sectioned_dashboard_layout(historical_kpis, historical_main, historical_support,
            warning=historical_warning, support_title="Supporting analysis — use Date range and Zone filters",
            support_rows=[([historical_support[0], historical_support[1]], 6, 36),
                          ([historical_support[2]], 12, 42)]),
        json_metadata=historical_metadata([datasets[n] for n in (
            "v_batch_vehicle_activity", "v_batch_camera_activity", "v_batch_daily_zone_activity",
            "v_batch_spatial_activity", "v_batch_hourly_activity", "v_batch_hourly_zone_activity",
            "v_batch_camera_rate_by_zone")]))

    live_id = ensure_dashboard(api, LIVE_DASHBOARD, "emcode-live-monitoring")
    live_kpis = [
        ensure_chart(api, live_id, datasets["v_live_kpis"], "Live | Recently Seen Vehicles", "big_number_total",
                     {"metric": metric("Vehicles reporting within 10 minutes", "MAX(vehicles_10m)"), "y_axis_format": "SMART_NUMBER"}),
        ensure_chart(api, live_id, datasets["v_live_kpis"], "Live | Camera Events — Last 15 Minutes", "big_number_total",
                     {"metric": metric("Camera events", "MAX(camera_15m)"), "y_axis_format": "SMART_NUMBER"},
                     legacy_titles=("Live | Camera Events Last 15 Minutes",)),
        ensure_chart(api, live_id, datasets["v_live_kpis"], "Live | GPS Records — Last 15 Minutes", "big_number_total",
                     {"metric": metric("GPS position records", "MAX(gps_15m)"), "y_axis_format": "SMART_NUMBER"}),
        ensure_chart(api, live_id, datasets["v_live_kpis"], "Live | Latest Event Age (seconds)", "big_number_total",
                     {"metric": metric("Seconds since latest ingested event", "GREATEST(0, EXTRACT(EPOCH FROM (NOW() - MAX(latest_event_received_at)))::BIGINT)"),
                      "y_axis_format": "SMART_NUMBER"},
                     legacy_titles=("Live | Latest Event Received", "Live | Latest Event Received (UTC)")),
    ]
    live_main = [
        ensure_chart(api, live_id, datasets["v_live_vehicle_latest"], "Live | Latest Vehicle Locations", "deck_scatter",
                     {"spatial": {"type": "latlong", "latCol": "latitude", "lonCol": "longitude"}, "dimension": "zone_name",
                      "tooltip_columns": ["vehicle_id", "event_time", "speed_kph"], "row_limit": 100,
                      "mapbox_style": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                      "viewport": {"longitude": 55.24, "latitude": 25.12, "zoom": 9, "bearing": 0, "pitch": 0},
                      "point_radius_fixed": {"type": "fix", "value": 7}, "point_unit": "pixels", "color_scheme": "supersetColors"}),
        ensure_chart(api, live_id, datasets["v_live_gps_5m"], "Live | GPS Position Events Over Time", "echarts_timeseries_bar",
                     {"x_axis": "window_start", "granularity_sqla": "window_start", "time_grain_sqla": "PT5M",
                      "metrics": [metric("GPS position events", "SUM(gps_record_count)")], "groupby": [],
                      "time_range": "Last 30 minutes", "row_limit": 20, "show_legend": False, "show_value": True}),
        ensure_chart(api, live_id, datasets["v_live_camera_5m"], "Live | Camera Events Over Time", "echarts_timeseries_bar",
                     {"x_axis": "window_start", "granularity_sqla": "window_start", "time_grain_sqla": "PT5M",
                      "metrics": [metric("Camera events", "SUM(camera_event_count)")], "groupby": [],
                      "time_range": "Last 30 minutes", "row_limit": 20, "show_legend": False, "show_value": True},
                     legacy_titles=("Live | Camera Event Timeline",)),
        ensure_chart(api, live_id, datasets["v_live_vehicles_by_zone_30m"], "Live | Vehicle Activity by Zone", "echarts_timeseries_bar",
                     {"x_axis": "zone_name", "metrics": [metric("Unique vehicles", "SUM(unique_vehicles)")], "groupby": [],
                      "row_limit": 20, "orientation": "horizontal", "sort_metric": metric("Unique vehicles", "SUM(unique_vehicles)"),
                      "x_axis_sort": "Unique vehicles", "x_axis_sort_asc": True, "show_legend": False, "show_value": True},
                     legacy_titles=("Live | Zone Vehicle and Camera Activity",)),
    ]
    live_support = [
        ensure_chart(api, live_id, datasets["v_live_events_recent"], "Live | Camera Events by Type", "echarts_timeseries_bar",
                     {"x_axis": "event_category", "granularity_sqla": "event_time", "metrics": [metric("Camera events")],
                      "groupby": [], "time_range": "Last 30 minutes", "row_limit": 20,
                      "orientation": "horizontal", "show_legend": False, "show_value": True}),
        ensure_chart(api, live_id, datasets["v_live_camera_by_zone_30m"], "Live | Camera Events by Zone — Last 30 Minutes", "echarts_timeseries_bar",
                     {"x_axis": "zone_name", "metrics": [metric("Camera events", "SUM(camera_events)")], "groupby": [],
                      "row_limit": 20, "orientation": "horizontal", "sort_metric": metric("Camera events", "SUM(camera_events)"),
                      "x_axis_sort": "Camera events", "x_axis_sort_asc": True, "show_legend": False, "show_value": True},
                     legacy_titles=("Live | Camera Events by Zone",)),
    ]
    live_ml = [
        ensure_chart(api, live_id, datasets["v_ml_live_latest"], "ML | Live Congestion Risk by Zone", "echarts_timeseries_bar",
                     {"x_axis":"zone_name","groupby":[],"metrics":[metric("Congestion risk", "MAX(congestion_probability)")],
                      "orientation":"horizontal","row_limit":10,"show_legend":False,"show_value":True,"y_axis_format":".1%",
                      "x_axis_sort":"Congestion risk","sort_metric":metric("Congestion risk", "MAX(congestion_probability)"),"x_axis_sort_asc":True}),
        ensure_chart(api, live_id, datasets["v_ml_live_latest"], "ML | Current vs Predicted Speed (km/h)", "echarts_timeseries_bar",
                     {"x_axis":"zone_name","groupby":[],"metrics":[metric("Observed current (km/h)","AVG(current_avg_speed_kph)"),
                      metric("Predicted next hour (km/h)","AVG(predicted_avg_speed_kph)")],"row_limit":10,"show_legend":True,"show_value":False}),
        ensure_chart(api, live_id, datasets["v_ml_live_history"], "ML | Live Prediction History", "echarts_timeseries_line",
                     {"x_axis":"prediction_time","granularity_sqla":"prediction_time","time_grain_sqla":"PT1M",
                      "metrics":[metric("Congestion risk","AVG(congestion_probability)")],"groupby":[],"time_range":"No filter",
                      "row_limit":500,"show_legend":False,"show_value":False,
                      "adhoc_filters":[{"expressionType":"SIMPLE","subject":"zone_name","operator":"==","comparator":"Demo Marina","clause":"WHERE"}]}),
        ensure_chart(api, live_id, datasets["v_ml_live_latest_status"], "ML | Latest Prediction Status by Zone", "table",
                     {"all_columns":["zone_name","prediction_time","target_start","target_end","prediction_status",
                      "congestion_probability","predicted_congested","predicted_avg_speed_kph","classifier_version"],"metrics":[],"row_limit":10}),
    ]
    live_all = live_kpis + live_main + live_support + live_ml
    live_warning = "## SYNTHETIC LIVE DEMO\nLive charts query stream-only inserts. The map shows each vehicle's latest position only while its reported event time is under 10 minutes old. Dashboard refresh is browser polling, not push streaming; choose 10 seconds from the dashboard menu."
    configure_dashboard(api, live_id, live_all,
        position_json=live_predictions_layout(live_kpis, live_main, live_support, live_ml, live_warning),
        json_metadata=json.dumps({"color_scheme": "supersetColors", "default_filters": "{}", "timed_refresh_immune_slices": []}))

    diagnostic_id = ensure_dashboard(api, "EMCODE | Congestion Diagnostics", "emcode-congestion-diagnostics")
    diagnostic_charts = [
        ensure_chart(api, diagnostic_id, datasets["v_ml_congestion_forecast"], "Next-hour Congestion Risk by Zone", "echarts_timeseries_bar",
                     {"x_axis": "zone_name", "granularity_sqla": "event_time",
                      "metrics": [metric("Synthetic risk score (%)", "MAX(congestion_risk_pct) / 100.0")], "orientation": "horizontal",
                      "sort_metric": metric("Synthetic risk score (%)", "MAX(congestion_risk_pct) / 100.0"),
                      "x_axis_sort": "Synthetic risk score (%)", "x_axis_sort_asc": True,
                      "row_limit": 20, "show_value": True, "show_legend": False, "y_axis_format": ".1%"}),
        ensure_chart(api, diagnostic_id, datasets["v_batch_hourly_zone_activity"], "Average Speed by Hour of Day", "echarts_timeseries_line",
                     {"x_axis": "local_hour", "granularity_sqla": "event_time",
                      "metrics": [metric("Average speed (km/h)", "AVG(avg_speed_kph)")], "groupby": [],
                      "row_limit": 24, "show_legend": False}, legacy_titles=("Hourly Average Speed by Zone",)),
        *[ensure_chart(api, diagnostic_id, datasets["v_ml_report_metrics"], f"ML | {name}", "big_number_total",
                       {"metric": metric(name, f"MAX({field})"), "y_axis_format": ".1%"})
          for name, field in (("Accuracy", "accuracy"), ("Precision", "precision"), ("Recall", "recall"), ("F1-score", "f1"))],
        ensure_chart(api, diagnostic_id, datasets["v_ml_confusion_matrix"], "ML | Test-set Confusion Matrix", "table",
                     {"all_columns": ["actual_label", "predicted_not_congested", "predicted_congested", "row_total"],
                      "metrics": [], "row_limit": 10},
                     legacy_titles=("Test-set Predictions vs Proxy Labels by Zone",)),
        ensure_chart(api, diagnostic_id, datasets["v_ml_model_summary"], "ML | Model Evaluation Summary", "table",
                     {"all_columns": ["model_version", "trained_at", "train_size", "validation_size", "test_size",
                                      "positive_class_rate", "decision_threshold", "baseline_accuracy", "label_definition",
                                      "latest_forecast_target", "forecast_freshness"], "metrics": [], "row_limit": 10}),
        *[ensure_chart(api, diagnostic_id, datasets["v_ml_regression_metrics"], f"ML | {name}", "big_number_total",
                       {"metric":metric(name,f"MAX({field})"),"y_axis_format":fmt})
          for name,field,fmt in (("MAE (km/h)","mae","SMART_NUMBER"),("RMSE (km/h)","rmse","SMART_NUMBER"),("R²","r2",".2f"))],
        ensure_chart(api, diagnostic_id, datasets["v_ml_regression_holdout"], "ML | Actual vs Predicted Speed by Zone", "echarts_timeseries_bar",
                     {"x_axis":"zone_name","groupby":[],"metrics":[metric("Actual test speed (km/h)","AVG(actual_avg_speed_kph)"),
                      metric("Predicted test speed (km/h)","AVG(predicted_avg_speed_kph)")],"row_limit":10,"show_legend":True,"show_value":False}),
        ensure_chart(api, diagnostic_id, datasets["v_ml_regression_metrics"], "ML | Regression Baseline and Model Summary", "table",
                     {"all_columns":["model_version","trained_at","test_size","mae","baseline_mae","rmse","baseline_rmse","r2"],
                      "metrics":[],"row_limit":10}),
    ]
    configure_dashboard(api, diagnostic_id, diagnostic_charts,
        position_json=diagnostics_dashboard_layout(diagnostic_charts),
        json_metadata=zone_filter_metadata([datasets["v_batch_hourly_zone_activity"], datasets["v_ml_congestion_forecast"]]))
    print(f"Provisioned 3 dashboards, {len(datasets)} datasets, and "
          f"{len(historical_all) + len(live_all) + len(diagnostic_charts)} charts", flush=True)
    if _PROVISION_LOCK_CONNECTION is not None:
        _PROVISION_LOCK_CONNECTION.close()


if __name__ == "__main__":
    main()

