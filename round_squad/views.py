# round_squad/views.py
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt

from matches.models import Match
from players.positions import BEST_XI_SLOT_LABELS
from round_squad.models import RoundBestXI
from round_squad.services import (
    ROUND_CONFIDENT_VOTES_THRESHOLD,
    ROUND_MIN_VOTES_FOR_CANDIDATE,
    ROUND_VOTE_SHRINKAGE_C,
    resolve_current_tour,
)
# Раскладка поля и стиль кольца — из season_squad.views.
from season_squad.views import (
    PITCH_ROWS,
    _RING_STYLE_CONFIDENT,
    _RING_STYLE_EMPTY,
    _RING_STYLE_LOW_CONFIDENCE,
)
from seasons.models import Season


def _resolve_season(season_id):
    """Сезон из URL или активный. None — данных нет, 404 только для неверного id."""
    if season_id:
        return get_object_or_404(Season.objects.select_related('league'), pk=season_id)
    return Season.get_primary_active()


def _resolve_latest_tour(season):
    """Тур по умолчанию — resolve_current_tour (как у кнопки в шапке)."""
    return resolve_current_tour(season)


def _slot_to_card(slot, slot_code):
    """Слот -> плоский dict для рендера."""
    label = BEST_XI_SLOT_LABELS.get(slot_code, slot_code)
    if slot is None or not slot.content_type_id:
        return {
            'slot_code': slot_code, 'label': label, 'filled': False,
            'occupant_name': '', 'occupant_team_name': '', 'occupant_photo_url': '',
            'occupant_profile_url': '', 'round_score': None, 'votes_count': 0,
            'is_confident': False, 'explanation': '', 'ring_style': _RING_STYLE_EMPTY,
        }
    return {
        'slot_code': slot_code, 'label': label, 'filled': True,
        'occupant_name': slot.occupant_name, 'occupant_team_name': slot.occupant_team_name,
        'occupant_photo_url': slot.occupant_photo_url, 'occupant_profile_url': slot.occupant_profile_url,
        'round_score': slot.round_score, 'votes_count': slot.votes_count, 'is_confident': slot.is_confident,
        'explanation': slot.explanation,
        'ring_style': _RING_STYLE_CONFIDENT if slot.is_confident else _RING_STYLE_LOW_CONFIDENCE,
    }


def _round_context(season_id, tour):
    """Контекст страницы. None — нет сезона или ни одного сыгранного тура."""
    season = _resolve_season(season_id)
    if season is None:
        return None
    if tour is None:
        tour = _resolve_latest_tour(season)
    if tour is None:
        return None

    round_xi, _created = RoundBestXI.objects.get_or_create(season=season, tour=tour)
    slots_by_code = {s.slot_code: s for s in round_xi.slots.all()}

    pitch_rows = [
        {'name': name, 'slots': [_slot_to_card(slots_by_code.get(code), code) for code in codes]}
        for name, codes in PITCH_ROWS
    ]

    player_of_round_card = None
    if round_xi.player_of_round_name:
        player_of_round_card = {
            'occupant_name': round_xi.player_of_round_name,
            'occupant_team_name': round_xi.player_of_round_team_name,
            'occupant_photo_url': round_xi.player_of_round_photo_url,
            'occupant_profile_url': round_xi.player_of_round_profile_url,
            'round_score': round_xi.player_of_round_score,
            'votes_count': round_xi.player_of_round_votes,
            'explanation': round_xi.player_of_round_explanation,
            'ring_style': (
                _RING_STYLE_CONFIDENT if round_xi.player_of_round_votes >= ROUND_CONFIDENT_VOTES_THRESHOLD
                else _RING_STYLE_LOW_CONFIDENCE
            ),
        }

    return {
        'season': season,
        'tour': tour,
        'round_xi': round_xi,
        'pitch_rows': pitch_rows,
        'coach_card': _slot_to_card(slots_by_code.get('COACH'), 'COACH'),
        'player_of_round_card': player_of_round_card,
        'dramatic_match': round_xi.most_dramatic_match,
        'dramatic_match_explanation': round_xi.most_dramatic_match_explanation,
        # Методология — те же константы, что в services.py.
        'round_vote_shrinkage_c': ROUND_VOTE_SHRINKAGE_C,
        'round_min_votes_for_candidate': ROUND_MIN_VOTES_FOR_CANDIDATE,
        'round_confident_votes_threshold': ROUND_CONFIDENT_VOTES_THRESHOLD,
    }


def round_of_week(request, season_id=None, tour=None):
    """Страница «DOPX Лучшие тура»."""
    context = _round_context(season_id, tour)
    if context is None:
        return render(request, 'round_squad/no_data.html', {
            'title': 'DOPX Лучшие тура',
            'message': 'Пока нет ни одного завершённого тура с данными — загляните чуть позже.',
        })
    round_xi = context['round_xi']
    if round_xi.share_card_path:
        from django.core.files.storage import default_storage

        context['og_image'] = request.build_absolute_uri(default_storage.url(round_xi.share_card_path))
    context['share_text'] = (
        f"{round_xi.brand_title}: игрок тура — {round_xi.player_of_round_name or '?'}. "
        f"Сборная тура и разбор матчей — на DOPX"
    )
    context['page_title'] = f"{round_xi.brand_title} — {context['season'].league.name}"

    # Embed-код.
    widget_url = request.build_absolute_uri(
        reverse('round_squad:round_widget', args=[context['season'].id, context['tour']])
    )
    context['widget_embed_code'] = (
        f'<iframe src="{widget_url}" width="320" height="420" '
        f'style="border:none;border-radius:16px;overflow:hidden" '
        f'title="{round_xi.brand_title} на DOPX"></iframe>'
    )
    return render(request, 'round_squad/round.html', context)


def round_of_week_partial(request, season_id=None, tour=None):
    """HTMX-партиал для поллинга."""
    context = _round_context(season_id, tour)
    if context is None:
        return render(request, 'round_squad/_no_data_partial.html')
    return render(request, 'round_squad/_round_content.html', context)


@xframe_options_exempt
def round_widget(request, season_id=None, tour=None):
    """Виджет для чужих сайтов: 11 слотов без тренера.
    @xframe_options_exempt + своя CSP (WIDGET_PATH_PATTERN).
    """
    season = _resolve_season(season_id) if season_id else Season.get_primary_active()
    if tour is None and season is not None:
        tour = _resolve_latest_tour(season)

    pitch_rows = []
    round_xi = None
    if season is not None and tour is not None:
        round_xi, _created = RoundBestXI.objects.get_or_create(season=season, tour=tour)
        slots_by_code = {s.slot_code: s for s in round_xi.slots.all()}
        pitch_rows = [
            {'name': name, 'slots': [_slot_to_card(slots_by_code.get(code), code) for code in codes]}
            for name, codes in PITCH_ROWS
        ]

    from partners.services import track_widget_embed_view

    track_widget_embed_view(
        widget_type="round_best_xi",
        entity_id=f"{season.id}:{tour}" if season and tour else "none",
        request=request,
    )

    round_url = (
        request.build_absolute_uri(reverse('round_squad:round', args=[season.id, tour]))
        if season and tour else request.build_absolute_uri(reverse('round_squad:round'))
    )

    return render(request, 'widgets/round.html', {
        'season': season,
        'tour': tour,
        'round_xi': round_xi,
        'pitch_rows': pitch_rows,
        'round_url': round_url,
    })
