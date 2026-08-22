# parsers/management/commands/scrape_kff_photos.py
"""
manage.py scrape_kff_photos [--apply] [--team NAME] [--limit N]

Сопоставление команд/игроков DOPX с kffleague.kz (parsers/kff/photo_scraper.py) —
та же двухрежимная логика доверия, что у dedupe_referees_coaches.py:

1. Без --apply — dry-run: показывает, какие команды/игроки совпали по
   имени (normalize_kz), какие не нашлись ни с одной стороны. НИЧЕГО не
   пишет в БД — можно проверить качество совпадений перед реальным запуском.
2. --apply — реально проставляет Team.kff_website_id/Player.kff_website_id
   и бэкафиллит ПУСТОЙ Player.position грубым кодом (GK/DF/MF/FW),
   выведенным из группировки состава на публичном сайте — см. докстринг
   parsers/kff/photo_scraper.py::scrape_team_squad. Никогда не перетирает
   уже проставленную позицию (она обычно приходит из JSON API с более
   точным кодом).

ФОТО НЕ СКАЧИВАЕТ (команда переименована из "фото" по названию, но с
2026-08-21 скачивание убрано целиком — см. core/templatetags/avatar_extras.py:
вместо фото везде, где его нет, показывается генеративный аватар). Имя
команды и файла оставлено прежним ради минимального диффа и привычки в
крон-джобах/памяти — переименование самой management-команды не требуется
для решения задачи.

--team NAME фильтрует по названию клуба (подстрока, регистронезависимо)
— полезно прогнать сначала на одной команде и проверить результат глазами
перед прогоном на всю лигу.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from parsers.kff.photo_scraper import match_and_fetch_players_for_team, match_teams
from teams.models import Team


class Command(BaseCommand):
    help = "Сопоставляет команды/игроков DOPX с kffleague.kz (ID + позиция, без фото)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать kff_website_id и позицию")
        parser.add_argument("--team", type=str, default=None, help="Ограничить одной командой (подстрока названия)")
        parser.add_argument("--limit", type=int, default=None, help="Максимум команд за прогон (для теста)")

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]
        dry_run = not apply_changes

        self.stdout.write(self.style.NOTICE(
            f"Режим: {'ПРИМЕНЯЕМ изменения' if apply_changes else 'dry-run (ничего не меняем)'}"
        ))

        self.stdout.write("Шаг 1/2: сопоставление команд по названию (kffleague.kz/teams)...")
        team_report = match_teams(dry_run=dry_run)
        for name, wid in team_report["matched"]:
            self.stdout.write(f"  ✅ {name} -> kff_website_id={wid}")
        if team_report["already_set"]:
            self.stdout.write(f"  ℹ️  уже сопоставлены ранее: {len(team_report['already_set'])}")
        if team_report["unmatched_kff"]:
            self.stdout.write(self.style.WARNING(
                f"  ⚠️ на сайте KFF есть, в DOPX не нашли: {', '.join(team_report['unmatched_kff'])}"
            ))
        if team_report["unmatched_dopx"]:
            self.stdout.write(self.style.WARNING(
                f"  ⚠️ в DOPX есть, на сайте KFF не нашли: {', '.join(team_report['unmatched_dopx'])}"
            ))

        if dry_run:
            self.stdout.write(self.style.NOTICE(
                "\nЭто был dry-run команд — id НЕ сохранены. Запустите --apply, чтобы продолжить к игрокам "
                "(шаг 2 команде нужен реальный Team.kff_website_id, поэтому во время dry-run он пропускается)."
            ))
            return

        self.stdout.write("\nШаг 2/2: сопоставление игроков по каждой команде...")
        teams_qs = Team.objects.filter(is_active=True, kff_website_id__isnull=False)
        if options["team"]:
            teams_qs = teams_qs.filter(name__icontains=options["team"])
        if options["limit"]:
            teams_qs = teams_qs[: options["limit"]]

        if not teams_qs.exists():
            self.stdout.write(self.style.WARNING(
                "Нет команд с проставленным kff_website_id, подходящих под фильтр — нечего скрапить."
            ))
            return

        for team in teams_qs:
            self.stdout.write(f"\n— {team.name} —")
            report = match_and_fetch_players_for_team(team, dry_run=False)
            if "error" in report:
                self.stdout.write(self.style.ERROR(f"  {report['error']}"))
                continue
            self.stdout.write(f"  Сопоставлено точно: {len(report['matched'])}")
            if report["fuzzy_matched"]:
                self.stdout.write(self.style.NOTICE(
                    f"  ✨ Сопоставлено по похожести (проверьте глазами): " + ", ".join(
                        f"{dopx_name} ≈ {kff_name} ({ratio})"
                        for dopx_name, kff_name, ratio in report["fuzzy_matched"]
                    )
                ))
            if report["review_candidates"]:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠️ Похожи, но НЕ сопоставлены (нужна ручная проверка): " + ", ".join(
                        f"{dopx_name} ?= {kff_name} ({ratio})"
                        for dopx_name, kff_name, ratio in report["review_candidates"]
                    )
                ))
            if report["positions_backfilled"]:
                self.stdout.write(self.style.SUCCESS(
                    f"  Позиция проставлена (была пустой): " + ", ".join(
                        f"{name} → {code}" for name, code in report["positions_backfilled"]
                    )
                ))
            if report["unmatched_kff"]:
                self.stdout.write(self.style.WARNING(
                    f"  На сайте есть, в DOPX не нашли: {', '.join(report['unmatched_kff'])}"
                ))
                # Расшифровка ПОЧЕМУ — на каждое имя из строки выше, без
                # этого приходится каждый раз вручную считать difflib-ratio
                # руками, чтобы понять, опечатка это или два разных
                # человека (см. докстринг match_and_fetch_players_for_team).
                for kff_name, reason in report["unmatched_kff_details"]:
                    self.stdout.write(f"      · {kff_name} — {reason}")
            if report["unmatched_dopx"]:
                self.stdout.write(self.style.WARNING(
                    f"  В DOPX есть, на сайте не нашли: {', '.join(report['unmatched_dopx'])}"
                ))

        self.stdout.write(self.style.SUCCESS("\nГотово."))
