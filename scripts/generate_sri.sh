#!/usr/bin/env bash
# scripts/generate_sri.sh
#
# Скачивает закреплённые CDN-ресурсы из base.html/base_auth.html, считает SHA-384
# и проставляет integrity=. Запускать при каждом обновлении версий пакетов,
# потом проверить в браузере, что всё грузится.
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
# Alpine — CSP-сборка (@alpinejs/csp), не требует 'unsafe-eval'.
ALPINE_HASH=$(sri "https://cdn.jsdelivr.net/npm/@alpinejs/csp@3.15.8/dist/cdn.min.js")
ALPINE_COLLAPSE_HASH=$(sri "https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.15.8/dist/cdn.min.js")
# HTMX: временный sha256 заменится на sha384 при запуске скрипта.
HTMX_HASH=$(sri "https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js")

for f in templates/base.html templates/base_auth.html; do
    # Идемпотентно: удаляем старый integrity= и вставляем заново.
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
