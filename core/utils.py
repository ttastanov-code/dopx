# core/utils.py
"""
Общие утилиты, переиспользуемые в нескольких приложениях.

get_client_ip нужен в нескольких местах антифрод-контура: регистрация
(users/views.py::RegisterView), завершение оценки (evaluations/views.py::
EvaluateMatchFinalView), контакты (core/views.py::ContactsView).

is_rate_limited — лёгкий rate-limiter на Django cache (Redis, см.
dopx/settings.py::CACHES), без зависимости от django-ratelimit — для
точечных лимитов вроде "не больше 5 регистраций с одного IP в час" хватает.

normalize_kz/KAZAKH_LOOKALIKE_MAP — раньше жили ТОЛЬКО в
dashboard/parser_tools.py (поиск матча в staff-панели по названию команд),
но тот же баг ("Кайрат" по-русски не находит "Қайрат") оказался и в
поиске на страницах teams/players/coaches/referees — вынесено сюда как
общую утилиту, чтобы не дублировать таблицу транслитерации в 5 местах.
"""
from __future__ import annotations

from django.core.cache import cache
from django.http import HttpRequest


def get_auth_panel_stats() -> dict:
    """
    Лёгкая сводка реальных чисел платформы для брендовой панели на
    страницах входа/регистрации (auth/login.html, auth/register.html,
    templates/components/_auth_panel.html) — продуктовый запрос
    2026-08-26: "хочу качественную страницу входа/регистрации, как у
    крупных проектов". Раньше вместо этого показывались абстрактные
    ярлыки "Бесплатно / 2 минуты / Сообщество" без реального содержания;
    три настоящих числа платформы работают как социальное доказательство
    (тот же приём, что у Stripe/Linear/Vercel на их страницах входа).

    Кэш на 10 минут: страницы входа/регистрации открываются часто (в том
    числе ботами и сканерами до всякой авторизации), точность до минуты
    здесь не нужна — три COUNT-запроса на каждый заход того не стоят.

    Импорты моделей внутри функции: get_client_ip и is_rate_limited в
    этом модуле используются из users/forms.py и других мест, которые
    сами могут импортироваться раньше полной инициализации app registry —
    тот же осторожный паттерн, что и в core/context_processors.py.
    """
    cache_key = 'auth_panel_stats_v1'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from evaluations.models import MatchEvaluation
    from matches.models import Match
    from users.models import User

    stats = {
        'total_matches': Match.objects.filter(status='finished').count(),
        'total_evaluations': MatchEvaluation.objects.count(),
        'total_users': User.objects.filter(is_verified=True).count(),
    }
    cache.set(cache_key, stats, 600)
    return stats

# 9 казахских букв (Ә Ғ Қ Ң Ө Ұ Ү Һ І) — отдельные Unicode-символы, не
# просто похожие на русские: "Қ" (U+049A) и "К" (U+041A) для icontains
# полностью не связаны. У большинства пользователей нет казахской
# раскладки — схлопываем оба варианта буквы в один канонический символ
# перед сравнением строк.
KAZAKH_LOOKALIKE_MAP = str.maketrans({
    "ә": "а", "Ә": "А",
    "ғ": "г", "Ғ": "Г",
    "қ": "к", "Қ": "К",
    "ң": "н", "Ң": "Н",
    "ө": "о", "Ө": "О",
    "ұ": "у", "Ұ": "У",
    "ү": "у", "Ү": "У",
    "һ": "х", "Һ": "Х",
    "і": "и", "І": "И",
})


def normalize_kz(text: str) -> str:
    """Казахская буква и её русский "омограф" после этой функции дают
    ОДИНАКОВУЮ строку — сравнение перестаёт зависеть от того, какой
    раскладкой набирали запрос/название."""
    return (text or "").translate(KAZAKH_LOOKALIKE_MAP).lower()


