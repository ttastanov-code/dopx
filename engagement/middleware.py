# engagement/middleware.py
"""Отмечает день серии и задания «загляните на страницу» при обычном заходе (не фоновые запросы и не HTMX).
День серии — до рендера, чтобы страница сразу показала новое значение."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# url_name -> задание дня за визит.
VISIT_QUESTS = {
    "users:leaderboard": "leaderboard",
    "engagement:season_pass": "season_pass",
    "players:detail": "player_page",
    "round_squad:round": "round",
}


class DailyStreakMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        tracked = (
            request.method == "GET" and user is not None and user.is_authenticated
            and not request.headers.get("HX-Request") and not request.headers.get("X-Live-Refresh")
            and not request.path.startswith(("/static/", "/media/", "/api/", "/admin/"))
        )
        if tracked:
            self._touch(request, user)
        # Ссылка «поделиться» с кодом автора (?from=): засчитать автору, если открыл другой человек.
        code = request.GET.get("from") if request.method == "GET" else None
        if code and len(code) <= 32 and not request.headers.get("HX-Request"):
            try:
                from engagement.quests import credit_share_open

                credit_share_open(request, code)
            except Exception:
                logger.exception("engagement share credit failed")
        response = self.get_response(request)
        if tracked and response.status_code == 200:
            match = getattr(request, "resolver_match", None)
            quest = VISIT_QUESTS.get(match.view_name) if match else None
            if quest:
                try:
                    from engagement.quests import track

                    track(user, quest)
                except Exception:
                    logger.exception("engagement visit quest failed")
        return response

    @staticmethod
    def _touch(request, user):
        from engagement.quests import remember_ip

        remember_ip(request, user)  # чтобы свою же ссылку, открытую с того же адреса, не засчитать
        try:
            from django.contrib import messages

            from engagement.streaks import toast, touch

            streak = touch(user)
            text = toast(streak) if streak else ""
            if text:
                messages.info(request, text)
        except Exception:  # серия не должна ломать страницу
            logger.exception("engagement streak touch failed")
