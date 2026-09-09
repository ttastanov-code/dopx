# core/templatetags/math_extras.py
from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Получение значения из словаря по ключу"""
    return dictionary.get(key) if dictionary else None

@register.filter
def div(value, arg):
    """Деление значения на аргумент"""
    try:
        return float(value) / float(arg)
    except (ValueError, ZeroDivisionError, TypeError):
        return 0

@register.filter
def mul(value, arg):
    """Умножение значения на аргумент"""
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0
    
    
@register.filter
def subtract(value, arg):
    """Вычитание: value - arg"""
    try:
        return float(value) - float(arg)
    except (ValueError, TypeError):
        return 0


@register.filter
def bipolar_bar_pct(value, scale=10):
    """
    Процент заполнения для CSS `width:` прогресс-бара МЕТРИКИ, диапазон
    которой [-scale, +scale] (двуполярная/net-метрика), а не [0, scale].

    БАГ, КОТОРЫЙ ТУТ БЫЛ (найден пользователем по скриншоту, 2026-09-07:
    "показатель зрелости минусовое значение, а ползунок полный"): бар
    "Зрелость" на странице игрока (`stats.avg_maturity`, см.
    `evaluations/models.py::PlayerEvaluation.maturity_score` = `contribution
    - risk`, обе 1-10 → сама метрика в диапазоне примерно [-9, +9], НЕ
    [0, 10]) считал ширину через `{% widthratio value 10 100 %}` — ту же
    формулу, что верна для ПРОСТЫХ 0-10 метрик рядом (Вклад/Риск/Потенциал).
    Для отрицательного value это даёт `width: -38%` — невалидное значение
    CSS, браузер его игнорирует, и `<div>` без явного класса ширины
    заполняет весь родительский блок (100%) — визуально "ползунок полный"
    при отрицательном числе рядом, ровно как на скриншоте.

    Фикс — раздельная семантика: НЕ переиспользовать `widthratio` для
    метрик, диапазон которых не [0, N], а свой фильтр с явным клэмпом
    (`max(0, min(100, ...))` — защита не только от отрицательного `width`,
    но и от `>100%`, если якорение/усреднение когда-нибудь даст выброс за
    ±scale) и линейным маппингом всего диапазона [-scale, +scale] → [0, 100]:
    `(value + scale) / (2 * scale) * 100`. Ноль (контрибуция = риску)
    попадает точно на середину бара (50%), не на 0%, как было бы при
    наивном `abs(value)/scale*100`.
    """
    try:
        value = float(value)
        scale = float(scale)
    except (ValueError, TypeError):
        return 0
    if scale <= 0:
        return 0
    pct = (value + scale) / (2 * scale) * 100
    return max(0, min(100, round(pct)))