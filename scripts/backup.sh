#!/usr/bin/env bash
# scripts/backup.sh — ежедневный бэкап: база (pg_dump) и медиа (фото, баннеры, аватары).
#
#   scripts/backup.sh            база каждый раз, медиа — по воскресеньям
#   scripts/backup.sh --media    база и медиа сейчас
#
# Хранение: база — BACKUP_KEEP_DAYS дней (по умолчанию 14), медиа — 4 последних архива.
# В cron (aaPanel → Cron → Shell Script), каждый день в 04:30:
#   cd /www/dopx && scripts/backup.sh >> logs/backup.log 2>&1
set -Eeuo pipefail
cd "$(dirname "$0")/.."

DIR="backups"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DIR"

echo "[$(date '+%F %T')] Бэкап базы…"
DUMP="$DIR/db_${STAMP}.sql.gz"
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' | gzip > "$DUMP"
# Пустой или битый архив — ошибка, а не «успех».
gzip -t "$DUMP"
[ "$(stat -c %s "$DUMP" 2>/dev/null || stat -f %z "$DUMP")" -gt 1024 ] || { echo "Дамп подозрительно маленький: $DUMP"; exit 1; }
echo "  $DUMP ($(du -h "$DUMP" | cut -f1))"
find "$DIR" -name 'db_*.sql.gz' -mtime +"$KEEP_DAYS" -delete

if [ "${1:-}" = "--media" ] || [ "$(date +%u)" = "7" ]; then
    echo "[$(date '+%F %T')] Бэкап медиа…"
    tar -czf "$DIR/media_${STAMP}.tar.gz" media
    ls -1t "$DIR"/media_*.tar.gz | tail -n +5 | xargs -r rm -f
fi
echo "[$(date '+%F %T')] Готово."
