FROM python:3.12.7-slim-bookworm

WORKDIR /app
RUN pip install --no-cache-dir psycopg2-binary==2.9.11
COPY app /app/app
COPY sql /app/sql

CMD ["python", "-m", "app.stream"]

