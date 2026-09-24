# aggregates/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class PlayerMatchAggregate(BaseModel):
    """Агрегированные оценки игрока за матч."""
    player = models.ForeignKey(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='match_aggregates',
        verbose_name=_('Игрок')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='player_aggregates',
        verbose_name=_('Матч')
    )
    
    # Базовые метрики
    avg_contribution = models.FloatField(_('Средний вклад'), default=0.0)
    avg_risk = models.FloatField(_('Средний риск'), default=0.0)
    avg_potential = models.FloatField(_('Средний потенциал'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    
    # Вычисляемые индексы
    performance_score = models.FloatField(_('Рейтинг выступления'), default=0.0)
    # Вшитая в performance_score поправка — детектор сравнивает чистую оценку.
    rating_correction_applied = models.FloatField(_('Вшитая авто-поправка'), default=0.0)
    risk_index = models.FloatField(_('Индекс риска'), default=0.0)
    maturity_score = models.FloatField(_('Индекс зрелости'), default=0.0)
    stability_index = models.FloatField(_('Индекс стабильности'), default=0.0)
    clutch_index = models.FloatField(_('Индекс решающих моментов'), default=0.0)

    # Средние по лагерям (свои / соперник / нейтралы). Не влияют на performance_score.
    own_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов игрока'), null=True, blank=True,
        help_text=_('avg(contribution) от зрителей, поддержавших команду игрока'),
    )
    rival_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов соперника'), null=True, blank=True,
        help_text=_('avg(contribution) от зрителей, поддержавших команду-соперника'),
    )
    neutral_avg = models.FloatField(
        _('Средняя оценка от нейтральных зрителей'), null=True, blank=True,
        help_text=_('avg(contribution) от зрителей без выбранной стороны/контекста'),
    )

    class Meta:
        verbose_name = _('Агрегат игрока')
        verbose_name_plural = _('Агрегаты игроков')
        constraints = [
            models.UniqueConstraint(fields=['player', 'match'], name='unique_player_match_aggregate')
        ]
        indexes = [
            models.Index(fields=['player', 'match']),
            models.Index(fields=['match', 'player']),
            models.Index(fields=['-performance_score']),
            models.Index(fields=['match', '-performance_score']),
        ]
        ordering = ['-performance_score']
    
    def __str__(self):
        return f"{self.player} - {self.match}"
    
    @property
    def potential_index(self):
        return self.avg_potential


class CoachMatchAggregate(BaseModel):
    """Агрегированные оценки тренера за матч."""
    coach = models.ForeignKey(
        'coaches.Coach',
        on_delete=models.CASCADE,
        related_name='match_aggregates',
        verbose_name=_('Тренер')
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='coach_aggregates',
        verbose_name=_('Матч')
    )
    avg_tactics = models.FloatField(_('Средняя тактика'), default=0.0)
    avg_substitutions = models.FloatField(_('Средние замены'), default=0.0)
    avg_management = models.FloatField(_('Среднее управление'), default=0.0)
    avg_impact = models.FloatField(_('Среднее влияние'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)

    # Средние по лагерям, как у игрока.
    own_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов команды тренера'), null=True, blank=True,
        help_text=_('avg(average_score) от зрителей, поддержавших команду тренера'),
    )
    rival_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов соперника'), null=True, blank=True,
        help_text=_('avg(average_score) от зрителей, поддержавших команду-соперника'),
    )
    neutral_avg = models.FloatField(
        _('Средняя оценка от нейтральных зрителей'), null=True, blank=True,
        help_text=_('avg(average_score) от зрителей без выбранной стороны/контекста'),
    )

    class Meta:
        verbose_name = _('Агрегат тренера')
        verbose_name_plural = _('Агрегаты тренеров')
        constraints = [
            models.UniqueConstraint(fields=['coach', 'match'], name='unique_coach_match_aggregate')
        ]
        indexes = [
            models.Index(fields=['coach', 'match']),
        ]
        ordering = ['-match__start_time']

    def __str__(self):
        return f"{self.coach} - {self.match}"

    @property
    def average_score(self):
        if self.total_votes == 0:
            return 0.0
        return (self.avg_tactics + self.avg_substitutions + self.avg_management + self.avg_impact) / 4