def get_client_ip(request: HttpRequest) -> str | None:
    """
    Определяет реальный IP клиента с учётом обратного прокси.

    БАГ, КОТОРЫЙ ТУТ БЫЛ: брался ПЕРВЫЙ адрес из `X-Forwarded-For` — эта
    часть заголовка целиком под контролем клиента (curl -H "X-Forwarded-For:
    1.2.3.4" ...), значит IP-based rate limit (регистрация, антифрод,
    is_rate_limited) обходился одной строкой заголовка.

    Правильно: `X-Forwarded-For` — это ЦЕПОЧКА "клиент, прокси1, прокси2,
    ...", которая РАСТЁТ по мере прохождения запроса через каждый реальный
    прокси (nginx с `proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for;` ДОПИСЫВАЕТ IP в конец, не заменяет). Если
    перед Gunicorn стоит РОВНО settings.TRUSTED_PROXY_COUNT доверенных
    прокси, то последние TRUSTED_PROXY_COUNT записей — это IP самих
    прокси (их нельзя подделать снаружи, их дописал наш же nginx), а
    элемент ПЕРЕД ними — это и есть настоящий IP клиента. Всё, что левее —
    контролируется атакующим и не заслуживает доверия.

    ВАЖНО: это не замена сетевой защиты — если Gunicorn доступен извне
    напрямую (в обход nginx), у атакующего в заголовке будет всего один
    (поддельный) элемент, который и окажется "на нужной позиции". Прямой
    доступ к Gunicorn должен быть закрыт файрволом/security group — см.
    docs/BACKLOG.md.
    """
    from django.conf import settings

    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        chain = [ip.strip() for ip in x_forwarded_for.split(",") if ip.strip()]
        trusted_proxy_count = getattr(settings, "TRUSTED_PROXY_COUNT", 1)
        if chain:
            # БАГ, КОТОРЫЙ ТУТ БЫЛ (найден полным аудитом, август 2026):
            # формула содержала лишний "-1" (`len(chain) - 1 - trusted_proxy_count`),
            # из-за чего при TRUSTED_PROXY_COUNT=1 функция возвращала chain[0] —
            # ПЕРВЫЙ, полностью подделываемый клиентом элемент — вместо
            # последнего доверенного. Пример: клиент шлёт заголовок
            # "X-Forwarded-For: 6.6.6.6", nginx дописывает свой $remote_addr
            # (реальный IP клиента) → цепочка ["6.6.6.6", "9.9.9.9"]. Старая
            # формула отдавала "6.6.6.6" (подделка), новая — "9.9.9.9" (правда).
            # Каждый доверенный прокси добавляет РОВНО одну запись в конец
            # цепочки, поэтому правильный индекс с начала — это просто
            # len(chain) - trusted_proxy_count, без дополнительного сдвига.
            client_index = len(chain) - trusted_proxy_count
            return chain[client_index] if client_index >= 0 else chain[0]
    return request.META.get("REMOTE_ADDR")


def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """
    Простой rate-limiter по алгоритму fixed window на Django cache.

    :param key: уникальный ключ бакета, например `f"register:{ip}"`.
    :param limit: сколько попыток разрешено за окно.
    :param window_seconds: длительность окна в секундах.
    :return: True, если лимит уже исчерпан (запрос СЛЕДУЕТ отклонить).

    Не претендует на идеальную точность скользящего окна (fixed window
    подвержен "двойному всплеску" на границе окна) — для защиты от
    примитивного бот-фарминга регистраций этого достаточно; для более
    строгих гарантий стоит переходить на `django-ratelimit`/токен-бакет.

    БАГ, КОТОРЫЙ ТУТ БЫЛ (найден полным аудитом, август 2026): между
    cache.get() и последующим cache.set()/incr() было окно для гонки
    (TOCTOU) — два параллельных запроса могли оба увидеть current=None
    (или оба увидеть current < limit на последней разрешённой попытке) и
    оба "проскочить" мимо лимита, прежде чем кто-то из них успевал
    записать инкремент. Под нагрузкой (например, бот-фарминг регистраций
    в несколько потоков с одного IP) это позволяло превысить limit на
    число гоняющихся потоков. Переписано на cache.add() + cache.incr() —
    обе операции атомарны на уровне бэкенда (у Redis — SETNX/INCR), гонка
    исключена.
    """
    cache_key = f"ratelimit:{key}"
    # add() создаёт ключ со значением 0, только если его ещё нет
    # (атомарно); если ключ уже существует — no-op. Затем incr()
    # атомарно увеличивает счётчик и возвращает новое значение.
    cache.add(cache_key, 0, timeout=window_seconds)
    try:
        current = cache.incr(cache_key)
    except ValueError:
        # Ключ протух между add() и incr() (окно истекло ровно в этот
        # момент) — считаем это новым окном.
        cache.add(cache_key, 0, timeout=window_seconds)
        current = cache.incr(cache_key)
    # Первый вызов в окне: add() создаёт 0, incr() возвращает 1 — это
    # первая из `limit` разрешённых попыток, поэтому сравнение строго
    # "больше", а не "больше или равно".
    return current > limit