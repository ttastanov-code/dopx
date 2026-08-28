#!/bin/sh
# docker/entrypoint.sh
#
# Точка входа для ВСЕХ контейнеров на базе этого образа: web (gunicorn),
# celery_worker и celery_beat (см. docker-compose.yml, у них разные
# command:, но один и тот же entrypoint). Общая логика — подождать базу
# данных, специфичная для web-роли логика (миграции, статика) — только
# когда реальная команда контейнера это gunicorn, чтобы celery-контейнеры
# не гонялись друг с другом за применением миграций при одновременном
# старте всего стека.
set -e

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"

echo "[entrypoint] Жду базу данных ${DB_HOST}:${DB_PORT}..."
python <<PYEOF
import os
import socket
import sys
import time

host = os.environ.get("DB_HOST", "db")
port = int(os.environ.get("DB_PORT", "5432"))

for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        time.sleep(1)
else:
    print(f"[entrypoint] База {host}:{port} не ответила за 60 секунд", file=sys.stderr)
    sys.exit(1)
PYEOF
echo "[entrypoint] База данных доступна."

# Миграции и collectstatic — только в web-контейнере (команда начинается
# с gunicorn). Если бы это выполнялось в каждом контейнере (web + worker +
# beat), при одновременном первом запуске всего стека три процесса
# одновременно попытались бы накатить одни и те же миграции — Postgres
# это переживёт (миграции идут в транзакциях), но это гонка, которой
# незачем быть. Один явный "владелец" миграций — web.
case "$1" in
    gunicorn)
        echo "[entrypoint] Применяю миграции..."
        python manage.py migrate --noinput

        # collectstatic гоняется на КАЖДОМ старте web-контейнера (не только
        # при первом деплое) — иначе новая статика из свежего образа не
        # подхватится после обновления. Но --clear нельзя гонять каждый
        # раз: static_volume, который он чистит, в этот момент читает live
        # nginx (тот же volume смонтирован туда read-only, см.
        # docker-compose.yml) — рестарт web среди бела дня на секунду-две
        # оставил бы nginx без статики (404 на CSS/JS у живых пользователей).
        # Поэтому --clear только один раз (по маркеру внутри самого volume,
        # он переживает рестарт контейнера, т.к. volume персистентный), а
        # на всех следующих стартах — обычный collectstatic без --clear:
        # он только добавляет/перезаписывает файлы, старые "осиротевшие"
        # (от файлов, которые переименовали/удалили в коде) не подчищает,
        # зато не создаёт окна с пустой статикой.
        STATICFILES_MARKER="/app/staticfiles/.collectstatic_done"
        if [ -f "$STATICFILES_MARKER" ]; then
            echo "[entrypoint] Собираю статику (без --clear, маркер уже есть)..."
            python manage.py collectstatic --noinput
        else
            echo "[entrypoint] Собираю статику (первый запуск, с --clear)..."
            python manage.py collectstatic --noinput --clear
            touch "$STATICFILES_MARKER"
        fi

        # Необязательный автосоздание суперпользователя для первого деплоя.
        # Работает, только если заданы все три переменные — так что по
        # умолчанию (переменные не заданы) это no-op, ничего лишнего не
        # создаёт при каждом рестарте контейнера.
        if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
            echo "[entrypoint] Проверяю/создаю суперпользователя ${DJANGO_SUPERUSER_USERNAME}..."
            python manage.py createsuperuser --noinput || true
        fi
        ;;
esac

echo "[entrypoint] Запускаю: $*"
exec "$@"
