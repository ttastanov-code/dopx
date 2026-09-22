# parsers/management/commands/verify_names_with_ai.py
"""
manage.py verify_names_with_ai [--all] [--entity player|referee|coach]
                                [--limit N] [--delay SECONDS] [--recheck]
                                [--dry-run]

Находит Player/Referee/Coach, чьё текущее ФИО НЕ подтверждено внешним
источником (name_source='guessed_transliteration' — см. core/models.py::
NAME_SOURCE_CHOICES и докстринг parsers/sportmonks/importers.py::
_resolve_cyrillic_name), и запрашивает у Gemini API (parsers/name_ai.py)
реальное написание с веб-поиском. Результат ложится в
NameVerificationSuggestion — НЕ применяется автоматически, staff
подтверждает/отклоняет в очереди на дашборде (dashboard/views.py::
names_review, прямое решение пользователя: "всегда через ручное
подтверждение").

2026-09-22, прямая просьба пользователя после жалобы "Сергий Малий" вместо
"Сергий Малый": "надо что-то 100% рабочее придумать... уйти от того что мы
персонально каждого обрабатываем и сидим ищем". См. полный разбор вариантов
(браузерная автоматизация ChatGPT/Gemini/Claude Pro отклонена как нарушение
ToS + технически хрупко) в докстринге parsers/name_ai.py.

--all — разовый полный прогон по ВСЕМ записям с sportmonks_id (не только
guessed_transliteration) — по прямой просьбе пользователя "плюс разовый
прогон по всем уже существующим записям".

ДЕДУПЛИКАЦИЯ: пропускает сущности, у которых УЖЕ есть хотя бы одна
NameVerificationSuggestion (в любом статусе) — если staff уже отклонил
предложение (решил, что текущее написание верное) или уже одобрил, повторный
прогон не должен снова тратить запрос к API на ту же запись. --recheck
снимает это ограничение (например, после того как исходные данные у
Sportmonks сами изменились).

RATE LIMIT: бесплатный тариф Gemini API ограничен по запросам в
минуту/день — точные текущие цифры не проверить без живого ключа (в
песочнице этой сессии его нет, см. parsers/name_ai.py). --delay (по
умолчанию 4 секунды между вызовами) — консервативная защита с запасом;
если бесплатный тариф окажется жёстче, уменьшите --limit и запускайте
почаще, а не увеличивайте риск словить 429.
"""
from __future__ import annotations

import time

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import IntegrityError

from coaches.models import Coach
from core.models import NAME_SOURCE_GUESSED_TRANSLITERATION
from parsers import name_ai
from parsers.models import NameVerificationSuggestion
from players.models import Player
from referees.models import Referee

_ENTITY_CONFIG = {
    "player": {"model": Player, "label_ru": "игрока"},
    "referee": {"model": Referee, "label_ru": "судьи"},
    "coach": {"model": Coach, "label_ru": "тренера"},
}


