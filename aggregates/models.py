# aggregates/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel

class PlayerMatchAggregate(BaseModel):
    """Агрегированные оценки игрока за матч"""
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
    risk_index = models.FloatField(_('Индекс риска'), default=0.0)
    maturity_score = models.FloatField(_('Индекс зрелости'), default=0.0)
    stability_index = models.FloatField(_('Индекс стабильности'), default=0.0)
    clutch_index = models.FloatField(_('Индекс решающих моментов'), default=0.0)

    # Сегментация contribution по лагерю голосующего (свои фанаты / фанаты
    # соперника / нейтралы) — не меняет formula performance_score, чисто
    # разрез для отображения в дерби, где расхождение мнений подрывает
    # доверие к общему рейтингу. null=True: не в каждом сегменте есть голоса.
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
    """Агрегированные оценки тренера за матч"""
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

    # Сегментация по лагерю голосующего — тот же разрез, что у
    # PlayerMatchAggregate (own_fans_avg/rival_fans_avg/neutral_avg, см.
    # 0002_playermatchaggregate_bias_segments) — 2026-08-23, продуктовый
    # запрос на защиту от сговора фан-базы попросил ту же прозрачность не
    # только для игроков. average_score здесь используется как единое
    # значение "оценки тренера" для сегментации (среднее 4 полей).
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
    """
    Агрегированные оценки КОМАНДЫ за матч (TeamEvaluation: тактика/
    самоотдача/организация/менталитет).

    2026-08-23: до этой модели рейтинг команды нигде не кэшировался и не
    защищался — teams/views.py::TeamDetailView и team_rating_widget
    считали `Avg()` НАПРЯМУЮ по всем TeamEvaluation команды за всю
    карьеру синхронно на каждый рендер страницы: без веса пользователя,
    без винзоризации, без сегментации свои/чужие — тот же класс дыры, что
    был у игроков/тренеров (см. aggregates/services.py), только команды
    не имели вообще никакого промежуточного агрегата, даже наивного.
    Заведена по образцу CoachMatchAggregate — тот же паттерн: пересчёт
    ПЕР МАТЧ асинхронной Celery-задачей (recalculate_team_aggregates),
    профиль команды/виджет читают уже готовый рейтинг по матчам, а не
    считают его на лету.
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
    """
    Агрегированные оценки СУДЕЙСТВА за матч (RefereeEvaluation:
    decision_quality/influence_score + fairness с MatchEvaluation).

    2026-08-23: та же дыра, что и у команд — referees/views.py и
    season_squad/services.py::_build_referee_pool оба считали формулу
    судейского рейтинга НАПРЯМУЮ из RefereeEvaluation/MatchEvaluation на
    лету (и вдобавок ДВАЖДЫ дублировали одну и ту же формулу в двух
    файлах). Формула перенесена сюда в одно место, пересчитывается
    асинхронно per-match вместе с остальными агрегатами.

    Сегментация — не "свои/чужие" (у судьи нет своей команды), а
    "домашние/гостевые фанаты/нейтралы": обе стороны матча могут
    считать, что судья был предвзят именно ПРОТИВ них — расхождение
    home_fans_avg/away_fans_avg наглядно это показывает.
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
    """Агрегированные оценки матча"""
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
    """
    Автоматическая, самозатухающая поправка к performance_score команды —
    структурный ответ на detect_rating_stats_divergence_task (aggregates/
    tasks.py::_check_team_stats_divergence), а НЕ ручной переключатель.

    2026-08-24, продуктовое решение: раньше stats_divergence был чисто
    информационным сигналом (создавал флаг в очереди и всё) — пользователь
    справедливо указал, что без действия это бесполезно, а "разбирать
    руками каждое совпадение" не вариант. Теперь этот сигнал работает как
    остальные структурные слои защиты (винзоризация, нейтральный якорь):
    применяется САМ, без участия модератора, ограничен по величине (см.
    STATS_DIVERGENCE_MAX_CORRECTION в aggregates/tasks.py) и САМ затухает
    к нулю на следующих прогонах, если расхождение перестало наблюдаться —
    никто не должен "выключать" её вручную.

    Поправка применяется ТОЛЬКО к будущим матчам, пересчитываемым ПОСЛЕ
    её обновления (recalculate_team_aggregates читает текущее значение на
    каждый пересчёт) — уже показанные исторические рейтинги задним числом
    не переписываются, это было бы менее прозрачно.

    Модератор всё ещё может вмешаться: действие "Отклонить" на флаге
    stats_divergence в admin (users/admin.py::SuspiciousActivityFlagAdmin)
    обнуляет поправку конкретной команды, если решил, что расхождение
    объяснимо (травмы, судейство и т.д.) и его не нужно компенсировать.
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
        help_text=_(
            'БАГ, КОТОРЫЙ ТУТ БЫЛ: mark_dismissed в users/admin.py обнулял correction, '
            'но не оставлял никакого cooldown — следующий суточный прогон '
            'detect_rating_stats_divergence_task (aggregates/tasks.py) заново находил тот же '
            'паттерн и заново перезаписывал correction, тихо отменяя решение модератора. '
            'Пока это поле в будущем, _check_team_stats_divergence пропускает команду, не трогая поправку.'
        ),
    )

    class Meta:
        verbose_name = _('Поправка рейтинга команды (авто)')
        verbose_name_plural = _('Поправки рейтинга команд (авто)')

    def __str__(self):
        return f"{self.team}: {self.correction:+.2f}"