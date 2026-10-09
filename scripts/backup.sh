#!/usr/bin/env bash
# scripts/backup.sh — ежедневный бэкап: база (pg_dump), медиа (фото, баннеры, аватары) и архив данных API (data/).
#
#   scripts/backup.sh            база каждый раз; по воскресеньям — медиа с data/ и проверка восстановления
#   scripts/backup.sh --media    база, медиа с data/ и проверка сейчас
#
# Хранение: база — BACKUP_KEEP_DAYS дней (по умолчанию 14), медиа — 4 последних архива.
# BACKUP_RCLONE_REMOTE (например, gdrive:dopx-backups) — копия за пределы сервера через rclone.
# В cron (aaPanel → Cron → Shell Script), каждый день в 04:30:
#   cd /www/dopx && scripts/backup.sh >> logs/backup.log 2>&1
set -Eeuo pipefail
cd "$(dirname "$0")/.."
# cron не читает .env — берём из него только BACKUP_*.
[ -f .env ] && eval "$(grep -E '^BACKUP_[A-Z_]+=' .env | sed 's/^/export /')"

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
    echo "[$(date '+%F %T')] Бэкап медиа и архива данных…"
    tar -czf "$DIR/media_${STAMP}.tar.gz" media $( [ -d data ] && echo data )
    ls -1t "$DIR"/media_*.tar.gz | tail -n +5 | xargs -r rm -f
    # Раз в неделю — реально восстанавливаем свежий дамп во временную базу.
    scripts/verify_backup.sh "$DUMP"
fi

if [ -n "${BACKUP_RCLONE_REMOTE:-}" ]; then
    echo "[$(date '+%F %T')] Копия в $BACKUP_RCLONE_REMOTE…"
    rclone copy "$DIR" "$BACKUP_RCLONE_REMOTE" --max-age 8d
fi
echo "[$(date '+%F %T')] Готово."
