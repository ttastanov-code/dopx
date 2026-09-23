# core/admin.py
# УДАЛЕНО (2026-09-09, решение пользователя): StadiumAdmin/Stadium убраны
# из проекта целиком — venue-данные Sportmonks для КПЛ принципиально
# ненадёжны (клубы играют "домашние" матчи на разных стадионах в разных
# городах в течение сезона). Файл оставлен пустым (не удалён), а не
# убран из INSTALLED_APPS/urls — Django ожидает admin.py как штатную точку
# регистрации для приложения `core`, будущие core-модели регистрируются
# здесь же.
from django.contrib import admin
from unfold.admin import ModelAdmin

from core.models import PlatformSetting


@admin.register(PlatformSetting)
class PlatformSettingAdmin(ModelAdmin):
    """2026-09-23, прямая просьба пользователя — новые разделы этой сессии
    должны быть видны и в стандартном Django /admin/, не только в
    /staff/dashboard/settings/ (там своя, более простая форма для staff,
    здесь — резервный/технический доступ с полным набором полей).
    Оба интерфейса читают/пишут одну и ту же таблицу PlatformSetting.
    Разница: правка через /staff/dashboard/settings/ явно сбрасывает кэш
    (dashboard/views.py) и применяется почти сразу; правка отсюда (Django
    admin) кэш явно не трогает — применится по истечении текущего 60-
    секундного окна кэша (core.models.PLATFORM_SETTING_CACHE_TTL), то есть
    в течение минуты, а не мгновенно."""

    list_display = ("key", "value", "value_type", "updated_at", "updated_by")
    list_filter = ("value_type",)
    search_fields = ("key", "description")
    readonly_fields = ("updated_at",)
    ordering = ("key",)
