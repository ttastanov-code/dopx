#!/usr/bin/env bash
# scripts/vendor_frontend_assets.sh
#
# Скачивает закреплённые версии CDN-скриптов/шрифтов (Alpine CSP, Alpine
# Collapse, HTMX, Tabler Icons webfont) в static/vendor/ — часть миграции с
# CDN на локально отдаваемые файлы. См.
# docs/adr/0025-remove-cdn-dependencies.md.
#
# В отличие от Tailwind/daisyUI (которым нужна настоящая сборка — см.
# package.json/static_src/app.css), эти четыре пакета используются как
# готовые файлы — просто скачать один раз и отдавать со своего /static/,
# без npm build. SRI (integrity=) после этого не нужен — файлы same-origin,
# защита от подмены обеспечивается тем, что их вообще не грузят с чужого
# домена, а не хэшем.
#
# Использование: bash scripts/vendor_frontend_assets.sh
# Запускать при первой настройке проекта и при обновлении версии любого
# из этих пакетов (поменять URL/версию ниже, перезапустить скрипт,
# закоммитить обновлённые файлы в static/vendor/).

set -euo pipefail
cd "$(dirname "$0")/.."

VENDOR_DIR="static/vendor"
mkdir -p "$VENDOR_DIR/alpine" "$VENDOR_DIR/htmx" "$VENDOR_DIR/tabler-icons/fonts"

echo "Скачиваю Alpine.js CSP build..."
curl -sfL "https://cdn.jsdelivr.net/npm/@alpinejs/csp@3.15.8/dist/cdn.min.js" \
    -o "$VENDOR_DIR/alpine/alpine-csp.min.js"

echo "Скачиваю Alpine Collapse plugin..."
curl -sfL "https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.15.8/dist/cdn.min.js" \
    -o "$VENDOR_DIR/alpine/alpine-collapse.min.js"

echo "Скачиваю HTMX..."
curl -sfL "https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js" \
    -o "$VENDOR_DIR/htmx/htmx.min.js"

echo "Скачиваю Tabler Icons webfont (CSS + шрифты)..."
curl -sfL "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.46.0/dist/tabler-icons.min.css" \
    -o "$VENDOR_DIR/tabler-icons/tabler-icons.min.css"

# CSS ссылается на файлы шрифтов относительными путями вида
# "./fonts/tabler-icons.woff2" — качаем те же имена, чтобы относительные
# пути в скачанном CSS не пришлось переписывать вручную.
for ext in woff2 woff ttf eot; do
    echo "  fonts/tabler-icons.${ext}"
    curl -sfL "https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.46.0/dist/fonts/tabler-icons.${ext}" \
        -o "$VENDOR_DIR/tabler-icons/fonts/tabler-icons.${ext}" || \
        echo "  ⚠️  ${ext} недоступен по этому URL — проверить вручную на jsdelivr.net"
done

echo "Готово. Дальше:"
echo "  1. Проверить глазами static/vendor/tabler-icons/tabler-icons.min.css — пути к шрифтам должны резолвиться в ./fonts/."
echo "  2. Обновить templates/base.html и templates/base_auth.html — заменить CDN-теги на {% static_v 'vendor/...' %}."
echo "  3. Убрать cdn.jsdelivr.net из dopx/settings.py CSP-директив (script-src/style-src/font-src)."
echo "  4. bash scripts/generate_sri.sh становится не нужен для этих 4 пакетов (integrity не требуется для same-origin) — можно удалить соответствующие строки, когда миграция завершена целиком."
