# matches/models.py
from django.db import models
from django.utils.translation import gettext_lazy as _
from core.models import BaseModel
from leagues.models import League
from seasons.models import Season
from teams.models import Team
from coaches.models import Coach
from referees.models import Referee
from core.models_stadium import Stadium


class Match(BaseModel):
    """Футбольный матч"""
    STATUS_CHOICES = [
        ("scheduled", _("Запланирован")),
        ("live", _("Идёт")),
        ("finished", _("Завершён")),
        ("postponed", _("Перенесён")),
        ("cancelled", _("Отменён")),
    ]
    
    # БАГ, КОТОРЫЙ ТУТ БЫЛ: on_delete=CASCADE на справочных сущностях League/
    # Season — удаление ОДНОЙ League/Season сносило ВСЕ матчи этой лиги/
    # сезона (и каскадно всё, что висит на матчах: события, составы,
    # статистику, оценки). PROTECT — Django не даст удалить League/Season,
    # пока на них ссылаются матчи, что и требуется для справочных сущностей.
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
    voting_open_until = models.DateTimeField(_('Голосование до'))
    external_id = models.CharField(
        _('Внешний ID'),
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )
    stadium = models.ForeignKey(
        Stadium,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Стадион')
    )
    # KFF публикует "перенесён на неопределённый срок" как текстовый
    # баннер на странице матча ЗАДОЛГО до того, как структурные поля
    # status/date в их API меняются (проверено вручную пользователем на
    # kff.kz — дата/статус конкретного матча остаются старыми, меняется
    # только баннер). Значит staff узнаёт о переносе раньше парсера и
    # правит статус вручную в админке — но БЕЗ этого флага
    # update_match_statuses на следующем цикле тихо откатил бы правку
    # обратно в "scheduled", т.к. api_status у KFF всё ещё "scheduled".
    # Пока флаг True, автосинк пропускает статус/дату этого матча целиком
    # (см. parsers/tasks.py::update_match_statuses) — снимается вручную,
    # когда KFF наконец опубликует настоящую новую дату.
    manual_override = models.BooleanField(
        _('Статус вручную (не трогать автосинком)'),
        default=False,
        help_text=_(
            'Включите, если поправили статус/дату матча вручную раньше, чем '
            'KFF обновил официальные данные (например, KFF уже показывает баннер '
            '"перенесён", но дата в их API ещё старая). Пока включено, '
            'автоматическая синхронизация не трогает статус и дату этого матча.'
        ),
    )
    # KFF отдаёт номер тура прямо в /games/{id} (поле "tour"), просто раньше
    # никто его не читал и не сохранял. Пользователь запросил явно
    # 2026-08-21: при переносе матча start_time перестаёт быть надёжным
    # ориентиром "какой это тур" (перенесённый матч может сыграться в дату
    # совсем другого тура) — номер тура от KFF не меняется вместе с датой,
    # поэтому это единственный устойчивый признак.
    tour = models.PositiveSmallIntegerField(_('Тур'), null=True, blank=True)

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
        """Корректное отображение счёта"""
        home = self.home_score if self.home_score is not None else '-'
        away = self.away_score if self.away_score is not None else '-'
        return f"{home} : {away}"

    @property
    def is_derby(self) -> bool:
        """
        НОВОЕ: матч между "принципиальными соперниками" (`Team.rivals`,
        см. teams/models.py и teams/admin.py::TeamAdmin.filter_horizontal).
        Раньше пары соперников можно было проставить только в админке, но
        нигде на сайте это никак не отображалось — бейдж "Дерби-эксперт"
        (users/badges.py) начислялся полностью незаметно для пользователя,
        который не мог понять, какие матчи вообще считаются "дерби".
        Используется в шаблонах списка/детали матча. Дёшево при
        prefetch_related('home_team__rivals') в queryset вьюхи — иначе один
        доп. запрос на матч (rivals редко больше 1-2 записей на команду).
        """
        if not self.home_team_id or not self.away_team_id:
            return False
        return any(r.id == self.away_team_id for r in self.home_team.rivals.all())

    def is_voting_open(self):
        """Проверка открыто ли голосование"""
        from django.utils import timezone
        return self.status == 'finished' and timezone.now() <= self.voting_open_until

    # Сколько дней ДО стартового свистка открывается приём прогнозов 1X2.
    # ИЗМЕНЕНО (2026-08-21, по прямому запросу продукта): раньше окно не
    # имело нижней границы вообще — прогноз можно было отдать хоть за год
    # вперёд, что бессмысленно (расписание может измениться, состав
    # неизвестен и т.д.) и обесценивает саму механику "прогноз вслепую
    # незадолго до игры". Значение подобрано как разумный баланс: достаточно
    # заранее, чтобы прогноз пожил и набрал голосов сообщества к матчу, но
    # не настолько рано, чтобы это было гаданием на кофейной гуще.
    PREDICTION_WINDOW_DAYS = 5

    def prediction_opens_at(self):
        """Момент открытия окна прогноза — используется и в is_prediction_open(),
        и в шаблоне виджета для сообщения "прогнозы откроются <дата>"."""
        from datetime import timedelta
        return self.start_time - timedelta(days=self.PREDICTION_WINDOW_DAYS)

    def is_prediction_open(self):
        """
        Окно для краудсорс-прогноза 1X2 (predictions app, задача "Прогнозы
        на матчи в стиле Sofascore") — симметрично `is_voting_open()`, но
        для ПРОТИВОПОЛОЖНОГО края жизни матча: прогноз на исход имеет
        смысл только ДО стартового свистка, тогда как оценка (evaluations)
        возможна только ПОСЛЕ него.

        Намеренно НЕ используем только `status == 'scheduled'` без проверки
        верхней границы времени: `manual_override`-матч мог быть вручную
        помечен 'scheduled' с устаревшей `start_time` в прошлом (см.
        коммент у `manual_override` выше) — секундная проверка
        `timezone.now() < self.start_time` подстраховывает от приёма
        прогнозов на матч, который по факту уже должен был начаться, даже
        если статус ещё не синхронизирован.

        НИЖНЯЯ граница — `prediction_opens_at()` (см. `PREDICTION_WINDOW_DAYS`
        выше) — добавлена 2026-08-21: без неё голосовать можно было хоть за
        год вперёд.
        """
        from django.utils import timezone
        now = timezone.now()
        return self.status == 'scheduled' and self.prediction_opens_at() <= now < self.start_time

    def prediction_window_not_yet_open(self):
        """Отдельно от `is_prediction_open()` — виджету (_prediction_widget.html)
        нужно различать ДВЕ разных причины "кнопки задизейблены": окно ещё не
        наступило (этот метод) vs уже закрылось после старта матча. Разные
        сообщения пользователю, поэтому не сворачиваем в одно bool-значение."""
        from django.utils import timezone
        return self.status == 'scheduled' and timezone.now() < self.prediction_opens_at()

    @property
    def final_result(self):
        """
        '1' (победа хозяев) / 'X' (ничья) / '2' (победа гостей) для сверки
        с прогнозами пользователей, либо None, если матч ещё не завершён
        или счёт по какой-то причине не заполнен (не должно происходить у
        `finished`-матча в норме, но `home_score`/`away_score` формально
        nullable — лучше явно вернуть None, чем уронить сравнение).
        """
        if self.status != 'finished' or self.home_score is None or self.away_score is None:
            return None
        if self.home_score > self.away_score:
            return '1'
        if self.home_score < self.away_score:
            return '2'
        return 'X'


