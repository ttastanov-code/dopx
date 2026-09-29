# engagement/views_experts.py
"""Страница эксперта по ссылке-приглашению: без регистрации, токен в адресе."""
from __future__ import annotations

from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from core.utils import is_rate_limited

from . import expert_invites
from .forms import ExpertSubmitForm, match_or_none, match_players

# Отправок формы с одной ссылки в час.
SUBMITS_PER_HOUR = 20


def _private(response):
    # Токен не уходит на чужие сайты через Referer (no-referrer ломает CSRF: Origin: null) и в поиск.
    response["Referrer-Policy"] = "same-origin"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _invite_or_404(token):
    invite = expert_invites.find(token)
    if invite is None:
        raise Http404("Ссылка не найдена")
    return invite


@never_cache
def expert_write(request, token):
    invite = _invite_or_404(token)
    takes = list(invite.takes.select_related("match__home_team", "match__away_team").order_by("created_at"))
    status = invite.status()
    take = None
    if request.GET.get("take"):
        take = next((t for t in takes if str(t.pk) == request.GET["take"] and expert_invites.can_edit(t)), None)
    can_write = status == "active" or (take is not None and status in ("active", "used"))

    form = None
    if can_write:
        if request.method == "POST":
            form = ExpertSubmitForm(request.POST, request.FILES, invite=invite, take=take)
            if is_rate_limited(f"expert-invite:{invite.pk}", SUBMITS_PER_HOUR, 3600):
                form.add_error(None, "Слишком много отправок подряд. Попробуйте через час.")
            elif form.is_valid():
                saved = expert_invites.submit(invite, form.cleaned_data, take=take)
                return _private(redirect(f"{reverse('engagement:expert_write', args=[token])}?sent={saved.pk}"))
        else:
            form = ExpertSubmitForm(invite=invite, take=take)

    sent = next((t for t in takes if str(t.pk) == request.GET.get("sent")), None)
    return _private(render(request, "engagement/expert_write.html", {
        "page_title": "Мнение эксперта · DOPX",
        "invite": invite,
        "status": status,
        "takes": takes,
        "take": take,
        "form": form,
        "sent": sent,
        "left": max(0, invite.max_takes - len(takes)),
    }))


def expert_write_players(request, token):
    """<option> игроков выбранного матча для формы эксперта."""
    invite = _invite_or_404(token)
    if invite.status() != "active":
        raise Http404
    match = match_or_none(request.GET.get("match"))
    if match is None or not expert_invites.open_matches().filter(pk=match.pk).exists():
        match = None
    return _private(render(request, "engagement/_expert_player_options.html", {"players": match_players(match)}))
