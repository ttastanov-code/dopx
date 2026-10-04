# dashboard/views/partners.py
"""Партнёры, баннеры и песочница баннеров."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import services
from ..audit import log_staff_action
from ..models import AuditAction


# ============================================================
# Партнёры и баннеры (CRUD). Вход — со страницы «Реклама».
# ============================================================

PARTNERS_PAGE_SIZE = 30


@staff_member_required
def partners_list(request):
    from partners.models import PartnerType

    search = request.GET.get("q", "").strip()
    qs = services.partners_queryset(search=search)
    partners_page = Paginator(qs, PARTNERS_PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page_title": "Партнёры — DOPX Staff",
        "active_tab": "ads",
        "partners": partners_page,
        "search": search,
        "partner_type_choices": PartnerType.choices,
    }
    return render(request, "dashboard/partners_list.html", context)


@staff_member_required
@require_POST
def partner_create(request):
    from django.db import IntegrityError

    from partners.models import Partner, PartnerType

    name = request.POST.get("name", "").strip()
    slug = request.POST.get("slug", "").strip()
    partner_type = request.POST.get("partner_type", "").strip()

    if not name or not slug:
        messages.error(request, "Название и слаг обязательны.")
        return redirect("dashboard:partners_list")
    if partner_type not in dict(PartnerType.choices):
        messages.error(request, "Некорректный тип партнёра.")
        return redirect("dashboard:partners_list")

    try:
        partner = Partner.objects.create(
            name=name, slug=slug, partner_type=partner_type,
            contact_name=request.POST.get("contact_name", "").strip(),
            contact_email=request.POST.get("contact_email", "").strip(),
            website=request.POST.get("website", "").strip(),
            notes=request.POST.get("notes", "").strip(),
            is_active=request.POST.get("is_active") in ("on", "1", "true"),
        )
    except IntegrityError:
        messages.error(request, f"Слаг «{slug}» уже занят другим партнёром.")
        return redirect("dashboard:partners_list")

    messages.success(request, f"Партнёр «{partner.name}» создан.")
    log_staff_action(
        request, AuditAction.PARTNER_CREATED,
        target=partner.name, details={"partner_id": str(partner.id), "slug": partner.slug},
    )
    return redirect("dashboard:partner_detail", partner_id=partner.id)


@staff_member_required
def partner_detail(request, partner_id):
    from partners.models import Partner, PartnerType
    from partners.selectors import stats_by_banner

    partner = get_object_or_404(Partner, id=partner_id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        partner_type = request.POST.get("partner_type", "").strip()
        if not name:
            messages.error(request, "Название обязательно.")
        elif partner_type not in dict(PartnerType.choices):
            messages.error(request, "Некорректный тип партнёра.")
        else:
            before_active = partner.is_active
            partner.name = name
            partner.partner_type = partner_type
            partner.contact_name = request.POST.get("contact_name", "").strip()
            partner.contact_email = request.POST.get("contact_email", "").strip()
            partner.website = request.POST.get("website", "").strip()
            partner.notes = request.POST.get("notes", "").strip()
            partner.is_active = request.POST.get("is_active") in ("on", "1", "true")
            partner.save(update_fields=[
                "name", "partner_type", "contact_name", "contact_email",
                "website", "notes", "is_active", "updated_at",
            ])
            messages.success(request, f"Партнёр «{partner.name}» обновлён.")
            log_staff_action(
                request, AuditAction.PARTNER_UPDATED,
                target=partner.name,
                details={
                    "partner_id": str(partner.id),
                    "is_active_before": before_active, "is_active_after": partner.is_active,
                },
            )
            return redirect("dashboard:partner_detail", partner_id=partner.id)

    context = {
        "page_title": f"{partner.name} — DOPX Staff",
        "active_tab": "ads",
        "partner": partner,
        "partner_type_choices": PartnerType.choices,
        "banners": _annotate_banners(
            list(partner.banners.all().order_by("-created_at")),
            stats_by_banner(days=30), stats_by_banner(days=None),
        ),
        "report_url": request.build_absolute_uri(
            reverse("partners:report", args=[partner.slug, partner.feed_token])
        ),
    }
    return render(request, "dashboard/partner_detail.html", context)


@staff_member_required
@require_POST
def partner_delete(request, partner_id):
    from partners.models import Partner

    partner = get_object_or_404(Partner, id=partner_id)
    name = partner.name
    banner_count = partner.banners.count()
    partner.delete()
    messages.success(
        request,
        f"Партнёр «{name}» удалён" + (f" ({banner_count} баннеров остались без привязки к партнёру)" if banner_count else "") + ".",
    )
    log_staff_action(
        request, AuditAction.PARTNER_DELETED,
        target=name, details={"partner_id": str(partner_id), "banners_orphaned": banner_count},
    )
    return redirect("dashboard:partners_list")


BANNERS_PAGE_SIZE = 30

# Статус баннера -> (подпись, класс бейджа). Порядок — порядок фильтров.
BANNER_STATUS_META = {
    "running": ("Показывается", "badge-success"),
    "scheduled": ("Запланирован", "badge-info"),
    "paused": ("На паузе", "badge-ghost"),
    "limit": ("Лимит исчерпан", "badge-warning"),
    "finished": ("Завершён", "badge-neutral"),
    "draft": ("Черновик", "badge-ghost"),
}


def _banner_preview_url(request, banner) -> str | None:
    """Страница сайта, где стоит зона баннера, с ?ad_preview=<id>."""
    from matches.models import Match
    from partners.models import BannerZone

    if banner.zone in (BannerZone.HOME_HERO, BannerZone.SIDEBAR):
        path = reverse("core:home")
    elif banner.zone == BannerZone.MATCH_DETAIL:
        match = Match.objects.filter(status="finished").order_by("-start_time").only("id").first()
        if not match:
            return None
        path = reverse("matches:detail", args=[match.id])
    else:
        path = reverse("users:leaderboard")
    return f"{path}?ad_preview={banner.pk}"


def _annotate_banners(banners, stats_30d, stats_all):
    empty = {"impressions": 0, "clicks": 0, "ctr_percent": 0.0}
    for b in banners:
        b.stats_30d = stats_30d.get(b.pk, empty)
        b.stats_all = stats_all.get(b.pk, empty)
        b.current_status = b.status(b.stats_all)
        b.status_label, b.status_class = BANNER_STATUS_META[b.current_status]
        limit_pairs = [(b.stats_all["impressions"], b.max_impressions), (b.stats_all["clicks"], b.max_clicks)]
        b.limit_pct = max((round(done * 100 / cap) for done, cap in limit_pairs if cap), default=None)
    return banners


@staff_member_required
def banners_list(request):
    from partners.models import BannerZone
    from partners.selectors import banner_totals, stats_by_banner

    zone_filter = request.GET.get("zone", "").strip()
    status_filter = request.GET.get("status", "").strip()
    banners = _annotate_banners(
        list(services.banners_queryset(zone=zone_filter, partner_id=request.GET.get("partner", "")).order_by("-created_at")),
        stats_by_banner(days=30), stats_by_banner(days=None),
    )
    counts = {key: 0 for key in BANNER_STATUS_META}
    for b in banners:
        counts[b.current_status] += 1
    if status_filter in BANNER_STATUS_META:
        banners = [b for b in banners if b.current_status == status_filter]
    context = {
        "page_title": "Баннеры — DOPX Staff",
        "active_tab": "ads",
        "banners": banners,
        "zone_filter": zone_filter,
        "status_filter": status_filter,
        "zone_choices": BannerZone.choices,
        "status_choices": [(k, v[0], counts[k]) for k, v in BANNER_STATUS_META.items()],
        "totals_30d": banner_totals(days=30),
    }
    if request.GET.get("partial"):
        return render(request, "dashboard/_banners_table.html", context)
    return render(request, "dashboard/banners_list.html", context)


def _render_banner_form(request, form, banner=None):
    from partners.models import ZONE_SPECS, BannerZone

    context = {
        "page_title": (f"{banner.title} — DOPX Staff" if banner else "Новый баннер — DOPX Staff"),
        "active_tab": "ads",
        "form": form,
        "banner": banner,
        "zone_specs": [
            {"value": value, "label": label, **ZONE_SPECS[value]} for value, label in BannerZone.choices
        ],
    }
    if banner:
        context.update(_banner_stats_context(banner))
        context["preview_url"] = _banner_preview_url(request, banner)
    return render(request, "dashboard/banner_form.html", context)


def _banner_stats_context(banner) -> dict:
    from partners.selectors import banner_daily_series, banner_stats

    stats_all = banner_stats(banner.pk, days=None)
    status = banner.status(stats_all)
    series = banner_daily_series([banner.pk], days=30)
    peak = max((d["impressions"] for d in series), default=0) or 1
    for d in series:
        d["pct"] = round(d["impressions"] * 100 / peak)
    return {
        "banner": banner,
        "stats_all": stats_all,
        "stats_30d": banner_stats(banner.pk, days=30),
        "series": series,
        "status": status,
        "status_label": BANNER_STATUS_META[status][0],
        "status_class": BANNER_STATUS_META[status][1],
    }


@staff_member_required
def banner_version(request, banner_id):
    """Время последнего изменения баннера: проверка вида перезагружает рамки, когда он поменялся."""
    from django.http import JsonResponse

    from partners.models import Banner

    updated = Banner.objects.filter(id=banner_id).values_list("updated_at", flat=True).first()
    return JsonResponse({"version": updated.isoformat() if updated else None})


@staff_member_required
def banner_stats_partial(request, banner_id):
    """Блок статистики карточки баннера для поллинга."""
    from partners.models import Banner

    banner = get_object_or_404(Banner.objects.select_related("partner"), id=banner_id)
    return render(request, "dashboard/_banner_stats.html", _banner_stats_context(banner))


@staff_member_required
def banner_create(request):
    from partners.forms import BannerForm

    if request.method == "POST":
        form = BannerForm(request.POST, request.FILES)
        if form.is_valid():
            banner = form.save()
            for warning in form.warnings:
                messages.warning(request, warning)
            messages.success(request, f"Баннер «{banner.title}» создан.")
            log_staff_action(
                request, AuditAction.BANNER_CREATED,
                target=banner.title, details={"banner_id": str(banner.id), "zone": banner.zone},
            )
            return redirect("dashboard:banner_detail", banner_id=banner.id)
    else:
        initial = {"zone": request.GET.get("zone") or "sidebar", "partner": request.GET.get("partner") or None}
        form = BannerForm(initial=initial)
    return _render_banner_form(request, form)


@staff_member_required
def banner_detail(request, banner_id):
    from partners.forms import BannerForm
    from partners.models import Banner

    banner = get_object_or_404(Banner.objects.select_related("partner"), id=banner_id)
    if request.method == "POST":
        form = BannerForm(request.POST, request.FILES, instance=banner)
        if form.is_valid():
            form.save()
            for warning in form.warnings:
                messages.warning(request, warning)
            messages.success(request, f"Баннер «{banner.title}» сохранён.")
            log_staff_action(
                request, AuditAction.BANNER_UPDATED,
                target=banner.title, details={"banner_id": str(banner.id), "changed": form.changed_data},
            )
            return redirect("dashboard:banner_detail", banner_id=banner.id)
    else:
        form = BannerForm(instance=banner)
    return _render_banner_form(request, form, banner)


@staff_member_required
@require_POST
def banner_toggle(request, banner_id):
    from partners.models import Banner

    banner = get_object_or_404(Banner, id=banner_id)
    banner.is_active = not banner.is_active
    banner.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"Баннер «{banner.title}» {'включён' if banner.is_active else 'на паузе'}.")
    log_staff_action(
        request, AuditAction.BANNER_UPDATED,
        target=banner.title, details={"banner_id": str(banner.id), "is_active": banner.is_active},
    )
    from django.utils.http import url_has_allowed_host_and_scheme

    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect("dashboard:banners_list")


@staff_member_required
@require_POST
def banner_duplicate(request, banner_id):
    from partners.models import Banner

    source = get_object_or_404(Banner, id=banner_id)
    import uuid

    copy = Banner.objects.get(pk=source.pk)
    copy.id = uuid.uuid4()
    copy._state.adding = True
    copy.title = f"{source.title} (копия)"[:150]
    copy.is_active = False
    copy.save()
    messages.success(request, "Копия создана на паузе. Проверьте даты и включите.")
    log_staff_action(
        request, AuditAction.BANNER_CREATED,
        target=copy.title, details={"banner_id": str(copy.id), "copied_from": str(source.id)},
    )
    return redirect("dashboard:banner_detail", banner_id=copy.id)


# Демо-баннеры для проверки вида, когда своих ещё нет.
SANDBOX_DEMOS = {
    "demo-native": {"label": "Демо: карточка", "format": "native"},
    "demo-image": {"label": "Демо: картинка", "format": "image"},
}
SANDBOX_DEVICES = {
    "desktop": {"label": "Компьютер", "icon": "ti-device-desktop", "width": 1280, "height": 800},
    "tablet": {"label": "Планшет", "icon": "ti-device-tablet", "width": 820, "height": 1100},
    "phone": {"label": "Телефон", "icon": "ti-device-mobile", "width": 390, "height": 800},
}


def _sandbox_banner(key: str, zone: str):
    """Сохранённый баннер по id или демо (не сохраняется в базу)."""
    from partners.models import Banner

    if key in SANDBOX_DEMOS:
        demo = Banner(
            zone=zone, format=SANDBOX_DEMOS[key]["format"], title="Демо-баннер", advertiser="Ваш бренд",
            target_url="https://dopx.kz", headline="Здесь может быть реклама вашего бренда",
            body="Карточка подстраивается под ширину колонки и тему сайта.", cta_label="Узнать больше",
        )
        demo.is_demo = True
        return demo
    return Banner.objects.select_related("partner").filter(pk=key).first() if key else None


@staff_member_required
def banner_sandbox(request):
    """Проверка вида баннера: компьютер, планшет и телефон, светлая и тёмная тема. Статистика не пишется."""
    from partners.models import Banner, BannerZone

    banners = list(Banner.objects.select_related("partner").order_by("-created_at"))
    key = request.GET.get("banner") or (str(banners[0].pk) if banners else "demo-native")
    selected = next((b for b in banners if str(b.pk) == key), None)
    zone = request.GET.get("zone") or (selected.zone if selected else "sidebar")
    if selected:
        zone = selected.zone
    theme = request.GET.get("theme") if request.GET.get("theme") in ("light", "dark") else "light"
    frames = [
        {**spec, "key": dev, "src": f"{reverse('dashboard:banner_sandbox_frame')}?banner={key}&zone={zone}&theme={theme}"}
        for dev, spec in SANDBOX_DEVICES.items()
    ]
    context = {
        "page_title": "Проверка рекламы — DOPX Staff",
        "active_tab": "ads",
        "banners": banners,
        "demos": SANDBOX_DEMOS,
        "key": key,
        "selected": selected,
        "zone": zone,
        "zone_choices": BannerZone.choices,
        "theme": theme,
        "frames": frames,
        "site_preview_url": _banner_preview_url(request, selected) if selected else None,
    }
    return render(request, "dashboard/banner_sandbox.html", context)


@staff_member_required
@xframe_options_sameorigin
def banner_sandbox_frame(request):
    """Содержимое рамки: макет страницы с баннером в своей зоне."""
    from partners.models import BannerZone

    zone = request.GET.get("zone") if request.GET.get("zone") in dict(BannerZone.choices) else "sidebar"
    banner = _sandbox_banner(request.GET.get("banner", ""), zone)
    context = {
        "banner": banner,
        "zone": zone,
        "theme": "dark" if request.GET.get("theme") == "dark" else "light",
        "is_demo": getattr(banner, "is_demo", False),
    }
    return render(request, "dashboard/banner_sandbox_frame.html", context)


@staff_member_required
@require_POST
def banner_delete(request, banner_id):
    from partners.models import Banner

    banner = get_object_or_404(Banner, id=banner_id)
    title = banner.title
    banner.delete()
    messages.success(request, f"Баннер «{title}» удалён.")
    log_staff_action(
        request, AuditAction.BANNER_DELETED,
        target=title, details={"banner_id": str(banner_id)},
    )
    return redirect("dashboard:banners_list")
