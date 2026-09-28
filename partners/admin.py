# partners/admin.py
"""Админка партнёров: партнёр + баннеры инлайном + копирование ссылок."""
import uuid

from django.contrib import admin
from django.conf import settings
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline

from core.admin_actions import export_as_csv

from .models import Banner, Partner
from .selectors import banner_stats, partner_referral_visits


def _copyable(url: str) -> str:
    """<code> с абсолютным URL + кнопка «Копировать» (clipboard API)."""
    return format_html(
        '<span style="display:inline-flex;align-items:center;gap:6px;">'
        '<code style="font-size:12px;">{}</code>'
        '<button type="button" onclick="navigator.clipboard.writeText(\'{}\'); '
        'this.textContent=\'Скопировано!\'; setTimeout(()=>this.textContent=\'Копировать\', 1500);" '
        'style="font-size:11px;padding:2px 8px;border:1px solid #d1d5db;border-radius:6px;cursor:pointer;background:#f9fafb;">'
        'Копировать</button></span>',
        url, url,
    )


class BannerInline(TabularInline):
    """Баннеры инлайном на странице партнёра."""
    model = Banner
    extra = 0
    fields = ('zone', 'format', 'title', 'image_preview_inline', 'is_active', 'priority', 'requires_age_disclaimer')
    readonly_fields = ('image_preview_inline',)
    show_change_link = True

    def image_preview_inline(self, obj):
        if obj and obj.image:
            return format_html(
                '<img src="{}" style="max-height:40px;max-width:80px;object-fit:contain;border-radius:4px;">',
                obj.image.url,
            )
        return '—'
    image_preview_inline.short_description = 'Превью'


