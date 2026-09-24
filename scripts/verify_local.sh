#!/usr/bin/env bash
# scripts/verify_local.sh
#
# Полный набор проверок в локальном окружении (Python 3.12, Postgres, Redis)
# с отчётом в scripts/verify_reports/verify_YYYY-MM-DD_HH-MM-SS.txt.
# То же гоняет CI (кроме SRI и pip check).
#
# Использование: bash scripts/verify_local.sh (из корня, в активированном venv)

set -uo pipefail
cd "$(dirname "$0")/.."

REPORT_DIR="scripts/verify_reports"
mkdir -p "$REPORT_DIR"
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
REPORT="$REPORT_DIR/verify_${TIMESTAMP}.txt"

# Каждый шаг пишет статус в SUMMARY, итог — в конце.
STEPS_OK=0
STEPS_FAIL=0
SUMMARY=""

run_step() {
    local name="$1"
    shift
    {
        echo "===================================================================="
        echo "ШАГ: $name"
        echo "КОМАНДА: $*"
        echo "===================================================================="
    } | tee -a "$REPORT"

    if "$@" >>"$REPORT" 2>&1; then
        echo "→ OK: $name" | tee -a "$REPORT"
        SUMMARY="${SUMMARY}  OK    $name\n"
        STEPS_OK=$((STEPS_OK + 1))
    else
        local code=$?
        echo "→ FAIL (exit $code): $name" | tee -a "$REPORT"
        SUMMARY="${SUMMARY}  FAIL  $name\n"
        STEPS_FAIL=$((STEPS_FAIL + 1))
    fi
    echo "" >>"$REPORT"
}

skip_step() {
    local name="$1"
    local reason="$2"
    echo "→ SKIP: $name ($reason)" | tee -a "$REPORT"
    SUMMARY="${SUMMARY}  SKIP  $name — $reason\n"
    echo "" >>"$REPORT"
}

{
    echo "Отчёт проверки DOPX"
    echo "Дата: $(date)"
    echo "Python: $(python --version 2>&1)"
    echo "Git commit: $(git rev-parse --short HEAD 2>/dev/null || echo 'н/д') ($(git branch --show-current 2>/dev/null || echo 'н/д'))"
    echo "Git статус: $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') незакоммиченных файлов"
    echo ""
} | tee "$REPORT"

# 1. Python >= 3.12 (требование Django 6).
PY_MAJOR=$(python -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python -c 'import sys; print(sys.version_info.minor)')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 12 ]; }; then
    echo "→ FAIL: нужен Python ≥3.12, а активен $(python --version 2>&1)" | tee -a "$REPORT"
    SUMMARY="${SUMMARY}  FAIL  Версия Python (нужен >=3.12)\n"
    STEPS_FAIL=$((STEPS_FAIL + 1))
else
    echo "→ OK: версия Python $(python --version 2>&1)" | tee -a "$REPORT"
    SUMMARY="${SUMMARY}  OK    Версия Python\n"
    STEPS_OK=$((STEPS_OK + 1))
fi
echo "" >>"$REPORT"

# 2. Установленные зависимости не конфликтуют между собой.
run_step "pip check (конфликты зависимостей)" pip check

# 2b. Известные CVE (pip-audit, если установлен).
if python -c "import pip_audit" >/dev/null 2>&1; then
    run_step "pip-audit (известные CVE в зависимостях)" pip-audit -r requirements.txt
else
    skip_step "pip-audit (известные CVE в зависимостях)" "не установлен — pip install pip-audit, если нужен этот шаг"
fi

# 3. Django system check — базовая проверка конфигурации приложения.
run_step "manage.py check" python manage.py check

# 4. Deploy-чеклист Django — FAIL локально не всегда критичен.
run_step "manage.py check --deploy" python manage.py check --deploy

# 5. Модели и миграции не разошлись.
run_step "manage.py makemigrations --check --dry-run" python manage.py makemigrations --check --dry-run

# 6. План миграций (ничего не применяет).
run_step "manage.py migrate --plan" python manage.py migrate --plan

# 7. Тесты. Если параллельный прогон мешает — убрать --parallel.
run_step "manage.py test" python manage.py test --verbosity=2

# 8. pyflakes (если установлен).
if python -c "import pyflakes" >/dev/null 2>&1; then
    run_step "pyflakes (статический анализ)" python -m pyflakes .
else
    skip_step "pyflakes (статический анализ)" "не установлен — pip install pyflakes, если нужен этот шаг"
fi

# 9. На CDN-тегах есть integrity= и нет плейсхолдеров REPLACE_.
{
    echo "===================================================================="
    echo "ШАГ: Проверка SRI-хэшей в шаблонах"
    echo "===================================================================="
} >>"$REPORT"
if grep -rnE 'integrity="sha(256|384)-REPLACE' templates/ >>"$REPORT" 2>&1; then
    echo "→ FAIL: найден незаполненный плейсхолдер внутри integrity=\"...\" в templates/" | tee -a "$REPORT"
    SUMMARY="${SUMMARY}  FAIL  SRI-хэши (найден REPLACE_ внутри integrity=)\n"
    STEPS_FAIL=$((STEPS_FAIL + 1))
else
    INTEGRITY_BASE=$(grep -c 'integrity=' templates/base.html 2>/dev/null || echo 0)
    INTEGRITY_AUTH=$(grep -c 'integrity=' templates/base_auth.html 2>/dev/null || echo 0)
    echo "integrity= в base.html: $INTEGRITY_BASE, в base_auth.html: $INTEGRITY_AUTH" >>"$REPORT"
    echo "→ OK: плейсхолдеров REPLACE_ не найдено" | tee -a "$REPORT"
    SUMMARY="${SUMMARY}  OK    SRI-хэши (плейсхолдеров нет)\n"
    STEPS_OK=$((STEPS_OK + 1))
fi
echo "" >>"$REPORT"

{
    echo "===================================================================="
    echo "ИТОГО"
    echo "===================================================================="
    echo -e "$SUMMARY"
    echo "Успешно: $STEPS_OK, провалено: $STEPS_FAIL"
    echo ""
    echo "Полный отчёт сохранён в: $REPORT"
} | tee -a "$REPORT"

if [ "$STEPS_FAIL" -gt 0 ]; then
    echo ""
    echo "Есть проваленные шаги — пришлите файл $REPORT целиком в чат."
    exit 1
else
    echo ""
    echo "Все проверки пройдены. Можно приложить $REPORT для истории, но всё чисто."
    exit 0
fi
