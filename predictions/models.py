# predictions/models.py
"""
Краудсорс-прогноз на исход матча (1X2), в стиле Sofascore — задача из
бэклога "Прогнозы на матчи". Пользователь выбирает один из трёх вариантов
ДО стартового свистка, видит % голосов сообщества сразу (не дожидаясь
конца голосования, в отличие от evaluations — там результаты открываются
только после `voting_open_until`, здесь наоборот, самое интересное как раз
"кто сейчас лидирует в опросе" ДО матча).

ПОЧЕМУ ОТДЕЛЬНОЕ ПРИЛОЖЕНИЕ, А НЕ `evaluations`/`events`: как и
`EventReaction` (events/models.py), это не переиспользует
`EvaluationSession`/6-шаговый вайзард — семантика другая (формальный выбор
одной из 3 опций на ВЕСЬ матч, а не эмоциональная реакция на конкретное
событие и не пост-матчевая оценка вклада/риска). Но, в отличие от
`EventReaction`, здесь НЕТ toggle-off — прогноз это не лайк, "отменить
прогноз совсем" не имеет пользовательского смысла, можно только сменить
выбор (см. `predictions/services.py::submit_prediction`).
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class MatchPrediction(BaseModel):
    CHOICE_HOME = '1'
    CHOICE_DRAW = 'X'
    CHOICE_AWAY = '2'
    CHOICE_CHOICES = [
        (CHOICE_HOME, _('П1 — победа хозяев')),
        (CHOICE_DRAW, _('X — ничья')),
        (CHOICE_AWAY, _('П2 — победа гостей')),
    ]

    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.CASCADE,
        related_name='predictions',
        verbose_name=_('Матч'),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='match_predictions',
        verbose_name=_('Пользователь'),
    )
    choice = models.CharField(_('Прогноз'), max_length=1, choices=CHOICE_CHOICES)

    class Meta:
        verbose_name = _('Прогноз на матч')
        verbose_name_plural = _('Прогнозы на матчи')
        constraints = [
            # Один пользователь — один (актуальный) прогноз на матч; смена
            # выбора до старта — UPDATE этой же строки через update_or_create
            # в services.py, не вторая запись.
            models.UniqueConstraint(fields=['match', 'user'], name='unique_match_prediction'),
        ]
        indexes = [
            # Явное имя — миграции в проекте пишутся вручную, без доступа к
            # makemigrations (см. CLAUDE.md/docs), автогенерируемый хэш
            # индекса пришлось бы всё равно печатать руками.
            models.Index(fields=['match', 'choice'], name='match_prediction_choice_idx'),
        ]

    def __str__(self):
        return f"{self.user} → {self.match}: {self.choice}"

    @property
    def is_correct(self):
        """
        True/False после того, как у матча появился `final_result`
        (matches/models.py::Match.final_result), иначе None — явное
        "неизвестно ещё", а не молчаливый False, который выглядел бы как
        "прогноз не сбылся" для матча, который просто ещё не начался.
        """
        result = self.match.final_result
        if result is None:
            return None
        return self.choice == result
