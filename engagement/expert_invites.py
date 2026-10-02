# engagement/expert_invites.py
"""Ссылки для экспертов: мнение без регистрации, по одноразовой ссылке с ограниченным сроком."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Expert, ExpertInvite, ExpertTake

# Запасное окно по датам, когда туров нет.
MATCHES_BACK_DAYS, MATCHES_AHEAD_DAYS = 14, 7
# Перенесённые матчи прошлых туров: сыгранные за столько дней или назначенные на столько вперёд.
RESCHEDULED_DAYS = 10
EXPIRY_CHOICES = [(1, "1 день"), (3, "3 дня"), (7, "неделя"), (14, "2 недели"), (30, "месяц")]
EXTEND_DAYS = 7


def invite_url(request, invite) -> str:
    return request.build_absolute_uri(reverse("engagement:expert_write", args=[invite.token]))


def share_text(invite) -> str:
    """Текст приглашения для мессенджера (ссылка добавляется отдельно)."""
    hello = f"Здравствуйте, {invite.expert.name}!" if invite.expert else "Здравствуйте!"
    about = f"о матче {invite.match.home_team.name} – {invite.match.away_team.name}" if invite.match else "о матче тура"
    until = timezone.localtime(invite.expires_at).strftime("%d.%m %H:%M")
    return (f"{hello} Приглашаем вас написать мнение эксперта {about} для DOPX. "
            f"Регистрация не нужна, ссылка действует до {until}.")


def find(token: str):
    return ExpertInvite.objects.select_related("expert", "match__home_team", "match__away_team").filter(token=token).first()


def match_groups(extra_back_days: int = 0) -> list[tuple[str, list, bool]]:
    """Матчи для мнения: ближайший тур, последний сыгранный (по номеру тура) и перенесённые.
    Элемент: (подпись, матчи, показывать ли тур); extra_back_days — группа «Раньше» для дашборда."""
    from django.db.models import Max

    from matches.models import Match

    from .season import current_season

    now = timezone.now()
    base = Match.objects.exclude(status__in=["cancelled", "postponed"]).select_related("home_team", "away_team")
    season = current_season()
    groups: list[tuple[str, list, bool]] = []
    shown: set = set()

    def add(label, matches, with_tour=True):
        matches = [m for m in matches if m.pk not in shown]
        if matches:
            shown.update(m.pk for m in matches)
            groups.append((label, matches, with_tour))

    if season is not None:
        in_season = base.filter(season=season, tour__isnull=False)
        last_tour = in_season.filter(status="finished").aggregate(t=Max("tour"))["t"]
        upcoming = in_season.filter(status__in=["scheduled", "live"], start_time__gte=now - timedelta(hours=3))
        if last_tour is not None:
            upcoming = upcoming.filter(tour__gt=last_tour)
        next_match = upcoming.order_by("tour", "start_time").first()
        if next_match is not None:
            add(f"{next_match.tour}-й тур · впереди, превью",
                in_season.filter(tour=next_match.tour, status__in=["scheduled", "live"]).order_by("start_time"), False)
        if last_tour is not None:
            add(f"{last_tour}-й тур · сыгран",
                in_season.filter(tour=last_tour, status="finished").order_by("start_time"), False)
            window = timedelta(days=RESCHEDULED_DAYS)
            rescheduled = in_season.filter(tour__lte=last_tour).filter(
                Q(status="finished", start_time__gte=now - window)
                | Q(status__in=["scheduled", "live"], start_time__gte=now - timedelta(hours=3), start_time__lte=now + window)
            ).order_by("start_time")
            add("Перенесённые матчи", rescheduled)
    if not groups or extra_back_days:
        # Нет сезона или туров — просто недавние и ближайшие по датам.
        back = timedelta(days=extra_back_days or MATCHES_BACK_DAYS)
        recent = base.filter(start_time__gte=now - back, start_time__lte=now + timedelta(days=MATCHES_AHEAD_DAYS))
        add("Раньше" if groups else "Недавние и ближайшие", recent.order_by("-start_time"))
    return groups


def open_matches():
    """Матчи, о которых эксперт может написать по открытой ссылке."""
    from matches.models import Match

    ids = [m.pk for _, matches, _ in match_groups() for m in matches]
    return Match.objects.filter(pk__in=ids).select_related("home_team", "away_team")


def can_edit(take) -> bool:
    """Эксперт правит своё мнение, пока его не опубликовали."""
    return not take.is_published


@transaction.atomic
def submit(invite, data: dict, take=None):
    """Сохранить мнение эксперта; при первом визите без карточки — создать эксперта."""
    invite = ExpertInvite.objects.select_for_update().get(pk=invite.pk)
    if invite.expert is None:
        invite.expert = Expert.objects.create(name=data["name"], title=data.get("title", ""),
                                              photo=data.get("photo") or "")
    elif data.get("photo"):
        invite.expert.photo = data["photo"]
        invite.expert.save(update_fields=["photo", "updated_at"])
    invite.last_used_at = timezone.now()
    invite.save(update_fields=["expert", "last_used_at", "updated_at"])

    created = take is None
    if created:
        take = ExpertTake(invite=invite, expert=invite.expert, is_published=invite.auto_publish)
    take.match = invite.match or data["match"]
    take.headline = data.get("headline", "")
    take.text = data["text"]
    take.key_player = data.get("key_player")
    take.save()
    transaction.on_commit(lambda: _notify_staff(take, created))
    return take


def staff_recipients():
    """Кто модерирует мнения: суперпользователи и сотрудники с разделом «Эксперты»."""
    from dashboard.models import StaffAccessGrant
    from users.models import User

    granted = StaffAccessGrant.objects.filter(allowed_sections__contains=["experts"]).values("user_id")
    return User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(is_staff=True, pk__in=granted))


def _notify_staff(take, created: bool) -> None:
    from .notify import notify

    match = take.match
    title = ("🎙️ Новое мнение эксперта" if created else "🎙️ Эксперт поправил мнение")
    state = "опубликовано" if take.is_published else "ждёт проверки"
    notify(list(staff_recipients()), title=title,
           body=f"{take.display_name}: {match.home_team.name} – {match.away_team.name}, {state}.",
           url=reverse("dashboard:expert_take_edit", args=[take.pk]), kind="default", tag=f"expert-take-{take.pk}")
