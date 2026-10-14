from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class SupersetAPI:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("SUPERSET_URL", "http://superset:8088")).rstrip("/")
        self.access_token = ""
        self.csrf_token = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method: str, path: str, payload=None, authenticate: bool = True):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if authenticate and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if authenticate and self.csrf_token and method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers["X-CSRFToken"] = self.csrf_token
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method.upper()
        )
        for attempt in range(5):
            try:
                with self.opener.open(request, timeout=60) as response:
                    body = response.read()
                    return response.status, json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < 4:
                    # Superset 6.x applies a process-local 50 req/s limit. A
                    # single full-window wait lets idempotent bootstrap proceed.
                    time.sleep(1.05)
                    continue
                raise RuntimeError(f"Superset {method} {path} returned HTTP {exc.code}: {body}") from exc

    def login(self) -> None:
        username = os.getenv("SUPERSET_ADMIN_USERNAME", "admin")
        password = os.getenv("SUPERSET_ADMIN_PASSWORD", "admin")
        _, response = self.request(
            "POST",
            "/api/v1/security/login",
            {"username": username, "password": password, "provider": "db", "refresh": True},
            authenticate=False,
        )
        self.access_token = response["access_token"]
        _, csrf = self.request("GET", "/api/v1/security/csrf_token/")
        self.csrf_token = csrf["result"]

    def find_one(self, resource: str, column: str, value):
        query = urllib.parse.quote(
            json.dumps(
                {
                    "filters": [{"col": column, "opr": "eq", "value": value}],
                    "page_size": 100,
                },
                separators=(",", ":"),
            )
        )
        _, response = self.request("GET", f"/api/v1/{resource}/?q={query}")
        results = response.get("result", [])
        return results[0] if results else None

    def list_all(self, resource: str):
        query = urllib.parse.quote(json.dumps({"page_size": 100}, separators=(",", ":")))
        _, response = self.request("GET", f"/api/v1/{resource}/?q={query}")
        return response.get("result", [])

