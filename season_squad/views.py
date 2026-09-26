# season_squad/views.py
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt

from aggregates.services import CONFIDENT_VOTES_THRESHOLD, min_votes_for_display
from players.positions import BEST_XI_SLOT_LABELS
from season_squad.models import SeasonBestXI
from season_squad.services import MIN_MATCHES_FOR_CANDIDATE, SHRINKAGE_C
from seasons.models import Season

# Ряды на поле сверху вниз (атака -> защита -> вратарь).
PITCH_ROWS = [
    ('attack', ['LW', 'ST', 'RW']),
    ('midfield', ['CM2', 'DM', 'CM1']),
    ('defense', ['LB', 'CB1', 'CB2', 'RB']),
    ('goalkeeper', ['GK']),
]


def _resolve_season(season_id):
    """Сезон из URL (404 для неверного id) или активный. None — данных нет, не ошибка."""
    if season_id:
        return get_object_or_404(Season.objects.select_related('league'), pk=season_id)
    return Season.get_primary_active()


# ring_style — сырой CSS через var(--color-*): Tailwind runtime не знает цвета daisyUI.
_RING_STYLE_EMPTY = 'outline: 2px dashed rgba(255,255,255,.35); outline-offset: 2px;'
_RING_STYLE_CONFIDENT = 'outline: 2px solid var(--color-success); outline-offset: 2px;'
_RING_STYLE_LOW_CONFIDENCE = 'outline: 2px solid var(--color-warning); outline-offset: 2px;'


def _slot_to_card(slot, slot_code):
    """Слот (или его отсутствие) -> плоский dict для шаблона."""
    label = BEST_XI_SLOT_LABELS.get(slot_code, slot_code)
    if slot is None or not slot.content_type_id:
        return {
            'slot_code': slot_code, 'label': label, 'filled': False,
            'occupant_name': '', 'occupant_team_name': '', 'occupant_photo_url': '',
            'occupant_profile_url': '', 'season_score': None, 'matches_count': 0,
            'votes_count': 0, 'is_confident': False, 'rank_change': 'new',
            'rank_change_delta': None, 'explanation': '', 'ring_style': _RING_STYLE_EMPTY,
        }
    return {
        'slot_code': slot_code, 'label': label, 'filled': True,
        'occupant_name': slot.occupant_name, 'occupant_team_name': slot.occupant_team_name,
        'occupant_photo_url': slot.occupant_photo_url, 'occupant_profile_url': slot.occupant_profile_url,
        'season_score': slot.season_score, 'matches_count': slot.matches_count,
        'votes_count': slot.votes_count, 'is_confident': slot.is_confident,
        'rank_change': slot.rank_change, 'rank_change_delta': slot.rank_change_delta,
        'explanation': slot.explanation,
        'ring_style': _RING_STYLE_CONFIDENT if slot.is_confident else _RING_STYLE_LOW_CONFIDENCE,
    }


def _best_xi_context(season_id):
    """None — нет активного сезона, вызывающий код проверяет."""
    season = _resolve_season(season_id)
    if season is None:
        return None
    best_xi, _created = SeasonBestXI.objects.get_or_create(season=season)
    slots_by_code = {s.slot_code: s for s in best_xi.slots.all()}

    pitch_rows = [
        {'name': name, 'slots': [_slot_to_card(slots_by_code.get(code), code) for code in codes]}
        for name, codes in PITCH_ROWS
    ]

    # Другие сезоны со сборной — для переключателя и архива.
    other_seasons = list(
        Season.objects.filter(best_xi__slots__content_type__isnull=False)
        .exclude(pk=season.pk).select_related('league').distinct().order_by('-year')
    )

    return {
        'season': season,
        'best_xi': best_xi,
        'other_seasons': other_seasons,
        'has_filled_slots': any(s.content_type_id for s in slots_by_code.values()),
        'pitch_rows': pitch_rows,
        'coach_card': _slot_to_card(slots_by_code.get('COACH'), 'COACH'),
        'referee_card': _slot_to_card(slots_by_code.get('REFEREE'), 'REFEREE'),
        # Методология — те же константы, что в services.py.
        'shrinkage_c': SHRINKAGE_C,
        'min_matches_for_candidate': MIN_MATCHES_FOR_CANDIDATE,
        'min_votes_for_display': min_votes_for_display(),
        'confident_votes_threshold': CONFIDENT_VOTES_THRESHOLD,
    }


def best_xi(request, season_id=None):
    """Страница «Живая сборная сезона»."""
    context = _best_xi_context(season_id)
    if context is None:
        return render(request, 'season_squad/no_data.html', {
            'title': 'Сборная DOPX сезона',
            'message': 'Пока нет активного сезона с данными — загляните чуть позже.',
        })

    # Embed-код.
    season = context['season']
    widget_url_name = 'season_squad:widget'
    widget_url = (
        request.build_absolute_uri(reverse(widget_url_name, args=[season.id]))
        if season_id else request.build_absolute_uri(reverse(widget_url_name))
    )
    context['widget_embed_code'] = (
        f'<iframe src="{widget_url}" width="320" height="420" '
        f'style="border:none;border-radius:16px;overflow:hidden" '
        f'title="Сборная DOPX сезона {season.year} на DOPX"></iframe>'
    )
    context['page_title'] = f"Сборная DOPX сезона {season.year} — {season.league.name}"
    return render(request, 'season_squad/best_xi.html', context)


def best_xi_partial(request, season_id=None):
    """HTMX-партиал для поллинга (каждые 60 с). Пересчёт — Celery Beat раз в 15 минут."""
    context = _best_xi_context(season_id)
    if context is None:
        return render(request, 'season_squad/_no_data_partial.html')
    return render(request, 'season_squad/_best_xi_content.html', context)


@xframe_options_exempt
def best_xi_widget(request, season_id=None):
    """Виджет сборной для чужих сайтов: 11 слотов, без тренера/судьи и тултипов."""
    if season_id:
        season = get_object_or_404(Season.objects.select_related('league'), pk=season_id)
    else:
        season = Season.get_primary_active()

    pitch_rows = []
    best_xi = None
    if season is not None:
        best_xi, _created = SeasonBestXI.objects.get_or_create(season=season)
        slots_by_code = {s.slot_code: s for s in best_xi.slots.all()}
        pitch_rows = [
            {'name': name, 'slots': [_slot_to_card(slots_by_code.get(code), code) for code in codes]}
            for name, codes in PITCH_ROWS
        ]

    from partners.services import track_widget_embed_view

    track_widget_embed_view(
        widget_type="best_xi",
        entity_id=str(season.id) if season else "none",
        request=request,
    )

    # Абсолютная ссылка на полную страницу.
    season_url = (
        request.build_absolute_uri(reverse('season_squad:best_xi', args=[season.id]))
        if season else request.build_absolute_uri(reverse('season_squad:best_xi'))
    )

    return render(request, 'widgets/best_xi.html', {
        'season': season,
        'best_xi': best_xi,
        'pitch_rows': pitch_rows,
        'season_url': season_url,
    })
