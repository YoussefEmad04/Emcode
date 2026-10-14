#!/usr/bin/env sh
set -eu
docker compose exec simulator python -m app.verify