@admin.register(Partner)
class PartnerAdmin(ModelAdmin):
    # Unfold-хук: инструкция над таблицей списка.
    list_before_template = "admin/partners/partner/list_before.html"
    list_display = ('name', 'partner_type', 'slug', 'banner_count', 'visits_30d', 'is_active_badge')
    list_filter = ('partner_type', 'is_active')
    search_fields = ('name', 'slug', 'contact_email', 'contact_name')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'updated_at', 'referral_url_display', 'feed_url_display')
    inlines = [BannerInline]
    fieldsets = (
        (None, {'fields': ('name', 'slug', 'partner_type', 'is_active')}),
        ('Контакты', {'fields': ('contact_name', 'contact_email', 'website', 'notes')}),
        ('Ссылки для партнёра', {
            'fields': ('referral_url_display', 'feed_url_display'),
            'description': (
                'Реферальная ссылка — дать партнёру для размещения у себя (переходы засчитываются '
                'ему). Ссылка контент-фида — приватная, только для этого партнёра, не публиковать.'
            ),
        }),
        ('Мета', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    actions = [export_as_csv, 'regenerate_feed_token']

    def get_queryset(self, request):
        # banner_count через annotate — без N+1.
        return super().get_queryset(request).annotate(_banner_count=Count('banners', distinct=True))

    def banner_count(self, obj):
        return obj._banner_count
    banner_count.short_description = 'Баннеров'
    banner_count.admin_order_field = '_banner_count'

    def referral_url_display(self, obj):
        # UUID pk есть и у несохранённого объекта — проверяем _state.adding.
        if obj._state.adding or not obj.slug:
            return '— появится после сохранения —'
        url = f"{settings.SITE_URL.rstrip('/')}{reverse('partners:referral_redirect', args=[obj.slug])}"
        return _copyable(url)
    referral_url_display.short_description = 'Реферальная ссылка'

    def feed_url_display(self, obj):
        if obj._state.adding or not obj.slug:
            return '— появится после сохранения —'
        url = f"{settings.SITE_URL.rstrip('/')}{reverse('partners:content_feed', args=[obj.slug, obj.feed_token])}"
        return _copyable(url)
    feed_url_display.short_description = 'Ссылка контент-фида (приватная)'

    def visits_30d(self, obj):
        return partner_referral_visits(obj.slug, days=30)
    visits_30d.short_description = 'Визитов за 30д'

    def is_active_badge(self, obj):
        if obj.is_active:
            return mark_safe('<span style="color:#10b981;">● Активен</span>')
        return mark_safe('<span style="color:#6b7280;">○ Выключен</span>')
    is_active_badge.short_description = 'Статус'

    @admin.action(description='Обновить токен контент-фида (старая ссылка перестанет работать)')
    def regenerate_feed_token(self, request, queryset):
        updated = 0
        for partner in queryset:
            partner.feed_token = uuid.uuid4()
            partner.save(update_fields=['feed_token', 'updated_at'])
            updated += 1
        self.message_user(request, f'Токен контент-фида обновлён у {updated} партнёров. Старые ссылки на фид больше не работают.')


class ActivelyShowingFilter(admin.SimpleListFilter):
    """Фильтр «показывается сейчас» по Banner.status()."""
    title = 'Показывается сейчас'
    parameter_name = 'showing_now'

    def lookups(self, request, model_admin):
        return (('yes', 'Да'), ('no', 'Нет'))

    def queryset(self, request, queryset):
        ids_showing = [b.id for b in queryset.select_related('partner') if b.is_currently_active()]
        if self.value() == 'yes':
            return queryset.filter(id__in=ids_showing)
        if self.value() == 'no':
            return queryset.exclude(id__in=ids_showing)
        return queryset


@admin.register(Banner)
class BannerAdmin(ModelAdmin):
    list_before_template = "admin/partners/banner/list_before.html"
    list_display = (
        'image_preview', 'title', 'zone', 'format', 'partner', 'is_currently_active_badge', 'priority', 'stats_30d',
    )
    list_filter = ('zone', 'format', ActivelyShowingFilter, 'is_active', 'requires_age_disclaimer', 'partner')
    search_fields = ('title', 'advertiser', 'target_url', 'partner__name')
    autocomplete_fields = ('partner',)
    readonly_fields = ('created_at', 'updated_at', 'image_preview', 'stats_30d_display')
    actions = [export_as_csv]
    fieldsets = (
        ('Размещение', {'fields': ('partner', 'advertiser', 'zone', 'format', 'title', 'target_url')}),
        ('Картинка', {'fields': ('image', 'image_mobile', 'image_preview')}),
        ('Карточка', {'fields': ('logo', 'headline', 'body', 'cta_label')}),
        ('Когда и кому', {'fields': (
            'is_active', 'starts_at', 'ends_at', 'priority', 'audience',
            'max_impressions', 'max_clicks', 'daily_cap_per_visitor', 'requires_age_disclaimer',
        )}),
        ('Статистика', {'fields': ('stats_30d_display',)}),
        ('Мета', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def image_preview(self, obj):
        if obj and obj.image:
            return format_html(
                '<img src="{}" style="max-height:60px;max-width:140px;object-fit:contain;'
                'border-radius:6px;border:1px solid #e5e7eb;">',
                obj.image.url,
            )
        return '—'
    image_preview.short_description = 'Превью'

    def is_currently_active_badge(self, obj):
        status = obj.status()
        colors = {'running': '#10b981', 'scheduled': '#3b82f6', 'paused': '#6b7280', 'finished': '#6b7280', 'draft': '#f59e0b'}
        labels = {'running': 'Показывается', 'scheduled': 'Запланирован', 'paused': 'На паузе', 'finished': 'Завершён', 'draft': 'Черновик'}
        return format_html('<span style="color:{};">● {}</span>', colors.get(status, '#6b7280'), labels.get(status, status))
    is_currently_active_badge.short_description = 'Показ'

    def stats_30d(self, obj):
        stats = banner_stats(obj.id, days=30)
        return format_html('{} показов / {} кликов ({}% CTR)', stats['impressions'], stats['clicks'], stats['ctr_percent'])
    stats_30d.short_description = 'За 30 дней'

    def stats_30d_display(self, obj):
        if obj._state.adding:
            return '— появится после сохранения —'
        return self.stats_30d(obj)
    stats_30d_display.short_description = 'Показы/клики за 30 дней'
