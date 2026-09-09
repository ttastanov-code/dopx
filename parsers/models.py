# parsers/models.py
"""
До этого момента `parsers` жил вообще без models.py — задачи синхронизации
(`parsers/tasks.py`) только логировали результат (`logger.info(f"🏁 Match
sync completed: {stats}")`) и возвращали dict, который никто не читает
обратно (Celery result backend — Redis, TTL, не БД; `django_celery_results`
не установлен). Значит на вопрос "когда последний раз синк падал и почему"
можно было ответить только grep'ом по логам на сервере — не вариант для
дашборда. `ParserSyncRun` — минимальная персистентность ИМЕННО того, что уже
считалось в `update_match_statuses`, ничего лишнего.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class ParserSyncRun(BaseModel):
    """Один запуск celery-задачи синхронизации матчей (`update_match_statuses`
    у KFF и родственные Sportmonks-таски). Пишется ОДИН раз в конце задачи —
    не на каждый матч, чтобы не раздувать таблицу на проде (задача крутится
    каждые 1-2 минуты)."""

    # НОВОЕ (cutover, фаза 6, docs/sportmonks-migration-plan.md, 2026-09-08):
    # изначально модель писалась ТОЛЬКО из KFF-задач (см. докстринг ниже до
    # правки), поэтому dashboard/services.py::data_health_summary называл
    # раздел "здоровье KFF-синка" и не различал источник. После переключения
    # CELERY_BEAT_SCHEDULE на sportmonks-* задачи (см. dopx/settings.py)
    # KFF-запись перестала писаться регулярно — БЕЗ этого поля дашборд
    # выглядел бы так, будто синк "замёрз" много часов назад, хотя сайт
    # реально живёт на свежих данных от нового источника. default='kff' —
    # безопасно для уже накопленных строк (все они действительно от KFF).
    SOURCE_CHOICES = [
        ("kff", "KFF"),
        ("sportmonks", "Sportmonks"),
    ]
    source = models.CharField(_('Источник'), max_length=20, choices=SOURCE_CHOICES, default="kff", db_index=True)

    # `created_at` (из BaseModel) — момент записи строки, ОН ЖЕ момент
    # завершения синка (запись создаётся в самом конце задачи). Отдельный
    # `finished_at` был бы дублем created_at при auto_now_add=True — не
    # заводим лишнее поле, только `started_at`, чтобы посчитать duration.
    task_name = models.CharField(_('Задача'), max_length=100, db_index=True)
    started_at = models.DateTimeField(_('Начало'))

    total = models.PositiveIntegerField(_('Всего матчей'), default=0)
    updated = models.PositiveIntegerField(_('Обновлено'), default=0)
    unchanged = models.PositiveIntegerField(_('Без изменений'), default=0)
    errors = models.PositiveIntegerField(_('Ошибок'), default=0)
    new_events = models.PositiveIntegerField(_('Новых событий'), default=0)
    status_changes = models.PositiveIntegerField(_('Смен статуса'), default=0)
    skipped_locked = models.PositiveIntegerField(_('Пропущено (лок)'), default=0)

    # Компактные сэмплы ошибок (НЕ полный traceback — тот всё равно уходит
    # в лог через logger.error(..., exc_info=True); здесь только то, что
    # нужно дашборду, чтобы показать "что именно падало", без раздувания
    # JSONField до размера полного стектрейса на каждую строку).
    error_samples = models.JSONField(_('Сэмплы ошибок'), default=list, blank=True)

    class Meta:
        verbose_name = _('Запуск синхронизации')
        verbose_name_plural = _('Запуски синхронизации')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task_name', '-created_at'], name='parser_run_task_time_idx'),
        ]

    def __str__(self):
        return f"{self.task_name} @ {self.created_at:%Y-%m-%d %H:%M} ({self.errors} ошибок из {self.total})"

    @property
    def duration_seconds(self) -> float:
        return (self.created_at - self.started_at).total_seconds()

    @property
    def error_rate_percent(self) -> float:
        if not self.total:
            return 0.0
        return round(self.errors / self.total * 100, 1)


class ParserDiscrepancy(BaseModel):
    """
    Матч, у которого поле, УЖЕ считавшееся окончательным (счёт или статус
    матча, стоявшего 'finished' до этого импорта), изменилось при
    повторном импорте с KFF — то есть не обычный прогресс матча
    (scheduled → live → finished, счёт растёт по ходу игры), а правка
    задним числом внешним источником данных.

    ПОЧЕМУ ЭТО ЗАВЕДЕНО (2026-09-04, внешний аудит + план перехода на
    платный Stratorium, см. docs/CODEX_AUDIT_RESPONSE_2026-09-04.md,
    раздел "Парсинг KFF / план Б"): пока проект на бесплатном парсинге KFF,
    у нас нет способа отличить "источник поправил опечатку" от "источник
    временно отдал неверные данные" — молчаливое перезаписывание счёта уже
    завершённого матча могло бы годами оставаться незамеченным, при этом
    именно от счёта косвенно зависят агрегаты и сезонная статистика. Это
    НЕ замена мониторингу ошибок (`ParserSyncRun.errors`/`error_samples` —
    те про исключения при запросе к API), а отдельный сигнал "данные
    пришли успешно, но не совпадают с тем, что мы уже считали фактом".

    ⚠️ РЕГРЕССИЯ (2026-09-09, полное удаление KFF-парсера по решению
    пользователя): писалось ТОЛЬКО в `parsers/kff/importers.py::
    import_match_core`, единственном месте, где матч, уже бывший
    'finished' на момент импорта (`was_finished_before`), мог получить
    новые значения `home_score`/`away_score`/`status`. Этот файл физически
    удалён, а `parsers/sportmonks/importers.py` эквивалентной проверки НЕ
    делает — новые расхождения больше НЕ детектируются и НЕ пишутся сюда.
    Модель и таблица оставлены как есть (исторические строки, admin-страница
    parsers/admin.py::ParserDiscrepancyAdmin, карточка "Расхождения импорта"
    на /staff/dashboard/data-health/ — всё продолжает работать и читать
    старые данные), просто новых строк больше не появится, пока кто-то не
    реализует такую же проверку поверх Sportmonks-импортёра.

    `reviewed`/`reviewed_by` — staff разбирает записи в админке (см.
    parsers/admin.py) и отмечает результат разбора в `note`
    ("подтверждено KFF, счёт правда изменился" / "ложное срабатывание" и
    т.п.) — без автоматического отката: пусть решение всегда принимает
    человек, не код.
    """

    match = models.ForeignKey(
        'matches.Match',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='parser_discrepancies',
        verbose_name=_('Матч'),
    )
    # Снэпшот названия — переживает удаление матча (on_delete=SET_NULL) и
    # не требует лишнего JOIN на списке в админке.
    match_label = models.CharField(_('Матч (снэпшот)'), max_length=200)
    field_name = models.CharField(_('Поле'), max_length=50)
    old_value = models.CharField(_('Было'), max_length=200)
    new_value = models.CharField(_('Стало'), max_length=200)

    reviewed = models.BooleanField(_('Разобрано'), default=False, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name=_('Кто разобрал'),
    )
    reviewed_at = models.DateTimeField(_('Когда разобрано'), null=True, blank=True)
    note = models.TextField(_('Заметка'), blank=True)

    class Meta:
        verbose_name = _('Расхождение импорта')
        verbose_name_plural = _('Расхождения импорта')
        ordering = ['reviewed', '-created_at']
        indexes = [
            models.Index(fields=['reviewed', '-created_at'], name='parser_discrepancy_review_idx'),
        ]

    def __str__(self):
        return f"{self.match_label}: {self.field_name} {self.old_value} → {self.new_value}"
