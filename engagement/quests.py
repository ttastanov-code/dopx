# engagement/quests.py
"""Задания дня: каталог, умное назначение и учёт без накрутки.

Назначение — четыре слота в день: главное (что срочно: закрывается голосование, матчдень),
вовлечение (live, опросы, реакции), рост (недостающие шаги профиля или соцзадания) и знакомство с сайтом.
Сложность растёт с уровнем, вчерашние задания не повторяются, разовые после выполнения не выдаются.

Защита: каждое действие засчитывается по объекту (матч, игрок, лига, опрос…) один раз навсегда — QuestCredit;
отмена действия (отписка, удаление лиги, отключение push) снимает прогресс и забирает опыт.
События шлёт record(): сигналы engagement/signals.py, вьюхи и middleware визитов."""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

QUESTS_PER_DAY = 4
ALL_DONE_KEY = "all_done"
ALL_DONE_XP = 30
# Свежая оценка — в течение стольких часов после финального свистка.
FRESH_HOURS = 3


@dataclass(frozen=True)
class QuestDef:
    key: str
    title: str
    icon: str
    xp: int
    slot: str  # core / engage / growth / explore
    events: tuple  # какие события засчитываются
    target: Callable  # user -> int; 0 — сегодня недоступно
    url: str = ""  # url_name или путь
    one_time: bool = False  # после выполнения больше не выдаётся
    min_level: int = 1
    param: Callable | None = None  # user -> str (например, номер тура)
    accept: Callable | None = None  # (quest, ctx) -> bool: подходит ли событие этому заданию
    hint: str = ""


# ---------------------------------------------------------------- данные о пользователе и матчах

def _votable(user):
    from core.personal import votable_matches_for

    return votable_matches_for(user)


def _predictable(user):
    from predictions.services import unpredicted_matches

    return unpredicted_matches(user)


def _closing_soon(user) -> bool:
    """Голосование по неоценённому матчу закрывается в ближайшие сутки."""
    return _votable(user).filter(voting_open_until__lte=timezone.now() + timedelta(hours=24)).exists()


def _fresh_votable(user) -> int:
    return int(_votable(user).filter(end_time__gte=timezone.now() - timedelta(hours=FRESH_HOURS)).exists())


def _next_tour(user) -> tuple[str, int]:
    """(тур, сколько его матчей без прогноза) — если открыты прогнозы на весь ближайший тур."""
    from matches.models import Match

    first = _predictable(user).exclude(tour__isnull=True).first()
    if not first:
        return "", 0
    tour_matches = Match.objects.filter(season=first.season, tour=first.tour, status="scheduled")
    if any(not m.is_prediction_open() for m in tour_matches):
        return "", 0
    left = _predictable(user).filter(season=first.season, tour=first.tour).count()
    return str(first.tour), left if left >= 3 else 0


def _live_today() -> bool:
    """Идёт матч или сегодня есть матч с объявленным временем — будут live-реакции."""
    from matches.models import Match

    today = timezone.localdate()
    if Match.objects.filter(status="live").exists():
        return True
    return any(m.kickoff_known for m in Match.objects.filter(status="scheduled", start_time__date=today))


def _reactable(user) -> int:
    from matches.models import Match, MatchReaction

    recent = Match.objects.filter(status="finished", end_time__gte=timezone.now() - timedelta(days=10))
    return recent.exclude(id__in=MatchReaction.objects.filter(user=user).values("match_id")).count()


def _open_poll(user, kind: str) -> int:
    from engagement.polls import active_polls

    return int(any(p.kind == kind and not p.votes.filter(user=user).exists() for p in active_polls()))


def _club(user):
    from users.models import Follow

    follow = Follow.objects.filter(user=user, team__isnull=False).select_related("team").first()
    return follow.team if follow else None


def _followed_players(user) -> int:
    from users.models import Follow

    return Follow.objects.filter(user=user, player__isnull=False).count()


def _has_league(user) -> bool:
    return user.friend_league_memberships.exists()


def _owns_league(user) -> bool:
    return user.owned_friend_leagues.exists()


def _telegram_ready() -> bool:
    from fanbot import services

    return services.enabled() and bool(services.bot_username())


def _has_telegram(user) -> bool:
    from fanbot.models import TelegramAccount

    return TelegramAccount.objects.filter(user=user).exists()


