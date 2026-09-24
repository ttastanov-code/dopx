# matches/models.py
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from leagues.models import League
from seasons.models import Season
from teams.models import Team
from coaches.models import Coach
from referees.models import Referee


class Match(BaseModel):
    """Футбольный матч."""
    STATUS_CHOICES = [
        ("scheduled", _("Запланирован")),
        ("live", _("Идёт")),
        ("finished", _("Завершён")),
        ("postponed", _("Перенесён")),
        ("cancelled", _("Отменён")),
    ]
    
    # PROTECT — нельзя удалить лигу/сезон, пока на них есть матчи.
    league = models.ForeignKey(League, on_delete=models.PROTECT, verbose_name=_('Лига'))
    season = models.ForeignKey(Season, on_delete=models.PROTECT, verbose_name=_('Сезон'))
    home_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="home_matches",
        verbose_name=_('Домашняя команда')
    )
    away_team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="away_matches",
        verbose_name=_('Гостевая команда')
    )
    home_coach = models.ForeignKey(
        Coach,
        on_delete=models.SET_NULL,
        null=True,
        related_name="home_coached_matches",
        verbose_name=_('Домашний тренер')
    )
    away_coach = models.ForeignKey(
        Coach,
        on_delete=models.SET_NULL,
        null=True,
        related_name="away_coached_matches",
        verbose_name=_('Гостевой тренер')
    )
    referee = models.ForeignKey(
        Referee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Судья')
    )
    start_time = models.DateTimeField(_('Время начала'))
    end_time = models.DateTimeField(_('Время окончания'), null=True, blank=True)
    status = models.CharField(
        _('Статус'),
        max_length=20,
        choices=STATUS_CHOICES,
        default="scheduled"
    )
    home_score = models.IntegerField(_('Счёт дома'), null=True, blank=True, default=0)
    away_score = models.IntegerField(_('Счёт гостей'), null=True, blank=True, default=0)
    has_lineup = models.BooleanField(_('Есть состав'), default=False)
    # Исходный статус Sportmonks для технических результатов (AWARDED/WO/ABANDONED).
    # У таких матчей законно нет составов — не алертим.
    decided_administratively = models.BooleanField(
        _('Решён технически (неявка/тех. поражение)'),
        default=False,
        help_text=_(
            'Матч завершён административным решением (неявка, техническое '
            'поражение, прерван и засчитан) — у источника данных никогда не '
            'будет состава и событий для такого матча, это не ошибка синка.'
        ),
    )
    voting_open_until = models.DateTimeField(_('Голосование до'))
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # ID матча в Sportmonks (external_id — старые KFF-данные).
    sportmonks_id = models.CharField(
        _('Sportmonks ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    # Бригада судей: {"assistant_1": ..., "assistant_2": ..., "fourth_official": ...}.
    # Главный судья — в FK referee.
    referee_crew = models.JSONField(
        _('Бригада судей (ассистенты, 4-й судья)'),
        null=True,
        blank=True,
    )
    # Если True — автосинк не трогает статус и дату (правка staff вручную).
    manual_override = models.BooleanField(
        _('Статус вручную (не трогать автосинком)'),
        default=False,
        help_text=_('Включите, если правили статус/дату вручную. Пока включено, автосинк не трогает статус и дату матча.'),
    )
    # Номер тура от источника — устойчив к переносам дат.
    tour = models.PositiveSmallIntegerField(_('Тур'), null=True, blank=True)

    # Матч перенесён относительно своего тура. Флаг липкий, автосинк его не снимает.
    was_rescheduled = models.BooleanField(
        _('Перенесён относительно своего тура'),
        default=False,
        help_text=_(
            'Дата матча существенно отличается от дат остальных матчей его тура — '
            'признак того, что игру перенесли. Ставится автоматически и не '
            'снимается синком; не влияет на текущий статус/доступность прогнозов.'
        ),
    )

    class Meta:
        verbose_name = _('Матч')
        verbose_name_plural = _('Матчи')
        ordering = ['-start_time']
        indexes = [
            models.Index(fields=['status', 'start_time']),
            models.Index(fields=['status', 'end_time']), 
            models.Index(fields=['league', 'season', 'start_time']),
        ]
    
    def __str__(self):
        return f"{self.home_team} vs {self.away_team}"
    
    def get_score_display(self):
        """Счёт для отображения."""
        home = self.home_score if self.home_score is not None else '-'
        away = self.away_score if self.away_score is not None else '-'
        return f"{home} : {away}"

    @property
    def is_derby(self) -> bool:
        """Матч между соперниками (Team.rivals). Нужен prefetch home_team__rivals."""
        if not self.home_team_id or not self.away_team_id:
            return False
        return any(r.id == self.away_team_id for r in self.home_team.rivals.all())

    def is_voting_open(self):
        """Открыто ли голосование."""
        from django.utils import timezone
        return self.status == 'finished' and timezone.now() <= self.voting_open_until

    # За сколько дней до старта открываются прогнозы 1X2.
    PREDICTION_WINDOW_DAYS = 5

    def prediction_opens_at(self):
        """Момент открытия прогнозов."""
        from datetime import timedelta
        return self.start_time - timedelta(days=self.PREDICTION_WINDOW_DAYS)

    def is_prediction_open(self):
        """Можно ли сейчас сделать прогноз: окно открыто и матч ещё не начался."""
        from django.utils import timezone
        now = timezone.now()
        return self.status == 'scheduled' and self.prediction_opens_at() <= now < self.start_time

    def prediction_window_not_yet_open(self):
        """Окно прогнозов ещё не открылось (для текста виджета)."""
        from django.utils import timezone
        return self.status == 'scheduled' and timezone.now() < self.prediction_opens_at()

    @property
    def final_result(self):
        """'1' / 'X' / '2' или None, если матч не завершён или нет счёта."""
        if self.status != 'finished' or self.home_score is None or self.away_score is None:
            return None
        if self.home_score > self.away_score:
            return '1'
        if self.home_score < self.away_score:
            return '2'
        return 'X'


class MatchReaction(BaseModel):
    """Реакция на завершённый матч: «Матч тура» / «Неожиданно» / «Скучно».
    Один пользователь — один актуальный выбор на матч.
    """
    REACTION_MATCH_OF_ROUND = 'match_of_round'
    REACTION_UPSET = 'upset'
    REACTION_BORING = 'boring'
    REACTION_CHOICES = [
        (REACTION_MATCH_OF_ROUND, _('Матч тура')),
        (REACTION_UPSET, _('Неожиданный результат')),
        (REACTION_BORING, _('Скучный матч')),
    ]

    match = models.ForeignKey(
        Match, on_delete=models.CASCADE, related_name='reactions', verbose_name=_('Матч'),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='match_reactions', verbose_name=_('Пользователь'),
    )
    reaction = models.CharField(_('Реакция'), max_length=20, choices=REACTION_CHOICES)

    class Meta:
        verbose_name = _('Реакция на матч')
        verbose_name_plural = _('Реакции на матчи')
        constraints = [
            models.UniqueConstraint(fields=['match', 'user'], name='unique_match_reaction'),
        ]
        indexes = [
            # Явное имя индекса.
            models.Index(fields=['match', 'reaction'], name='match_reaction_type_idx'),
        ]

    def __str__(self):
        return f"{self.user} → {self.match}: {self.get_reaction_display()}"


class MatchTeamStatistics(BaseModel):
    """Объективная статистика команды за матч (удары, владение, карточки...).
    Источник — Sportmonks. Используется в антифроде как независимый сигнал.
    Поля nullable — набор зависит от матча. Полный ответ — в raw.
    """
    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name='team_statistics',
        verbose_name=_('Матч'),
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='match_statistics',
        verbose_name=_('Команда'),
    )
    possession_percent = models.FloatField(_('Владение мячом, %'), null=True, blank=True)
    shots = models.IntegerField(_('Удары'), null=True, blank=True)
    shots_on_goal = models.IntegerField(_('Удары в створ'), null=True, blank=True)
    shots_on_bar = models.IntegerField(_('Удары в штангу'), null=True, blank=True)
    shots_blocked = models.IntegerField(_('Удары заблокированы'), null=True, blank=True)
    corners = models.IntegerField(_('Угловые'), null=True, blank=True)
    offsides = models.IntegerField(_('Офсайды'), null=True, blank=True)
    fouls = models.IntegerField(_('Фолы'), null=True, blank=True)
    yellow_cards = models.IntegerField(_('Жёлтые карточки'), null=True, blank=True)
    red_cards = models.IntegerField(_('Красные карточки'), null=True, blank=True)
    penalties = models.IntegerField(_('Пенальти'), null=True, blank=True)
    saves = models.IntegerField(_('Сейвы'), null=True, blank=True)
    xg = models.FloatField(_('Ожидаемые голы (xG)'), null=True, blank=True)
    passes = models.IntegerField(_('Передачи'), null=True, blank=True)
    pass_accuracy = models.FloatField(_('Точность передач, %'), null=True, blank=True)
    key_passes = models.IntegerField(_('Ключевые передачи'), null=True, blank=True)
    crosses = models.IntegerField(_('Кроссы'), null=True, blank=True)
    # Опасные атаки (DANGEROUS_ATTACKS).
    dangerous_attacks = models.IntegerField(_('Опасные атаки'), null=True, blank=True)
    raw = models.JSONField(_('Сырые данные из API'), default=dict, blank=True)

    class Meta:
        verbose_name = _('Статистика команды за матч')
        verbose_name_plural = _('Статистика команд за матч')
        constraints = [
            models.UniqueConstraint(fields=['match', 'team'], name='unique_match_team_statistics'),
        ]

    def __str__(self):
        return f"{self.team} — статистика ({self.match})"


class MatchPlayerStatistics(BaseModel):
    """Объективная статистика игрока за матч.
    team хранится отдельно — игрок мог перейти в другой клуб.
    """
    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name='player_statistics',
        verbose_name=_('Матч'),
    )
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='match_statistics',
        verbose_name=_('Игрок'),
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='player_match_statistics',
        verbose_name=_('Команда'),
    )
    fouls = models.IntegerField(_('Фолы'), null=True, blank=True)
    saves = models.IntegerField(_('Сейвы'), null=True, blank=True)
    shots = models.IntegerField(_('Удары'), null=True, blank=True)
    shots_on_target = models.IntegerField(_('Удары в створ'), null=True, blank=True)
    shots_missed = models.IntegerField(_('Удары мимо'), null=True, blank=True)
    shots_on_bar = models.IntegerField(_('Удары в штангу'), null=True, blank=True)
    shots_blocked = models.IntegerField(_('Удары заблокированы'), null=True, blank=True)
    corners = models.IntegerField(_('Угловые'), null=True, blank=True)
    offsides = models.IntegerField(_('Офсайды'), null=True, blank=True)
    penalties = models.IntegerField(_('Пенальти'), null=True, blank=True)
    missed_penalty = models.IntegerField(_('Незабитые пенальти'), null=True, blank=True)
    possessions = models.IntegerField(_('Владения мячом'), null=True, blank=True)
    raw = models.JSONField(_('Сырые данные из API'), default=dict, blank=True)

    class Meta:
        verbose_name = _('Статистика игрока за матч')
        verbose_name_plural = _('Статистика игроков за матч')
        constraints = [
            models.UniqueConstraint(fields=['match', 'player'], name='unique_match_player_statistics'),
        ]
        indexes = [
            models.Index(fields=['team', 'match']),
        ]

    def __str__(self):
        return f"{self.player} — статистика ({self.match})"