#!/usr/bin/env bash
# The restore drill (T-6.7, REQ-097) — the deliverable of Phase 6.
#
# The point of this phase is not "backups are configured." It is:
#
#     I restored to an empty container and found the DD-214.
#
# Untested backups are a story you tell yourself. This script forces the story
# to be checked: it stands up a *clean* Postgres on a throwaway port, restores
# a backup generation into it, verifies every blob the restored database
# references actually exists in the backup, and then runs a real search for a
# term you supply — defaulting to the DD-214, the document this whole archive
# was built around.
#
# It never touches the live stack. It never writes to the live database. It
# tears its scratch container down on exit, including on failure.
#
# Usage:
#   scripts/restore-drill.sh /path/to/backup/20260828-031500 [search-term]

set -euo pipefail

BACKUP="${1:?usage: restore-drill.sh <backup-directory> [search-term]}"
SEARCH_TERM="${2:-DD-214}"
# Doubled for SQL string literals below.
SEARCH_SQL="${SEARCH_TERM//\'/\'\'}"

CONTAINER="bindery-restore-drill"
DRILL_PORT="${DRILL_PORT:-55432}"
DRILL_PASSWORD="drill-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

step "Checking the backup is intact before trusting it"
[ -d "$BACKUP" ]                 || { red "no such backup directory: $BACKUP"; exit 1; }
[ -f "$BACKUP/bindery.dump" ]    || { red "no bindery.dump in $BACKUP"; exit 1; }
[ -d "$BACKUP/blobs" ]           || { red "no blobs/ in $BACKUP"; exit 1; }
if [ -f "$BACKUP/manifest.json" ]; then
  python3 -c "
import json,sys
m=json.load(open('$BACKUP/manifest.json'))
print(f\"  created {m['created_at']}, {m['blob_count']} blobs, {m['blob_bytes']} bytes\")
integrity=m.get('integrity')
if integrity and not integrity.get('healthy', True):
    print('  WARNING: this backup was taken over a failing integrity check')
"
else
  echo "  (no manifest — an older backup generation)"
fi

step "Starting a clean, empty Postgres on port $DRILL_PORT"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$DRILL_PASSWORD" \
  -e POSTGRES_USER=bindery \
  -e POSTGRES_DB=bindery \
  -p "127.0.0.1:${DRILL_PORT}:5432" \
  "$PG_IMAGE" >/dev/null

printf '  waiting for the empty database'
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U bindery -q 2>/dev/null; then break; fi
  printf '.'; sleep 1
done
echo
docker exec "$CONTAINER" pg_isready -U bindery -q || { red "database never became ready"; exit 1; }

# The extensions must exist before the dump's tables that use them. The live
# stack creates these in postgres-init; a bare postgres:16 image does not.
docker exec "$CONTAINER" psql -U bindery -d bindery -q \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS btree_gist;'
if ! docker exec "$CONTAINER" psql -U bindery -d bindery -qc \
    'CREATE EXTENSION IF NOT EXISTS vector;' 2>/dev/null; then
  red "  pgvector is not in $PG_IMAGE — set PG_IMAGE to your pgvector image and rerun"
  exit 1
fi

step "Restoring the dump into the empty database"
docker cp "$BACKUP/bindery.dump" "$CONTAINER:/tmp/bindery.dump"
docker exec "$CONTAINER" pg_restore \
  --username=bindery --dbname=bindery --no-owner --exit-on-error /tmp/bindery.dump
green "  restore completed without error"

step "Verifying every original the restored database references is in the backup"
docker exec "$CONTAINER" psql -U bindery -d bindery -At \
  -c 'SELECT sha256 FROM source_file' > /tmp/drill-shas.txt
TOTAL=$(wc -l < /tmp/drill-shas.txt | tr -d ' ')
MISSING=0
while read -r sha; do
  [ -n "$sha" ] || continue
  if [ ! -f "$BACKUP/blobs/${sha:0:2}/${sha:2:2}/$sha" ]; then
    red "  MISSING BLOB $sha"
    MISSING=$((MISSING + 1))
  fi
done < /tmp/drill-shas.txt
if [ "$MISSING" -gt 0 ]; then
  red "  $MISSING of $TOTAL originals are not in this backup — it is not restorable"
  exit 1
fi
green "  all $TOTAL originals present"

step "Searching the restored archive for: $SEARCH_TERM"
# Deliberately the same full-text path the application uses, against the
# restored data, not the live data.
docker exec "$CONTAINER" psql -U bindery -d bindery -At -c "
  SELECT d.title || ' | pp. ' || d.page_start || '-' || d.page_end
    FROM document d
    JOIN page p ON p.source_file_id = d.source_file_id
                AND p.page_number BETWEEN d.page_start AND d.page_end
   WHERE d.superseded_at IS NULL
     AND p.text_tsv @@ websearch_to_tsquery('english', '$SEARCH_SQL')
   GROUP BY d.id, d.title, d.page_start, d.page_end
   LIMIT 10;
" > /tmp/drill-hits.txt || true

# Known forms are matched structurally rather than by OCR text, so a DD-214 can
# be found even when the scan reads "DD FORM 214" and defeats the text query.
docker exec "$CONTAINER" psql -U bindery -d bindery -At -c "
  SELECT d.title || ' | pp. ' || d.page_start || '-' || d.page_end || ' | form ' || k.code
    FROM document d JOIN known_form k ON k.id = d.known_form_id
   WHERE d.superseded_at IS NULL
     AND upper(replace(k.code,'-','')) LIKE upper(replace('%$SEARCH_SQL%','-',''))
   LIMIT 10;
" >> /tmp/drill-hits.txt || true

HITS=$(grep -c . /tmp/drill-hits.txt || true)
if [ "${HITS:-0}" -eq 0 ]; then
  red "DRILL FAILED — restored the archive but could not find \"$SEARCH_TERM\" in it."
  red "A backup you cannot retrieve from is not a backup."
  exit 1
fi

echo
sed 's/^/  /' /tmp/drill-hits.txt
echo
green "DRILL PASSED — restored to a clean database and found \"$SEARCH_TERM\" ($HITS matches)."
echo "  Scratch container torn down. The live stack was never touched."
