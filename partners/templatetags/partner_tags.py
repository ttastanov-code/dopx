# partners/templatetags/partner_tags.py
from __future__ import annotations

from django import template

from partners.services import get_active_banner_for_zone, track_banner_impression

register = template.Library()


@register.inclusion_tag("components/_banner.html", takes_context=True)
def render_banner(context, zone: str):
    """
    {% render_banner "sidebar" %} — рендерит один активный баннер зоны
    (ротация по priority, см. partners/services.py::get_active_banner_for_zone)
    и логирует показ. takes_context=True — импрессия трекается тем же
    request, что и рендер страницы, без отдельного JS-пинга.
    """
    request = context.get("request")
    banner = get_active_banner_for_zone(zone)
    if banner is None:
        return {"show": False}

    if request is not None:
        track_banner_impression(banner, request)

    return {"show": True, "banner": banner}
