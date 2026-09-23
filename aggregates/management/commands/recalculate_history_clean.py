# aggregates/management/commands/recalculate_history_clean.py
"""
Пересчёт рейтингов игроков и команд по ВСЕМ завершённым матчам БЕЗ
авто-поправки от защиты от накрутки — 2026-09-24.

Зачем: до поля rating_correction_applied (миграция aggregates/0008) нигде
не записывалось, сколько поправки вшито в рейтинг каждого матча. Поэтому в
истории игроков/команд лежат рейтинги с поправками, которые с тех пор могли
смениться (у Мартыновича: сначала −0.23, потом +0.25). Этот пересчёт
приводит историю к чистой оценке болельщиков (с той же защитой от выбросов
и сговора фанатов — она зависит только от самих голосов). Новые матчи
дальше считаются как обычно — с текущей поправкой, если она есть.

Синхронно, без Celery. В БД меняет только PlayerMatchAggregate и
TeamMatchAggregate (performance_score и связанные индексы). Составы
«Лучшие тура» и сборные сезона НЕ пересобирает — при желании запустите
соответствующий пересчёт из «Скриптов» в дашборде.

Использование:
    python manage.py recalculate_history_clean           # только посчитать, сколько матчей
    python manage.py recalculate_history_clean --apply   # пересчитать (так же — кнопка «Применить» в дашборде)
"""
from django.core.management.base import BaseCommand

from aggregates.tasks import recalculate_player_aggregates, recalculate_team_aggregates
from matches.models import Match


class Command(BaseCommand):
    help = "Пересчитать рейтинги всех завершённых матчей без авто-поправки (чистая история)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально пересчитать (без флага — только показать число матчей)")

    def handle(self, *args, **options):
        match_ids = list(
            Match.objects.filter(status="finished").order_by("start_time").values_list("id", flat=True)
        )
        total = len(match_ids)
        self.stdout.write(f"Завершённых матчей: {total}")
        if not total:
            return
        if not options["apply"]:
            self.stdout.write("Пробный запуск — ничего не изменено. Для пересчёта добавьте --apply.")
            return

        ok = 0
        for i, match_id in enumerate(match_ids, 1):
            # .run — синхронный вызов тела задачи (без брокера и rate_limit).
            p = recalculate_player_aggregates.run(str(match_id), apply_correction=False)
            t = recalculate_team_aggregates.run(str(match_id), apply_correction=False)
            if p or t:
                ok += 1
            if i % 25 == 0 or i == total:
                self.stdout.write(f"[{i}/{total}] пересчитано")

        self.stdout.write(self.style.SUCCESS(
            f"Готово: {ok} матч(ей) пересчитано без поправок. Новые матчи будут считаться с поправкой как обычно."
        ))
