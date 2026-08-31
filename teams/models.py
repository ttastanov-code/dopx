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
        verbose_name=_('Логотип')
    )
    logo_url = models.URLField(
        blank=True,
        null=True,
        verbose_name=_('URL логотипа')
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
    # НОВОЕ (2026-08-31): автоматическая "фирменная палитра" клуба для
    # hero-баннера страницы матча (templates/matches/_match_header.html) —
    # чтобы шапка красилась в цвета команд, а не только в цвет статуса
    # матча (live/завершён/...). Считается автоматически из logo/logo_url
    # через teams/services.py::extract_team_colors — см. management-
    # команду teams/management/commands/compute_team_colors.py, сигнал
    # teams/signals.py (пересчёт при сохранении новой команды) и действие
    # в админке TeamAdmin. Пустая строка = ещё не посчитан или у команды
    # нет логотипа — шаблон в этом случае просто не подставляет цвет
    # команды и hero остаётся окрашен по статусу матча, как раньше.
    primary_color = models.CharField(
        _('Основной фирменный цвет (HEX)'),
        max_length=7,
        blank=True,
        help_text=_('Например #1a2b3c — извлекается автоматически из логотипа, вручную менять не обязательно.'),
    )
    # Большинство эмблем в лиге двухцветные ("Қайрат" — жёлтый+чёрный,
    # "Ордабасы" — голубой+белый и т.д.) — второй отчётливый цвет
    # логотипа, если он есть (см. docstring extract_team_colors). Пустая
    # строка = логотип фактически однотонный либо второй цвет ещё не
    # посчитан — шаблон в этом случае просто использует один primary_color.
    secondary_color = models.CharField(
        _('Второй фирменный цвет (HEX)'),
        max_length=7,
        blank=True,
        help_text=_('Второй цвет двухцветной эмблемы — тоже извлекается автоматически, может быть пустым для однотонных логотипов.'),
    )

    class Meta:
        verbose_name = _('Команда')
        verbose_name_plural = _('Команды')
        ordering = ['name']
    
    def __str__(self):
        return self.name
    
    @property
    def logo_display(self):
        """Возвращает логотип (URL или файл)"""
        return self.logo_url or (self.logo.url if self.logo else None)


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