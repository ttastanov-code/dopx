# players/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from teams.models import Team

class Player(BaseModel):
    """Футбольный игрок"""
    first_name = models.CharField(_('Имя'), max_length=120)
    last_name = models.CharField(_('Фамилия'), max_length=120)
    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        related_name="players",
        verbose_name=_('Команда')
    )
    position = models.CharField(_('Позиция'), max_length=50, blank=True)
    number = models.IntegerField(_('Номер'), null=True, blank=True)
    photo = models.ImageField(_('Фото'), upload_to="players/", null=True, blank=True)
    is_active = models.BooleanField(_('Активен'), default=True)
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # Другой id, чем external_id (см. тот же комментарий в teams/models.py::
    # Team.kff_website_id) — id ЭТОГО игрока на публичном сайте kffleague.kz
    # (/ru/player/<id>), не совпадает с id из JSON API составов. Нужен
    # только для сопоставления фото при повторных запусках скрапера, чтобы
    # не парсить состав команды заново на каждый прогон.
    kff_website_id = models.CharField(
        _('ID игрока на сайте KFF'),
        max_length=20, blank=True, null=True, unique=True,
        help_text=_('Числовой id из URL kffleague.kz/ru/player/<id> — для скрапинга фото.'),
    )
    # НОВОЕ (2026-08-31): автоматическое обнаружение игроков, покинувших
    # клуб в течение сезона (найдено на примерах: Дастан Сатпаев, Хуан
    # Себастьян Зебальос — числились в составе на сайте DOPX, хотя реально
    # уже ушли). Раньше состав команды (teams/views.py::TeamDetailView)
    # опирался ТОЛЬКО на Player.team, который обновляется реактивно и с
    # лагом (только когда игрок сыграет за новый клуб и попадёт в протокол
    # матча) — если игрок просто ушёл и нигде больше не сыграл (другая
    # лига, завершил карьеру, долгая пауза), Player.team навсегда
    # оставался указывать на старый клуб. У KFF, как выяснилось, есть
    # публичная страница АКТУАЛЬНОГО состава команды
    # (kffleague.kz/team/<id>?tab=squad, см. parsers/kff/photo_scraper.py),
    # уже скрапится раз в 3 дня для фото/позиций — используем тот же скрап
    # (match_and_fetch_players_for_team) и для этого: если игрок команды
    # (is_active=True) НЕ найден на свежей странице состава — счётчик
    # растёт на 1; если найден — сбрасывается в 0. При достижении
    # ROSTER_ABSENCE_THRESHOLD (см. photo_scraper.py) подряд — считаем уход
    # подтверждённым и автоматически снимаем is_active (защита от
    # ложных срабатываний из-за случайного сбоя скрапинга одной странице —
    # см. докстринг check_roster_departures()).
    roster_absence_streak = models.PositiveIntegerField(
        _('Подряд отсутствовал в составе на сайте KFF'),
        default=0,
        help_text=_('Считает подряд идущие проверки состава на kffleague.kz, где игрока не нашли — при достижении порога is_active снимается автоматически.'),
    )

    class Meta:
        verbose_name = _('Игрок')
        verbose_name_plural = _('Игроки')
        ordering = ['last_name', 'first_name']
        indexes = [
            models.Index(fields=['team', 'is_active']),
            models.Index(fields=['last_name', 'first_name']),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"