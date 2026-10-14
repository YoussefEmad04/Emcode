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
    granularity = params.get("granularity_sqla") or params.get("x_axis")
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
        radius = params.get("point_radius_fixed", {})
        if radius.get("type") == "metric" and isinstance(radius.get("value"), dict):
            metrics = [radius["value"]]
    elif viz_type == "pie":
        columns = list(params.get("groupby", []))
        metrics = [params["metric"]]
    elif viz_type == "big_number_total":
        metrics = [params["metric"]]
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
            [[params["sort_metric"], False]]
            if isinstance(params.get("sort_metric"), dict)
            else []
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
    connection = connect_with_retry()
    try:
        for attempt in range(1, 91):
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.v_batch_vehicle_activity')")
                if cursor.fetchone()[0]:
                    print("Analytics schema and seed views are ready", flush=True)
                    return
            time.sleep(2)
        raise RuntimeError("Timed out waiting for analytics views")
    finally:
        connection.close()


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


def historical_dashboard_layout(kpi_ids: list[int], chart_ids: list[int]) -> str:
    layout: dict[str, object] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
        "GRID_ID": {
            "id": "GRID_ID",
            "type": "GRID",
            "children": ["ROW-INTRO", "ROW-KPIS", "ROW-MAIN-1", "ROW-MAIN-2"],
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
                    "## SYNTHETIC DEMO DATA\n"
                    "Artificial, reproducible records for product demonstration only — not real traffic or surveillance data.  "
                    "**Total GPS Records** counts position observations, not vehicles. "
                    "**Distinct Vehicles** counts unique synthetic vehicle IDs. "
                    "**Total Camera Events** counts fictitious event records. "
                    "**Monitored Zones** counts represented demo zones."
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
    for row_number, row_charts in enumerate((chart_ids[:2], chart_ids[2:]), 1):
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
                "meta": {"chartId": chart_id, "height": 44, "width": 6},
                "parents": ["ROOT_ID", "GRID_ID", row_id],
            }
    return json.dumps(layout)


