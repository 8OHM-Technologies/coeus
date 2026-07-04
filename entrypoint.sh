#!/bin/sh
set -e

echo "Running container environment setup..."

while ! (timeout 1 bash -c '</dev/null > /dev/tcp/postgres/5432') 2>/dev/null; do
  echo "Waiting for Postgres..."
  sleep 1
done

echo "Applying database migrations..."
python control_plane/manage.py migrate

echo "Creating superuser..."
DJANGO_SUPERUSER_USERNAME=${DJANGO_SUPERUSER_USERNAME:-admin} \
DJANGO_SUPERUSER_EMAIL=${DJANGO_SUPERUSER_EMAIL:-admin@example.com} \
DJANGO_SUPERUSER_PASSWORD=${DJANGO_SUPERUSER_PASSWORD:-DefaultSecurePass123!} \
python control_plane/manage.py createsuperuser --no-input || echo "Superuser already exists or creation failed."

echo "Collecting static files..."
python control_plane/manage.py collectstatic --noinput

echo "Setup complete. Handing over to CMD..."

exec "$@"
