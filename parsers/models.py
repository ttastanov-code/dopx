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

from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import BaseModel


class ParserSyncRun(BaseModel):
    """Один запуск celery-задачи синхронизации с KFF (`update_match_statuses`
    и родственные). Пишется ОДИН раз в конце задачи — не на каждый матч,
    чтобы не раздувать таблицу на проде (задача крутится каждые 2 минуты)."""

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
