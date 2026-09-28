#!/usr/bin/env bash
# scripts/server-setup.sh — первичная настройка Linux-сервера (Ubuntu/Debian), один раз.
#
#   curl -fsSL https://raw.githubusercontent.com/<владелец>/<репозиторий>/main/scripts/server-setup.sh -o setup.sh
#   sudo bash setup.sh git@github.com:<владелец>/<репозиторий>.git dopx.kz
#
# Что делает: Docker (если нет), папка /www/dopx, клон репозитория, .env со случайными секретами,
# права на media/logs, ежедневный бэкап в cron. Повторный запуск ничего не ломает.
# Потом: заполнить .env (почта, Sportmonks, Sentry, Telegram) и выполнить scripts/deploy.sh.
set -Eeuo pipefail

REPO="${1:?Укажите адрес репозитория: git@github.com:владелец/репозиторий.git}"
DOMAIN="${2:?Укажите домен сайта, например dopx.kz}"
APP_DIR="${APP_DIR:-/www/dopx}"
APP_USER="${APP_USER:-${SUDO_USER:-root}}"

step() { echo; echo "==> $*"; }
[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo."; exit 1; }

step "Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker не найден. Ставлю официальным скриптом (в aaPanel можно поставить через App Store → Docker)."
    curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "Нужен плагин docker compose v2."; exit 1; }
[ "$APP_USER" != "root" ] && usermod -aG docker "$APP_USER" || true
systemctl enable --now docker >/dev/null 2>&1 || true

step "Ключ для GitHub (deploy key только на чтение)"
KEY="/home/$APP_USER/.ssh/id_ed25519"
[ "$APP_USER" = "root" ] && KEY="/root/.ssh/id_ed25519"
if [ ! -f "$KEY" ]; then
    sudo -u "$APP_USER" ssh-keygen -t ed25519 -N "" -f "$KEY" -C "dopx-server" >/dev/null
fi
echo "Добавьте этот ключ в GitHub → репозиторий → Settings → Deploy keys (Allow write не нужен):"
cat "$KEY.pub"
ssh-keyscan -t ed25519 github.com 2>/dev/null | sudo -u "$APP_USER" tee -a "$(dirname "$KEY")/known_hosts" >/dev/null || true
read -r -p "Добавили ключ? Нажмите Enter, чтобы продолжить… " _

step "Код в $APP_DIR"
mkdir -p "$APP_DIR"
chown "$APP_USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

step "Папки для файлов, логов и бэкапов"
mkdir -p media logs/deploy backups
# Внутри контейнеров приложение работает от UID 1000.
chown -R 1000:1000 media logs
chown "$APP_USER" backups logs/deploy

step ".env"
if [ ! -f .env ]; then
    cp .env.example .env
    secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')"
    dbpass="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    sed -i \
        -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" \
        -e "s|^DB_PASSWORD=.*|DB_PASSWORD=${dbpass}|" \
        -e "s|^ALLOWED_HOSTS=.*|ALLOWED_HOSTS=${DOMAIN},www.${DOMAIN}|" \
        -e "s|^CSRF_TRUSTED_ORIGINS=.*|CSRF_TRUSTED_ORIGINS=https://${DOMAIN},https://www.${DOMAIN}|" \
        -e "s|^SITE_URL=.*|SITE_URL=https://${DOMAIN}|" \
        .env
    chmod 600 .env
    chown "$APP_USER" .env
    echo "Создан .env с новым SECRET_KEY и паролем базы. Допишите почту, Sportmonks, Sentry и Telegram."
else
    echo ".env уже есть — не трогаю."
fi

step "Бэкап каждый день в 04:30"
CRON="30 4 * * * cd $APP_DIR && scripts/backup.sh >> logs/backup.log 2>&1"
( crontab -u "$APP_USER" -l 2>/dev/null | grep -v 'scripts/backup.sh'; echo "$CRON" ) | crontab -u "$APP_USER" -

cat <<EOF

Готово. Дальше (подробно — docs/DEPLOYMENT.md, «День запуска»):
  1. nano $APP_DIR/.env — почта, SPORTMONKS_API_TOKEN, SENTRY_DSN, TELEGRAM_BOT_TOKEN/CHAT_ID.
  2. Данные с вашего компьютера: $APP_DIR/scripts/launch-import.sh /root/launch-bundle.tar.gz
  3. aaPanel: сайт $DOMAIN → SSL → Reverse Proxy на http://127.0.0.1:8080 (+ X-Forwarded-Proto).
  4. GitHub → Secrets: DEPLOY_HOST, DEPLOY_USER=$APP_USER, DEPLOY_SSH_KEY, DEPLOY_PATH=$APP_DIR.
  5. GitHub → Actions → Run workflow — первый деплой, версия 1.0.0.
EOF