def _has_push(user) -> bool:
    from users.models import PushSubscription

    return PushSubscription.objects.filter(user=user).exists()


def _level(user) -> int:
    xp = getattr(user, "xp", None)
    return xp.level if xp else 1


def _tour_matches(quest, ctx) -> bool:
    return str(ctx.get("tour") or "") == quest.param


def _is_fresh(quest, ctx) -> bool:
    return bool(ctx.get("fresh"))


def _full_mode(quest, ctx) -> bool:
    return ctx.get("mode") == "full"


def _kind(kind):
    return lambda quest, ctx: ctx.get("kind") == kind


# ---------------------------------------------------------------- каталог

POOL = (
    # Главное: оценки и прогнозы двигают рейтинги.
    QuestDef("evaluate", "Оцените матч", "ti-dopx-rate", 20, "core", ("evaluation",),
             lambda u: min(1, _votable(u).count()), "matches:list?status=votable"),
    QuestDef("evaluate_2", "Оцените два матча", "ti-dopx-rate", 40, "core", ("evaluation",),
             lambda u: 2 if _votable(u).count() >= 2 else 0, "matches:list?status=votable", min_level=3),
    QuestDef("evaluate_fresh", "Оцените матч в первые 3 часа после свистка", "ti-bolt", 30, "core", ("evaluation",),
             _fresh_votable, "matches:list?status=votable", accept=_is_fresh),
    QuestDef("evaluate_full", "Оцените матч в полном режиме", "ti-list-details", 30, "core", ("evaluation",),
             lambda u: min(1, _votable(u).count()), "matches:list?status=votable", min_level=4, accept=_full_mode,
             hint="Полный режим — все игроки, тренеры и судья"),
    QuestDef("predict", "Сделайте прогноз на матч", "ti-crystal-ball", 10, "core", ("prediction",),
             lambda u: min(1, _predictable(u).count()), "matches:list?status=scheduled"),
    QuestDef("predict_3", "Сделайте прогнозы на три матча", "ti-crystal-ball", 25, "core", ("prediction",),
             lambda u: 3 if _predictable(u).count() >= 3 else 0, "matches:list?status=scheduled", min_level=2),
    QuestDef("predict_round", "Спрогнозируйте весь ближайший тур", "ti-calendar-stats", 45, "core", ("prediction",),
             lambda u: _next_tour(u)[1], "matches:list?status=scheduled", min_level=3,
             param=lambda u: _next_tour(u)[0], accept=_tour_matches),
    # Вовлечение.
    QuestDef("live_react", "Отреагируйте на три момента во время матча", "ti-activity-heartbeat", 15, "engage",
             ("event_reaction",), lambda u: 3 if _live_today() else 0, "matches:list?status=live"),
    QuestDef("react", "Поставьте реакцию на матч", "ti-mood-happy", 10, "engage", ("match_reaction",),
             lambda u: min(2, _reactable(u)), "matches:list?status=finished"),
    QuestDef("episode_vote", "Рассудите спорный момент тура", "ti-scale", 10, "engage", ("poll_vote",),
             lambda u: _open_poll(u, "episode"), "/#polls", accept=_kind("episode")),
    QuestDef("duel_vote", "Дуэль тура: кто сыграл сильнее?", "ti-swords", 10, "engage", ("poll_vote",),
             lambda u: _open_poll(u, "duel"), "/#polls", accept=_kind("duel")),
    # Рост: разовые шаги профиля.
    QuestDef("follow_team", "Выберите любимый клуб", "ti-heart", 15, "growth", ("follow_team",),
             lambda u: int(_club(u) is None), "teams:list", one_time=True),
    QuestDef("telegram", "Подключите Telegram-бота для уведомлений", "ti-brand-telegram", 15, "growth",
             ("telegram_linked",), lambda u: int(_telegram_ready() and not _has_telegram(u)),
             "users:notification_settings", one_time=True),
    QuestDef("phone", "Подтвердите номер телефона", "ti-phone-check", 20, "growth", ("phone_verified",),
             lambda u: int(_telegram_ready() and not u.phone), "users:profile_edit", one_time=True),
    QuestDef("push", "Включите push-уведомления", "ti-bell-ringing", 10, "growth", ("push_enabled",),
             lambda u: int(not _has_push(u)), "users:notification_settings", one_time=True),
    QuestDef("avatar", "Добавьте аватар в профиль", "ti-photo", 10, "growth", ("avatar_set",),
             lambda u: int(not u.avatar), "users:profile_edit", one_time=True),
    QuestDef("create_league", "Создайте лигу прогнозистов с друзьями", "ti-trophy", 15, "growth", ("league_created",),
             lambda u: int(not _has_league(u)), "engagement:friend_leagues", one_time=True),
    # Рост: соцзадания — засчитываются только по действиям других людей.
    QuestDef("share_open", "Отправьте другу ссылку на матч", "ti-send", 15, "growth", ("share_open",),
             lambda u: 1, "matches:list", hint="Засчитается, когда друг откроет вашу ссылку"),
    QuestDef("league_invite", "Позовите друга в свою лигу", "ti-user-plus", 25, "growth", ("league_joined",),
             lambda u: int(_owns_league(u)), "engagement:friend_leagues", min_level=2,
             hint="Засчитается, когда друг вступит"),
    QuestDef("invite_friend", "Пригласите друга на DOPX", "ti-users", 40, "growth", ("friend_registered",),
             lambda u: 1, "engagement:invite", min_level=3, hint="Засчитается, когда друг зарегистрируется по вашей ссылке"),
    QuestDef("follow_player", "Подпишитесь на нового игрока", "ti-user-star", 5, "growth", ("follow_player",),
             lambda u: int(_followed_players(u) < 15), "players:list"),
    # Знакомство с сайтом: визиты, один раз в день.
    QuestDef("round", "Посмотрите «Лучших тура»", "ti-calendar-star", 5, "explore", ("visit:round",), lambda u: 1,
             "round_squad:round"),
    QuestDef("leaderboard", "Проверьте своё место в рейтинге болельщиков", "ti-chart-bar", 5, "explore",
             ("visit:leaderboard",), lambda u: 1, "users:leaderboard"),
    QuestDef("season_pass", "Загляните в абонемент", "ti-ticket", 5, "explore", ("visit:season_pass",), lambda u: 1,
             "engagement:season_pass"),
    QuestDef("player_page", "Изучите профиль игрока", "ti-id", 5, "explore", ("visit:player_page",), lambda u: 1,
             "players:list"),
    QuestDef("fan_zone", "Загляните в фан-зону своего клуба", "ti-flag", 5, "explore", ("visit:fan_zone",),
             lambda u: int(_club(u) is not None), ""),
    QuestDef("league", "Проверьте таблицу своей лиги", "ti-users-group", 5, "explore", ("visit:league",),
             lambda u: int(_has_league(u)), "engagement:friend_leagues"),
    QuestDef("match_story", "Узнайте, как трибуны прожили матч", "ti-heartbeat", 5, "explore", ("visit:match_story",),
             lambda u: 1, "matches:list?status=finished"),
    QuestDef("expert_read", "Прочитайте мнение эксперта", "ti-microphone", 5, "explore", ("visit:expert_read",),
             lambda u: 1, "matches:list?status=finished"),
)
POOL_BY_KEY = {q.key: q for q in POOL}
SLOTS = ("core", "engage", "growth", "explore")
# Главное: если голосование скоро закроется — оценка в приоритете.
CORE_URGENT = ("evaluate_fresh", "evaluate_2", "evaluate", "evaluate_full")
CORE_CALM = ("predict_round", "predict_3", "predict", "evaluate_full", "evaluate")


