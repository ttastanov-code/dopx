# core/management/commands/resync_finished_match_coaches.py
"""
manage.py resync_finished_match_coaches [--apply] [--team NAME] [--limit N]

Проверка/починка находки 4 из docs/BACKLOG.md ("Тренеры считают не свои
матчи" — пример: тренер провёл 2 матча, а на сайте у него было 21).

parsers/kff/importers.py::import_coaches теперь НЕ трогает home_coach/
away_coach у завершённого матча, если поле уже заполнено (guard,
добавленный вместе с этой командой) — это останавливает ДАЛЬНЕЙШУЮ порчу
данных, но не чинит то, что уже испорчено.

Эта команда — единственное место, где guard намеренно снимается: на КАЖДЫЙ
завершённый матч по очереди временно обнуляет home_coach/away_coach и
заново вызывает import_coaches со свежим ответом KFF /lineup — то есть
делает ровно то же самое, что произошло бы при первом импорте матча
сегодня. Если KFF отдаёт исторически верного тренера по конкретному
match_id — счётчики починятся. Если KFF ВСЕГДА отдаёт текущий тренерский
штаб клуба (даже для матчей трёхмесячной давности) — ничего не изменится,
и это будет означать, что проблема на стороне данных KFF, а не в нашем
коде (тогда чинить нечего, разве что игнорировать coach-поле KFF).

Никакие другие поля/модели НЕ трогает (счёт, события, оценки, агрегаты) —
только home_coach/away_coach конкретно. Безопаснее и на порядок быстрее
полного пересинка сезона.

Без --apply — прогоняет ВЕСЬ реальный код (реальные запросы к KFF,
реальный import_coaches), но откатывает транзакцию каждого матча в конце
— ничего не остаётся в БД. Это позволяет увидеть точный будущий результат
перед тем, как его сохранять.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from matches.models import Match
from parsers.kff.client import KFFClient
from parsers.kff.importers import import_coaches


class Command(BaseCommand):
    help = "Пересинк home_coach/away_coach у завершённых матчей (проверка/починка находки 4, docs/BACKLOG.md)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально сохранить изменения (по умолчанию — dry-run)")
        parser.add_argument("--team", type=str, help="Ограничить одной командой (подстрока названия, без учёта регистра)")
        parser.add_argument("--limit", type=int, default=None, help="Максимум матчей за прогон (по умолчанию — все)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        team_filter = options["team"]
        limit = options["limit"]

        mode = "ПРИМЕНИТЬ" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run — реальные запросы к KFF, но БД не меняется)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        qs = Match.objects.filter(status="finished").select_related(
            "home_team", "away_team", "season"
        ).order_by("-start_time")
        if team_filter:
            qs = qs.filter(Q(home_team__name__icontains=team_filter) | Q(away_team__name__icontains=team_filter))
        if limit:
            qs = qs[:limit]

        matches = list(qs)
        self.stdout.write(f"Матчей к проверке: {len(matches)}\n")

        client = KFFClient()
        changed = 0
        unchanged = 0
        errors = 0
        skipped_no_data = 0

        for match in matches:
            if not match.external_id:
                continue

            tournament_code = getattr(match.season, "tournament_code", "pl")
            try:
                lineup_resp = client.get_lineup(match.external_id, tournament_code=tournament_code)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  {match}: ошибка запроса к KFF — {e}"))
                errors += 1
                continue

            if not lineup_resp:
                skipped_no_data += 1
                continue
            lineup_data = lineup_resp.get("data", lineup_resp) if isinstance(lineup_resp, dict) else lineup_resp
            if not lineup_data:
                skipped_no_data += 1
                continue

            old_home = match.home_coach
            old_away = match.away_coach

            with transaction.atomic():
                # Намеренно снимаем guard РОВНО для этого прогона — обнуляем
                # поля, чтобы import_coaches отработал так же, как при самом
                # первом импорте (guard срабатывает только когда поле уже
                # заполнено).
                match.home_coach = None
                match.away_coach = None
                match.save(update_fields=["home_coach", "away_coach"])

                import_coaches(match, lineup_data)
                match.refresh_from_db(fields=["home_coach", "away_coach"])

                new_home = match.home_coach
                new_away = match.away_coach

                if new_home != old_home or new_away != old_away:
                    changed += 1
                    self.stdout.write(
                        f"  ИЗМЕНИЛОСЬ  {match.start_time:%d.%m.%Y}  {match}\n"
                        f"      дома:  {old_home or '—'}  ->  {new_home or '—'}\n"
                        f"      гости: {old_away or '—'}  ->  {new_away or '—'}"
                    )
                else:
                    unchanged += 1

                if not apply_changes:
                    # dry-run: реальный код отработал (включая возможные
                    # get_or_create_coach), но ничего не остаётся в БД.
                    transaction.set_rollback(True)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nГотово. Изменилось: {changed}, без изменений: {unchanged}, "
                f"нет данных от KFF: {skipped_no_data}, ошибок: {errors}"
            )
        )
        if changed:
            if apply_changes:
                self.stdout.write(
                    "Изменения сохранены. Проверьте счётчик матчей у тренера на сайте — "
                    "если совпадает с реальностью, находка 4 закрыта. Если результат "
                    "по-прежнему неверный — проблема на стороне данных KFF, не в нашем коде."
                )
            else:
                self.stdout.write(
                    "Это был dry-run — ничего не сохранено. Если список изменений выглядит "
                    "правильно (тренеры соответствуют реальности) — повторите с --apply."
                )
        elif not apply_changes:
            self.stdout.write(
                "Ни один матч не изменился бы даже при повторном импорте — похоже, "
                "текущие данные и есть то, что реально возвращает KFF (то есть "
                "искажение, если оно есть, на стороне API, а не в нашем guard-фиксе)."
            )
