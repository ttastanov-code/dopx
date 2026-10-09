#!/usr/bin/env bash
# scripts/launch-export.sh — на СВОЁМ компьютере перед запуском: упаковать данные для сервера.
#
#   scripts/launch-export.sh           → launch-bundle.tar.gz в папке проекта
#
# Что внутри: база (команды, игроки, матчи, статистика Sportmonks, правки ФИО от Gemini, ваш
# аккаунт сотрудника), media/ (фото, логотипы, баннеры) и data/ (архив сырых ответов API — переимпорт без доступа к API). Перед выгрузкой сделайте
# «Чистый старт» в дашборде (Скрипты → reset_user_activity), чтобы не везти тестовую активность.
# .env и ключи сюда не входят: на сервере свой .env.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'; }
DB_NAME="$(env_value DB_NAME)"; DB_USER="$(env_value DB_USER)"
DB_HOST="$(env_value DB_HOST)"; DB_PORT="$(env_value DB_PORT)"
PGPASSWORD="$(env_value DB_PASSWORD)"
export PGPASSWORD

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Выгружаю базу $DB_NAME…"
pg_dump -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" -U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges \
    | gzip > "$TMP/db.sql.gz"
gzip -t "$TMP/db.sql.gz"

echo "Упаковываю media…"
tar -czf "$TMP/media.tar.gz" media
if [ -d data ]; then
    echo "Упаковываю архив данных API…"
    tar -czf "$TMP/data.tar.gz" data
fi

git rev-parse --short HEAD > "$TMP/COMMIT"
date -u +%Y-%m-%dT%H:%M:%SZ > "$TMP/CREATED"
tar -czf launch-bundle.tar.gz -C "$TMP" .
echo "Готово: launch-bundle.tar.gz ($(du -h launch-bundle.tar.gz | cut -f1))."
echo "Отправить на сервер:  scp launch-bundle.tar.gz <пользователь>@<сервер>:/www/dopx/"
