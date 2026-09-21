# core/management/commands/diagnose_nominations.py
"""
manage.py diagnose_nominations

ТОЛЬКО ЧТЕНИЕ — ничего не меняет и не удаляет. Написана 2026-09-21 в ответ
на жалобу пользователя: на главной странице виджет "Номинации сезона"
показывает всего 2 карточки (обе — игрок "Under Dog", 0.0/10), а переход
на профиль игрока даёт 404 "Не найден ни один Игрок" — хотя на странице
конкретной лиги (leagues/views.py::LeagueDetailView, отфильтровано по
лиге+активному сезону) та же самая витрина полностью заполнена реальными
данными.

ПОЧЕМУ ОТДЕЛЬНАЯ ДИАГНОСТИЧЕСКАЯ КОМАНДА, А НЕ СРАЗУ ФИКС: в песочнице
разработки этой сессии нет доступа к реальной БД пользователя (только к
файлам кода) — из чтения одного core/nominations.py нельзя однозначно
отличить две РАЗНЫЕ по природе причины:

  (а) core/views.py::HomeView намеренно зовёт get_nominations() БЕЗ
      фильтра — "по всей платформе, за всё время" (см. докстринг
      core/nominations.py, пункт "Используется на двух страницах") — и
      если в этом безлимитном пуле есть ХОТЬ ОДНА строка-агрегат с
      реальным total_votes, но НЕ пересчитанным (default 0.0)
      avg_potential/risk_index (например, старая запись до появления
      этих полей, ни разу не пересчитанная заново), она может выигрывать
      номинацию просто потому, что остальные строки либо тоже на 0.0
      (тогда выигрывает "первая по сортировке"), либо не набирают
      MIN_VOTES=3 вообще;
  (б) сама строка-агрегат ссылается (`player_id`) на игрока, которого
      физически больше нет в `players_player` — при том что
      PlayerMatchAggregate.player — обычный Django ForeignKey с
      on_delete=CASCADE (aggregates/models.py) на PostgreSQL, где такое
      не должно происходить ни при удалении через ORM, ни при обычном
      DELETE в БД с реальным constraint'ом — если это подтвердится,
      это отдельная, более серьёзная проблема целостности данных.

Эта команда печатает факты по обеим гипотезам — дальше уже понятно, что
чинить: код (core/views.py::HomeView, scope), данные (пересчитать/удалить
конкретную "мусорную" строку) или и то, и другое.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from aggregates.models import PlayerMatchAggregate
from core.nominations import MIN_VOTES, _aggregate, _scope
from players.models import Player


class Command(BaseCommand):
    help = "Диагностика (только чтение) виджета 'Номинации сезона' на главной странице"

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("1. Реальные номинации (как их сейчас видит главная — без фильтра)"))

        from core.nominations import get_nominations
        from django.core.cache import cache
        cache.delete('nominations_global')  # обходим 5-минутный кэш — нужны свежие данные прямо сейчас
        nominations = get_nominations()
        if not nominations:
            self.stdout.write("  (пусто — get_nominations() вообще ничего не вернула)")
        for nom in nominations:
            self.stdout.write(
                f"  [{nom['key']}] {nom['entity_name']!r} — {nom['value_label']} "
                f"(votes={nom['votes']}, entity_kind={nom['entity_kind']}, entity_id={nom['entity_id']})"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("2. Проверка: entity_id из п.1 реально существует в своей таблице?"))
        for nom in nominations:
            if nom['entity_kind'] != 'player':
                continue
            exists = Player.objects.filter(id=nom['entity_id']).exists()
            marker = self.style.SUCCESS("OK") if exists else self.style.ERROR("ОРФАН — строки Player с таким id НЕТ")
            self.stdout.write(f"  player id={nom['entity_id']} ({nom['entity_name']!r}): {marker}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "3. Все PlayerMatchAggregate с total_votes >= %d, но avg_potential=0.0 И risk_index=0.0 "
            "(подозрительные — либо реально нулевые, либо никогда не пересчитывались)" % MIN_VOTES
        ))
        suspicious = (
            PlayerMatchAggregate.objects
            .filter(total_votes__gte=MIN_VOTES, avg_potential=0.0, risk_index=0.0)
            .select_related('player', 'match', 'match__season', 'match__league')
            .order_by('-total_votes')[:20]
        )
        if not suspicious.exists():
            self.stdout.write("  (таких строк нет)")
        for row in suspicious:
            player_label = f"{row.player} (id={row.player_id})" if row.player_id else f"<player_id={row.player_id} — БЕЗ игрока>"
            match_label = f"{row.match} / {row.match.season} / {row.match.league}" if row.match_id else "<без матча>"
            self.stdout.write(f"  aggregate id={row.id}: player={player_label}, votes={row.total_votes}, match={match_label}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "4. Все PlayerMatchAggregate, чей player_id указывает на несуществующего игрока (настоящий орфан на уровне БД)"
        ))
        orphaned = (
            PlayerMatchAggregate.objects
            .exclude(player_id__in=Player.objects.values('id'))
            .select_related('match')[:20]
        )
        if not orphaned.exists():
            self.stdout.write("  (орфанов нет — FK-констрейнт держит целостность, как и должен на PostgreSQL)")
        for row in orphaned:
            self.stdout.write(
                f"  aggregate id={row.id}: player_id={row.player_id} (НЕ СУЩЕСТВУЕТ), "
                f"votes={row.total_votes}, avg_potential={row.avg_potential}, risk_index={row.risk_index}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(
            "5. Сколько всего PlayerMatchAggregate-строк набирают MIN_VOTES=%d без фильтра по лиге/сезону "
            "(это и есть пул, из которого главная выбирает 'Игрока на грани' / 'Скрытый потенциал')" % MIN_VOTES
        ))
        pool = _aggregate(_scope(PlayerMatchAggregate.objects.all(), None, None), 'player', 'risk_index', ())
        self.stdout.write(f"  строк в пуле: {pool.count()}")
