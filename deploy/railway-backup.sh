#!/usr/bin/env bash
# Körs lokalt efter `railway login` och `railway link` (projekt + tjänst):
#   bash deploy/railway-backup.sh [--documents] [utkatalog]
#
# Tar en konsistent kopia av produktionsdatabasen med SQLites backup-API
# inne i Railway-containern, för över den som gzip+base64 (tål terminalens
# textkanal) och verifierar kontrollsumma och integrity_check lokalt.
# Skriver aldrig till produktionen utöver en temporär fil i /tmp.
set -euo pipefail

WITH_DOCUMENTS=0
if [ "${1:-}" = "--documents" ]; then WITH_DOCUMENTS=1; shift; fi
OUT="${1:-backups}"
mkdir -p "$OUT"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
db_out="$OUT/screener-$stamp.db"

remote_db='
import base64, gzip, hashlib, sqlite3, sys
src = sqlite3.connect("file:/app/data/screener.db?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect("/tmp/screener-backup.db")
src.backup(dst)
dst.close(); src.close()
raw = open("/tmp/screener-backup.db", "rb").read()
print("SHA256:" + hashlib.sha256(raw).hexdigest())
print("BEGIN-BACKUP")
print(base64.b64encode(gzip.compress(raw)).decode())
print("END-BACKUP")
'

echo "Hämtar databasen från Railway…"
timeout 170 railway ssh -- python3 -c "$remote_db" | tr -d '\r' > "$OUT/.transfer-$stamp.txt"

expected="$(sed -n 's/^SHA256://p' "$OUT/.transfer-$stamp.txt")"
sed -n '/^BEGIN-BACKUP$/,/^END-BACKUP$/p' "$OUT/.transfer-$stamp.txt" \
  | sed '1d;$d' | tr -d '\n' | base64 -d | gunzip > "$db_out"
rm -f "$OUT/.transfer-$stamp.txt"

actual="$(sha256sum "$db_out" | cut -d' ' -f1)"
[ -n "$expected" ] && [ "$expected" = "$actual" ] \
  || { echo "Kontrollsumman stämmer inte (förväntad $expected, fick $actual)"; exit 1; }

python3 - "$db_out" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
check = connection.execute("PRAGMA integrity_check").fetchone()[0]
if check != "ok":
    sys.exit(f"integrity_check: {check}")
for table in ("issuers", "documents", "financing_events", "runway_assessments"):
    print(f"{table}: {connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
PY
echo "Verifierad databasbackup: $db_out ($actual)"

if [ "$WITH_DOCUMENTS" = 1 ]; then
  echo "Storlek på dokumentkatalogen i Railway:"
  timeout 60 railway ssh -- du -sh /app/data/documents
  docs_out="$OUT/documents-$stamp.tar.gz"
  remote_docs='
import base64, hashlib, io, sys, tarfile
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
    archive.add("/app/data/documents", arcname="documents")
raw = buffer.getvalue()
print("SHA256:" + hashlib.sha256(raw).hexdigest())
print("BEGIN-BACKUP")
print(base64.b64encode(raw).decode())
print("END-BACKUP")
'
  timeout 170 railway ssh -- python3 -c "$remote_docs" | tr -d '\r' > "$OUT/.transfer-docs-$stamp.txt"
  expected="$(sed -n 's/^SHA256://p' "$OUT/.transfer-docs-$stamp.txt")"
  sed -n '/^BEGIN-BACKUP$/,/^END-BACKUP$/p' "$OUT/.transfer-docs-$stamp.txt" \
    | sed '1d;$d' | tr -d '\n' | base64 -d > "$docs_out"
  rm -f "$OUT/.transfer-docs-$stamp.txt"
  actual="$(sha256sum "$docs_out" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || { echo "Dokumentarkivets kontrollsumma stämmer inte"; exit 1; }
  tar -tzf "$docs_out" >/dev/null
  echo "Verifierat dokumentarkiv: $docs_out"
fi
