# players/management/commands/clear_player_photos.py
"""
manage.py clear_player_photos [--apply]

Разовая чистка фото игроков, оставшихся от отменённого автоматического
импорта с kffleague.kz (решение 2026-08-21 — см. core/templatetags/
avatar_extras.py и parsers/kff/photo_scraper.py: скачивание фото убрано
из кода, но уже СКАЧАННЫЕ файлы и заполненное поле Player.photo в БД
код сам по себе не трогает — понадобился отдельный разовый прогон).

Без --apply — dry-run: только считает, сколько игроков затронет, ничего
не удаляет и не пишет в БД (тот же паттерн, что у dedupe_referees_coaches.py
и scrape_kff_photos.py). --apply — реально удаляет файл с диска
(player.photo.delete) и очищает поле, после чего на сайте у этих игроков
показывается генеративный аватар (градиент + инициалы) вместо фото — см.
templates/components/_avatar.html.

НЕ трогает Coach.photo — те фото не из отменённого KFF-скрапера (он работал
только с Player), их удаление не входило в задачу отката.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from players.models import Player


class Command(BaseCommand):
    help = "Удаляет фото игроков (файл + поле Player.photo), оставшиеся от отменённого импорта с KFF"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить файлы и очистить поле")

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]

        players_with_photo = Player.objects.exclude(photo="").exclude(photo__isnull=True)
        count = players_with_photo.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("У игроков нет проставленных фото — нечего чистить."))
            return

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                f"dry-run: у {count} игроков проставлено фото. Запустите с --apply, чтобы удалить файлы "
                f"и очистить поле (на сайте вместо них появится генеративный аватар)."
            ))
            for player in players_with_photo[:20]:
                self.stdout.write(f"  · {player.full_name} ({player.team.name if player.team else '—'})")
            if count > 20:
                self.stdout.write(f"  … и ещё {count - 20}")
            return

        cleared = 0
        for player in players_with_photo.iterator():
            # delete(save=False) — сначала удаляем файл с диска/стораджа,
            # save() вызываем один раз сами ниже вместе с очисткой поля,
            # чтобы не делать два отдельных UPDATE на каждого игрока.
            player.photo.delete(save=False)
            player.photo = None
            player.save(update_fields=["photo"])
            cleared += 1

        self.stdout.write(self.style.SUCCESS(f"Готово — очищено фото у {cleared} игроков."))
