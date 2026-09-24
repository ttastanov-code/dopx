# core/templatetags/querystring_extras.py
"""query_transform — ссылка с текущими GET-параметрами и изменением одного."""
from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def query_transform(context, **kwargs):
    """query_transform(season='all') -> ?q=...&team=...&season=all; пустые убираются, page сбрасывается."""
    request = context['request']
    params = request.GET.copy()
    params.pop('page', None)
    for key, value in kwargs.items():
        if value is None or value == '':
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()
