# Dockerfile
#
# Двухстадийная (multi-stage) сборка образа DOPX.
#
# Зачем две стадии, а не одна: на стадии "builder" ставится компилятор
# (gcc) и dev-заголовки библиотек (libpq-dev, libjpeg-dev и т.д.) — они
# нужны ТОЛЬКО чтобы собрать питоновские пакеты с C-расширениями
# (psycopg, Pillow, lxml, gevent...). Финальный образ ("runtime") эти
# инструменты не тащит — только готовый venv и runtime-версии тех же
# библиотек. Итог: собранный образ заметно меньше и с меньшей площадью
# атаки (нет компилятора внутри контейнера, которым можно было бы
# воспользоваться при компрометации).
#
# ИСПРАВЛЕНО: изначально здесь стоял python:3.10-slim — ошибка, проверялась
# версия питона в песочнице агента, а не реального venv проекта. Реальный
# app_venv/pyvenv.cfg показывает version = 3.12.0, и это не просто вкусовщина:
# Django==6.0.8 (requirements.txt) официально требует Python >= 3.12 —
# на 3.10 pip install падает с "Could not find a version that satisfies
# the requirement Django==6.0.8" (было воспроизведено при первом прогоне
# сборки). 3.12 — обязательное условие, а не опция.

# syntax=docker/dockerfile:1

########################################
# Stage 0: frontend-builder — компилирует static/css/app.css (Tailwind v4 +
# daisyUI) из static_src/app.css. См.
# docs/adr/0025-remove-cdn-dependencies.md — эта стадия заменяет рантайм-
# CDN-скрипт @tailwindcss/browser, который раньше компилировал CSS в
# браузере пользователя на каждой загрузке страницы.
#
# Отдельный образ node:22-slim, не добавление Node в python-стадии ниже —
# тот же принцип, что у builder/runtime split: инструменты сборки (npm,
# сам Node) не должны попадать в финальный образ, там нужен только готовый
# CSS-файл.
########################################
FROM node:22-slim AS frontend-builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY static_src ./static_src
COPY templates ./templates
COPY static/js ./static/js
RUN npm run build:css

########################################
# Stage 1: builder — только для сборки зависимостей
########################################
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential — gcc/make для сборки C-расширений.
# libpq-dev — заголовки PostgreSQL, нужны psycopg (пакет "psycopg" без
#   extras — чистый Python + требует настоящую libpq, не бандлит её).
# libjpeg-dev/zlib1g-dev — Pillow (обработка фото игроков, share-карточки).
# libxml2-dev/libxslt1-dev — lxml (парсер KFF).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Отдельный venv (а не системный python) — чище копировать одним куском
# на вторую стадию, без риска утащить системные пакеты Debian заодно.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

########################################
# Stage 2: runtime — то, что реально едет в продакшн
########################################
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=dopx.settings

# Только runtime-версии тех же библиотек (без -dev/без компилятора).
# curl — для healthcheck'ов в docker-compose.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
        zlib1g \
        libxml2 \
        libxslt1.1 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 django && useradd -u 1000 -g django -d /app -s /usr/sbin/nologin django
# ФИКСИРОВАННЫЙ UID/GID (1000), а не "первый свободный системный" (что
# дал бы `useradd -r` без -u) — важно, потому что media/ и logs/ ниже
# смонтированы в docker-compose.yml как bind mount (`./media:/app/media`),
# а НЕ named volume. Bind mount на реальном Linux-сервере не подстраивает
# владельца под пользователя в контейнере — если UID процесса внутри
# контейнера не совпадёт с владельцем каталога на хосте, Django получит
# Permission Denied при попытке сохранить аватарку/фото игрока/лог. С
# зафиксированным UID это одна команда на сервере (см. docs/DEPLOYMENT.md):
# `chown -R 1000:1000 media logs`. На Docker Desktop (Mac/Windows) это
# не критично — там bind mount'ы разрешают запись почти всегда независимо
# от UID, но на голом Linux-сервере, куда всё в итоге переедет, это
# ОБЯЗАТЕЛЬНО.

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Сначала код, потом создаём рабочие директории — так права выставляются
# один раз и на всё сразу, не теряются при последующих COPY.
COPY --chown=django:django . .

# Собранный Tailwind/daisyUI CSS из frontend-builder — static/css/app.css НЕ
# коммитится в git (build-артефакт, как staticfiles/), поэтому копируем его
# сюда ПОСЛЕ основного COPY выше, а не полагаемся на то, что он был в
# исходниках. См. docs/adr/0025-remove-cdn-dependencies.md.
COPY --from=frontend-builder --chown=django:django /build/static/css/app.css ./static/css/app.css

# staticfiles/media/logs/celerybeat — точки монтирования docker-volume'ов
# (см. docker-compose.yml). Создаём заранее, чтобы entrypoint и Django не
# спотыкались о несуществующую директорию при первом запуске на чистом сервере.
RUN mkdir -p /app/staticfiles /app/media /app/logs /app/celerybeat \
    && chown -R django:django /app/staticfiles /app/media /app/logs /app/celerybeat

COPY --chown=django:django docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Непривилегированный пользователь — если через уязвимость в приложении
# кто-то получит выполнение кода внутри контейнера, у него не будет root'а.
USER django

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
# Дефолтная команда — веб-процесс. celery worker/beat переопределяют
# command: в docker-compose.yml, используя тот же образ и тот же entrypoint.
CMD ["gunicorn", "dopx.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]
