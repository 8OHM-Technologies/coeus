FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY requirements-app.txt .
RUN pip install --no-cache-dir -r requirements-app.txt

COPY control_plane/ ./control_plane/
COPY extraction_workers/ ./extraction_workers/

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]

CMD ["gunicorn", "core.wsgi:application", "--chdir", "control_plane", "--bind", "0.0.0.0:8001", "--workers", "3"]
