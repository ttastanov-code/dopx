# 📋 ПОЛНЫЙ СПРАВОЧНИК КОМАНД `sync_kff`

Сохраните этот файл для будущего использования!

---

## 🎯 БАЗОВЫЕ КОМАНДЫ

```bash
# Базовая синхронизация (авто-поиск сезона Премьер-Лиги)
python manage.py sync_kff

# Синхронизация с указанием сезона
python manage.py sync_kff --season 200

# Синхронизация по году (авто-поиск сезона)
python manage.py sync_kff --year 2025
```

---

## 🏆 ТУРНИРЫ

```bash
# Премьер-Лига (по умолчанию)
python manage.py sync_kff --tournament pl

# Первая лига
python manage.py sync_kff --tournament 1l

# Вторая лига
python manage.py sync_kff --tournament 2l

# Кубок Казахстана
python manage.py sync_kff --tournament cup

# Женская лига
python manage.py sync_kff --tournament el

# Суперкубок
python manage.py sync_kff --tournament sc
```

---

## 📊 КОНКРЕТНЫЕ МАТЧИ

```bash
# Один конкретный матч
python manage.py sync_kff --match-id 1100

# Список матчей (через запятую, без пробелов)
python manage.py sync_kff --match-ids 1100,1099,1091

# С указанием сезона для конкретного матча
python manage.py sync_kff --match-id 1100 --season 200
```

---

## 🔄 ТИПЫ СИНХРОНИЗАЦИИ

```bash
# Только последние завершённые матчи (по умолчанию 10)
python manage.py sync_kff --recent-only

# Последние N завершённых матчей
python manage.py sync_kff --recent-only --limit 5
python manage.py sync_kff --recent-only --limit 20

# Полная синхронизация сезона (все матчи)
python manage.py sync_kff --full

# Полная синхронизация с указанием сезона
python manage.py sync_kff --season 200 --full

# Полная синхронизация другого турнира
python manage.py sync_kff --tournament 1l --full
```

---

## 🔧 ОТЛАДКА И ТЕСТИРОВАНИЕ

```bash
# Dry-run (показать что будет импортировано без сохранения)
python manage.py sync_kff --dry-run

# Dry-run для конкретного матча
python manage.py sync_kff --match-id 1100 --dry-run

# Dry-run для полного сезона
python manage.py sync_kff --full --dry-run

# Debug API (вывод сырых ответов API)
python manage.py sync_kff --debug-api

# Debug API для конкретного турнира
python manage.py sync_kff --tournament pl --debug-api

# Комбинированный debug
python manage.py sync_kff --full --debug-api --dry-run
```

---

## ⚙️ НАСТРОЙКИ АВТО-ОПРЕДЕЛЕНИЯ

```bash
# Отключить авто-определение сезона (требует --season)
python manage.py sync_kff --no-auto-detect --season 200

# Авто-определение по году
python manage.py sync_kff --year 2025

# Авто-определение для другого турнира
python manage.py sync_kff --tournament 1l --year 2025
```

---

## 📦 КОМБИНИРОВАННЫЕ КОМАНДЫ

```bash
# === Быстрая синхронизация для разработки ===
python manage.py sync_kff --recent-only --limit 5

# === Полная синхронизация Премьер-Лиги ===
python manage.py sync_kff --tournament pl --full

# === Синхронизация конкретного матча с отладкой ===
python manage.py sync_kff --match-id 1100 --debug-api

# === Тест перед полным импортом ===
python manage.py sync_kff --full --dry-run

# === Синхронизация Первой лиги ===
python manage.py sync_kff --tournament 1l --recent-only --limit 10

# === Принудительная синхронизация сезона ===
python manage.py sync_kff --season 200 --full --no-auto-detect

# === Отладка API для конкретного турнира ===
python manage.py sync_kff --tournament cup --debug-api --dry-run
```

---

## 🆘 СПРАВКА

```bash
# Показать всю справку по команде
python manage.py sync_kff --help
```

---

## 📊 ВСЕ ДОСТУПНЫЕ ОПЦИИ

| Опция | Тип | Описание | Пример |
|-------|-----|----------|--------|
| `--season` | int | ID сезона (если не указан — авто-поиск) | `--season 200` |
| `--year` | int | Год для авто-поиска (2024, 2025, 2026) | `--year 2025` |
| `--tournament` | str | Код турнира | `--tournament pl` |
| `--match-id` | int | Синхронизировать один матч | `--match-id 1100` |
| `--match-ids` | str | Список ID матчей через запятую | `--match-ids 1100,1099` |
| `--limit` | int | Лимит для recent-матчей (по умолчанию 10) | `--limit 5` |
| `--recent-only` | flag | Только последние завершённые матчи | `--recent-only` |
| `--full` | flag | Полная синхронизация сезона | `--full` |
| `--no-auto-detect` | flag | Отключить авто-определение сезона | `--no-auto-detect` |
| `--dry-run` | flag | Показать что будет импортировано (без сохранения) | `--dry-run` |
| `--debug-api` | flag | Вывод сырых ответов API для отладки | `--debug-api` |

---

## 🏟️ ДОСТУПНЫЕ ТУРНИРЫ

| Код | Название | Описание |
|-----|----------|----------|
| `pl` | Премьер-Лига | Основной турнир (по умолчанию) |
| `1l` | Первая лига | Второй дивизион |
| `2l` | Вторая лига | Третий дивизион |
| `cup` | Кубок Казахстана | Кубковый турнир |
| `el` | Женская лига | Женский футбол |
| `sc` | Суперкубок | Матч за суперкубок |

---

## 💡 РЕКОМЕНДУЕМЫЕ СЦЕНАРИИ

### 🚀 Для ежедневного использования
```bash
# Утренняя проверка (последние матчи)
python manage.py sync_kff --recent-only --limit 10

# Проверка статуса API
python manage.py sync_kff --dry-run
```

### 🔨 Для разработки
```bash
# Тест одного матча
python manage.py sync_kff --match-id 1100 --debug-api

# Тест без сохранения
python manage.py sync_kff --recent-only --limit 3 --dry-run
```

### 📦 Для продакшена
```bash
# Полная синхронизация (ночью)
python manage.py sync_kff --tournament pl --full

# Синхронизация другого турнира
python manage.py sync_kff --tournament 1l --full
```

### 🐛 Для отладки проблем
```bash
# Вывод сырых данных API
python manage.py sync_kff --match-id 1100 --debug-api

# Проверка без записи в БД
python manage.py sync_kff --full --dry-run --debug-api
```

---

## ⚠️ ВАЖНЫЕ ЗАМЕТКИ

1. **По умолчанию** синхронизируется только **Премьер-Лига** (`pl`)
2. **Авто-определение сезона** включено по умолчанию (можно отключить `--no-auto-detect`)
3. **Dry-run** не сохраняет данные в БД — безопасно для тестов
4. **Debug API** может выводить много данных — используйте с `--dry-run`
5. **Match-ids** передаются через запятую **без пробелов**: `1100,1099,1091`

---
