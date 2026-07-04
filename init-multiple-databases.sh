#!/bin/bash
# init-multiple-databases.sh
set -e
# Split comma-separated string into an array
IFS=',' read -ra DB_ARRAY <<< "$POSTGRES_MULTIPLE_DATABASES"

for db in "${DB_ARRAY[@]}"; do
    # Remove leading/trailing whitespaces
    db=$(echo "$db" | xargs)
    
    # Generate CREATE DATABASE query only if it does not exist, then pipe to \gexec
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
        SELECT 'CREATE DATABASE "$db"' 
        WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db') 
        \gexec
EOSQL
done