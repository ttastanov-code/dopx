# season_squad/urls.py
from django.urls import path

from season_squad import views

app_name = 'season_squad'

urlpatterns = [
    # Без season_id — активный сезон главной лиги (Season.get_primary_active,
    # тот же принцип "умолчания", что у players:season_recap).
    path('best-xi/', views.best_xi, name='best_xi'),
    path('best-xi/partial/', views.best_xi_partial, name='best_xi_partial'),
    path('<uuid:season_id>/best-xi/', views.best_xi, name='best_xi'),
    path('<uuid:season_id>/best-xi/partial/', views.best_xi_partial, name='best_xi_partial'),
    # Embed-виджет для чужих сайтов (players:widget/teams:widget — тот же
    # паттерн) — см. views.py::best_xi_widget.
    path('best-xi/widget/', views.best_xi_widget, name='widget'),
    path('<uuid:season_id>/best-xi/widget/', views.best_xi_widget, name='widget'),
]