class TeamMatchAggregate(BaseModel):
    """Агрегированные оценки команды за матч (тактика/самоотдача/организация/менталитет).
    Пересчёт — recalculate_team_aggregates.
    """
    team = models.ForeignKey(
        'teams.Team',
        on_delete=models.CASCADE,
        related_name='match_aggregates',
        verbose_name=_('Команда'),
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='team_aggregates',
        verbose_name=_('Матч'),
    )
    avg_tactics = models.FloatField(_('Средняя тактика'), default=0.0)
    avg_effort = models.FloatField(_('Средняя самоотдача'), default=0.0)
    avg_organization = models.FloatField(_('Средняя организация'), default=0.0)
    avg_mentality = models.FloatField(_('Средний менталитет'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    performance_score = models.FloatField(
        _('Рейтинг команды'), default=0.0,
        help_text=_('Взвешенное и винзоризованное среднее average_score (см. aggregates/services.py)'),
    )
    # Вшитая поправка — см. PlayerMatchAggregate.
    rating_correction_applied = models.FloatField(_('Вшитая авто-поправка'), default=0.0)

    own_fans_avg = models.FloatField(
        _('Средняя оценка от своих фанатов'), null=True, blank=True,
        help_text=_('avg(average_score) от зрителей, поддержавших ЭТУ команду'),
    )
    rival_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов соперника'), null=True, blank=True,
        help_text=_('avg(average_score) от зрителей, поддержавших команду-соперника'),
    )
    neutral_avg = models.FloatField(
        _('Средняя оценка от нейтральных зрителей'), null=True, blank=True,
        help_text=_('avg(average_score) от зрителей без выбранной стороны/контекста'),
    )

    class Meta:
        verbose_name = _('Агрегат команды')
        verbose_name_plural = _('Агрегаты команд')
        constraints = [
            models.UniqueConstraint(fields=['team', 'match'], name='unique_team_match_aggregate')
        ]
        indexes = [
            models.Index(fields=['team', 'match']),
            models.Index(fields=['-performance_score']),
        ]
        ordering = ['-match__start_time']

    def __str__(self):
        return f"{self.team} - {self.match}"


class RefereeMatchAggregate(BaseModel):
    """Агрегированные оценки судейства за матч.
    Лагеря: болельщики хозяев / гостей / нейтралы.
    """
    referee = models.ForeignKey(
        'referees.Referee',
        on_delete=models.CASCADE,
        related_name='match_aggregates',
        verbose_name=_('Судья'),
    )
    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='referee_aggregates',
        verbose_name=_('Матч'),
    )
    avg_influence = models.FloatField(_('Среднее влияние на матч'), default=0.0)
    avg_decision_quality = models.FloatField(_('Среднее качество решений'), default=0.0)
    avg_fairness = models.FloatField(
        _('Средняя справедливость матча'), default=0.0,
        help_text=_('avg(MatchEvaluation.fairness) за этот матч — общий сигнал, не привязан к одному судье напрямую'),
    )
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    performance_score = models.FloatField(
        _('Рейтинг судейства'), default=0.0,
        help_text=_(
            '0.6*decision_quality + 0.3*fairness + 0.1*(10 - influence/10) — '
            'см. season_squad/services.py::_build_referee_pool (перенесённая формула)'
        ),
    )

    home_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов домашней команды'), null=True, blank=True,
        help_text=_('avg(decision_quality) от зрителей, поддержавших домашнюю команду'),
    )
    away_fans_avg = models.FloatField(
        _('Средняя оценка от фанатов гостевой команды'), null=True, blank=True,
        help_text=_('avg(decision_quality) от зрителей, поддержавших гостевую команду'),
    )
    neutral_avg = models.FloatField(
        _('Средняя оценка от нейтральных зрителей'), null=True, blank=True,
        help_text=_('avg(decision_quality) от зрителей без выбранной стороны/контекста'),
    )

    class Meta:
        verbose_name = _('Агрегат судейства')
        verbose_name_plural = _('Агрегаты судейства')
        constraints = [
            models.UniqueConstraint(fields=['referee', 'match'], name='unique_referee_match_aggregate')
        ]
        indexes = [
            models.Index(fields=['referee', 'match']),
            models.Index(fields=['-performance_score']),
        ]
        ordering = ['-match__start_time']

    def __str__(self):
        return f"{self.referee} - {self.match}"


class MatchAggregate(BaseModel):
    """Агрегированные оценки матча."""
    match = models.OneToOneField(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='aggregate',
        verbose_name=_('Матч')
    )
    avg_entertainment = models.FloatField(_('Средняя зрелищность'), default=0.0)
    avg_tension = models.FloatField(_('Среднее напряжение'), default=0.0)
    avg_fairness = models.FloatField(_('Средняя справедливость'), default=0.0)
    turning_point_ratio = models.FloatField(_('Доля переломных моментов'), default=0.0)
    total_votes = models.IntegerField(_('Всего голосов'), default=0)
    drama_index = models.FloatField(_('Индекс драмы'), default=0.0)
    
    class Meta:
        verbose_name = _('Агрегат матча')
        verbose_name_plural = _('Агрегаты матчей')
        indexes = [
            models.Index(fields=['match']),
        ]
        ordering = ['-match__start_time']
    
    def __str__(self):
        return f"Агрегат - {self.match}"

    def calculate_drama_index(self):
        return self.avg_entertainment * self.avg_tension


