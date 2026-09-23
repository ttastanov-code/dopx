# players/management/commands/diagnose_duplicate_players.py
"""
manage.py diagnose_duplicate_players

2026-09-22, прямая просьба пользователя после того, как обнаружил живьём
на странице команды «Женис» одного и того же игрока (Николай Горобченко,
Ермухаммед Бесенгалиев) с двумя РАЗНЫМИ sportmonks_id — статистика и
рейтинги размазаны между двумя записями. Причина: Sportmonks иногда
переоформляет/переиздаёт id одного и того же реального человека (возврат
из аренды, техническая правка данных на их стороне и т.п.), а наш
импортёр (parsers/sportmonks/importers.py) матчит игрока СТРОГО по
sportmonks_id — при новом id для уже известного человека создаёт новую
запись Player вместо того, чтобы узнать существующую.

Read-only отчёт (danger="readonly" в dashboard/commands_registry.py) —
НИЧЕГО не меняет и не сливает автоматически (прямое решение пользователя:
"сначала отчёт, потом вручную решаю"). Для каждой найденной группы дублей
печатает ключевые цифры — по ним staff сам решает, какую запись оставить
(--keep), а какую слить (--merge) через merge_duplicate_players.

Цифры разбиты на два блока по ПРОИСХОЖДЕНИЮ данных, не по "надёжности" —
какой блок весомее для решения, staff оценивает сам по ситуации на своей
базе (2026-09-22: на dev/staging базе прямо сейчас player_evaluations
насеяны тестовыми ботами через seed_full_history/seed_match_votes, и
тогда "составов"/"последний_матч" — единственный осмысленный сигнал; но
это свойство ТЕКУЩЕГО состояния данных, не факт о коде — в проде
"оценок"/"агрегатов" будут настоящими голосами пользователей и станут не
менее значимым сигналом, чем протоколы матчей. Поэтому в самом выводе
команды НЕТ жёсткого суждения "это фейк, не смотри сюда" — такое суждение
устарело бы в проде и вводило бы в заблуждение):
  - "протоколы" — из импорта Sportmonks (составов/событий/последний_матч).
  - "вовлечённость" — из community-слоя сайта (оценок/агрегатов рейтинга).

ОГРАНИЧЕНИЕ: ищет ТОЛЬКО точное совпадение (нормализованное через
core.utils.normalize_kz — снимает разницу казахских/русских омографов и
регистра) first_name+last_name в пределах одной команды. Если Sportmonks
прислал РАЗНОЕ написание для одного и того же человека (см. живой пример
из этого же прогона: "Владислав Наумец" и мусорная запись "Настоящая
тяжелая работа" под тем же 9-м номером «Жениса») — этот отчёт такую пару
не увидит, нужна ручная проверка по номеру/фото/команде отдельно.
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
            # 2026-09-22: значения ниже НАМЕРЕННО без пробелов внутри (дата
            # последнего матча — через "T", не через " ") — формат
            # dashboard/templatetags/dashboard_extras.py::format_command_output
            # раскрашивает строки вида "ключ=значение ключ2=значение2 ..." по
            # ПРОБЕЛАМ между парами, пробел ВНУТРИ значения ломает разбор.
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
                # Две строки по ПРОИСХОЖДЕНИЮ данных (см. докстринг команды
                # выше — почему тут нет суждения "фейк"/"настоящее"). "тип=..."
                # — часть той же key=value строки, не отдельный текст в
                # скобках, иначе format_command_output не подхватит строку
                # под бейджи.
                self.stdout.write(f"    тип=протоколы составов={appearances} событий={events_count} последний_матч={last_match}")
                self.stdout.write(f"    тип=вовлечённость оценок={evaluations_count} агрегатов={aggregates_count}")

        self.stdout.write(self.style.SUCCESS(
            f"\nГотово. Групп для разбора: {len(dupe_groups)}. Ничего не изменено — только отчёт."
        ))
