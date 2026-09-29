# dashboard/views_experts.py
"""Раздел «Эксперты»: мнения о матчах и карточки экспертов."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from datetime import timedelta

from django.utils import timezone

from engagement import expert_invites
from engagement.forms import ExpertForm, ExpertInviteForm, ExpertTakeForm, match_or_none, match_players
from engagement.models import Expert, ExpertInvite, ExpertTake

from .audit import log_staff_action
from .models import AuditAction


def _back(request, default: str):
    """?next= со своего сайта (например, со страницы матча), иначе default."""
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect(default)


@staff_member_required
def experts(request):
    """Список мнений с фильтром и экспертов."""
    takes = ExpertTake.objects.select_related("match__home_team", "match__away_team", "expert", "key_player")
    status = request.GET.get("status", "")
    if status == "published":
        takes = takes.filter(is_published=True)
    elif status == "draft":
        takes = takes.filter(is_published=False)
    elif status == "review":
        takes = takes.filter(is_published=False, invite__isnull=False)
    query = request.GET.get("q", "").strip()
    if query:
        takes = takes.filter(Q(match__home_team__name__icontains=query) | Q(match__away_team__name__icontains=query)
                             | Q(expert__name__icontains=query) | Q(headline__icontains=query))
    page = Paginator(takes.order_by("-match__start_time", "created_at"), 30).get_page(request.GET.get("page"))
    return render(request, "dashboard/experts.html", {
        "page_title": "Мнения экспертов — DOPX Staff",
        "active_tab": "experts",
        "page": page,
        "status": status,
        "query": query,
        "experts": Expert.objects.annotate(takes_count=Count("takes")),
        "invites": _invite_rows(request),
        "new_invite": request.GET.get("invite", ""),
        "counts": {
            "review": ExpertTake.objects.filter(is_published=False, invite__isnull=False).count(),
            "all": ExpertTake.objects.count(),
            "published": ExpertTake.objects.filter(is_published=True).count(),
            "draft": ExpertTake.objects.filter(is_published=False).count(),
        },
    })


def _take_form_page(request, form, take=None):
    from engagement.models import LONG_TAKE_CHARS

    experts_json = {str(e.pk): {"name": e.name, "title": e.title, "initials": e.initials,
                                "photo": e.photo.url if e.photo else ""}
                    for e in form.fields["expert"].queryset}
    match = form.fields["match"].queryset.filter(pk=form["match"].value()).first() if form["match"].value() else None
    return render(request, "dashboard/expert_take_form.html", {
        "page_title": "Мнение эксперта — DOPX Staff",
        "active_tab": "experts",
        "form": form,
        "take": take,
        "match": match,
        "experts_json": experts_json,
        "long_chars": LONG_TAKE_CHARS,
        "next": request.GET.get("next") or request.POST.get("next", ""),
        "other_takes": (ExpertTake.objects.filter(match=match).exclude(pk=getattr(take, "pk", None))
                        .select_related("expert") if match else []),
    })


def _save_take(request, form, created: bool):
    take = form.save(commit=False)
    if created:
        take.author = request.user
    take.save()
    log_staff_action(request, AuditAction.EXPERT_TAKE_SAVED, target=str(take)[:300],
                     details={"take_id": str(take.pk), "match_id": str(take.match_id), "created": created,
                              "is_published": take.is_published})
    messages.success(request, "Мнение опубликовано." if take.is_published else "Мнение сохранено как черновик.")
    if request.POST.get("then") == "match":
        return redirect(reverse("matches:detail", args=[take.match_id]) + "#expert-takes")
    return _back(request, "dashboard:experts")


@staff_member_required
def expert_take_create(request):
    if request.method == "POST":
        form = ExpertTakeForm(request.POST)
        if form.is_valid():
            return _save_take(request, form, created=True)
    else:
        form = ExpertTakeForm(match_id=request.GET.get("match"))
    return _take_form_page(request, form)


@staff_member_required
def expert_take_edit(request, take_id):
    take = get_object_or_404(ExpertTake, pk=take_id)
    if request.method == "POST":
        form = ExpertTakeForm(request.POST, instance=take)
        if form.is_valid():
            return _save_take(request, form, created=False)
    else:
        form = ExpertTakeForm(instance=take)
    return _take_form_page(request, form, take)


@staff_member_required
def expert_take_players(request):
    """<option> игроков выбранного матча — форма подменяет список при смене матча (HTMX)."""
    match = match_or_none(request.GET.get("match"))
    return render(request, "engagement/_expert_player_options.html", {"players": match_players(match)})


@staff_member_required
@require_POST
def expert_take_toggle(request, take_id):
    take = get_object_or_404(ExpertTake, pk=take_id)
    take.is_published = not take.is_published
    take.save(update_fields=["is_published", "updated_at"])
    log_staff_action(request, AuditAction.EXPERT_TAKE_SAVED, target=str(take)[:300],
                     details={"take_id": str(take.pk), "is_published": take.is_published})
    messages.success(request, "Мнение опубликовано." if take.is_published else "Мнение снято с сайта.")
    return _back(request, "dashboard:experts")


@staff_member_required
@require_POST
def expert_take_delete(request, take_id):
    take = get_object_or_404(ExpertTake, pk=take_id)
    label = str(take)[:300]
    take.delete()
    log_staff_action(request, AuditAction.EXPERT_TAKE_DELETED, target=label, details={"take_id": str(take_id)})
    messages.success(request, "Мнение удалено.")
    return _back(request, "dashboard:experts")


@staff_member_required
def expert_edit(request, expert_id=None):
    """Создание и правка эксперта."""
    expert = get_object_or_404(Expert, pk=expert_id) if expert_id else None
    if request.method == "POST":
        form = ExpertForm(request.POST, request.FILES, instance=expert)
        if form.is_valid():
            expert = form.save()
            log_staff_action(request, AuditAction.EXPERT_SAVED, target=expert.name,
                             details={"expert_id": str(expert.pk)})
            messages.success(request, f"Эксперт «{expert.name}» сохранён.")
            return _back(request, reverse("dashboard:experts") + "#experts")
    else:
        form = ExpertForm(instance=expert)
    return render(request, "dashboard/expert_form.html", {
        "page_title": "Эксперт — DOPX Staff",
        "active_tab": "experts",
        "form": form,
        "expert": expert,
        "next": request.GET.get("next", ""),
    })


INVITE_STATUS = {
    "active": ("действует", "badge-success"),
    "used": ("всё написано", "badge-info"),
    "expired": ("истекла", "badge-ghost"),
    "revoked": ("отключена", "badge-ghost"),
}


def _invite_rows(request):
    rows = []
    invites = (ExpertInvite.objects.select_related("expert", "match__home_team", "match__away_team")
               .annotate(used=Count("takes")).order_by("-created_at")[:20])
    for invite in invites:
        status = invite.status()
        label, css = INVITE_STATUS[status]
        rows.append({"invite": invite, "status": status, "label": label, "css": css,
                     "url": expert_invites.invite_url(request, invite), "text": expert_invites.share_text(invite)})
    return rows


@staff_member_required
def expert_invite_create(request):
    if request.method == "POST":
        form = ExpertInviteForm(request.POST)
        if form.is_valid():
            invite = form.save(commit=False)
            invite.created_by = request.user
            invite.save()
            log_staff_action(request, AuditAction.EXPERT_INVITE_CREATED, target=str(invite),
                             details={"invite_id": str(invite.pk), "match_id": str(invite.match_id or ""),
                                      "days": form.cleaned_data["days"], "auto_publish": invite.auto_publish})
            messages.success(request, "Ссылка готова. Скопируйте её или отправьте в мессенджер.")
            return redirect(f"{reverse('dashboard:experts')}?invite={invite.pk}#invites")
    else:
        form = ExpertInviteForm(match_id=request.GET.get("match"), expert_id=request.GET.get("expert"))
    return render(request, "dashboard/expert_invite_form.html", {
        "page_title": "Ссылка для эксперта — DOPX Staff",
        "active_tab": "experts",
        "form": form,
    })


@staff_member_required
@require_POST
def expert_invite_action(request, invite_id, action):
    """Отключить ссылку или продлить её на EXTEND_DAYS."""
    if action not in ("revoke", "extend"):
        from django.http import Http404
        raise Http404
    invite = get_object_or_404(ExpertInvite, pk=invite_id)
    if action == "revoke":
        invite.revoked_at = timezone.now()
        messages.success(request, "Ссылка отключена.")
    else:
        invite.revoked_at = None
        invite.expires_at = max(invite.expires_at, timezone.now()) + timedelta(days=expert_invites.EXTEND_DAYS)
        messages.success(request, f"Ссылка продлена до {timezone.localtime(invite.expires_at):%d.%m %H:%M}.")
    invite.save(update_fields=["revoked_at", "expires_at", "updated_at"])
    log_staff_action(request, AuditAction.EXPERT_INVITE_CHANGED, target=str(invite),
                     details={"invite_id": str(invite.pk), "action": action})
    return redirect(reverse("dashboard:experts") + "#invites")
