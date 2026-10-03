from django.urls import path

from . import views

app_name = "fanbot"

urlpatterns = [
    path("login/", views.login_widget, name="login"),
    path("app/", views.miniapp, name="miniapp"),
    path("app/auth/", views.miniapp_auth, name="miniapp_auth"),
    path("app/enter/", views.miniapp_enter, name="miniapp_enter"),
    path("app/allow/", views.miniapp_allow, name="miniapp_allow"),
    path("link/", views.link_start, name="link"),
    path("unlink/", views.unlink, name="unlink"),
    path("notify/", views.toggle_notify, name="toggle_notify"),
]
