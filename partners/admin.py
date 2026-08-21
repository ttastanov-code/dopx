# partners/admin.py
"""
Admin для партнёрской инфраструктуры. Основной сценарий работы staff:
завести Partner → сразу добавить его баннер(ы) инлайном на той же
странице → скопировать готовые ссылки (реферальную и фида) одной кнопкой,
не собирая их вручную из slug/токена.
"""
import uuid

from django.contrib import admin
from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline

from core.admin_actions import export_as_csv

from .models import Banner, Partner
from .selectors import banner_stats, partner_referral_visits


# Зоны показа баннера + рекомендуемый размер/пояснение + демо-картинка
# (static/img/banner-examples/<slug>.png, сгенерирована один раз скриптом
# Pillow под реальные габариты контейнера каждой зоны — см. точные
# CSS-контейнеры в templates/core/home.html, templates/matches/detail.html,
# templates/users/leaderboard.html). Один источник правды на обе формы
# (BannerAdmin.zone_guide ниже) — если появится новая BannerZone, картинку
# и строку сюда нужно добавить вручную, автогенерации из модели нет
# намеренно: у каждой зоны свой контейнер на сайте, это нельзя вывести
# автоматически из одного только текста choice.
BANNER_ZONE_GUIDE = [
    (
        "home_hero", "Главная — верх", "1200 × 300 px",
        "Растягивается на всю ширину страницы (десктоп ~1200 px, на мобильном — по ширине экрана). "
        "Широкий формат вроде билборда, картинка не должна быть перегружена мелким текстом.",
    ),
    (
        "sidebar", "Боковая колонка", "300 × 600 px",
        "Узкая правая колонка на главной (~380–400 px на десктопе). Высокий вертикальный формат "
        "смотрится в ней лучше, чем широкий.",
    ),
    (
        "match_detail", "Страница матча", "300 × 250 px",
        "Такая же узкая правая колонка (~380–400 px), но на странице конкретного матча.",
    ),
    (
        "leaderboard", "Лидерборд", "728 × 90 px",
        "Во всю ширину контента над таблицей рейтинга (той же ширины, что и сама таблица) — короткий "
        "широкий формат, классический IAB «leaderboard».",
    ),
]


def _zone_guide_html() -> str:
    # object-fit:contain (не cover!) — иначе высокие/узкие картинки зон
    # (sidebar 300×600, match_detail 300×250) обрезаются почти целиком под
    # фиксированную высоту превью. contain показывает картинку целиком,
    # letterbox-фон #f3f4f6 заполняет пустоты по бокам/сверху-снизу.
    cards = [
        format_html(
            '<div style="border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;background:#fff;">'
            '<img src="{}" alt="Пример баннера: {}" style="width:100%;height:130px;object-fit:contain;'
            'background:#f3f4f6;display:block;">'
            '<div style="padding:10px 12px;">'
            '<div style="font-weight:600;font-size:13px;">{}</div>'
            '<div style="font-size:12px;opacity:.65;margin:2px 0 6px;font-family:monospace;">{}</div>'
            '<div style="font-size:11px;opacity:.55;line-height:1.4;">{}</div>'
            '</div></div>',
            static(f"img/banner-examples/{slug}.png"), title, title, size, note,
        )
        for slug, title, size, note in BANNER_ZONE_GUIDE
    ]
    grid = format_html(
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:14px;">{}</div>',
        mark_safe("".join(cards)),
    )
    # Статичный HTML без подстановок — format_html() под Django 6.0.3 требует
    # хотя бы один арг/kwarg (см. тот же баг, уже пойманный на is_active_badge/
    # is_currently_active_badge выше), mark_safe — правильный инструмент здесь.
    rules = mark_safe(
        '<div style="font-size:12px;opacity:.7;line-height:1.6;">'
        '<b>Формат файла:</b> jpg/png/webp, без жёстких требований — картинка растягивается по ширине '
        'контейнера с сохранением пропорций (чем ближе к рекомендованному размеру, тем меньше искажений).<br>'
        '<b>Плашка «Реклама»</b> добавляется автоматически поверх любого баннера — убрать нельзя, это '
        'маркировка рекламного размещения.<br>'
        '<b>Клик</b> всегда идёт через редирект <code>/ad/&lt;id&gt;/click/</code>, а не напрямую на ссылку '
        'партнёра — так считаются клики в статистике ниже.<br>'
        '<b>Контент 18+:</b> для любого возрастного контента (букмекеры/гэмблинг, алкоголь, табак и т.п.) '
        'включите «Требует пометки 18+» в блоке «Комплаенс» — под баннером автоматически появится '
        'нейтральный дисклеймер о возрастном ограничении.<br>'
        '<b>Ротация:</b> если в одной зоне несколько активных баннеров, показывается случайный с весом по '
        'полю «Приоритет» — выше число, чаще показ.'
        '</div>'
    )
    return grid + rules


