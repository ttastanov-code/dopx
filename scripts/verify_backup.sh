#!/usr/bin/env bash
# scripts/verify_backup.sh — проверка, что бэкап базы реально восстанавливается.
#
#   scripts/verify_backup.sh                       последний backups/db_*.sql.gz
#   scripts/verify_backup.sh backups/db_X.sql.gz   конкретный дамп
#
# Поднимает временный postgres:16 (как в проде), заливает дамп, сверяет ключевые таблицы
# (есть и не пустые), удаляет контейнер. Боевую базу не трогает. Код выхода 0 — бэкап годный.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

DUMP="${1:-$(ls -1t backups/db_*.sql.gz 2>/dev/null | head -n 1)}"
[ -n "$DUMP" ] && [ -f "$DUMP" ] || { echo "Нет дампа для проверки"; exit 1; }
NAME="dopx-restore-check-$$"
# Обязательные — без них сайт пуст; остальные только показываем (до сезона оценок может не быть).
REQUIRED="matches_match players_player teams_team users_user"
INFO="events_matchevent evaluations_evaluationsession aggregates_playermatchaggregate"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[$(date '+%F %T')] Проверка восстановления: $DUMP"
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=check -e POSTGRES_DB=check postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do
    docker exec "$NAME" pg_isready -U postgres -d check >/dev/null 2>&1 && break
    sleep 1
done
# ON_ERROR_STOP — любая ошибка SQL проваливает проверку, а не проходит молча.
gunzip -c "$DUMP" | docker exec -i "$NAME" psql -q -v ON_ERROR_STOP=1 -U postgres -d check >/dev/null

count() { docker exec "$NAME" psql -tA -U postgres -d check -c "select count(*) from $1" 2>/dev/null || echo "нет"; }
failed=0
for t in $REQUIRED; do
    n="$(count "$t")"
    if [ "$n" = "нет" ] || [ "$n" = "0" ]; then echo "  ✗ $t: $n"; failed=1; else echo "  ✓ $t: $n"; fi
done
for t in $INFO; do
    n="$(count "$t")"
    [ "$n" = "нет" ] && { echo "  ✗ $t: таблицы нет"; failed=1; } || echo "  · $t: $n"
done
[ "$failed" = "0" ] && echo "[$(date '+%F %T')] Бэкап годный." || { echo "[$(date '+%F %T')] БЭКАП НЕ ГОДИТСЯ"; exit 1; }
