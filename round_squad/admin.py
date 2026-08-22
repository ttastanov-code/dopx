# round_squad/admin.py
from django.contrib import admin, messages
from unfold.admin import ModelAdmin, TabularInline

from .models import RoundBestXI, RoundBestXISlot


class RoundBestXISlotInline(TabularInline):
    model = RoundBestXISlot
    extra = 0
    can_delete = False
    fields = ('slot_code', 'occupant_name', 'occupant_team_name', 'round_score', 'votes_count', 'is_confident')
    readonly_fields = fields
    ordering = ('order',)

    def has_add_permission(self, request, obj=None):
        # Слоты создаёт/обновляет только recompute_round (services.py) —
        # ручное добавление сломало бы уникальность (round_best_xi, slot_code).
        return False


@admin.register(RoundBestXI)
class RoundBestXIAdmin(ModelAdmin):
    list_display = (
        'season', 'tour', 'is_final', 'player_of_round_name',
        'most_dramatic_match', 'last_computed_at', 'slots_filled',
    )
    list_filter = ('is_final', 'season__league', 'season')
    autocomplete_fields = ('season', 'most_dramatic_match')
    readonly_fields = ('last_computed_at', 'finalized_at', 'share_card_path')
    inlines = [RoundBestXISlotInline]
    actions = ['recompute_now', 'force_finalize']
    search_fields = ('player_of_round_name', 'tour')

    @admin.display(description='Слотов заполнено')
    def slots_filled(self, obj):
        total = obj.slots.count()
        filled = obj.slots.exclude(content_type__isnull=True).count()
        return f"{filled}/{total or 12}"

    @admin.action(description='Пересчитать сейчас')
    def recompute_now(self, request, queryset):
        from round_squad.services import recompute_round

        done = 0
        for round_xi in queryset:
            if round_xi.is_final:
                self.message_user(
                    request, f"Тур {round_xi.tour} ({round_xi.season}): уже зафиксирован — пропущен",
                    level=messages.WARNING,
                )
                continue
            recompute_round(round_xi.season, round_xi.tour)
            done += 1
        if done:
            self.message_user(request, f"Пересчитано туров: {done}", level=messages.SUCCESS)

    @admin.action(description='Зафиксировать вручную (без ожидания закрытия голосования)')
    def force_finalize(self, request, queryset):
        """Ручной аналог автофиксации в recompute_round (см. докстринг
        round_squad/models.py) — для случаев, когда стафф хочет закрыть тур
        раньше, чем voting_open_until истечёт у всех матчей. Как и в
        автоматическом пути, при первой фиксации собираем share-карточку и
        ставим в очередь рассылку итогов (round_squad/tasks.py::
        send_round_results_notification) — те же побочные эффекты, только
        триггер другой."""
        from django.utils import timezone

        from core.services.share_cards import build_round_squad_share_card
        from round_squad.tasks import send_round_results_notification

        to_finalize = list(queryset.filter(is_final=False))
        now = timezone.now()
        for round_xi in to_finalize:
            round_xi.is_final = True
            round_xi.finalized_at = now
            if not round_xi.share_card_path:
                try:
                    dramatic = round_xi.most_dramatic_match
                    round_xi.share_card_path = build_round_squad_share_card(
                        season_year=round_xi.season.year,
                        tour=round_xi.tour,
                        player_of_round_name=round_xi.player_of_round_name or "—",
                        player_of_round_score=round_xi.player_of_round_score,
                        dramatic_match_label=(
                            f"{dramatic.home_team.name} {dramatic.home_score}:{dramatic.away_score} "
                            f"{dramatic.away_team.name}" if dramatic else ""
                        ),
                    )
                except Exception:
                    pass
            round_xi.save(update_fields=['is_final', 'finalized_at', 'share_card_path'])
            send_round_results_notification.delay(str(round_xi.id))

        if to_finalize:
            self.message_user(
                request,
                f"Зафиксировано вручную: {len(to_finalize)}. Рассылка итогов поставлена в очередь.",
                level=messages.SUCCESS,
            )
        else:
            self.message_user(request, "Нечего фиксировать — выбранные туры уже зафиксированы", level=messages.WARNING)
