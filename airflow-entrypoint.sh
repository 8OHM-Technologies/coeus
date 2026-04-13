#!/bin/bash
# airflow-entrypoint.sh

# 1. Start Airflow in the background so we can run commands against it
airflow standalone &

# 2. Wait a few seconds for the database to initialize and the admin user to be auto-created
echo "Waiting for Airflow to initialize..."
sleep 15

# 3. Force the admin password to 'admin'
echo "Setting admin password to 'admin'..."
airflow users password --username admin --password admin

# 4. Bring the background process back to the foreground so the container stays alive
wait