# ---------------------------------------------------------------- назначение

def _choose(user, today) -> list:
    """Четыре задания по слотам; набор стабилен в течение дня (seed по пользователю и дате)."""
    from engagement.models import DailyQuest

    rng = random.Random(f"{user.pk}:{today.isoformat()}")
    level = _level(user)
    yesterday = set(DailyQuest.objects.filter(user=user, date=today - timedelta(days=1)).values_list("key", flat=True))
    done_once = set(DailyQuest.objects.filter(user=user, completed_at__isnull=False,
                                              key__in=[q.key for q in POOL if q.one_time]).values_list("key", flat=True))
    # Сколько раз за две недели выполнял задания каждой категории — редкое поднимаем выше.
    recent = list(DailyQuest.objects.filter(user=user, date__gte=today - timedelta(days=14), completed_at__isnull=False)
                  .values_list("key", flat=True))

    def available(q) -> int:
        if q.min_level > level or q.key in done_once:
            return 0
        try:
            return q.target(user)
        except Exception:  # одно сломанное задание не должно ломать панель
            return 0

    chosen: list[tuple[QuestDef, int]] = []
    used = set()

    def take(candidates) -> bool:
        for q in candidates:
            if q.key in used:
                continue
            target = available(q)
            if target > 0:
                chosen.append((q, target))
                used.add(q.key)
                return True
        return False

    def ranked(slot):
        """Кандидаты слота: не вчерашние и реже выполнявшиеся — первыми, при равенстве — случайно."""
        items = [q for q in POOL if q.slot == slot]
        rng.shuffle(items)
        return sorted(items, key=lambda q: (q.key in yesterday, recent.count(q.key)))

    # 1. Главное.
    order = CORE_URGENT if _closing_soon(user) else CORE_CALM
    take([POOL_BY_KEY[k] for k in order]) or take(ranked("core"))
    # 2. Вовлечение: live в приоритете, затем опросы и реакции.
    take([POOL_BY_KEY["live_react"]]) or take(ranked("engage")) or take(ranked("core"))
    # 3. Рост: сначала недостающие шаги профиля, иначе соцзадания.
    onboarding = [q for q in ranked("growth") if q.one_time]
    take(onboarding) or take([q for q in ranked("growth") if not q.one_time])
    # 4. Знакомство с сайтом.
    take(ranked("explore"))
    # Добор, если какой-то слот пуст.
    for slot in SLOTS:
        while len(chosen) < QUESTS_PER_DAY and take(ranked(slot)):
            pass
    return [DailyQuest(user=user, date=today, key=q.key, target=t, xp_reward=q.xp,
                       param=(q.param(user) if q.param else "")) for q, t in chosen[:QUESTS_PER_DAY]]


