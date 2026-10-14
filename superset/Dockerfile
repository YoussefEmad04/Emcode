FROM apache/superset:6.1.0

USER root
RUN . /app/.venv/bin/activate && uv pip install --no-cache psycopg2-binary==2.9.11 \
    && mkdir -p /app/superset_home /app/emcode \
    && chown -R superset:superset /app/superset_home /app/emcode
COPY --chown=superset:superset app /app/emcode/app
COPY --chown=superset:superset sql /app/emcode/sql
COPY --chown=superset:superset superset/superset_config.py /app/superset_config.py
COPY --chown=superset:superset superset/bootstrap.sh /app/bootstrap.sh
COPY --chown=superset:superset superset/service-worker.js /app/superset/static/service-worker.js
# Superset 6.1.0's prebuilt frontend still contains an unconditional Scarf pixel
# even when SCARF_ANALYTICS=false. Replace only that known URL so this local
# Phase 1 stack makes no telemetry request and remains compatible with its CSP.
RUN sed -i 's#https://apachesuperset.gateway.scarf.sh/pixel/0d3461e1-abb1-4691-a0aa-5ed50de66af0/${e}/${a}/${t}#data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==#g' /app/superset/static/assets/*entry.js \
    && ! grep -Rq 'apachesuperset.gateway.scarf.sh/pixel' /app/superset/static/assets \
    && chmod +x /app/bootstrap.sh

USER superset
CMD ["/app/bootstrap.sh"]

