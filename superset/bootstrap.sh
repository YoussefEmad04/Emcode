#!/usr/bin/env bash
set -Eeuo pipefail

echo "Running Superset metadata migrations"
superset db upgrade

echo "Ensuring the local admin account exists"
users_output="$(superset fab list-users)"
if grep -Fq "username:${SUPERSET_ADMIN_USERNAME} |" <<<"${users_output}"; then
  echo "Superset admin ${SUPERSET_ADMIN_USERNAME} already exists"
else
  superset fab create-admin \
    --username "${SUPERSET_ADMIN_USERNAME}" \
    --firstname EMCODE \
    --lastname Admin \
    --email "${SUPERSET_ADMIN_EMAIL}" \
    --password "${SUPERSET_ADMIN_PASSWORD}"
fi

echo "Synchronizing Superset roles and permissions"
superset init

echo "Starting Superset web server"
gunicorn \
  --bind 0.0.0.0:8088 \
  --workers 2 \
  --worker-class gthread \
  --threads 8 \
  --timeout 120 \
  --limit-request-line 0 \
  "superset.app:create_app()" &
server_pid=$!
trap 'kill -TERM "${server_pid}" 2>/dev/null || true; wait "${server_pid}" 2>/dev/null || true' TERM INT

python - <<'PY'
import time
import urllib.request

for attempt in range(1, 61):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8088/health", timeout=5) as response:
            if response.status == 200:
                print("Superset health endpoint is ready", flush=True)
                break
    except Exception as exc:
        print(f"Waiting for Superset HTTP ({attempt}/60): {exc}", flush=True)
        time.sleep(2)
else:
    raise SystemExit("Superset HTTP did not become ready")
PY

echo "Provisioning EMCODE database, datasets, charts, and dashboards"
python -m app.provision_superset

echo "Superset bootstrap and asset provisioning completed"
wait "${server_pid}"

