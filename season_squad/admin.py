# season_squad/admin.py
from django.contrib import admin, messages
from unfold.admin import ModelAdmin, TabularInline

from .models import SeasonBestXI, SeasonBestXISlot, SeasonPositionRanking


class SeasonBestXISlotInline(TabularInline):
    model = SeasonBestXISlot
    extra = 0
    can_delete = False
    fields = (
        'slot_code', 'occupant_name', 'occupant_team_name',
        'season_score', 'matches_count', 'votes_count',
        'is_confident', 'rank_change', 'rank_change_delta',
    )
    readonly_fields = fields
    ordering = ('order',)

    def has_add_permission(self, request, obj=None):
        # Слоты создаёт/обновляет только recompute_best_xi (services.py) —
        # ручное добавление строки из админки создало бы слот без
        # content_type/object_id и сломало бы уникальность (best_xi, slot_code).
        return False


@admin.register(SeasonBestXI)
class SeasonBestXIAdmin(ModelAdmin):
    list_display = ('season', 'formation', 'is_final', 'last_computed_at', 'slots_filled')
    list_filter = ('is_final', 'season__league')
    autocomplete_fields = ('season',)
    readonly_fields = ('last_computed_at', 'finalized_at')
    inlines = [SeasonBestXISlotInline]
    actions = ['recompute_now', 'mark_as_final']

    @admin.display(description='Слотов заполнено')
    def slots_filled(self, obj):
        total = obj.slots.count()
        filled = obj.slots.exclude(content_type__isnull=True).count()
        return f"{filled}/{total or 13}"

    @admin.action(description='Пересчитать сейчас')
    def recompute_now(self, request, queryset):
        from season_squad.services import recompute_best_xi

        done = 0
        for best_xi in queryset:
            if best_xi.is_final:
                self.message_user(
                    request,
                    f"{best_xi.season}: зафиксирована как итоговая — пропущена",
                    level=messages.WARNING,
                )
                continue
            recompute_best_xi(best_xi.season)
            done += 1
        if done:
            self.message_user(request, f"Пересчитано сборных: {done}", level=messages.SUCCESS)

    @admin.action(description='Зафиксировать как итоговую (сезон завершён)')
    def mark_as_final(self, request, queryset):
        from django.utils import timezone

        updated = queryset.filter(is_final=False).update(is_final=True, finalized_at=timezone.now())
        self.message_user(
            request,
            f"Зафиксировано как «Итоговая сборная сезона»: {updated}. "
            f"Автопересчёт для них больше не выполняется.",
            level=messages.SUCCESS,
        )


@admin.register(SeasonPositionRanking)
class SeasonPositionRankingAdmin(ModelAdmin):
    """Служебная модель (полная история ранжирования для расчёта ↑/↓) —
    в основном для отладки алгоритма, не для повседневного использования
    стаффом. Список без inline-редактирования, только просмотр."""
    list_display = ('best_xi', 'slot_code', 'rank', 'season_score', 'matches_count', 'votes_count', 'computed_at')
    list_filter = ('slot_code', 'best_xi__season')
    ordering = ('-computed_at', 'slot_code', 'rank')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
