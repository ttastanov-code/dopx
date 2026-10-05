from django.urls import path

from . import views, views_experts

app_name = "engagement"

urlpatterns = [
    path("season/", views.season_pass, name="season_pass"),
    path("friends/", views.friend_leagues_view, name="friend_leagues"),
    path("friends/<str:code>/", views.friend_league_view, name="friend_league"),
    path("friends/<str:code>/join/", views.friend_league_join, name="friend_league_join"),
    path("friends/<str:code>/leave/", views.friend_league_leave, name="friend_league_leave"),
    # Хвост после кода (приклеенный мессенджером текст) — редирект на чистую ссылку.
    path("friends/<str:code>/<path:tail>", views.clean_link, {"name": "engagement:friend_league"}),
    path("invite/", views.invite, name="invite"),
    path("r/<str:code>/", views.referral, name="referral"),
    path("r/<str:code>/m/<uuid:match_id>/", views.challenge, name="challenge"),
    path("r/<str:code>/<path:tail>", views.clean_link, {"name": "engagement:referral"}),
    path("polls/<uuid:poll_id>/vote/", views.poll_vote, name="poll_vote"),
    path("share/brag/<str:username>/<str:kind>.png", views.brag_card, name="brag_card"),
    path("share/story/<str:username>/<str:kind>.png", views.story_card, name="story_card"),
    path("expert/<str:token>/", views_experts.expert_write, name="expert_write"),
    path("expert/<str:token>/players/", views_experts.expert_write_players, name="expert_write_players"),
    path("expert/<str:token>/<path:tail>", views.clean_link_token, {"name": "engagement:expert_write"}),
    path("widget/team/<uuid:team_id>/players/", views.team_players_widget, name="team_players_widget"),
]
