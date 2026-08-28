#!/usr/bin/env bash
# scripts/generate_sri.sh
#
# Скачивает закреплённые версии CDN-ресурсов (templates/base.html,
# templates/base_auth.html), считает их SHA-384 и ВСТАВЛЯЕТ integrity=
# рядом с crossorigin="anonymous" на каждом теге.
#
# ИСТОРИЯ: раньше здесь стояли плейсхолдеры sha384-REPLACE_*, которые так и
# не заменили реальным хэшем, из-за чего браузер отказывался загружать
# Tailwind/DaisyUI/Alpine/Tabler ЦЕЛИКОМ (SRI-мисматч блокирует ресурс
# полностью, не только "предупреждает"). Урок: integrity= можно добавлять
# коммитом ТОЛЬКО вместе с реально посчитанным хэшем в том же коммите —
# никогда как заглушку "заполню потом". Task #138 закрыл это реальными
# хэшами для Tailwind/DaisyUI/Tabler/Alpine Collapse.
#
# 2026-08-21: alpinejs → @alpinejs/csp (см. коммент у ALPINE_HASH ниже) —
# integrity= для НОВОГО пакета сейчас снова не проставлен на теге ядра
# Alpine в base.html/base_auth.html, ОБЯЗАТЕЛЬНО прогнать этот скрипт перед
# деплоем.
#
# Запускать при первой настройке проекта и при КАЖДОМ обновлении версии
# любого из этих пакетов в шаблонах. Перед коммитом результата — открыть
# сайт локально и глазами убедиться, что стили/Alpine/иконки грузятся
# (Network tab не должен показывать заблокированные по integrity запросы).
#
# Использование: bash scripts/generate_sri.sh

set -euo pipefail
cd "$(dirname "$0")/.."

sri() {
    curl -sfL "$1" | openssl dgst -sha384 -binary | openssl base64 -A
}

echo "Считаю SHA-384 для закреплённых версий CDN-ресурсов..."

TAILWIND_HASH=$(sri "https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.3")
DAISYUI_HASH=$(sri "https://cdn.jsdelivr.net/npm/daisyui@5.7.17")
DAISYUI_THEMES_HASH=$(sri "https://cdn.jsdelivr.net/npm/daisyui@5.7.17/themes.css")
TABLER_HASH=$(sri "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.46.0/dist/tabler-icons.min.css")
# 2026-08-21: alpinejs (обычная сборка) заменена на @alpinejs/csp — не
# компилирует x-выражения через new Function(...), поэтому CSP script-src
# больше не требует 'unsafe-eval' (см. dopx/middleware.py). Все x-data по
# шаблонам переведены на зарегистрированные Alpine.data()-компоненты в
# static/js/alpine-components.js — см. подробный коммент там же.
ALPINE_HASH=$(sri "https://cdn.jsdelivr.net/npm/@alpinejs/csp@3.15.8/dist/cdn.min.js")
ALPINE_COLLAPSE_HASH=$(sri "https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.15.8/dist/cdn.min.js")
# 2026-08-28: раньше здесь не было HTMX вообще — integrity= на теге htmx.org
# в base.html/base_auth.html так и не проставлялся ни разу. Временно (пока
# сеть недоступна) на этих двух тегах стоит sha256-хэш, взятый из
# data.jsdelivr.com — при первом же прогоне этого скрипта с доступом к сети
# sed ниже заменит его на sha384, приведя к единому алгоритму с остальными
# тегами (regex удаления захватывает и sha256-, и sha384-, см. ниже).
HTMX_HASH=$(sri "https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js")

for f in templates/base.html templates/base_auth.html; do
    # Идемпотентно: сначала убираем integrity=, если он уже стоит (повторный
    # запуск скрипта при обновлении версии, либо временный sha256-хэш из
    # комментария выше), потом вставляем заново рядом с
    # crossorigin="anonymous" — так не накапливаются дубли атрибута.
    sed -i '' -E 's/ integrity="sha(256|384)-[A-Za-z0-9+/=]+"//g' "$f"
    sed -i '' \
        -e "s|@tailwindcss/browser@4.3.3\" crossorigin|@tailwindcss/browser@4.3.3\" integrity=\"sha384-${TAILWIND_HASH}\" crossorigin|" \
        -e "s|npm/daisyui@5.7.17\" rel=\"stylesheet\" type=\"text/css\" crossorigin|npm/daisyui@5.7.17\" rel=\"stylesheet\" type=\"text/css\" integrity=\"sha384-${DAISYUI_HASH}\" crossorigin|" \
        -e "s|daisyui@5.7.17/themes.css\" rel=\"stylesheet\" type=\"text/css\" crossorigin|daisyui@5.7.17/themes.css\" rel=\"stylesheet\" type=\"text/css\" integrity=\"sha384-${DAISYUI_THEMES_HASH}\" crossorigin|" \
        -e "s|tabler-icons.min.css\" crossorigin|tabler-icons.min.css\" integrity=\"sha384-${TABLER_HASH}\" crossorigin|" \
        -e "s|@alpinejs/collapse@3.15.8/dist/cdn.min.js\" crossorigin|@alpinejs/collapse@3.15.8/dist/cdn.min.js\" integrity=\"sha384-${ALPINE_COLLAPSE_HASH}\" crossorigin|" \
        -e "s|npm/@alpinejs/csp@3.15.8/dist/cdn.min.js\" crossorigin|npm/@alpinejs/csp@3.15.8/dist/cdn.min.js\" integrity=\"sha384-${ALPINE_HASH}\" crossorigin|" \
        -e "s|npm/htmx.org@2.0.8/dist/htmx.min.js\" crossorigin|npm/htmx.org@2.0.8/dist/htmx.min.js\" integrity=\"sha384-${HTMX_HASH}\" crossorigin|" \
        "$f"
    echo "  ✓ $f обновлён"
done

echo "Готово. Проверьте:"
echo "  1. grep -rn 'REPLACE_' templates/   — должно быть пусто"
echo "  2. grep -c 'integrity=' templates/base.html templates/base_auth.html   — по 7 совпадений в каждом"
echo "  3. Откройте сайт локально — стили/иконки/Alpine/HTMX должны грузиться без ошибок в консоли."
