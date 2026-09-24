#!/bin/sh
# docker/entrypoint.sh
#
# Общая точка входа для web, celery_worker и celery_beat: ждём БД.
# Миграции и статика — только в web (команда gunicorn).
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

# Миграции и collectstatic — только в web, чтобы контейнеры не гонялись за миграциями.
case "$1" in
    gunicorn)
        echo "[entrypoint] Применяю миграции..."
        python manage.py migrate --noinput

        # collectstatic на каждом старте; --clear только один раз (маркер в volume) —
        # иначе nginx на пару секунд остаётся без статики.
        STATICFILES_MARKER="/app/staticfiles/.collectstatic_done"
        if [ -f "$STATICFILES_MARKER" ]; then
            echo "[entrypoint] Собираю статику (без --clear, маркер уже есть)..."
            python manage.py collectstatic --noinput
        else
            echo "[entrypoint] Собираю статику (первый запуск, с --clear)..."
            python manage.py collectstatic --noinput --clear
            touch "$STATICFILES_MARKER"
        fi

        # Автосоздание суперпользователя, только если заданы все три переменные.
        if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_EMAIL" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
            echo "[entrypoint] Проверяю/создаю суперпользователя ${DJANGO_SUPERUSER_USERNAME}..."
            python manage.py createsuperuser --noinput || true
        fi
        ;;
esac

echo "[entrypoint] Запускаю: $*"
exec "$@"
