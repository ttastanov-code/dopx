# core/management/commands/simulate_match_timing.py
"""
ТОЛЬКО для локального/staging тестирования — см. docs/BACKLOG.md, раздел
"Как тестировать retention loops без реальных матчей" (2026-08-21).

Реальные матчи приходят из KFF по своему расписанию — раз в несколько дней
или неделю, и виджет прогнозов (predictions app), и все Celery-таски
retention loops (notify_prediction_closing_soon, notify_prediction_results,
send_weekly_summary — см. notifications/tasks.py) завязаны на РЕАЛЬНОЕ
время (`Match.start_time`/`status`/`end_time`), поэтому без матча "прямо
сейчас, на нужной стадии" их руками не потрогать.

Эта команда НЕ создаёт синтетический матч с нуля (риск нарушить допущения
аггрегатов/парсера про реальные team/league/season FK, на которые опираются
многие queryset'ы по всему проекту) — она берёт УЖЕ существующий в БД матч
(любого статуса) и двигает его по времени/статусу/счёту, выставляя
`manual_override=True` (то же поле, что для матчей с перенесённой датой —
см. докстринг у поля в matches/models.py), чтобы автосинк
(parsers/tasks.py::update_match_statuses) не перезаписал подделанные данные
реальными от KFF на следующем цикле парсера.

После тестов подделанный матч стоит либо вернуть `--release`, либо (если
это тестовая учебная запись, а не настоящий будущий матч из расписания) не
трогать — очередной реальный прогон парсера всё равно не заденет его, пока
manual_override не снят вручную.

Первый аргумент принимает ЛИБО внутренний UUID (первичный ключ, обычно не
виден пользователю), ЛИБО `external_id` — числовой id матча из KFF (тот,
что виден в админке/`/staff/dashboard/parser/`) — например `1053`.

Примеры:

  # Не знаете id? Список последних матчей:
  python manage.py simulate_match_timing

  # Матч стартует через 50 минут — открывает окно прогноза (виджет на
  # странице матча) и заодно окно "закрывается через час" для ручного
  # запуска notify_prediction_closing_soon через /staff/dashboard/parser/:
  python manage.py simulate_match_timing <id> --status scheduled --start-in-minutes 50

  # Матч только что завершился 2:1 — для notify_prediction_results:
  python manage.py simulate_match_timing <id> --status finished --home-score 2 --away-score 1

  # Матч ещё далеко (за пределами PREDICTION_WINDOW_DAYS) — проверить
  # сообщение "прогнозы откроются позже" в виджете:
  python manage.py simulate_match_timing <id> --status scheduled --start-in-minutes 20160

  # Вернуть матч под управление автосинка после тестов:
  python manage.py simulate_match_timing <id> --release
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
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
            help='Снять manual_override — вернуть матч под управление автосинка KFF, ничего больше не менять.',
        )

    def handle(self, *args, **options):
        match_id = options['match_id']

        if not match_id:
            self._list_recent()
            return

        # Принимает и внутренний UUID (первичный ключ, скрыт от пользователя
        # в обычном UI), и `external_id` — числовой id матча из KFF, который
        # реально виден на сайте (админка, "Детали матча" в /staff/dashboard/
        # parser/, RAW JSON парсера) и которым пользователи и будут
        # пользоваться на практике.
        import uuid as uuid_module

        try:
            uuid_module.UUID(str(match_id))
            lookup = {'id': match_id}
        except (ValueError, AttributeError, TypeError):
            lookup = {'external_id': match_id}

        try:
            match = Match.objects.select_related('home_team', 'away_team').get(**lookup)
        except (Match.DoesNotExist, ValueError, ValidationError):
            raise CommandError(
                f"Матч с {list(lookup.keys())[0]}={match_id!r} не найден. "
                f"Запустите команду без аргументов, чтобы увидеть список последних матчей."
            )

        if options['release']:
            match.manual_override = False
            match.save(update_fields=['manual_override', 'updated_at'])
            self.stdout.write(self.style.SUCCESS(
                f"✅ manual_override снят — {match} снова под управлением автосинка KFF."
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

        # Иначе следующий прогон update_match_statuses может тут же
        # перезаписать подделанные данные реальными от KFF.
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
        self.stdout.write("Последние матчи (external_id — короче, передайте первым аргументом команды):\n")
        for m in matches:
            self.stdout.write(
                f"  external_id={m.external_id or '—':<8} [{m.status:<9}]  {m.start_time:%d.%m %H:%M}   "
                f"{m.home_team.name} vs {m.away_team.name}   ({m.get_score_display()})   uuid={m.id}"
            )
