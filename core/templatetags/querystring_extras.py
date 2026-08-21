# core/templatetags/querystring_extras.py
"""
query_transform — переиспользуемый тег для ссылок-переключателей (сезон,
пагинация и т.п.), которые должны сохранить все текущие GET-параметры
(поиск, фильтры), поменяв только один. Без него каждая страница со
списком (teams/players/coaches) городила бы свою ручную склейку
"{% if request.GET.q %}&q={{ request.GET.q }}{% endif %}" на каждый
параметр — уже было так в пагинации до этого тега, легко забыть параметр
и незаметно потерять фильтр при переходе на следующую страницу/сезон.
"""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def query_transform(context, **kwargs):
    """Возвращает querystring текущего запроса с применёнными изменениями.
    query_transform(season='all') сохранит ?q=...&team=...&season=all,
    убрав пустые/None-значения. Всегда сбрасывает page — смена фильтра
    должна возвращать на первую страницу результатов."""
    request = context['request']
    params = request.GET.copy()
    params.pop('page', None)
    for key, value in kwargs.items():
        if value is None or value == '':
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()
