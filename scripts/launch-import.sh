#!/usr/bin/env bash
# scripts/launch-import.sh — на СЕРВЕРЕ один раз, до первого scripts/deploy.sh.
#
#   scripts/launch-import.sh launch-bundle.tar.gz
#
# Поднимает только базу, загружает в неё дамп и распаковывает media/. Если в базе уже есть
# данные — спросит подтверждение: загрузка их заменит.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
BUNDLE="${1:?Укажите файл: scripts/launch-import.sh launch-bundle.tar.gz}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$BUNDLE" -C "$TMP"
echo "Пакет от $(cat "$TMP/CREATED"), коммит $(cat "$TMP/COMMIT")."

echo "Запускаю базу…"
docker compose up -d --wait db

tables=$(docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select count(*) from information_schema.tables where table_schema = '"'"'public'"'"'"')
if [ "$tables" -gt 0 ]; then
    read -r -p "В базе уже $tables таблиц. Стереть и загрузить из пакета? Введите yes: " answer
    [ "$answer" = "yes" ] || { echo "Отменено."; exit 1; }
    docker compose stop web celery_worker celery_realtime celery_beat 2>/dev/null || true
    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "drop schema public cascade; create schema public;"'
fi

echo "Загружаю базу…"
gunzip -c "$TMP/db.sql.gz" | docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null

echo "Распаковываю media…"
tar -xzf "$TMP/media.tar.gz"
if [ -f "$TMP/data.tar.gz" ]; then
    echo "Распаковываю архив данных API…"
    tar -xzf "$TMP/data.tar.gz"
    chown -R 1000:1000 data 2>/dev/null || true
fi
# Приложение в контейнерах пишет в media от UID 1000.
chown -R 1000:1000 media 2>/dev/null || echo "Не хватило прав на chown. Выполните: sudo chown -R 1000:1000 media"

echo "Готово. Дальше: scripts/deploy.sh — он накатит миграции и запустит сайт."
