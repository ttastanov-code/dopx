#!/usr/bin/env bash
# scripts/generate_sri.sh
#
# Скачивает закреплённые версии CDN-ресурсов (templates/base.html,
# templates/base_auth.html), считает их SHA-384 и ВСТАВЛЯЕТ integrity=
# рядом с crossorigin="anonymous" на каждом теге.
#
# ВАЖНО: integrity= сейчас НЕ проставлен на этих тегах (см. коммент в
# base.html) — раньше здесь стояли плейсхолдеры sha384-REPLACE_*, которые
# так и не заменили реальным хэшем, из-за чего браузер отказывался
# загружать Tailwind/DaisyUI/Alpine/Tabler ЦЕЛИКОМ (SRI-мисматч блокирует
# ресурс полностью, не только "предупреждает"). Урок: integrity= можно
# добавлять коммитом ТОЛЬКО вместе с реально посчитанным хэшем в том же
# коммите — никогда как заглушку "заполню потом".
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
ALPINE_HASH=$(sri "https://cdn.jsdelivr.net/npm/alpinejs@3.15.8/dist/cdn.min.js")
ALPINE_COLLAPSE_HASH=$(sri "https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.15.8/dist/cdn.min.js")

for f in templates/base.html templates/base_auth.html; do
    # Идемпотентно: сначала убираем integrity=, если он уже стоит (повторный
    # запуск скрипта при обновлении версии), потом вставляем заново рядом
    # с crossorigin="anonymous" — так не накапливаются дубли атрибута.
    sed -i '' -E 's/ integrity="sha384-[A-Za-z0-9+/=]+"//g' "$f"
    sed -i '' \
        -e "s|@tailwindcss/browser@4.3.3\" crossorigin|@tailwindcss/browser@4.3.3\" integrity=\"sha384-${TAILWIND_HASH}\" crossorigin|" \
        -e "s|npm/daisyui@5.7.17\" rel=\"stylesheet\" type=\"text/css\" crossorigin|npm/daisyui@5.7.17\" rel=\"stylesheet\" type=\"text/css\" integrity=\"sha384-${DAISYUI_HASH}\" crossorigin|" \
        -e "s|daisyui@5.7.17/themes.css\" rel=\"stylesheet\" type=\"text/css\" crossorigin|daisyui@5.7.17/themes.css\" rel=\"stylesheet\" type=\"text/css\" integrity=\"sha384-${DAISYUI_THEMES_HASH}\" crossorigin|" \
        -e "s|tabler-icons.min.css\" crossorigin|tabler-icons.min.css\" integrity=\"sha384-${TABLER_HASH}\" crossorigin|" \
        -e "s|@alpinejs/collapse@3.15.8/dist/cdn.min.js\" crossorigin|@alpinejs/collapse@3.15.8/dist/cdn.min.js\" integrity=\"sha384-${ALPINE_COLLAPSE_HASH}\" crossorigin|" \
        -e "s|npm/alpinejs@3.15.8/dist/cdn.min.js\" crossorigin|npm/alpinejs@3.15.8/dist/cdn.min.js\" integrity=\"sha384-${ALPINE_HASH}\" crossorigin|" \
        "$f"
    echo "  ✓ $f обновлён"
done

echo "Готово. Проверьте:"
echo "  1. grep -rn 'REPLACE_' templates/   — должно быть пусто"
echo "  2. grep -c 'integrity=' templates/base.html templates/base_auth.html   — по 6 совпадений в каждом"
echo "  3. Откройте сайт локально — стили/иконки/Alpine должны грузиться без ошибок в консоли."