class Command(BaseCommand):
    help = "Проверяет ФИО игроков/судей/тренеров через Gemini API (веб-поиск) и кладёт предложения в очередь на подтверждение staff"

    def add_arguments(self, parser):
        parser.add_argument(
            "--all", action="store_true",
            help="Проверить ВСЕ записи с sportmonks_id (не только с name_source='guessed_transliteration') — разовый полный прогон.",
        )
        parser.add_argument(
            "--entity", choices=["player", "referee", "coach"], default=None,
            help="Ограничиться одним типом сущности. По умолчанию — все три.",
        )
        parser.add_argument("--limit", type=int, default=20, help="Максимум вызовов Gemini за один запуск (по умолчанию 20 — бережём бесплатный лимит). 0 — без ограничения (для разового прогона по всей базе через --all).")
        parser.add_argument("--delay", type=float, default=4.0, help="Пауза в секундах между вызовами Gemini (по умолчанию 4с).")
        parser.add_argument("--recheck", action="store_true", help="Не пропускать записи, у которых уже есть предложение (любого статуса).")
        parser.add_argument("--dry-run", action="store_true", help="Только показать, кого бы проверили, не тратя вызовы Gemini.")

    def handle(self, *args, **options):
        if not name_ai.is_configured() and not options["dry_run"]:
            self.stderr.write(self.style.ERROR(
                "GEMINI_API_KEY не задан (dopx/settings.py) — задайте переменную окружения GEMINI_API_KEY "
                "(ключ с aistudio.google.com) или запустите с --dry-run, чтобы только посмотреть список кандидатов."
            ))
            return

        entities = [options["entity"]] if options["entity"] else list(_ENTITY_CONFIG.keys())
        # 2026-09-22: --limit 0 (или отрицательный) — "без ограничения", не
        # "ноль вызовов". Раньше `checked >= limit` при limit=0 обрывало
        # прогон СРАЗУ на первой же итерации (0 >= 0 — True), и --all
        # --limit 0 молча проверял 0 записей вместо ожидаемого "прогнать
        # всю базу" — ровно та ситуация, для которой --all и придумывался.
        # float('inf') с int'ом сравнивается нормально, просто снимает cap.
        limit = options["limit"] if options["limit"] > 0 else float("inf")
        delay = options["delay"]
        recheck = options["recheck"]
        sweep_all = options["all"]
        dry_run = options["dry_run"]

        checked = 0
        created_pending = 0
        created_failed = 0

        for entity_key in entities:
            if checked >= limit:
                break
            cfg = _ENTITY_CONFIG[entity_key]
            model = cfg["model"]
            content_type = ContentType.objects.get_for_model(model)

            qs = model.objects.filter(sportmonks_id__isnull=False).exclude(sportmonks_id="")
            if not sweep_all:
                qs = qs.filter(name_source=NAME_SOURCE_GUESSED_TRANSLITERATION)

            if not recheck:
                # 2026-09-22: check_failed — ТЕХНИЧЕСКИЙ сбой (429/сеть/
                # невалидный JSON), не настоящий ответ Gemini — не считаем
                # его "уже проверено". Раньше exclude() здесь не было, и
                # запись с 429 навсегда пропадала из обычных прогонов —
                # единственный способ перепроверить был --recheck, который
                # заново дёргает Gemini ВООБЩЕ по всем (включая уже
                # успешно подтверждённые/отклонённые), зря тратя дневной
                # лимит. Теперь упавшие сами попадают в следующий обычный
                # прогон, а pending_review/approved/rejected — реальные
                # исходы, их по-прежнему не трогаем без --recheck.
                already_suggested_ids = set(
                    NameVerificationSuggestion.objects.filter(content_type=content_type)
                    .exclude(status="check_failed")
                    .values_list("object_id", flat=True)
                )
                # object_id хранится как str (CharField) — сравниваем по str(id).
                qs = [obj for obj in qs if str(obj.id) not in already_suggested_ids]
            else:
                qs = list(qs)

            self.stdout.write(f"{cfg['label_ru'].capitalize()}: кандидатов на проверку — {len(qs)}")

            for obj in qs:
                if checked >= limit:
                    self.stdout.write(self.style.WARNING(f"Достигнут --limit={limit}, остальные кандидаты — в следующий запуск."))
                    break

                if dry_run:
                    self.stdout.write(f"  [dry-run] {cfg['label_ru']} {obj.first_name} {obj.last_name} (sportmonks_id={obj.sportmonks_id})")
                    checked += 1
                    continue

                team_name = ""
                team = getattr(obj, "team", None)
                if team is not None:
                    team_name = getattr(team, "name", "") or ""

                result = name_ai.verify_name(
                    cfg["label_ru"], obj.first_name, obj.last_name, team_name=team_name,
                )
                checked += 1

                suggestion = NameVerificationSuggestion(
                    content_type=content_type,
                    object_id=str(obj.id),
                    entity_label=entity_key,
                    sportmonks_id=obj.sportmonks_id or "",
                    current_first_name=obj.first_name,
                    current_last_name=obj.last_name,
                )
                if result.ok:
                    suggestion.suggested_first_name = result.first_name
                    suggestion.suggested_last_name = result.last_name
                    suggestion.confidence = result.confidence
                    suggestion.reasoning = result.reasoning
                    suggestion.matches_current = result.matches_current
                    suggestion.status = "pending_review"
                    created_pending += 1
                    self.stdout.write(
                        f"  {cfg['label_ru']} {obj.first_name} {obj.last_name} -> "
                        f"{result.first_name} {result.last_name} (уверенность: {result.confidence}"
                        f"{', совпадает с текущим' if result.matches_current else ''})"
                    )
                else:
                    suggestion.status = "check_failed"
                    suggestion.error_message = result.error
                    created_failed += 1
                    self.stdout.write(self.style.WARNING(f"  {cfg['label_ru']} {obj.first_name} {obj.last_name} -> ОШИБКА: {result.error}"))

                try:
                    suggestion.save()
                except IntegrityError:
                    # Гонка с --recheck/параллельным запуском — уже есть
                    # pending_review для этой же сущности, тихо пропускаем.
                    self.stdout.write(self.style.WARNING(f"  {cfg['label_ru']} {obj.first_name} {obj.last_name} — уже есть предложение на проверке, пропуск"))

                if checked < limit and delay > 0:
                    time.sleep(delay)

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"[dry-run] Всего кандидатов показано: {checked}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Готово: проверено {checked}, предложений создано {created_pending}, ошибок запроса {created_failed}. "
                f"Смотрите очередь в дашборде: /staff/dashboard/names-review/"
            ))
