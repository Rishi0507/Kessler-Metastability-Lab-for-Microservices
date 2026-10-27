#!/bin/sh
set -eu
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "cloning primary with pg_basebackup"
  export PGPASSWORD="$REPLICATION_PASSWORD"
  until pg_basebackup -h postgres -p 5432 -U replicator -D "$PGDATA" -R -X stream -c fast; do
    echo "primary not ready, retrying in 2s"
    sleep 2
  done
  chmod 0700 "$PGDATA"
fi
exec postgres -c hot_standby=on
