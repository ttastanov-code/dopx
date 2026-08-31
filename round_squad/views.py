# round_squad/views.py
from django.http import Http404
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
# Осознанно переиспользуем раскладку поля и стили кольца доверия из
# season_squad.views — тот же визуальный язык на "DOPX Лучшие тура", что и
# на "Сборной DOPX сезона", дублировать их здесь смысла нет (см. докстринг
# season_squad/views.py::_slot_to_card про причину raw CSS в ring_style).
from season_squad.views import (
    PITCH_ROWS,
    _RING_STYLE_CONFIDENT,
    _RING_STYLE_EMPTY,
    _RING_STYLE_LOW_CONFIDENCE,
)
from seasons.models import Season


def _resolve_season(season_id):
    if season_id:
        return get_object_or_404(Season.objects.select_related('league'), pk=season_id)
    season = Season.get_primary_active()
    if season is None:
        raise Http404("Нет активного сезона")
    return season


def _resolve_latest_tour(season):
    """Тур для дефолтного показа (URL без явного номера тура).

    ЧЕТВЁРТАЯ версия этой функции — предыдущие три ловили баги ровно на
    переносах матчей, которые сами и должны были обходить (см. докстринг
    round_squad/models.py про причину ребрендинга "не тур недели"):

    1) "последний тур с хотя бы одним завершённым матчем" (`-tour` по
       Match) — перенос ВПЕРЁД (матч сыгран заранее, до своего тура)
       ломает: одиночный завершённый матч тура 25 перебивал реально
       идущий тур 22.
    2) "последний ЗАФИКСИРОВАННЫЙ тур по finalized_at" — ломается на
       исторических турах без реальных голосов: Celery Beat при первом
       проходе взводит is_final у любого древнего тура, где voting_open_until
       формально истёк, и finalized_at там — момент пересчёта, а не игры
       (страница внезапно показала тур 17 вместо 22-го).
    3) "календарный фронтир" (тур N при условии, что 1..N завершены на
       100%) — тоже ломается, но зеркально: перенос НАЗАД (матч тура 6
       не доигран, перенесён на другую дату) блокирует фронтир целиком —
       страница застревает на туре 5, хотя туры 7-22 давно сыграны
       (баг, пойманный на прогоне 2026-08-22).

    Правильный критерий — не "100% завершено" и не "хотя бы один матч
    завершён", а "тур завершён НА ПРАКТИКЕ" (доля завершённых матчей >=
    ROUND_CURRENT_TOUR_MIN_COMPLETION_RATIO=0.75, см. round_squad/services.py),
    и ищем СВЕРХУ ВНИЗ — от самого большого номера тура, первый тур,
    прошедший этот порог:
      · перенос ВПЕРЁД (тур 25, 1 из 8 матчей = 12.5%) порог не проходит —
        сканирование идёт дальше вниз, к реальному текущему туру;
      · перенос НАЗАД внутри тура (тур 6, 7 из 8 = 87.5%) порог проходит —
        не блокирует, в отличие от строгих 100% фронтира;
      · так как сканируем сверху вниз, а не строим фронтир снизу вверх,
        одиночный "застрявший" ранний тур больше не может остановить
        весь расчёт для всех туров выше него.

    2026-08-26: сам расчёт вынесен в round_squad/services.py::
    resolve_practically_closed_tour — понадобился ещё и в
    core/context_processors.py для кнопки в шапке (см. докстринг там).
    Эта функция оставлена как тонкая обёртка, чтобы не трогать вызовы
    ниже по файлу.

    2026-08-31: обёртка переключена на round_squad/services.py::
    resolve_current_tour — см. её докстринг про баг рассинхрона с кнопкой
    в шапке (эта страница показывала практически-сыгранный, но ещё НЕ
    зафиксированный тур, пока кнопка держалась на последнем официально
    зафиксированном — два разных ответа на "какой тур сейчас")."""
    return resolve_current_tour(season)


def _slot_to_card(slot, slot_code):
    """Тот же принцип, что season_squad/views.py::_slot_to_card — плоский
    dict для единообразного рендера заполненного/пустого слота."""
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
    season = _resolve_season(season_id)
    if tour is None:
        tour = _resolve_latest_tour(season)
    if tour is None:
        raise Http404("В этом сезоне ещё нет завершённых туров")

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
        # Методология — те же числа, что реально использует алгоритм
        # (round_squad/services.py), чтобы раздел "Как считается?" не
        # разъехался с кодом.
        'round_vote_shrinkage_c': ROUND_VOTE_SHRINKAGE_C,
        'round_min_votes_for_candidate': ROUND_MIN_VOTES_FOR_CANDIDATE,
        'round_confident_votes_threshold': ROUND_CONFIDENT_VOTES_THRESHOLD,
    }


def round_of_week(request, season_id=None, tour=None):
    """Публичная страница «DOPX Лучшие тура»."""
    context = _round_context(season_id, tour)
    round_xi = context['round_xi']
    if round_xi.share_card_path:
        from django.core.files.storage import default_storage

        context['og_image'] = request.build_absolute_uri(default_storage.url(round_xi.share_card_path))
    context['share_text'] = (
        f"{round_xi.brand_title}: игрок тура — {round_xi.player_of_round_name or '?'}. "
        f"Сборная тура и разбор матчей — на DOPX"
    )
    context['page_title'] = f"{round_xi.brand_title} — {context['season'].league.name}"

    # Готовая строка <iframe> для кнопки "Получить embed-код" — тот же
    # паттерн, что у season_squad/views.py::best_xi.
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
    """HTMX-партиал для фонового поллинга, тот же принцип, что
    season_squad/views.py::best_xi_partial."""
    context = _round_context(season_id, tour)
    return render(request, 'round_squad/_round_content.html', context)


@xframe_options_exempt
def round_widget(request, season_id=None, tour=None):
    """
    Embeddable-виджет «DOPX Лучшие тура» для чужих сайтов — тот же паттерн,
    что season_squad/views.py::best_xi_widget (см. докстринг там):
    @xframe_options_exempt + отдельная CSP-политика для этого пути
    (dopx/middleware.py::WIDGET_PATH_PATTERN), изолированный HTML-документ
    без Alpine.js, трекинг через partners/services.py::track_widget_embed_view.
    Без тренера — только 11 полевых слотов, тот же принцип "без лишнего
    веса на маленькой карточке", что и у best_xi_widget.
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
