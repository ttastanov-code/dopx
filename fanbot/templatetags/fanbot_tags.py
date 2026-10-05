from django import template
from django.conf import settings
from django.urls import reverse
from django.utils.http import urlencode

register = template.Library()


@register.inclusion_tag("fanbot/_login_button.html", takes_context=True)
def telegram_login(context, next_url=""):
    """Кнопка «Войти через Telegram» (виджет). Без настроенного бота болельщиков — ничего."""
    username = settings.FAN_BOT_USERNAME.lstrip("@")
    request = context.get("request")
    # Виджет Telegram работает только на домене из /setdomain — на localhost не показываем.
    if not (settings.FAN_BOT_TOKEN and username) or (request and request.get_host().split(":")[0] in ("localhost", "127.0.0.1")):
        return {"enabled": False}
    url = reverse("fanbot:login") + ("?" + urlencode({"next": next_url}) if next_url else "")
    return {"enabled": True, "bot": username, "auth_url": request.build_absolute_uri(url) if request else url}


@register.simple_tag
def fanbot_enabled() -> bool:
    from fanbot import services

    return services.enabled() and bool(services.bot_username())
