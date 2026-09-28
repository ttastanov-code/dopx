#!/usr/bin/env bash
# scripts/deploy.sh — обновление прода до коммита.
#
#   scripts/deploy.sh [SHA]      без SHA — последний коммит origin/main
#
# Версия (1.2.3) приходит из GitHub Actions в DEPLOY_VERSION. При ручном запуске берётся
# из git-тегов: у помеченного коммита — сам тег, иначе «последний тег-N-gхеш».
#
# Порядок: блокировка → код → сборка образа (старый сайт работает) → бэкап БД → миграции →
# перезапуск → ожидание /healthz/ с новой версией. Не поднялось — откат на прошлый коммит.
# Лог: logs/deploy/<время>_<sha>.log, журнал для дашборда: logs/deploy/history.jsonl.
# Уведомление в Telegram, если в .env заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.
set -Eeuo pipefail

# DEPLOY_ROOT — когда скрипт запущен не из папки проекта (GitHub Actions берёт его из нового коммита).
cd "${DEPLOY_ROOT:-$(dirname "$0")/..}"
ROOT="$(pwd)"
BRANCH="${DEPLOY_BRANCH:-main}"
TARGET="${1:-}"
LOG_DIR="$ROOT/logs/deploy"
BACKUP_DIR="$ROOT/backups"
HEALTH_URL="http://127.0.0.1:${NGINX_PORT:-8080}/healthz/"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
KEEP_PRE_DEPLOY_BACKUPS=10

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_TS=$(date +%s)
LOG_FILE="$LOG_DIR/$(date +%Y%m%d-%H%M%S)_pending.log"
exec > >(tee -a "$LOG_FILE") 2>&1

# Одновременно — только один деплой (flock на Linux, каталог-замок там, где его нет).
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOG_DIR/.lock"
    flock -n 9 || { echo "Другой деплой ещё идёт — выходим."; exit 75; }
else
    mkdir "$LOG_DIR/.lock.d" 2>/dev/null || { echo "Другой деплой ещё идёт — выходим."; exit 75; }
    trap 'rmdir "$LOG_DIR/.lock.d" 2>/dev/null || true' EXIT
fi

env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true; }
HOST_HEADER="$(env_value ALLOWED_HOSTS | cut -d, -f1)"
TG_TOKEN="$(env_value TELEGRAM_BOT_TOKEN)"
TG_CHAT="$(env_value TELEGRAM_CHAT_ID)"

step() { echo; echo "==> [$(date +%H:%M:%S)] $*"; }

notify() {
    [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] || return 0
    curl -fsS -m 10 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" -d disable_web_page_preview=true >/dev/null || true
}

record() {
    # Строка в журнал деплоев: его читает дашборд («Системный статус»).
    # Данные — через переменные окружения: текст коммита не должен попадать в код скрипта.
    R_STATUS="$1" R_MESSAGE="$2" R_DURATION=$(( $(date +%s) - START_TS )) R_STARTED="$STARTED_AT" \
    R_SHA="${NEW_SHA:-}" R_PREV="${PREV_SHA:-}" R_COMMIT="${COMMIT_MSG:-}" R_LOG="$(basename "${FINAL_LOG:-$LOG_FILE}")" \
    R_VERSION="${NEW_VERSION:-}" \
    python3 - "$LOG_DIR/history.jsonl" <<'PY'
import json, os, sys
e = os.environ
entry = {
    "started_at": e["R_STARTED"], "status": e["R_STATUS"], "sha": e["R_SHA"], "previous_sha": e["R_PREV"],
    "version": e["R_VERSION"], "commit_message": e["R_COMMIT"][:200], "duration_sec": int(e["R_DURATION"]),
    "message": e["R_MESSAGE"][:300], "log": e["R_LOG"],
}
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
PY
}

wait_healthy() {
    # Ждём, пока /healthz/ ответит 200 с нужной версией (через nginx, как увидят посетители).
    local want="$1" deadline=$(( $(date +%s) + HEALTH_TIMEOUT )) body=""
    while [ "$(date +%s)" -lt "$deadline" ]; do
        body="$(curl -fsS -m 5 -H "Host: ${HOST_HEADER:-localhost}" -H 'X-Forwarded-Proto: https' "$HEALTH_URL" 2>/dev/null || true)"
        if echo "$body" | grep -q "\"commit\": \"$want\"" && ! echo "$body" | grep -q '"status": "fail"'; then
            echo "Сайт отвечает: $body"
            return 0
        fi
        sleep 5
    done
    echo "Не дождались здоровой версии $want за ${HEALTH_TIMEOUT} с. Последний ответ: ${body:-нет ответа}"
    return 1
}

