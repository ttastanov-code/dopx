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
    # См. комментарий у League.sportmonks_id (leagues/models.py). Заполняется
    # скриптом реконсиляции (docs/sportmonks-migration-plan.md, фаза 2):
    # сопоставление по (команда + normalize_kz(имя)), уверенные совпадения —
    # автоматически, спорные — в отчёт на ручную проверку, чтобы не
    # породить дублей поверх уже накопленной истории оценок игрока.
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100, blank=True, null=True, unique=True,
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
        help_text=_('Считает подряд идущие проверки состава на kffleague.kz, где игрока не нашли — при достижении порога is_active снимается автоматически. МЁРТВОЕ ПОЛЕ с 2026-09-09: parsers/kff удалён из проекта вместе со скрапером, который его инкрементировал (match_and_fetch_players_for_team/check_roster_departures) — ничего больше это поле не меняет. Оставлено как есть (не удалено), чтобы не терять историю на уже собранных данных.'),
    )
    # НОВОЕ (2026-09-09, жалоба пользователя со скриншотом — "Виктор Васин"
    # всё ещё в текущем составе "Кайрат", хотя последний раз играл в 2024):
    # корень бага — Player.team обновлялся КАЖДЫМ импортированным матчем,
    # включая бэкафилл сезонов НЕ в хронологическом порядке (см. правку в
    # parsers/sportmonks/importers.py::get_or_create_player) — фикстура
    # старого сезона, обработанная ПОСЛЕ новых, отматывала team игрока
    # назад. last_match_at — дата САМОГО СВЕЖЕГО матча, из которого сейчас
    # взят team/number/position; используется как "версия" записи: новую
    # фикстуру применяем, только если она не раньше уже известной. Также
    # даёт честный сигнал для teams/views.py::TeamDetailView — "давно не
    # играл" вместо бессрочного is_active=True (см. коммент к
    # roster_absence_streak про то, что автоматического снятия is_active
    # больше нет).
    last_match_at = models.DateTimeField(
        _('Дата последнего матча'),
        null=True, blank=True,
        help_text=_('Start_time самой свежей фикстуры, из которой обновлялись team/number/position — используется как защита от отката этих полей при бэкафилле не по хронологии.'),
    )

    class Meta:
        verbose_name = _('Игрок')
        verbose_name_plural = _('Игроки')
        ordering = ['last_name', 'first_name']
        # Явные имена (2026-09-09, найдено пользователем: "players app has
        # changes that are not yet reflected in a migration" после чистого
        # `migrate`) — ТОЧНО ТА ЖЕ болезнь, что уже чинили для
        # PlayerSidelined в 0006_rename_playersidelined_index.py: индексы
        # были объявлены без явного имени, а 0001_initial вручную угадал
        # автосгенерированное имя (players_pla_team_id_1a80c1_idx /
        # players_pla_last_na_1786cf_idx) неправильно — Django makemigrations
        # каждый раз видел "неприменённое изменение" на пустом месте. См.
        # players/migrations/0007_rename_player_indexes.py.
        indexes = [
            models.Index(fields=['team', 'is_active'], name='player_team_active_idx'),
            models.Index(fields=['last_name', 'first_name'], name='player_last_first_name_idx'),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class PlayerSidelined(BaseModel):
    """
    НОВОЕ (переход на Sportmonks, находка из тестового ключа): дисквалификации
    и травмы игрока — источник endpoint sidelined у Sportmonks
    (include=sidelined.player на команде), обновляется задачей
    sportmonks_sync_sidelined раз в сутки (docs/sportmonks-migration-plan.md,
    фаза 5). Проверено вживую: данные реальные и актуальные (на момент
    проверки — 1 игрок Тобола отстранён с 07.09 по 14.09.2026).

    У KFF аналога не было вообще — это новая возможность, а не миграция
    существующего поля. Используется бейджем "недоступен" на карточке
    игрока (players/templates), НЕ влияет на Player.is_active и на историю
    оценок — это просто временный статус доступности, отдельный от
    is_active (который про "ушёл из клуба навсегда", см. roster_absence_streak
    выше).
    """
    CATEGORY_CHOICES = [
        ("injury", _("Травма")),
        ("suspended", _("Дисквалификация")),
        ("other", _("Другое")),
    ]

    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="sidelined_periods",
        verbose_name=_('Игрок'),
    )
    category = models.CharField(_('Категория'), max_length=20, choices=CATEGORY_CHOICES, default="other")
    start_date = models.DateField(_('С'), null=True, blank=True)
    end_date = models.DateField(_('По'), null=True, blank=True)
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100, blank=True, null=True, unique=True,
    )

    class Meta:
        verbose_name = _('Недоступность игрока')
        verbose_name_plural = _('Недоступность игроков')
        ordering = ['-start_date']
        indexes = [
            # Явное имя — не полагаемся на автогенерируемый Django хеш (тот
            # же принцип, что у events.models.MatchEvent.match_event_match_
            # minute_idx / EventReaction.event_reaction_type_idx): миграции в
            # этом проекте пишутся вручную без доступа к реальной БД для
            # makemigrations, а угаданный вручную хеш ровно один раз уже
            # разошёлся с тем, что посчитал бы настоящий Django (см. миграцию
            # 0005_player_sportmonks_id_playersidelined.py) — из-за этого
            # `manage.py migrate` молча накатывался, а `makemigrations`
            # продолжал считать индекс "неприменённым изменением".
            models.Index(fields=['player', 'end_date'], name='player_sidelined_end_date_idx'),
        ]

    def __str__(self):
        return f"{self.player} — {self.get_category_display()} ({self.start_date}–{self.end_date or '…'})"

    @property
    def is_current(self) -> bool:
        """Действует ли ограничение прямо сейчас — используется для бейджа
        на карточке игрока, без похода в шаблон с логикой сравнения дат."""
        from django.utils import timezone
        today = timezone.now().date()
        if self.start_date and self.start_date > today:
            return False
        if self.end_date and self.end_date < today:
            return False
        return True