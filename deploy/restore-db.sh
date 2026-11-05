#!/usr/bin/env bash
# Körs på servern: deploy/restore-db.sh /srv/capital-radar/backups/screener-<tid>.db [documents.tar.gz]
# Stoppar appen, kontrollerar backupen, lägger den på plats och startar igen.
# En befintlig databas flyttas undan, den skrivs aldrig över.
set -euo pipefail

BACKUP="${1:?sökväg till backup-databasen saknas}"
DOCUMENTS="${2:-}"
DATA=/srv/capital-radar/data
cd /srv/capital-radar
# Starta om på samma version som senast driftsattes, inte på "latest".
export IMAGE_TAG="$(cat .deployed-tag 2>/dev/null || echo latest)"

check="$(sqlite3 "$BACKUP" 'PRAGMA integrity_check;')"
[ "$check" = "ok" ] || { echo "integrity_check misslyckades: $check"; exit 1; }
echo "Backupen är hel: $(sqlite3 "$BACKUP" 'SELECT COUNT(*) FROM issuers;') bolag, $(sqlite3 "$BACKUP" 'SELECT COUNT(*) FROM documents;') dokument"

docker compose stop radar radar-worker litestream 2>/dev/null || docker compose stop radar radar-worker

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
for suffix in "" "-wal" "-shm"; do
  if [ -e "$DATA/radar.db$suffix" ]; then
    mv "$DATA/radar.db$suffix" "/srv/capital-radar/backups/replaced-$stamp.db$suffix"
  fi
done
rm -f "$DATA"/radar.db-wal

cp "$BACKUP" "$DATA/radar.db"
if [ -n "$DOCUMENTS" ]; then
  tar -xzf "$DOCUMENTS" -C "$DATA"
fi
chown -R 1000:1000 "$DATA"

docker compose up -d
for _ in $(seq 1 30); do
  if curl -fsS --max-time 4 http://127.0.0.1:8001/v2/health; then
    echo
    echo "Återställning klar."
    exit 0
  fi
  sleep 2
done
echo "Appen blev inte frisk efter återställningen"
docker compose logs --tail 80 radar
exit 1
