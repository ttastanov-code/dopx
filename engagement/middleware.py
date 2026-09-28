# engagement/middleware.py
"""Отмечает день серии и задания «загляните на страницу» при обычном заходе (не фоновые запросы и не HTMX)."""
from __future__ import annotations

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
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if (
            request.method == "GET" and user is not None and user.is_authenticated
            and response.status_code == 200 and not request.headers.get("HX-Request")
            and not request.headers.get("X-Live-Refresh")
            and not request.path.startswith(("/static/", "/media/", "/api/", "/admin/"))
        ):
            try:
                from engagement.streaks import touch

                touch(user)
                match = getattr(request, "resolver_match", None)
                quest = VISIT_QUESTS.get(match.view_name) if match else None
                if quest:
                    from engagement.quests import track

                    track(user, quest)
            except Exception:  # серия не должна ломать страницу
                import logging

                logging.getLogger(__name__).exception("engagement visit tracking failed")
        return response
