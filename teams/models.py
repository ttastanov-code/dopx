# teams/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from seasons.models import Season  # ✅ Импортируем Season


class Team(BaseModel):
    """Футбольная команда"""
    
    name = models.CharField(
        max_length=255,
        verbose_name=_('Название')
    )
    logo = models.ImageField(
        upload_to='teams/',
        blank=True,
        null=True,
        verbose_name=_('Логотип (ручная загрузка)'),
        help_text=_(
            'Если загружен — всегда показывается вместо герба из Sportmonks '
            'и НИКОГДА не перезаписывается синком. Используйте, если герб '
            'источника устарел/неверен.'
        ),
    )
    logo_url = models.URLField(
        blank=True,
        null=True,
        verbose_name=_('URL логотипа (Sportmonks)'),
        help_text=_(
            'Заполняется и обновляется автоматически при каждом синке '
            'Sportmonks. Не редактируйте вручную — правки затрутся при '
            'следующем синке; для ручного герба используйте поле выше.'
        ),
    )
    city = models.CharField(
        max_length=120,
        blank=True,
        verbose_name=_('Город')
    )
    external_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
        verbose_name=_('Внешний ID')
    )
    # ВАЖНО: это ДРУГОЙ id, чем external_id выше. external_id — id команды
    # в JSON API KFF (parsers/kff/client.py, используется для импорта
    # матчей). kff_website_id — id той же команды на публичном сайте
    # kffleague.kz (URL вида /ru/team/{id}) — отдельная нумерация в другом
    # бэкенде того же KFF, нужна ТОЛЬКО для скрапинга фото игроков
    # (parsers/kff/photo_scraper.py), заполняется автоматически при первом
    # запуске скрапера через сопоставление названий команд.
    kff_website_id = models.CharField(
        _('ID команды на сайте KFF'),
        max_length=20, blank=True, null=True, unique=True,
        help_text=_('Числовой id из URL kffleague.kz/ru/team/<id> — для скрапинга фото игроков.'),
    )
    # См. комментарий у League.sportmonks_id (leagues/models.py). Для
    # команд заполняется один раз вручную по итогам сверки 16 клубов КПЛ
    # (docs/sportmonks-migration-plan.md, фаза 2) — список маленький и
    # стабильный, автоматический fuzzy-мэтчинг тут не нужен и рискованнее
    # ручной проверки.
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100, blank=True, null=True, unique=True,
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_('Активна')
    )
    # НОВОЕ: продуктовое решение (не автоматика) — какие пары команд
    # считаются принципиальными соперниками ("дерби"), проставляется один
    # раз вручную в админке (см. teams/admin.py::TeamAdmin.filter_horizontal).
    # Используется бейджем "derby_hunter" (users/badges.py). Самоссылочное
    # ManyToManyField по умолчанию симметрично: если A добавлен в rivals B,
    # то B автоматически оказывается в rivals A — отдельный related_name не
    # нужен.
    rivals = models.ManyToManyField(
        'self',
        blank=True,
        verbose_name=_('Принципиальные соперники'),
        help_text=_('Команды, матчи с которыми считаются дерби для бейджа «Дерби-эксперт».'),
    )
    class Meta:
        verbose_name = _('Команда')
        verbose_name_plural = _('Команды')
        ordering = ['name']
    
    def __str__(self):
        return self.name
    
    @property
    def logo_display(self):
        """Возвращает логотип (файл или URL).

        ВАЖНО (2026-09-09, вопрос пользователя "менеджер сказал логотипы
        обновят за 24 часа, но мы вроде сделали так, чтобы не
        обновлялись"): `logo_url` теперь СВОБОДНО перезаписывается каждым
        синком Sportmonks (см. parsers/sportmonks/importers.py::
        get_or_create_team) — если источник обновит герб, это само
        подтянется на сайт. Защита от затирания переехала сюда: `logo`
        (загруженный вручную в админке файл) — это staff-override, и он
        ВСЕГДА в приоритете над `logo_url`, независимо от того, что
        прислал Sportmonks. Раньше приоритет был обратный (logo_url
        всегда выигрывал), из-за чего ручная загрузка файла в админке
        молча игнорировалась на странице — то был реальный баг, а не
        просто "защита от обновлений", извиняюсь за путаницу.
        """
        return (self.logo.url if self.logo else None) or self.logo_url


