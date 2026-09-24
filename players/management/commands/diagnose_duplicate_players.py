# players/management/commands/diagnose_duplicate_players.py
"""manage.py diagnose_duplicate_players

Read-only отчёт о дублях игроков: одинаковое ФИО (normalize_kz) в одной команде
с разными sportmonks_id. По каждой группе — «протоколы» (составы/события/последний матч)
и «вовлечённость» (оценки/агрегаты). Слияние — merge_duplicate_players.
Разное написание имени не ловит.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from core.utils import normalize_kz
from players.models import Player


class Command(BaseCommand):
    help = "Read-only: ищет вероятных дублей игроков (одинаковое ФИО в одной команде, разные sportmonks_id)."

    def handle(self, *args, **options):
        players = list(
            Player.objects.select_related("team").only(
                "id", "first_name", "last_name", "team_id", "team__name",
                "sportmonks_id", "number", "name_source", "created_at",
                "is_active", "last_match_at",
            )
        )

        groups: dict[tuple, list[Player]] = {}
        for p in players:
            key = (p.team_id, normalize_kz(p.first_name.strip()), normalize_kz(p.last_name.strip()))
            groups.setdefault(key, []).append(p)

        dupe_groups = [g for g in groups.values() if len(g) > 1]

        if not dupe_groups:
            self.stdout.write(self.style.SUCCESS("Дублей по точному совпадению ФИО+команда не найдено."))
            return

        total_rows = sum(len(g) for g in dupe_groups)
        self.stdout.write(self.style.WARNING(
            f"Найдено групп: {len(dupe_groups)}, записей в них: {total_rows} "
            f"(из {len(players)} игроков всего).\n"
            "ВАЖНО: это только ТОЧНОЕ совпадение ФИО — если Sportmonks прислал разное "
            "написание для одного и того же человека, этот отчёт такую пару не покажет "
            "(нужна ручная проверка по номеру/фото/команде отдельно)."
        ))

        for group in sorted(dupe_groups, key=lambda g: (g[0].team.name if g[0].team else "", g[0].last_name)):
            team_name = group[0].team.name if group[0].team else "—"
            # Значения без пробелов — format_command_output делит пары по пробелам.
            self.stdout.write(f"\n=== {group[0].first_name} {group[0].last_name} — «{team_name}» ({len(group)} записи) ===")
            for p in group:
                appearances = p.matchlineupplayer_set.count()
                events_count = p.events.count()
                evaluations_count = p.player_evaluations.count()
                aggregates_count = p.match_aggregates.count()
                last_match = p.last_match_at.strftime("%Y-%m-%dT%H:%M") if p.last_match_at else "нет"
                self.stdout.write(
                    f"  id={p.id} sportmonks_id={p.sportmonks_id} номер={p.number if p.number is not None else '—'} "
                    f"активен={p.is_active} источник_фио={p.name_source or 'неизвестно'} создан={p.created_at:%Y-%m-%d}"
                )
                # Две строки key=value: протоколы и вовлечённость.
                self.stdout.write(f"    тип=протоколы составов={appearances} событий={events_count} последний_матч={last_match}")
                self.stdout.write(f"    тип=вовлечённость оценок={evaluations_count} агрегатов={aggregates_count}")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово. Групп для разбора: {len(dupe_groups)}. Ничего не изменено — только отчёт."
        ))