class TeamRatingCorrection(BaseModel):
    """Авто-поправка к performance_score команды при расхождении со статистикой.
    Ограничена STATS_DIVERGENCE_MAX_CORRECTION, сама затухает.
    Применяется к пересчётам, история не переписывается.
    «Отклонить» флага обнуляет поправку.
    """
    team = models.OneToOneField(
        'teams.Team',
        on_delete=models.CASCADE,
        related_name='rating_correction',
        verbose_name=_('Команда'),
    )
    correction = models.FloatField(
        _('Текущая поправка'), default=0.0,
        help_text=_('Прибавляется к performance_score на каждом пересчёте, ограничена и самозатухает.'),
    )
    last_pattern = models.CharField(
        _('Последний обнаруженный паттерн'), max_length=40, blank=True,
        help_text=_('underrated_despite_dominance / overrated_despite_poor_play / пусто, если сейчас идёт затухание.'),
    )
    suppressed_until = models.DateTimeField(
        _('Подавлено до'), null=True, blank=True,
        help_text=_('Пока дата в будущем, детектор расхождения не трогает поправку команды (флаг отклонён модератором).'),
    )

    class Meta:
        verbose_name = _('Поправка рейтинга команды (авто)')
        verbose_name_plural = _('Поправки рейтинга команд (авто)')

    def __str__(self):
        return f"{self.team}: {self.correction:+.2f}"

    @property
    def public_explanation(self) -> str:
        return _correction_public_text(self.correction, self.last_pattern, "команду")


def _correction_public_text(correction: float, last_pattern: str, who: str) -> str:
    """Пояснение поправки для посетителей."""
    up = correction > 0
    if last_pattern:
        if up:
            reason = (f"В последних матчах болельщики оценивали {who} ниже обычного, хотя по статистике "
                      f"матчей игра была лучше обычной — похоже на массовое занижение оценок.")
        else:
            reason = (f"В последних матчах болельщики оценивали {who} выше обычного, хотя по статистике "
                      f"матчей игра была хуже обычной — похоже на массовую накрутку оценок.")
    else:
        reason = "Расхождение оценок со статистикой было замечено раньше и уже пропало — поправка постепенно уходит в ноль."
    return (
        f"Автоматическая защита от накрутки. {reason} Поэтому к рейтингу каждого НОВОГО матча система "
        f"{'добавляет' if up else 'вычитает'} {abs(correction):.2f} балла. Оценки прошлых матчей не меняются. "
        f"Поправка небольшая (максимум ±0.4), каждый день перепроверяется и сама уменьшается, "
        f"когда расхождение пропадает. Её проверяет модератор."
    )


class PlayerRatingCorrection(BaseModel):
    """То же для игрока: объективный индекс сравнивается с историей самого игрока (z-score).
    Ограничена PLAYER_STATS_DIVERGENCE_MAX_CORRECTION, сама затухает.
    """
    player = models.OneToOneField(
        'players.Player',
        on_delete=models.CASCADE,
        related_name='rating_correction',
        verbose_name=_('Игрок'),
    )
    correction = models.FloatField(
        _('Текущая поправка'), default=0.0,
        help_text=_('Прибавляется к performance_score на каждом пересчёте, ограничена и самозатухает.'),
    )
    last_pattern = models.CharField(
        _('Последний обнаруженный паттерн'), max_length=40, blank=True,
        help_text=_('underrated_despite_stats / overrated_despite_stats / пусто, если сейчас идёт затухание.'),
    )
    suppressed_until = models.DateTimeField(
        _('Подавлено до'), null=True, blank=True,
        help_text=_('Пока дата в будущем, детектор расхождения не трогает поправку игрока (флаг отклонён модератором).'),
    )

    class Meta:
        verbose_name = _('Поправка рейтинга игрока (авто)')
        verbose_name_plural = _('Поправки рейтинга игроков (авто)')

    def __str__(self):
        return f"{self.player}: {self.correction:+.2f}"

    @property
    def public_explanation(self) -> str:
        return _correction_public_text(self.correction, self.last_pattern, "игрока")