def historical_metadata(zone_dataset_id: int) -> str:
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
                    "targets": [
                        {"column": {"name": "zone_name"}, "datasetId": zone_dataset_id}
                    ],
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
    datasets = {
        name: ensure_dataset(api, database_id, name)
        for name in (
            "v_batch_vehicle_activity",
            "v_batch_camera_activity",
            "v_batch_daily_zone_activity",
            "v_batch_spatial_activity",
            "v_live_vehicle_latest",
            "v_live_events_recent",
            "v_zone_activity_5m",
        )
    }

    historical_id = ensure_dashboard(api, HISTORICAL_DASHBOARD, "emcode-historical-batch")
    historical_kpis = [
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_vehicle_activity"],
            "Total GPS Records",
            "big_number_total",
            {
                "metric": metric("Total GPS Records"),
                "granularity_sqla": "event_time",
                "y_axis_format": "SMART_NUMBER",
            },
            legacy_titles=("Historical | Vehicle Position Count",),
        ),
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_vehicle_activity"],
            "Distinct Vehicles",
            "big_number_total",
            {
                "metric": metric("Distinct Vehicles", "COUNT(DISTINCT vehicle_id)"),
                "granularity_sqla": "event_time",
                "y_axis_format": "SMART_NUMBER",
            },
        ),
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_camera_activity"],
            "Total Camera Events",
            "big_number_total",
            {
                "metric": metric("Total Camera Events"),
                "granularity_sqla": "event_time",
                "y_axis_format": "SMART_NUMBER",
            },
        ),
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_vehicle_activity"],
            "Monitored Zones",
            "big_number_total",
            {
                "metric": metric("Monitored Zones", "COUNT(DISTINCT zone_id)"),
                "granularity_sqla": "event_time",
                "y_axis_format": "SMART_NUMBER",
            },
        ),
    ]
    historical_charts = [
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_daily_zone_activity"],
            "Daily GPS Records",
            "echarts_timeseries_bar",
            {
                "x_axis": "event_time",
                "granularity_sqla": "event_time",
                "time_grain_sqla": "P1D",
                "metrics": [metric("GPS Records", "SUM(gps_record_count)")],
                "groupby": [],
                "row_limit": 100,
                "show_legend": False,
                "show_value": True,
                "y_axis_title": "GPS records",
            },
            legacy_titles=("Historical | Positions Over Time",),
        ),
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_daily_zone_activity"],
            "GPS Records by Zone",
            "echarts_timeseries_bar",
            {
                "x_axis": "zone_name",
                "granularity_sqla": "event_time",
                "metrics": [metric("GPS Records", "SUM(gps_record_count)")],
                "groupby": [],
                "row_limit": 100,
                "orientation": "horizontal",
                "sort_metric": metric("GPS Records", "SUM(gps_record_count)"),
                "x_axis_sort": "GPS Records",
                "x_axis_sort_asc": False,
                "show_legend": False,
                "show_value": True,
                "x_axis_title": "GPS records",
            },
            legacy_titles=("Historical | Positions by Zone",),
        ),
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_spatial_activity"],
            "GPS Activity Clusters",
            "deck_scatter",
            {
                "spatial": {"type": "latlong", "latCol": "latitude", "lonCol": "longitude"},
                "dimension": "zone_name",
                "granularity_sqla": "event_time",
                "row_limit": 5_000,
                "mapbox_style": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                "viewport": {"longitude": 55.24, "latitude": 25.12, "zoom": 9, "bearing": 0, "pitch": 0},
                "point_radius_fixed": {
                    "type": "metric",
                    "value": metric("GPS Records", "SUM(gps_record_count)"),
                },
                "point_unit": "pixels",
                "min_radius": 3,
                "max_radius": 28,
                "multiplier": 1,
                "color_scheme": "supersetColors",
            },
            legacy_titles=("Historical | Position Map",),
        ),
        ensure_chart(
            api,
            historical_id,
            datasets["v_batch_daily_zone_activity"],
            "GPS vs Camera Records by Zone",
            "echarts_timeseries_bar",
            {
                "x_axis": "zone_name",
                "granularity_sqla": "event_time",
                "metrics": [
                    metric("GPS Records", "SUM(gps_record_count)"),
                    metric("Camera Events", "SUM(camera_event_count)"),
                ],
                "groupby": [],
                "row_limit": 100,
                "orientation": "vertical",
                "sort_metric": metric("GPS Records", "SUM(gps_record_count)"),
                "x_axis_sort": "GPS Records",
                "x_axis_sort_asc": False,
                "show_legend": True,
                "show_value": True,
                "y_axis_title": "Record count",
            },
        ),
    ]
    configure_dashboard(
        api,
        historical_id,
        historical_kpis + historical_charts,
        position_json=historical_dashboard_layout(historical_kpis, historical_charts),
        json_metadata=historical_metadata(datasets["v_batch_daily_zone_activity"]),
    )

    live_id = ensure_dashboard(api, LIVE_DASHBOARD, "emcode-live-monitoring")
    live_charts = [
        ensure_chart(
            api,
            live_id,
            datasets["v_live_vehicle_latest"],
            "Live | Latest Vehicle Locations",
            "deck_scatter",
            {
                "spatial": {"type": "latlong", "latCol": "latitude", "lonCol": "longitude"},
                "dimension": "vehicle_id",
                "row_limit": 1_000,
                "mapbox_style": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                "viewport": {"longitude": 55.24, "latitude": 25.12, "zoom": 9, "bearing": 0, "pitch": 0},
                "point_radius_fixed": {"type": "fix", "value": 8},
                "point_unit": "pixels",
                "color_scheme": "supersetColors",
            },
        ),
        ensure_chart(
            api,
            live_id,
            datasets["v_live_events_recent"],
            "Live | Camera Events Last 15 Minutes",
            "big_number_total",
            {
                "metric": metric("Camera Events"),
                "granularity_sqla": "event_time",
                "time_range": "Last 15 minutes",
                "y_axis_format": "SMART_NUMBER",
            },
        ),
        ensure_chart(
            api,
            live_id,
            datasets["v_live_events_recent"],
            "Live | Camera Event Timeline",
            "echarts_timeseries_line",
            {
                "x_axis": "event_time",
                "time_grain_sqla": "PT1M",
                "metrics": [metric("Camera Events")],
                "groupby": ["event_category"],
                "time_range": "Last 30 minutes",
                "row_limit": 10_000,
                "show_legend": True,
            },
        ),
        ensure_chart(
            api,
            live_id,
            datasets["v_live_events_recent"],
            "Live | Camera Events by Zone",
            "pie",
            {
                "groupby": ["zone_name"],
                "metric": metric("Camera Events"),
                "granularity_sqla": "event_time",
                "time_range": "Last 30 minutes",
                "row_limit": 100,
                "sort_by_metric": True,
                "donut": True,
            },
        ),
        ensure_chart(
            api,
            live_id,
            datasets["v_zone_activity_5m"],
            "Live | Zone Vehicle and Camera Activity",
            "echarts_timeseries_bar",
            {
                "x_axis": "window_start",
                "time_grain_sqla": "PT5M",
                "metrics": [
                    metric("Vehicle Positions", "SUM(vehicle_position_count)"),
                    metric("Camera Events", "SUM(camera_event_count)"),
                ],
                "groupby": ["zone_name"],
                "adhoc_filters": [
                    {
                        "clause": "WHERE",
                        "comparator": "stream",
                        "expressionType": "SIMPLE",
                        "operator": "==",
                        "sqlExpression": None,
                        "subject": "ingestion_mode",
                    }
                ],
                "time_range": "Last 30 minutes",
                "row_limit": 10_000,
                "show_legend": True,
            },
        ),
    ]
    configure_dashboard(api, live_id, live_charts)
    print(
        f"Provisioned 2 dashboards, {len(datasets)} datasets, and "
        f"{len(historical_kpis) + len(historical_charts) + len(live_charts)} charts",
        flush=True,
    )


if __name__ == "__main__":
    main()

