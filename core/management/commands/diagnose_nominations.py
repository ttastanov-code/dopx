# core/management/commands/diagnose_nominations.py
"""manage.py diagnose_nominations

Read-only диагностика номинаций на главной: агрегаты-победители без пересчитанных метрик
и агрегаты, ссылающиеся на несуществующих игроков.
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
        cache.delete('nominations_global')  # без 5-минутного кэша
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
