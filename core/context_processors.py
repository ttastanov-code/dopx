# core/context_processors.py
from django.conf import settings


def current_round_squad(request):
    """
    Глобальный контекст для навбара: номер тура для кнопки
    "DOPX Лучшие N-го тура".

    Два источника, по приоритету:
    1) RoundBestXI.is_final=True — тур ОФИЦИАЛЬНО зафиксирован (взводится
       периодической Celery-задачей round_squad.tasks.recompute_active_rounds,
       раз в 15 минут, см. CELERY_BEAT_SCHEDULE). Самый надёжный источник:
       номер уже не сдвинется, а RoundBestXI реально просчитан.
    2) Если такого тура ещё нет — round_squad/services.py::
       resolve_practically_closed_tour (та же 75%-эвристика "тур на
       практике сыгран", что определяет дефолтный тур для страницы
       /round/ без явного номера в URL). Без этого запасного варианта
       кнопка простаивала бы на дефолтном "Тур недели" до первого прогона
       Celery-задачи после того, как тур фактически завершился, — тур
       УЖЕ закрыт по факту (по данным Match), просто RoundBestXI для него
       ещё не создан/не зафиксирован.

    В обоих случаях ссылка в навбаре ведёт на этот тур ЯВНО (через
    season_id/tour в URL), а не на round_squad:round без параметров —
    иначе номер в кнопке и тур, который реально откроется по клику,
    могли бы разойтись.

    Импорты внутри функции, а не на уровне модуля — context_processors.py
    подключается в settings.py до полной инициализации app registry,
    прямой импорт моделей на верхнем уровне рискует словить circular import.
    """
    from types import SimpleNamespace

    from round_squad.models import RoundBestXI
    from round_squad.services import resolve_practically_closed_tour
    from seasons.models import Season

    season = Season.get_primary_active()
    if season is None:
        return {}

    round_xi = (
        RoundBestXI.objects
        .filter(season=season, is_final=True)
        .order_by('-tour')
        .first()
    )
    if round_xi is not None:
        return {'nav_current_round': round_xi}

    tour = resolve_practically_closed_tour(season)
    if tour is None:
        return {}
    return {
        'nav_current_round': SimpleNamespace(
            season_id=season.id,
            tour=tour,
            brand_title=f'DOPX Лучшие {tour}-го тура',
        ),
    }


def pwa_settings(request):
    """
    Продуктовый аудит, раздел 5c ("PWA + Web Push"): публичный VAPID-ключ
    нужен в JS на КАЖДОЙ странице (base.html кладёт его в data-атрибут
    <body>, static/js/push.js читает оттуда) — публичный ключ безопасно
    светить в HTML по определению (в отличие от VAPID_PRIVATE_KEY, который
    никогда не покидает settings.py/notifications/services.py).
    """
    return {'VAPID_PUBLIC_KEY': settings.VAPID_PUBLIC_KEY}


def indicator_tooltips(request):
    """Глобальный контекст с подсказками для всех индикаторов"""
    context = {
        'INDICATOR_TOOLTIPS': {
            # === ИГРОКИ ===
            'player': {
                'contribution': 'Вклад в игру: влияние на атаку, защиту, ключевые действия',
                'risk': 'Риск: количество ошибок, потерь, опасных моментов у своих ворот',
                'potential': 'Потенциал: перспективность игрока, запас роста',
                'performance_score': 'Рейтинг выступления: взвешенная оценка на основе вклада и риска',
                'maturity_score': 'Индекс зрелости: вклад минус риск (чем выше — тем стабильнее)',
                'stability_index': 'Стабильность: насколько оценки игрока последовательны',
                'clutch_index': 'Индекс решающих моментов: эффективность в напряжённых эпизодах',
                'avg_contribution': 'Средний вклад: усреднённая оценка влияния игрока',
                'avg_risk': 'Средний риск: усреднённая оценка ошибок игрока',
                'avg_potential': 'Средний потенциал: усреднённая оценка перспектив игрока',
            },
            # === КОМАНДЫ ===
            'team': {
                'tactics': 'Тактика: грамотность схемы, расстановки, игрового плана',
                'effort': 'Самоотдача: интенсивность, борьба, желание победить',
                'organization': 'Организация: дисциплина, взаимодействие, структура игры',
                'mentality': 'Менталитет: реакция на голы, удаления, давление — не сломались ли?',
                'average_score': 'Средний балл: усреднённая оценка по всем критериям',
            },
            # === ТРЕНЕРЫ ===
            'coach': {
                'tactics': 'Тактика: выбор схемы, адаптация под соперника',
                'substitutions': 'Замены: своевременность и эффективность замен',
                'game_management': 'Управление: контроль темпа, переломные решения',
                'impact': 'Влияние: общий вклад тренера в результат',
                'average_score': 'Средний балл: усреднённая оценка по всем критериям',
            },
            # === СУДЬИ ===
            'referee': {
                'influence_score': 'Влияние на матч: 0=незаметен, 50=норма, 100=решил исход',
                'decision_quality': 'Качество решений: точность свистков, работа с ВАР',
            },
            # === МАТЧИ ===
            'match': {
                'entertainment': 'Зрелищность: атаки, голы, моменты — было ли интересно?',
                'tension': 'Напряжение: интрига, борьба, драма до последней минуты',
                'fairness': 'Справедливость: соответствует ли счёт игре',
                'drama_index': 'Индекс драмы: зрелищность × напряжение (макс. 100)',
                'turning_point_ratio': 'Доля переломных моментов: процент оценок с переломным моментом',
                'avg_fairness': 'Средняя справедливость: усреднённая оценка соответствия счёта игре',
            },
            # === АГРЕГАТЫ ===
            'aggregate': {
                'total_votes': 'Всего голосов: количество пользователей, оценивших этот объект',
                'performance_score': 'Рейтинг выступления: итоговая оценка эффективности',
                'risk_index': 'Индекс риска: вероятность ошибок в ключевых моментах',
                'maturity_score': 'Индекс зрелости: баланс между вкладом и риском',
                'stability_index': 'Индекс стабильности: постоянство уровня игры',
                'clutch_index': 'Индекс решающих моментов: эффективность под давлением',
            },
        }
    }
    
    return context