class TeamSeason(BaseModel):
    """Привязка команды к сезону"""
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        verbose_name=_('Команда')
    )
    season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        verbose_name=_('Сезон')
    )
    
    class Meta:
        verbose_name = _('Команда в сезоне')
        verbose_name_plural = _('Команды в сезонах')
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'season'],
                name='unique_team_season'
            )
        ]
    
    def __str__(self):
        return f"{self.team} — {self.season}"


# ✅ НОВАЯ МОДЕЛЬ: Кэшированная статистика команды в сезоне
class TeamSeasonStats(BaseModel):
    """Кэшированная статистика команды в сезоне (для турнирной таблицы)"""
    
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        verbose_name=_('Команда')
    )
    season = models.ForeignKey(
        'seasons.Season',  # ✅ ПРАВИЛЬНО: 'app.Model'
        on_delete=models.CASCADE,
        verbose_name=_('Сезон')
    )
    
    # Статистика
    played = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Игры')
    )
    wins = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Победы')
    )
    draws = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Ничьи')
    )
    losses = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Поражения')
    )
    goals_scored = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Забитые голы')
    )
    goals_conceded = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Пропущенные голы')
    )
    goal_diff = models.IntegerField(
        default=0,
        verbose_name=_('Разница мячей')
    )
    points = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Очки')
    )
    
    # Позиция в таблице
    position = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Позиция')
    )
    
    class Meta:
        verbose_name = _('Статистика команды в сезоне')
        verbose_name_plural = _('Статистика команд в сезоне')
        ordering = ['-points', '-goal_diff', '-goals_scored']
        constraints = [
            models.UniqueConstraint(
                fields=['team', 'season'],
                name='unique_team_season_stats'
            )
        ]
        indexes = [
            models.Index(
                fields=['season', '-points', '-goal_diff'],
                name='team_season_stats_season_idx'
            ),
        ]
    
    def __str__(self):
        return f"{self.team} — {self.season} ({self.points} очков)"
    
    def update_stats(self):
        """Пересчитывает статистику из матчей"""
        from matches.models import Match
        from django.db.models import F, Q, Count, Sum, Coalesce
        
        stats = Match.objects.filter(
            season=self.season,
            status='finished'
        ).aggregate(
            played=Count('id', filter=Q(home_team=self.team) | Q(away_team=self.team)),
            wins=Count('id', filter=(
                (Q(home_team=self.team) & Q(home_score__gt=F('away_score'))) |
                (Q(away_team=self.team) & Q(away_score__gt=F('home_score')))
            )),
            draws=Count('id', filter=(
                (Q(home_team=self.team) & Q(home_score=F('away_score'))) |
                (Q(away_team=self.team) & Q(away_score=F('home_score')))
            )),
            goals_scored=Coalesce(Sum('home_score', filter=Q(home_team=self.team)), 0) + 
                         Coalesce(Sum('away_score', filter=Q(away_team=self.team)), 0),
            goals_conceded=Coalesce(Sum('away_score', filter=Q(home_team=self.team)), 0) + 
                          Coalesce(Sum('home_score', filter=Q(away_team=self.team)), 0),
        )
        
        self.played = stats['played'] or 0
        self.wins = stats['wins'] or 0
        self.draws = stats['draws'] or 0
        self.losses = self.played - self.wins - self.draws
        self.goals_scored = stats['goals_scored'] or 0
        self.goals_conceded = stats['goals_conceded'] or 0
        self.goal_diff = self.goals_scored - self.goals_conceded
        self.points = self.wins * 3 + self.draws
        
        self.save(update_fields=[
            'played', 'wins', 'draws', 'losses',
            'goals_scored', 'goals_conceded', 'goal_diff', 'points'
        ])