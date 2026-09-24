# core/management/commands/simulate_match_timing.py
"""Только для локальной отладки retention-задач и виджета прогнозов.
Двигает существующий матч по времени/статусу/счёту и ставит manual_override=True,
чтобы автосинк не перезаписал. Id — UUID, external_id или sportmonks_id.

Примеры:
  python manage.py simulate_match_timing                     # последние матчи
  python manage.py simulate_match_timing <id> --status scheduled --start-in-minutes 50
  python manage.py simulate_match_timing <id> --status finished --home-score 2 --away-score 1
  python manage.py simulate_match_timing <id> --status scheduled --start-in-minutes 20160
  python manage.py simulate_match_timing <id> --release       # вернуть автосинку
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from matches.models import Match


class Command(BaseCommand):
    help = (
        "Тестовый инструмент (НЕ для продакшена): двигает существующий матч "
        "по времени/статусу/счёту, чтобы вручную протестировать виджет "
        "прогнозов и retention-loop уведомления без ожидания реального матча."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'match_id', type=str, nargs='?', default=None,
            help='UUID матча. Не указан — команда выведет список последних матчей и их id.',
        )
        parser.add_argument(
            '--start-in-minutes', type=int, default=None,
            help='Сдвинуть start_time на N минут от текущего момента (можно отрицательное — "матч уже начался").',
        )
        parser.add_argument(
            '--status', type=str, default=None,
            choices=[choice[0] for choice in Match.STATUS_CHOICES],
        )
        parser.add_argument('--home-score', type=int, default=None)
        parser.add_argument('--away-score', type=int, default=None)
        parser.add_argument(
            '--voting-hours', type=int, default=48,
            help='Если --status finished — на сколько часов от сейчас открыть voting_open_until (по умолчанию 48).',
        )
        parser.add_argument(
            '--release', action='store_true',
            help='Снять manual_override — вернуть матч под управление автосинка (Sportmonks), ничего больше не менять.',
        )

    def handle(self, *args, **options):
        match_id = options['match_id']

        if not match_id:
            self._list_recent()
            return

        # UUID, external_id или sportmonks_id.
        import uuid as uuid_module

        try:
            uuid_module.UUID(str(match_id))
            match = Match.objects.select_related('home_team', 'away_team').filter(id=match_id).first()
        except (ValueError, AttributeError, TypeError):
            match = Match.objects.select_related('home_team', 'away_team').filter(
                Q(external_id=match_id) | Q(sportmonks_id=match_id)
            ).first()

        if not match:
            raise CommandError(
                f"Матч с id={match_id!r} не найден (искали по UUID/external_id/sportmonks_id). "
                f"Запустите команду без аргументов, чтобы увидеть список последних матчей."
            )

        if options['release']:
            match.manual_override = False
            match.save(update_fields=['manual_override', 'updated_at'])
            self.stdout.write(self.style.SUCCESS(
                f"✅ manual_override снят — {match} снова под управлением автосинка."
            ))
            return

        update_fields = []

        if options['start_in_minutes'] is not None:
            match.start_time = timezone.now() + timedelta(minutes=options['start_in_minutes'])
            update_fields.append('start_time')

        if options['status']:
            match.status = options['status']
            update_fields.append('status')
            if options['status'] == 'finished':
                match.end_time = timezone.now()
                match.voting_open_until = timezone.now() + timedelta(hours=options['voting_hours'])
                update_fields += ['end_time', 'voting_open_until']

        if options['home_score'] is not None:
            match.home_score = options['home_score']
            update_fields.append('home_score')

        if options['away_score'] is not None:
            match.away_score = options['away_score']
            update_fields.append('away_score')

        if not update_fields:
            self.stdout.write(self.style.WARNING(
                "Ничего не передано (--start-in-minutes / --status / --home-score / "
                "--away-score / --release) — матч не изменён."
            ))
            return

        # Иначе автосинк перезапишет данные.
        match.manual_override = True
        update_fields.append('manual_override')

        match.save(update_fields=update_fields + ['updated_at'])

        self.stdout.write(self.style.SUCCESS(
            f"✅ {match.home_team.name} vs {match.away_team.name}\n"
            f"   status={match.status}   start_time={match.start_time:%d.%m.%Y %H:%M}\n"
            f"   score={match.get_score_display()}\n"
            f"   prediction_opens_at={match.prediction_opens_at():%d.%m.%Y %H:%M}   "
            f"is_prediction_open={match.is_prediction_open()}\n"
            f"   manual_override=True — не забудьте вернуть командой "
            f"`--release`, если это настоящий будущий матч из расписания, а не тестовая запись.\n"
            f"   Страница матча: /matches/{match.id}/"
        ))

    def _list_recent(self):
        matches = Match.objects.select_related('home_team', 'away_team').order_by('-start_time')[:15]
        if not matches:
            self.stdout.write(self.style.WARNING("В БД нет ни одного матча."))
            return
        self.stdout.write(
            "Последние матчи (external_id/sportmonks_id — короче, передайте "
            "первым аргументом команды; матчи, созданные после cutover на "
            "Sportmonks, имеют только sportmonks_id, external_id у них пуст):\n"
        )
        for m in matches:
            self.stdout.write(
                f"  external_id={m.external_id or '—':<8} sportmonks_id={m.sportmonks_id or '—':<8} "
                f"[{m.status:<9}]  {m.start_time:%d.%m %H:%M}   "
                f"{m.home_team.name} vs {m.away_team.name}   ({m.get_score_display()})   uuid={m.id}"
            )