version_of() {
    # Версия коммита по тегам v1.2.3; без тегов — «0.0.0-хеш».
    git describe --tags --match 'v[0-9]*' "$1" 2>/dev/null | sed 's/^v//' || echo "0.0.0-$1"
}

build_and_up() {
    local sha="$1"
    export GIT_SHA="$sha" APP_VERSION="$2"
    step "Сборка образа $sha (сайт пока работает на старой версии)"
    docker compose build
    step "Бэкап базы перед миграциями"
    # На первом деплое база ещё не запущена — поднимаем её отдельно.
    docker compose up -d --wait db redis
    local dump
    dump="$BACKUP_DIR/pre-deploy_$(date +%Y%m%d-%H%M%S)_${sha}.sql.gz"
    docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' | gzip > "$dump"
    echo "Бэкап: $dump ($(du -h "$dump" | cut -f1))"
    ls -1t "$BACKUP_DIR"/pre-deploy_*.sql.gz 2>/dev/null | tail -n +$((KEEP_PRE_DEPLOY_BACKUPS + 1)) | xargs -r rm -f
    step "Миграции"
    docker compose run --rm -T web python manage.py migrate --noinput
    step "Перезапуск сервисов"
    docker compose up -d --remove-orphans
}

on_error() {
    local line="$1"
    trap - ERR
    echo "Ошибка на строке $line."
    if [ -n "${PREV_SHA:-}" ] && [ "${ROLLED:-}" != "1" ] && [ "${CODE_SWITCHED:-}" = "1" ]; then
        ROLLED=1
        step "ОТКАТ на $PREV_SHA"
        git reset --hard "$PREV_SHA"
        export GIT_SHA="$PREV_SHA" APP_VERSION
        APP_VERSION="$(version_of "$PREV_SHA")"
        if docker compose build && docker compose up -d --remove-orphans && wait_healthy "$PREV_SHA"; then
            finish "rolled_back" "Новая версия не поднялась, вернули $PREV_SHA. Миграции не откатываются — проверьте лог."
            notify "⚠️ DOPX: версия ${NEW_VERSION} (${NEW_SHA}) не поднялась, откатились на ${PREV_SHA}.
${COMMIT_MSG}
Лог: logs/deploy/$(basename "$FINAL_LOG")"
            exit 1
        fi
    fi
    finish "failed" "Деплой упал на строке $line"
    notify "❌ DOPX: деплой ${NEW_SHA:-?} упал. Сайт может не работать!
${COMMIT_MSG:-}
Лог: logs/deploy/$(basename "$FINAL_LOG")"
    exit 1
}

finish() {
    FINAL_LOG="$LOG_DIR/$(basename "$LOG_FILE" | sed "s/_pending/_${NEW_SHA:-unknown}/")"
    [ "$LOG_FILE" != "$FINAL_LOG" ] && cp "$LOG_FILE" "$FINAL_LOG" && rm -f "$LOG_FILE"
    record "$1" "$2"
    echo "Итог: $1 — $2 ($(( $(date +%s) - START_TS )) с)"
}

trap 'on_error $LINENO' ERR

step "Код: ветка $BRANCH"
PREV_SHA="$(git rev-parse --short HEAD)"
git fetch --prune --tags origin "$BRANCH"
TARGET="${TARGET:-origin/$BRANCH}"
NEW_SHA="$(git rev-parse --short "$TARGET")"
COMMIT_MSG="$(git log -1 --format='%s (%an)' "$TARGET")"
NEW_VERSION="${DEPLOY_VERSION:-$(version_of "$NEW_SHA")}"
echo "Было: $PREV_SHA → станет: $NEW_SHA, версия $NEW_VERSION — $COMMIT_MSG"
git reset --hard "$TARGET"
CODE_SWITCHED=1

build_and_up "$NEW_SHA" "$NEW_VERSION"

step "Проверка, что сайт поднялся"
wait_healthy "$NEW_SHA" || false

step "Уборка старых образов"
docker image prune -f >/dev/null
docker images 'dopx-app' --format '{{.Tag}}' | grep -v -e "^${NEW_SHA}$" -e "^${PREV_SHA}$" | xargs -r -I{} docker rmi "dopx-app:{}" >/dev/null 2>&1 || true

trap - ERR
finish "success" "Обновлено до версии $NEW_VERSION ($NEW_SHA)"
notify "✅ DOPX обновлён до версии ${NEW_VERSION} (${PREV_SHA} → ${NEW_SHA})
${COMMIT_MSG}
Заняло $(( $(date +%s) - START_TS )) с"
