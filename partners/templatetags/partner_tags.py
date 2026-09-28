from __future__ import annotations

from django import template

from partners.services import pick_banner

register = template.Library()


@register.inclusion_tag("components/_banner.html", takes_context=True)
def render_banner(context, zone: str):
    """{% render_banner "sidebar" %}. Показ засчитывает ads.js, когда баннер виден на экране.
    ?ad_preview=<id> — сотрудник видит любой баннер этой зоны без учёта статистики."""
    request = context.get("request")
    mourning = context.get("mourning")
    if mourning and mourning.get("hide_ads"):
        return {"show": False}

    preview_id = request.GET.get("ad_preview") if request is not None else None
    if preview_id and getattr(request.user, "is_staff", False):
        from partners.models import Banner

        banner = Banner.objects.filter(pk=preview_id, zone=zone).select_related("partner").first()
        if banner is not None:
            return {"show": True, "banner": banner, "preview": True}

    banner = pick_banner(zone, request)
    if banner is None:
        return {"show": False}
    return {"show": True, "banner": banner, "preview": False}