def _ensure(user, today):
    from engagement.models import DailyQuest

    existing = list(DailyQuest.objects.filter(user=user, date=today))
    if existing:
        return existing
    try:
        with transaction.atomic():
            DailyQuest.objects.bulk_create(_choose(user, today))
    except IntegrityError:  # параллельный запрос успел раньше
        pass
    return list(DailyQuest.objects.filter(user=user, date=today))


def _url(definition: QuestDef, user) -> str:
    from django.urls import NoReverseMatch, reverse

    if definition.key == "fan_zone":
        club = _club(user)
        return reverse("teams:detail", args=[club.pk]) + "#fan-zone" if club else ""
    raw = definition.url
    if not raw or raw.startswith("/"):
        return raw
    name, _, query = raw.partition("?")
    try:
        return reverse(name) + (f"?{query}" if query else "")
    except NoReverseMatch:
        return ""


def today_quests(user) -> dict:
    """Задания дня для панели: список, сколько выполнено, бонус за все."""
    today = timezone.localdate()
    items = []
    for q in _ensure(user, today):
        definition = POOL_BY_KEY.get(q.key)
        if q.key == ALL_DONE_KEY or not definition:
            continue
        items.append({
            "key": q.key, "title": definition.title, "icon": definition.icon, "xp": q.xp_reward, "hint": definition.hint,
            "progress": min(q.progress, q.target), "target": q.target, "done": q.is_done,
            "percent": round(min(q.progress, q.target) * 100 / q.target) if q.target else 100,
            "url": _url(definition, user),
        })
    done = sum(1 for i in items if i["done"])
    return {"items": items, "done": done, "total": len(items), "all_done": bool(items) and done == len(items),
            "bonus_xp": ALL_DONE_XP}


# ---------------------------------------------------------------- учёт

def record(user, event: str, ref, **ctx) -> None:
    """Событие пользователя: засчитать во все сегодняшние задания, которые его ждут.
    ref — объект действия; один и тот же объект в одно задание засчитывается один раз навсегда."""
    from engagement.models import DailyQuest, QuestCredit

    if not getattr(user, "is_authenticated", False):
        return
    today = timezone.localdate()
    _ensure(user, today)
    completed = []
    for quest in DailyQuest.objects.filter(user=user, date=today, completed_at__isnull=True).exclude(key=ALL_DONE_KEY):
        definition = POOL_BY_KEY.get(quest.key)
        if not definition or event not in definition.events:
            continue
        if definition.accept and not definition.accept(quest, ctx):
            continue
        with transaction.atomic():
            try:
                with transaction.atomic():
                    QuestCredit.objects.create(user=user, quest=quest, key=quest.key, ref=str(ref)[:64])
            except IntegrityError:
                continue  # этот объект уже засчитан в это задание
            locked = DailyQuest.objects.select_for_update().get(pk=quest.pk)
            if locked.completed_at:
                continue
            locked.progress = min(locked.target, locked.progress + 1)
            if locked.progress >= locked.target:
                locked.completed_at = timezone.now()
                completed.append(locked)
            locked.save(update_fields=["progress", "completed_at", "updated_at"])
    if completed:
        from engagement.rewards import award_xp

        for quest in completed:
            award_xp(user, quest.xp_reward, f"задание {quest.key}")
        _maybe_all_done(user, today)