def _copyable(url: str) -> str:
    """<code> с абсолютным URL + кнопка «Копировать» (просто clipboard API, без зависимостей от Alpine — админка Unfold её не гарантированно подключает на каждой странице)."""
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
    """Баннеры партнёра прямо на его странице — основной сценарий "завёл
    партнёра, тут же добавил ему баннер", без прыжков между разделами."""
    model = Banner
    extra = 0
    fields = ('zone', 'title', 'image_preview_inline', 'is_active', 'priority', 'requires_age_disclaimer')
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
    # Unfold-хук: рендерится в {% include %} над таблицей списка, тот же
    # request/context, что и у самой changelist-страницы — см. docstring
    # BANNER_ZONE_GUIDE выше про то, почему инструкция картинками не
    # генерируется из Python, а лежит прямо в шаблоне.
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

    def banner_count(self, obj):
        return obj.banners.count()
    banner_count.short_description = 'Баннеров'

    def referral_url_display(self, obj):
        # obj.pk у BaseModel — UUID с default=uuid.uuid4, он уже проставлен
        # ДАЖЕ на несохранённом объекте (в отличие от обычного AutoField) —
        # проверять нужно obj._state.adding, а не "if not obj.pk", иначе
        # на форме добавления получаем NoReverseMatch (slug ещё пустой).
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
    """
    is_active=True — не то же самое, что "показывается прямо сейчас": баннер
    может быть активен, но ещё не наступило starts_at или уже прошло ends_at.
    Стандартный list_filter по is_active это не различает, отдельный фильтр
    нужен, чтобы staff быстро увидел именно то, что видят пользователи сайчас.
    """
    title = 'Показывается сейчас'
    parameter_name = 'showing_now'

    def lookups(self, request, model_admin):
        return (('yes', 'Да'), ('no', 'Нет'))

    def queryset(self, request, queryset):
        ids_showing = [b.id for b in queryset if b.is_currently_active()]
        if self.value() == 'yes':
            return queryset.filter(id__in=ids_showing)
        if self.value() == 'no':
            return queryset.exclude(id__in=ids_showing)
        return queryset


@admin.register(Banner)
class BannerAdmin(ModelAdmin):
    list_before_template = "admin/partners/banner/list_before.html"
    list_display = (
        'image_preview', 'title', 'zone', 'partner', 'is_currently_active_badge',
        'priority', 'requires_age_disclaimer', 'stats_30d',
    )
    list_filter = ('zone', ActivelyShowingFilter, 'is_active', 'requires_age_disclaimer', 'partner')
    search_fields = ('title', 'target_url', 'partner__name')
    autocomplete_fields = ('partner',)
    readonly_fields = ('zone_guide', 'created_at', 'updated_at', 'image_preview', 'target_url_link', 'stats_30d_display')
    actions = [export_as_csv]
    fieldsets = (
        ('Инструкция: размеры и как это будет выглядеть', {'fields': ('zone_guide',)}),
        ('Размещение', {'fields': ('partner', 'zone', 'title', 'image', 'image_preview', 'target_url', 'target_url_link')}),
        ('Активность', {'fields': ('is_active', 'starts_at', 'ends_at', 'priority')}),
        ('Комплаенс', {
            'fields': ('requires_age_disclaimer',),
            'description': 'Включите для любого контента 18+ (букмекеры/гэмблинг, алкоголь, табак и т.п.) — под баннером покажется пометка 18+.',
        }),
        ('Статистика', {'fields': ('stats_30d_display',)}),
        ('Мета', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def zone_guide(self, obj=None):
        return _zone_guide_html()
    zone_guide.short_description = ''

    def image_preview(self, obj):
        if obj and obj.image:
            return format_html(
                '<img src="{}" style="max-height:60px;max-width:140px;object-fit:contain;'
                'border-radius:6px;border:1px solid #e5e7eb;">',
                obj.image.url,
            )
        return '—'
    image_preview.short_description = 'Превью'

    def target_url_link(self, obj):
        if not obj.target_url:
            return '—'
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', obj.target_url, obj.target_url)
    target_url_link.short_description = 'Открыть ссылку перехода'

    def is_currently_active_badge(self, obj):
        if obj.is_currently_active():
            return mark_safe('<span style="color:#10b981;">● Показывается</span>')
        if obj.is_active:
            return mark_safe('<span style="color:#f59e0b;">● Вне окна показа</span>')
        return mark_safe('<span style="color:#6b7280;">○ Выключен</span>')
    is_currently_active_badge.short_description = 'Показ'

    def stats_30d(self, obj):
        stats = banner_stats(obj.id, days=30)
        return format_html(
            '{} показов / {} кликов ({}% CTR)',
            stats['impressions'], stats['clicks'], stats['ctr_percent'],
        )
    stats_30d.short_description = 'За 30 дней'

    def stats_30d_display(self, obj):
        if obj._state.adding:
            return '— появится после сохранения —'
        return self.stats_30d(obj)
    stats_30d_display.short_description = 'Показы/клики за 30 дней'
