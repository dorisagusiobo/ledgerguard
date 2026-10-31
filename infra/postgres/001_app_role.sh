#!/bin/sh
# Run by the official entrypoint on first initialization. Never echo secrets.
# Use psql's quoted SQL variable, not shell interpolation into SQL.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_password LEDGERGUARD_APP_PASSWORD
CREATE ROLE ledgerguard_app LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
SQL
