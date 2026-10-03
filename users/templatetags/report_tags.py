# users/templatetags/report_tags.py
"""{% report_button 'user' username %} / {% report_button 'league' invite_code %} — кнопка «Пожаловаться»."""
from django import template
from django.urls import reverse

from users.models import UserReport

register = template.Library()


@register.inclusion_tag("components/_report_button.html", takes_context=True)
def report_button(context, kind: str, key: str):
    reasons = UserReport.REASON_CHOICES
    if kind == "league":
        # У лиги нет аватара и описания.
        reasons = [r for r in reasons if r[0] not in ("avatar", "bio", "impersonation")]
    return {
        "show": context["request"].user.is_authenticated,
        "action": reverse("users:report", args=[kind, key]),
        "reasons": reasons,
        "csrf_token": context.get("csrf_token"),
        "next": context["request"].get_full_path(),
    }