def revoke(user, event: str, ref) -> None:
    """Действие отменили (отписка, удаление лиги, отключение push): снять кредит, прогресс и опыт."""
    from engagement.models import DailyQuest, QuestCredit
    from engagement.rewards import revoke_xp

    keys = [q.key for q in POOL if event in q.events]
    credits = QuestCredit.objects.filter(user=user, key__in=keys, ref=str(ref)[:64], revoked_at__isnull=True)
    for credit in credits.select_related("quest"):
        with transaction.atomic():
            quest = DailyQuest.objects.select_for_update().get(pk=credit.quest_id)
            was_done = quest.completed_at is not None
            quest.progress = max(0, quest.progress - 1)
            quest.completed_at = None
            quest.save(update_fields=["progress", "completed_at", "updated_at"])
            # Запись не удаляем: повторное действие с тем же объектом не должно снова засчитаться.
            credit.revoked_at = timezone.now()
            credit.save(update_fields=["revoked_at", "updated_at"])
        if was_done:
            revoke_xp(user, quest.xp_reward, f"отмена задания {quest.key}")
            bonus = DailyQuest.objects.filter(user=user, date=quest.date, key=ALL_DONE_KEY).first()
            if bonus:
                bonus.delete()
                revoke_xp(user, bonus.xp_reward, "отмена бонуса за все задания")


def track(user, key: str, amount: int = 1) -> None:
    """Совместимость: визит на страницу — событие visit:<key> раз в день."""
    record(user, f"visit:{key}", timezone.localdate().isoformat())


def _maybe_all_done(user, today) -> None:
    from engagement.models import DailyQuest
    from engagement.rewards import award_xp

    quests = DailyQuest.objects.filter(user=user, date=today).exclude(key=ALL_DONE_KEY)
    if quests.exists() and not quests.filter(completed_at__isnull=True).exists():
        _, created = DailyQuest.objects.get_or_create(
            user=user, date=today, key=ALL_DONE_KEY,
            defaults={"target": 1, "progress": 1, "xp_reward": ALL_DONE_XP, "completed_at": timezone.now()},
        )
        if created:
            award_xp(user, ALL_DONE_XP, "все задания дня")


# ---------------------------------------------------------------- «Поделиться»: засчитываем открытие ссылки другом

def credit_share_open(request, code: str) -> None:
    """Ссылку с кодом пользователя открыл другой человек (не он сам, не с его адреса) — засчитать «Отправьте ссылку»."""
    from django.core.cache import cache

    from engagement.models import ReferralCode

    owner = ReferralCode.objects.filter(code=code).select_related("user").first()
    if owner is None:
        return
    visitor = getattr(request, "user", None)
    if visitor is not None and visitor.is_authenticated and visitor.pk == owner.user_id:
        return
    ip = request.META.get("REMOTE_ADDR", "")
    if ip and (ip == owner.user.registration_ip or ip in (cache.get(f"user_ips:{owner.user_id}") or [])):
        return  # открыл сам — с того же адреса
    if not request.session.session_key:
        request.session.save()
    visitor_key = f"u{visitor.pk}" if visitor is not None and visitor.is_authenticated else f"s{request.session.session_key}"
    record(owner.user, "share_open", visitor_key)


def remember_ip(request, user) -> None:
    """Последние адреса пользователя (для проверки, что ссылку открыл не он сам)."""
    from django.core.cache import cache

    ip = request.META.get("REMOTE_ADDR", "")
    if not ip:
        return
    key = f"user_ips:{user.pk}"
    ips = cache.get(key) or []
    if ip not in ips:
        cache.set(key, (ips + [ip])[-5:], 60 * 60 * 24 * 7)
