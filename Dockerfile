# syntax=docker/dockerfile:1
# Dockerfile
#
# Многостадийная сборка: frontend-builder (Tailwind CSS), builder (зависимости с C-расширениями),
# runtime (только venv и runtime-библиотеки, без компилятора).
# Python 3.12 обязателен — Django 6 не ставится на старых версиях.


########################################
# Stage 0: frontend-builder — собирает static/css/app.css (Tailwind v4 + daisyUI).
# Node не попадает в финальный образ.
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

# build-essential — gcc/make для C-расширений.
# libpq-dev — psycopg; libjpeg-dev/zlib1g-dev — Pillow; libxml2-dev/libxslt1-dev — lxml.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Отдельный venv — копируется на вторую стадию одним куском.
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

# Runtime-библиотеки без -dev. curl — для healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
        zlib1g \
        libxml2 \
        libxslt1.1 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 django && useradd -u 1000 -g django -d /app -s /usr/sbin/nologin django
# Фиксированный UID/GID 1000: media/ и logs/ — bind mount.
# На сервере: chown -R 1000:1000 media logs.

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Сначала код, потом каталоги — права выставляются один раз.
COPY --chown=django:django . .

# app.css — build-артефакт (не в git), берём из frontend-builder.
COPY --from=frontend-builder --chown=django:django /build/static/css/app.css ./static/css/app.css

# Точки монтирования volume'ов — создаём заранее.
RUN mkdir -p /app/staticfiles /app/media /app/logs /app/celerybeat \
    && chown -R django:django /app/staticfiles /app/media /app/logs /app/celerybeat

COPY --chown=django:django docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Непривилегированный пользователь.
USER django

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
# По умолчанию — веб-процесс; celery переопределяет command в docker-compose.yml.
CMD ["gunicorn", "dopx.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]
