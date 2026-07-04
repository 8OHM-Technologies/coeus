#!/bin/bash
# init-multiple-databases.sh
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    $(echo $POSTGRES_MULTIPLE_DATABASES | tr ',' '\n' | xargs -I {} echo "CREATE DATABASE \"{}\";")
EOSQL
