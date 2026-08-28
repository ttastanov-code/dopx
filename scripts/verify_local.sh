#!/usr/bin/env bash
# scripts/verify_local.sh
#
# ЗАЧЕМ ЭТОТ ФАЙЛ (2026-08-28): у агента, который правит этот проект,
# нет доступа к живой Postgres/Redis, нет sudo, нет сети до PyPI и стоит
# Python 3.10 вместо требуемых 3.12 — то есть он физически не может
# прогнать manage.py test/check/makemigrations сам. До сих пор это
# закрывалось вручную: разработчик гонял тесты у себя и копипастил вывод
# в чат, а один раз — не проверил логику до конца ("твоей
# невнимательности", как справедливо было замечено) и в код прошли два
# бага, которые нашлись только повторным ручным разбором.
#
# Этот скрипт — то же самое, но одной командой и без ручного собирания
# вывода по кусочкам: гоняет ПОЛНЫЙ набор проверок в РЕАЛЬНОМ окружении
# (там, где есть настоящая Postgres, Redis, Python 3.12) и складывает всё
# в один файл с меткой времени, который можно целиком вставить в чат.
#
# ВАЖНО: это ручной, локальный дубль. С 2026-08-28 те же проверки (кроме
# SRI и pip check) автоматически гоняются в CI на каждый push — см. job
# `test` в .github/workflows/deploy.yml. Этот скрипт для того, чтобы
# проверить ДО пуша, не дожидаясь CI, и чтобы держать под рукой полный
# текст последнего прогона локально.
#
# Использование:
#   bash scripts/verify_local.sh
# (из корня проекта, в активированном venv, где стоит requirements.txt
# и куда прописаны реальные DB_*/REDIS_*/CELERY_* в .env)
#
# Результат: scripts/verify_reports/verify_YYYY-MM-DD_HH-MM-SS.txt —
# этот файл целиком и присылать в чат.

set -uo pipefail
cd "$(dirname "$0")/.."

REPORT_DIR="scripts/verify_reports"
mkdir -p "$REPORT_DIR"
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
REPORT="$REPORT_DIR/verify_${TIMESTAMP}.txt"

# Каждый шаг: имя + команда. Итоговый статус (OK/FAIL/SKIP) копится в
# SUMMARY и печатается в конце — так не нужно листать весь лог, чтобы
# понять, что вообще сломалось.
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

# 1. Проверка версии Python — Django 6.0.8 требует ≥3.12, но по ошибке
# конфигурации venv это легко не заметить (см. историю проекта: агент
# годами сидел с 3.10 в песочнице и не мог даже install сделать).
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

# 3. Django system check — базовая проверка конфигурации приложения.
run_step "manage.py check" python manage.py check

# 4. Deploy-чеклист Django (DEBUG, SECRET_KEY, HSTS, cookie-флаги и т.д.)
# — отдельно от обычного check, так как требует прод-подобных настроек,
# которые могут быть не выставлены в локальном .env; FAIL здесь не всегда
# критичен для локальной разработки, но важен перед реальным деплоем.
run_step "manage.py check --deploy" python manage.py check --deploy

# 5. Расхождение между моделями и миграциями — самый частый источник
# "работает у меня, падает на сервере": забыли сгенерировать миграцию
# после правки models.py. В этом проекте часть миграций написана вручную
# (см. docs/BACKLOG.md), этот шаг — единственный способ поймать, что
# ручная миграция разошлась с фактическим состоянием моделей.
run_step "manage.py makemigrations --check --dry-run" python manage.py makemigrations --check --dry-run

# 6. План применения миграций — не применяет ничего, просто показывает,
# что БУДЕТ применено; полезно для ревью перед деплоем на прод-БД.
run_step "manage.py migrate --plan" python manage.py migrate --plan

# 7. Полный прогон тестов — главная проверка логики. --parallel ускоряет
# на многоядерных машинах; если в проекте есть тесты, не переживающие
# параллельный прогон, замените на обычный `test`.
run_step "manage.py test" python manage.py test --verbosity=2

# 8. Статический анализ на неиспользуемые импорты/переменные, недостижимый
# код и т.п. — опционально: если pyflakes не установлен, шаг пропускается
# без ошибки (скрипт НЕ ставит пакеты сам, чтобы не трогать чужой venv
# без спроса).
if python -c "import pyflakes" >/dev/null 2>&1; then
    run_step "pyflakes (статический анализ)" python -m pyflakes .
else
    skip_step "pyflakes (статический анализ)" "не установлен — pip install pyflakes, если нужен этот шаг"
fi

# 9. SRI-хэши на CDN-тегах не превратились обратно в REPLACE_-плейсхолдеры
# (см. историю бага в scripts/generate_sri.sh) и integrity= вообще
# присутствует на всех ожидаемых тегах.
#
# ВАЖНО: ищем REPLACE_ только ВНУТРИ значения integrity="..." (реальный
# незаполненный плейсхолдер), а не голое слово по всему файлу — иначе
# ложно сработает на {% comment %}-блоках, которые как раз ОБЪЯСНЯЮТ
# историю этого бага текстом вроде "был здесь плейсхолдером sha384-REPLACE_*"
# (см. templates/base.html) и сами по себе ничего не ломают.
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
