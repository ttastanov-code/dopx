#!/usr/bin/env bash
# scripts/generate_sri.sh
#
# Скачивает закреплённые версии CDN-ресурсов (templates/base.html,
# templates/base_auth.html), считает их SHA-384 и подставляет вместо
# REPLACE_* плейсхолдеров в обоих шаблонах.
#
# Запускать при первой настройке проекта и при КАЖДОМ обновлении версии
# любого из этих пакетов в шаблонах — иначе integrity= будет ссылаться
# на хэш старой версии, и браузер откажется грузить ресурс целиком.
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
TABLER_HASH=$(sri "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.46.0/tabler-icons.min.css")
ALPINE_HASH=$(sri "https://cdn.jsdelivr.net/npm/alpinejs@3.15.8/dist/cdn.min.js")
ALPINE_COLLAPSE_HASH=$(sri "https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.15.8/dist/cdn.min.js")

for f in templates/base.html templates/base_auth.html; do
    sed -i '' \
        -e "s|sha384-REPLACE_TAILWIND|sha384-${TAILWIND_HASH}|g" \
        -e "s|sha384-REPLACE_DAISYUI_THEMES|sha384-${DAISYUI_THEMES_HASH}|g" \
        -e "s|sha384-REPLACE_DAISYUI|sha384-${DAISYUI_HASH}|g" \
        -e "s|sha384-REPLACE_TABLER|sha384-${TABLER_HASH}|g" \
        -e "s|sha384-REPLACE_ALPINE_COLLAPSE|sha384-${ALPINE_COLLAPSE_HASH}|g" \
        -e "s|sha384-REPLACE_ALPINE|sha384-${ALPINE_HASH}|g" \
        "$f"
    echo "  ✓ $f обновлён"
done

echo "Готово. Проверьте, что в шаблонах не осталось REPLACE_ (grep -r REPLACE_ templates/)."