class MatchTeamStatistics(BaseModel):
    """
    Объективная статистика КОМАНДЫ за матч с KFF (не оценки пользователей —
    факты игры: удары, владение, карточки и т.д.), источник —
    GET /api/v1/games/{id}/stats (parsers/kff/client.py::get_stats).

    2026-08-23, независимый внешний сигнал для антифрода: пользовательские
    оценки (TeamMatchAggregate.performance_score) — субъективны и уязвимы к
    координированной накрутке/занижению (см. aggregates/services.py —
    градуированный штраф веса, нейтральный якорь, винзоризация). Эта
    модель — единственный источник данных в проекте, который НЕ зависит
    от голосов пользователей DOPX вообще: если сообщество массово занижает
    команду, которая объективно доминировала по ударам/угловым (по данным
    самой KFF), это конкретный, проверяемый признак предвзятости —
    используется в aggregates/tasks.py::detect_rating_stats_divergence_task.

    Поля намеренно nullable — реальный ответ KFF на разных матчах отдаёт
    разный набор полей (например, у матча 1058 передачи/xG были null,
    хотя удары/угловые/карточки — заполнены). Не все поля JSON вынесены
    отдельными колонками — только те, что нужны для антифрод-сигнала и
    отображения; полный сырой объект сохраняется в `raw` (тот же паттерн,
    что events.models.MatchEvent.extra_data) на случай будущего расширения
    без новой миграции.
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
    """
    Объективная статистика ИГРОКА за матч с KFF — тот же источник и то же
    назначение, что MatchTeamStatistics выше (см. её докстринг), только
    на уровне игрока. Набор полей у KFF на уровне игрока УЖЕ и стабильнее
    заполнен, чем на уровне команды (пас/xG там почти всегда null) —
    поэтому колонок меньше, все нужные поля почти всегда присутствуют.

    `team` продублирован рядом с `player` (а не читается через
    player.team) специально — состав игрока может смениться ПОСЛЕ матча
    (трансфер), а статистика должна навсегда остаться привязана к той
    команде, за которую он играл В ЭТОМ конкретном матче.
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