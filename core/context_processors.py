# core/context_processors.py
from django.conf import settings


def current_round_squad(request):
    """Номер текущего тура для кнопки «DOPX Лучшие N-го тура» (resolve_default_round).
    Ссылка — на этот тур явно. Импорты внутри функции — от циклических импортов.
    """
    from types import SimpleNamespace

    from round_squad.services import resolve_default_round

    # В начале сезона, пока туров нет, — последний зафиксированный тур прошлого сезона.
    season, tour = resolve_default_round()
    if season is None or tour is None:
        return {}
    return {
        'nav_current_round': SimpleNamespace(
            season_id=season.id,
            tour=tour,
            brand_title=f'DOPX Лучшие {tour}-го тура',
        ),
    }


def pwa_settings(request):
    """Публичный VAPID-ключ для push.js."""
    return {'VAPID_PUBLIC_KEY': settings.VAPID_PUBLIC_KEY}


def indicator_tooltips(request):
    """Тексты подсказок для индикаторов."""
    context = {
        'INDICATOR_TOOLTIPS': {
            # === ИГРОКИ ===
            'player': {
                # Оценка по статистике матча.
                'stat_rating': 'По статистике: оценка игры за матч (шкала 1–10), рассчитанная автоматически по данным матча — голы, передачи, отборы, перехваты, единоборства, сейвы и т.д., с учётом амплуа. Не зависит от голосов болельщиков — полезно сравнить с оценкой трибун.',
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
                'performance_score': (
                    'Итоговый рейтинг: 0.6×качество решений + 0.3×справедливость + '
                    '0.1×(10 − влияние÷10). Чем МЕНЬШЕ судья повлиял на исход матча, тем '
                    'выше итоговый балл — хороший судья должен быть незаметен, а не заметен.'
                ),
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

# Разделы, где нижняя панель вкладок не нужна (свои кнопки или служебный интерфейс).
TABBAR_HIDDEN_APPS = {'dashboard', 'admin', 'evaluations'}


def mobile_tabbar(request):
    """Нижняя панель вкладок на телефоне: показывать ли и счётчик «ждут оценки»."""
    match = getattr(request, 'resolver_match', None)
    app_name = match.app_name if match else ''
    if app_name in TABBAR_HIDDEN_APPS or request.path.startswith('/admin/'):
        return {'show_tabbar': False}
    from core.personal import pending_evaluations_count

    return {
        'show_tabbar': True,
        'tabbar_pending_count': pending_evaluations_count(request.user),
        'tabbar_section': app_name,
        'tabbar_url_name': match.url_name if match else '',
    }
