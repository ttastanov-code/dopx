# round_squad/management/commands/recompute_closed_rounds.py
"""
manage.py recompute_closed_rounds [--season-id ID] [--tour N]

2026-09-21, прямая просьба пользователя: "можешь мне написать команду,
которая перерасчёт делает всех закрытых туров сборные?" — до этой команды
единственным способом было руками снять is_final в админке и запустить
обычный пересчёт, что ПОВТОРНО рассылало письмо «итоги тура» всем
верифицированным пользователям (см. round_squad/services.py::recompute_round
и send_round_results_notification) — плохой способ для рутинной операции.

Эта команда вызывает recompute_round(..., force=True) — обходит ранний
выход по is_final, но НЕ трогает finalized_at и НЕ ставит повторную
рассылку (see докстринг recompute_round про was_final_before/just_finalized).
Без флагов — пересчитывает ВСЕ закрытые туры по всем сезонам/лигам (то же,
что делает кнопка на дашборде через round_squad.tasks.
recompute_all_closed_rounds_task, см. dashboard/parser_tools.py). Флаги —
для точечного пересчёта одного сезона/тура без затрагивания остальных.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from round_squad.models import RoundBestXI
from round_squad.services import recompute_round
from seasons.models import Season


class Command(BaseCommand):
    help = "Пересчитывает состав уже ЗАКРЫТЫХ (is_final=True) туров, не рассылая повторно письмо с итогами"

    def add_arguments(self, parser):
        parser.add_argument(
            "--season-id", type=str, default=None,
            help="Пересчитать закрытые туры только этого сезона (UUID). По умолчанию — все сезоны.",
        )
        parser.add_argument(
            "--tour", type=int, default=None,
            help="Пересчитать только этот номер тура (требует --season-id).",
        )

    def handle(self, *args, **options):
        season_id = options["season_id"]
        tour = options["tour"]

        if tour is not None and season_id is None:
            raise CommandError("--tour требует --season-id (иначе непонятно, тур какого сезона)")

        qs = RoundBestXI.objects.filter(is_final=True).select_related("season")
        if season_id:
            try:
                season = Season.objects.get(pk=season_id)
            except Season.DoesNotExist:
                raise CommandError(f"Сезон {season_id} не найден")
            qs = qs.filter(season=season)
        if tour is not None:
            qs = qs.filter(tour=tour)

        rounds = list(qs.order_by("season_id", "tour"))
        if not rounds:
            self.stdout.write(self.style.WARNING("Нет закрытых туров, подходящих под условия — пересчитывать нечего"))
            return

        self.stdout.write(f"Пересчитываю {len(rounds)} закрытых тур(ов)...")
        for round_xi in rounds:
            recompute_round(round_xi.season, round_xi.tour, force=True)
            self.stdout.write(f"  ✓ тур {round_xi.tour} ({round_xi.season})")

        self.stdout.write(self.style.SUCCESS(f"Готово: пересчитано {len(rounds)} тур(ов). Письма НЕ рассылались повторно."))
