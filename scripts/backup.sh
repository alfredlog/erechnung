#!/usr/bin/env bash
# Nightly backup of E-Rechnung Agent: database dump + all original invoice files.
# Keeps 30 days. Run from cron on the host:
#   15 3 * * * ~/apps/erechnung/scripts/backup.sh >> ~/backup.log 2>&1
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/erechnung}"
KEEP_DAYS="${KEEP_DAYS:-30}"
DAY="$(date +%F)"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"                    # invoices and bank data: only for this user
cd "$APP_DIR"

echo "$(date -Is) backup started"

# 1. Database. Written to a temporary file first, so a failed dump never replaces a good one.
tmp="$BACKUP_DIR/.db-$DAY.sql.gz.part"
docker compose exec -T db pg_dump -U erechnung --no-owner erechnung | gzip > "$tmp"
if [ "$(stat -c %s "$tmp")" -lt 1000 ]; then
  echo "database dump too small, keeping the old backups" >&2
  rm -f "$tmp"
  exit 1
fi
mv "$tmp" "$BACKUP_DIR/db-$DAY.sql.gz"

# 2. Original invoice files (the /data volume of the app), read through the api container.
tmp="$BACKUP_DIR/.files-$DAY.tgz.part"
docker compose exec -T api tar czf - -C /data . > "$tmp"
mv "$tmp" "$BACKUP_DIR/files-$DAY.tgz"

# 3. Remove backups older than KEEP_DAYS days.
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'db-*.sql.gz' -o -name 'files-*.tgz' \) -mtime +"$KEEP_DAYS" -delete

echo "$(date -Is) backup done: $(du -sh "$BACKUP_DIR/db-$DAY.sql.gz" | cut -f1) database," \
     "$(du -sh "$BACKUP_DIR/files-$DAY.tgz" | cut -f1) files"
