# season_squad/views.py
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt

from aggregates.services import CONFIDENT_VOTES_THRESHOLD, MIN_VOTES_FOR_DISPLAY
from players.positions import BEST_XI_SLOT_LABELS
from season_squad.models import SeasonBestXI
from season_squad.services import MIN_MATCHES_FOR_CANDIDATE, SHRINKAGE_C
from seasons.models import Season

# Раскладка карточек на поле сверху вниз (атака -> защита -> вратарь) —
# CM1/CM2 и CB1/CB2 из одного пула кандидатов (players/positions.py::
# SLOT_PROCESSING_ORDER), но разные карточки, поэтому просто два кода в
# одном ряду, без разницы "кто слева/справа" (алгоритм её не определяет).
PITCH_ROWS = [
    ('attack', ['LW', 'ST', 'RW']),
    ('midfield', ['CM2', 'DM', 'CM1']),
    ('defense', ['LB', 'CB1', 'CB2', 'RB']),
    ('goalkeeper', ['GK']),
]


def _resolve_season(season_id):
    if season_id:
        return get_object_or_404(Season.objects.select_related('league'), pk=season_id)
    season = Season.get_primary_active()
    if season is None:
        raise Http404("Нет активного сезона")
    return season


# Кольцо вокруг аватара кодирует доверие к рейтингу цветом — премиальный
# паттерн уровня Sofascore (вместо отдельного текстового бейджа "мало
# данных" под каждой карточкой): пусто/пунктир — слот ещё не занят,
# янтарное — занят, но голосов пока мало, изумрудное — данных достаточно.
#
# ring_style — СЫРАЯ CSS-строка через outline + var(--color-*), а НЕ
# Tailwind-класс вида ring-success. Баг, пойманный 2026-08-22: на этом
# сайте Tailwind загружен как отдельный браузерный рантайм
# (@tailwindcss/browser@4), а daisyUI — отдельным <link>-стилем, не как
# Tailwind-плагин/@theme. Tailwind поэтому НЕ знает про daisyUI-цвета
# "success"/"warning" как имена для генерации утилит: класс
# ring-success/80 тихо схлопывался в один и тот же нейтральный дефолт
# для обоих состояний — кольца "данных достаточно" и "данных мало"
# визуально не отличались вообще (см. templates/components/_avatar.html
# докстринг ring_style — там же объяснение, почему outline, а не
# box-shadow-ring). var(--color-success)/var(--color-warning) — реальные
# CSS custom properties, которые daisyUI ставит в :root, поэтому прямая
# ссылка на них в style работает корректно в обход Tailwind.
_RING_STYLE_EMPTY = 'outline: 2px dashed rgba(255,255,255,.35); outline-offset: 2px;'
_RING_STYLE_CONFIDENT = 'outline: 2px solid var(--color-success); outline-offset: 2px;'
_RING_STYLE_LOW_CONFIDENCE = 'outline: 2px solid var(--color-warning); outline-offset: 2px;'


def _slot_to_card(slot, slot_code):
    """Приводит SeasonBestXISlot (или его отсутствие — слот ещё не
    посчитан) к плоскому dict. Django-шаблоны резолвят и dict-ключи, и
    атрибуты объекта через один и тот же синтаксис `card.field`, поэтому
    единый dict для "заполнен"/"пусто" убирает ветвление в шаблоне."""
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
    season = _resolve_season(season_id)
    best_xi, _created = SeasonBestXI.objects.get_or_create(season=season)
    slots_by_code = {s.slot_code: s for s in best_xi.slots.all()}

    pitch_rows = [
        {'name': name, 'slots': [_slot_to_card(slots_by_code.get(code), code) for code in codes]}
        for name, codes in PITCH_ROWS
    ]

    return {
        'season': season,
        'best_xi': best_xi,
        'pitch_rows': pitch_rows,
        'coach_card': _slot_to_card(slots_by_code.get('COACH'), 'COACH'),
        'referee_card': _slot_to_card(slots_by_code.get('REFEREE'), 'REFEREE'),
        # Для раздела "Как считается?" — те же числа, что реально использует
        # алгоритм (season_squad/services.py), чтобы методология на странице
        # не разъехалась с кодом при будущих правках констант.
        'shrinkage_c': SHRINKAGE_C,
        'min_matches_for_candidate': MIN_MATCHES_FOR_CANDIDATE,
        'min_votes_for_display': MIN_VOTES_FOR_DISPLAY,
        'confident_votes_threshold': CONFIDENT_VOTES_THRESHOLD,
    }


def best_xi(request, season_id=None):
    """Публичная страница «Живая сборная сезона»."""
    context = _best_xi_context(season_id)

    # Готовая строка <iframe> для кнопки "Получить embed-код" — тот же
    # паттерн, что у players/views.py::PlayerDetailView и
    # teams/views.py::TeamDetailView (см. best_xi_widget выше).
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
    return render(request, 'season_squad/best_xi.html', context)


def best_xi_partial(request, season_id=None):
    """HTMX-партиал — только карточки поля, для фонового поллинга (см.
    шаблон best_xi.html, hx-trigger="every 60s"). Сам пересчёт идёт на
    сервере раз в 15 минут по расписанию Celery Beat (season_squad/tasks.py)
    — частый опрос здесь просто ловит момент готовности нового пересчёта,
    а не запускает его сам."""
    context = _best_xi_context(season_id)
    return render(request, 'season_squad/_best_xi_content.html', context)


@xframe_options_exempt
def best_xi_widget(request, season_id=None):
    """
    Embeddable-виджет «Сборной DOPX» — продуктовый запрос 2026-08-22 ("дать
    возможность вставлять этот модуль на другие сайты"), четвёртый виджет
    после players:widget/teams:widget/core:standings_widget (тот же паттерн
    — @xframe_options_exempt, отдельный изолированный HTML-документ,
    трекинг через partners/services.py::track_widget_embed_view). В отличие
    от них — не одна цифра/таблица, а мини-поле 4-3-3 с 11 позициями,
    поэтому переиспользует ту же раскладку и _slot_to_card, что и публичная
    страница, но БЕЗ тренера/судьи (не часть формации, для виджета это
    лишний вес) и без тултипов-объяснений: сам HTML-документ виджета не
    подключает Alpine.js (см. widgets/best_xi.html) — интерактивные
    подсказки там технически не заработают, а рейтинг цифрой под именем и
    так самодостаточен для беглого взгляда на чужом сайте.
    """
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

    # Абсолютная ссылка "DOPX" в шапке виджета — ведёт на полную страницу
    # с методологией/тултипами, которых в самом iframe нет (см. докстринг
    # widgets/best_xi.html). Собираем её здесь, а не через {% url %} внутри
    # шаблона виджета — у него нет доступа к `request`, чтобы получить
    # абсолютный (не относительный) адрес.
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
