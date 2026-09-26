# dopx/settings.py
import os
import sys
from datetime import timedelta
from dotenv import load_dotenv
from pathlib import Path
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from dopx.admin_nav import admin_perm, dashboard_perm

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Без явного ENVIRONMENT считаем окружение боевым: небезопасные дефолты только по явному development.
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
IS_PRODUCTION = ENVIRONMENT == "production"

if ENVIRONMENT == "development":
    # Только для локальной разработки без .env — небезопасный дефолт ОК.
    SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-key-change-in-prod")
else:
    SECRET_KEY = os.getenv("SECRET_KEY")
    if not SECRET_KEY:
        raise ImproperlyConfigured(
            "SECRET_KEY не задан в окружении, а ENVIRONMENT != 'development'. "
            "Небезопасный дефолт для не-development окружений запрещён — "
            "задайте SECRET_KEY в .env/переменных окружения сервера."
        )

DEBUG = os.getenv("DEBUG", "False") == "True"
# На проде ALLOWED_HOSTS задаётся явно; «*» только для локальной отладки.
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# Нужен за HTTPS, иначе любой POST падает на CSRF.
# Пример: CSRF_TRUSTED_ORIGINS=https://dopx.kz,https://www.dopx.kz
_csrf_trusted = os.getenv("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in _csrf_trusted.split(",") if origin.strip()]

# Sentry инициализируется до загрузки приложений. Без SENTRY_DSN — no-op.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(monitor_beat_tasks=True),
            # Breadcrumbs с WARNING, события — только с ERROR.
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        environment=ENVIRONMENT,
        # 10% трейсов.
        traces_sample_rate=0.1,
        # PII не отправляем.
        send_default_pii=False,
    )

# Application definition
INSTALLED_APPS = [
    # unfold должен стоять перед django.contrib.admin (приоритет шаблонов).
    'unfold',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
    # Third party
    'rest_framework',
    'drf_spectacular',
    'django_filters',
    # Self-hosted CAPTCHA (картинка рисуется Pillow, без внешних сервисов).
    'captcha',
    # Local apps
    'core',
    'users',
    'leagues',
    'seasons',
    'teams',
    'players',
    'coaches',
    'matches',
    'evaluations',
    'aggregates',
    'analytics',
    'api',
    'referees',
    'parsers',
    'events',
    'predictions',
    'lineups',
    'notifications',
    'dashboard',
    # Сборная сезона.
    'season_squad',
    # Сборная и игрок тура.
    'round_squad',
    # Партнёры и баннеры.
    'partners',
    # axes — защита от перебора паролей, django_otp — 2FA.
    'axes',
    'django_otp',
    'django_otp.plugins.otp_totp',
    'django_otp.plugins.otp_static',
]

# ============================================================
# django-unfold — тема админки. SIDEBAR — своё меню админки.
# ============================================================
UNFOLD = {
    "SITE_TITLE": "DOPX — администрирование",
    "SITE_HEADER": "DOPX",
    "SITE_SUBHEADER": "Панель администратора",
    "SITE_SYMBOL": "sports_soccer",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "SHOW_BACK_BUTTON": True,
    # Палитра под daisyUI-тему сайта (RGB-триплеты без запятых, как требует Unfold).
    "COLORS": {
        "primary": {
            "50": "238 242 255",
            "100": "224 231 255",
            "200": "199 210 254",
            "300": "165 180 252",
            "400": "129 140 248",
            "500": "99 102 241",
            "600": "79 70 229",
            "700": "67 56 202",
            "800": "55 48 163",
            "900": "49 46 129",
            "950": "30 27 75",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        # Не показываем автоматический список приложений — модели разложены по группам ниже.
        "show_all_applications": False,
        "navigation": [
            {
                "title": _("Staff-инструменты"),
                "separator": True,
                # Разделы staff-дашборда — в том же порядке, что в templates/dashboard/_nav.html.
                "items": [
                    {
                        "title": _("Дашборд — обзор"),
                        "icon": "dashboard",
                        "link": reverse_lazy("dashboard:overview"),
                        "permission": dashboard_perm("overview"),
                    },
                    {
                        "title": _("Трафик"),
                        "icon": "language",
                        "link": reverse_lazy("dashboard:traffic"),
                        "permission": dashboard_perm("traffic"),
                    },
                    {
                        "title": _("Матчи"),
                        "icon": "sports_soccer",
                        "link": reverse_lazy("dashboard:matches_list"),
                        "permission": dashboard_perm("matches"),
                    },
                    {
                        "title": _("Здоровье данных"),
                        "icon": "monitor_heart",
                        "link": reverse_lazy("dashboard:data_health"),
                        "permission": dashboard_perm("data_health"),
                    },
                    {
                        "title": _("Доверие к данным"),
                        "icon": "verified",
                        "link": reverse_lazy("dashboard:data_trust"),
                        "permission": dashboard_perm("data_trust"),
                    },
                    {
                        "title": _("Дубли игроков"),
                        "icon": "groups",
                        "link": reverse_lazy("dashboard:duplicate_players_review"),
                        "permission": dashboard_perm("duplicate_players"),
                    },
                    {
                        "title": _("Проверка ФИО"),
                        "icon": "auto_awesome",
                        "link": reverse_lazy("dashboard:names_review"),
                        "permission": dashboard_perm("names_review"),
                    },
                    {
                        "title": _("Оценки"),
                        "icon": "checklist",
                        "link": reverse_lazy("dashboard:evaluation_sessions_list"),
                        "permission": dashboard_perm("evaluation_sessions"),
                    },
                    {
                        "title": _("Пользователи"),
                        "icon": "group",
                        "link": reverse_lazy("dashboard:users_list"),
                        "permission": dashboard_perm("users"),
                    },
                    {
                        "title": _("Антифрод"),
                        "icon": "shield_moon",
                        "link": reverse_lazy("dashboard:antifraud"),
                        "permission": dashboard_perm("antifraud"),
                    },
                    {
                        "title": _("Парсер KFF"),
                        "icon": "cable",
                        "link": reverse_lazy("dashboard:parser_tools"),
                        "permission": dashboard_perm("parser_tools"),
                    },
                    {
                        "title": _("Реклама и виджеты"),
                        "icon": "code",
                        "link": reverse_lazy("dashboard:ads"),
                        "permission": dashboard_perm("ads"),
                    },
                    {
                        "title": _("Аудит-лог"),
                        "icon": "history",
                        "link": reverse_lazy("dashboard:audit_log"),
                        "permission": dashboard_perm("audit"),
                    },
                    {
                        "title": _("Объявления"),
                        "icon": "campaign",
                        "link": reverse_lazy("dashboard:announcements"),
                        "permission": dashboard_perm("announcements"),
                    },
                    {
                        "title": _("Настройки платформы"),
                        "icon": "tune",
                        "link": reverse_lazy("dashboard:platform_settings"),
                        "permission": dashboard_perm("platform_settings"),
                    },
                    {
                        "title": _("Системный статус"),
                        "icon": "monitor_heart",
                        "link": reverse_lazy("dashboard:system_status"),
                        "permission": dashboard_perm("system_status"),
                    },
                    {
                        "title": _("Скрипты"),
                        "icon": "terminal",
                        "link": reverse_lazy("dashboard:scripts"),
                        "permission": dashboard_perm("scripts"),
                    },
                    # Роли доступа — только для суперпользователей.
                    {
                        "title": _("Роли доступа"),
                        "icon": "admin_panel_settings",
                        "link": reverse_lazy("dashboard:access_roles_list"),
                        "permission": lambda request: request.user.is_superuser,
                    },
                    {
                        "title": _("На сайт"),
                        "icon": "open_in_new",
                        "link": reverse_lazy("core:home"),
                    },
                ],
            },
            # ------------------------------------------------------------
            # Модели Django, сгруппированные по смыслу (группы свёрнуты по умолчанию).
            # ------------------------------------------------------------
            {
                "title": _("Справочники"),
                "icon": "category",
                "collapsible": True,
                "items": [
                    {"title": _("Лиги"), "icon": "emoji_events", "link": reverse_lazy("admin:leagues_league_changelist"), "permission": admin_perm("leagues_league")},
                    {"title": _("Сезоны"), "icon": "calendar_month", "link": reverse_lazy("admin:seasons_season_changelist"), "permission": admin_perm("seasons_season")},
                    {"title": _("Команды"), "icon": "groups", "link": reverse_lazy("admin:teams_team_changelist"), "permission": admin_perm("teams_team")},
                    {"title": _("Команды в сезоне"), "icon": "table_rows", "link": reverse_lazy("admin:teams_teamseason_changelist"), "permission": admin_perm("teams_teamseason")},
                    {"title": _("Игроки"), "icon": "sports", "link": reverse_lazy("admin:players_player_changelist"), "permission": admin_perm("players_player")},
                    {"title": _("Тренеры"), "icon": "assignment_ind", "link": reverse_lazy("admin:coaches_coach_changelist"), "permission": admin_perm("coaches_coach")},
                    {"title": _("Судьи"), "icon": "sports_score", "link": reverse_lazy("admin:referees_referee_changelist"), "permission": admin_perm("referees_referee")},
                ],
            },
            {
                "title": _("Матчи и данные"),
                "icon": "scoreboard",
                "collapsible": True,
                "items": [
                    {"title": _("Матчи"), "icon": "sports_soccer", "link": reverse_lazy("admin:matches_match_changelist"), "permission": admin_perm("matches_match")},
                    {"title": _("Составы"), "icon": "assignment", "link": reverse_lazy("admin:lineups_matchlineup_changelist"), "permission": admin_perm("lineups_matchlineup")},
                    {"title": _("События матчей"), "icon": "bolt", "link": reverse_lazy("admin:events_matchevent_changelist"), "permission": admin_perm("events_matchevent")},
                    {"title": _("Реакции на события"), "icon": "mood", "link": reverse_lazy("admin:events_eventreaction_changelist"), "permission": admin_perm("events_eventreaction")},
                    {"title": _("Статистика игроков"), "icon": "leaderboard", "link": reverse_lazy("admin:matches_matchplayerstatistics_changelist"), "permission": admin_perm("matches_matchplayerstatistics")},
                    {"title": _("Статистика команд"), "icon": "bar_chart", "link": reverse_lazy("admin:matches_matchteamstatistics_changelist"), "permission": admin_perm("matches_matchteamstatistics")},
                    {"title": _("Реакции на матчи"), "icon": "add_reaction", "link": reverse_lazy("admin:matches_matchreaction_changelist"), "permission": admin_perm("matches_matchreaction")},
                    {"title": _("Прогнозы"), "icon": "online_prediction", "link": reverse_lazy("admin:predictions_matchprediction_changelist"), "permission": admin_perm("predictions_matchprediction")},
                ],
            },
            {
                "title": _("Оценки и вовлечённость"),
                "icon": "star_rate",
                "collapsible": True,
                "items": [
                    {"title": _("Сессии оценки"), "icon": "checklist", "link": reverse_lazy("admin:evaluations_evaluationsession_changelist"), "permission": admin_perm("evaluations_evaluationsession")},
                    {"title": _("Контекст оценки"), "icon": "visibility", "link": reverse_lazy("admin:evaluations_contextevaluation_changelist"), "permission": admin_perm("evaluations_contextevaluation")},
                    {"title": _("Оценки команд"), "icon": "shield", "link": reverse_lazy("admin:evaluations_teamevaluation_changelist"), "permission": admin_perm("evaluations_teamevaluation")},
                    {"title": _("Оценки игроков"), "icon": "person", "link": reverse_lazy("admin:evaluations_playerevaluation_changelist"), "permission": admin_perm("evaluations_playerevaluation")},
                    {"title": _("Оценки тренеров"), "icon": "badge", "link": reverse_lazy("admin:evaluations_coachevaluation_changelist"), "permission": admin_perm("evaluations_coachevaluation")},
                    {"title": _("Оценки судей"), "icon": "gavel", "link": reverse_lazy("admin:evaluations_refereeevaluation_changelist"), "permission": admin_perm("evaluations_refereeevaluation")},
                    {"title": _("Оценки матча"), "icon": "reviews", "link": reverse_lazy("admin:evaluations_matchevaluation_changelist"), "permission": admin_perm("evaluations_matchevaluation")},
                ],
            },
            {
                "title": _("Сборные"),
                "icon": "military_tech",
                "collapsible": True,
                "items": [
                    {"title": _("Сборные тура"), "icon": "star", "link": reverse_lazy("admin:round_squad_roundbestxi_changelist"), "permission": admin_perm("round_squad_roundbestxi")},
                    {"title": _("Сборные сезона"), "icon": "workspace_premium", "link": reverse_lazy("admin:season_squad_seasonbestxi_changelist"), "permission": admin_perm("season_squad_seasonbestxi")},
                    {"title": _("Рейтинг позиций сезона"), "icon": "format_list_numbered", "link": reverse_lazy("admin:season_squad_seasonpositionranking_changelist"), "permission": admin_perm("season_squad_seasonpositionranking")},
                ],
            },
            {
                "title": _("Пользователи"),
                "icon": "group",
                "collapsible": True,
                "items": [
                    {"title": _("Пользователи"), "icon": "person", "link": reverse_lazy("admin:users_user_changelist"), "permission": admin_perm("users_user")},
                    {"title": _("Бейджи"), "icon": "military_tech", "link": reverse_lazy("admin:users_userbadge_changelist"), "permission": admin_perm("users_userbadge")},
                    {"title": _("XP и уровни"), "icon": "trending_up", "link": reverse_lazy("admin:users_userxp_changelist"), "permission": admin_perm("users_userxp")},
                    {"title": _("Подписки (follow)"), "icon": "favorite", "link": reverse_lazy("admin:users_follow_changelist"), "permission": admin_perm("users_follow")},
                    {"title": _("Push-подписки"), "icon": "notifications_active", "link": reverse_lazy("admin:users_pushsubscription_changelist"), "permission": admin_perm("users_pushsubscription")},
                ],
            },
            {
                "title": _("Антифрод и обращения"),
                "icon": "gpp_maybe",
                "collapsible": True,
                "items": [
                    {"title": _("Подозрительная активность"), "icon": "warning", "link": reverse_lazy("admin:users_suspiciousactivityflag_changelist"), "permission": admin_perm("users_suspiciousactivityflag")},
                    {"title": _("Пороги антифрода"), "icon": "rule", "link": reverse_lazy("admin:users_antifraudthreshold_changelist"), "permission": admin_perm("users_antifraudthreshold")},
                    {"title": _("Обращения"), "icon": "mail", "link": reverse_lazy("admin:notifications_contactsubmission_changelist"), "permission": admin_perm("notifications_contactsubmission")},
                    {"title": _("Уведомления"), "icon": "notifications", "link": reverse_lazy("admin:notifications_notification_changelist"), "permission": admin_perm("notifications_notification")},
                ],
            },
            {
                "title": _("Аналитика и агрегаты"),
                "icon": "monitoring",
                "collapsible": True,
                "items": [
                    {"title": _("Агрегаты игроков"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_playermatchaggregate_changelist"), "permission": admin_perm("aggregates_playermatchaggregate")},
                    {"title": _("Агрегаты тренеров"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_coachmatchaggregate_changelist"), "permission": admin_perm("aggregates_coachmatchaggregate")},
                    {"title": _("Агрегаты матчей"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_matchaggregate_changelist"), "permission": admin_perm("aggregates_matchaggregate")},
                    {"title": _("Агрегаты команд"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_teammatchaggregate_changelist"), "permission": admin_perm("aggregates_teammatchaggregate")},
                    {"title": _("Агрегаты судей"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_refereematchaggregate_changelist"), "permission": admin_perm("aggregates_refereematchaggregate")},
                    {"title": _("Поправки рейтинга игроков"), "icon": "tune", "link": reverse_lazy("admin:aggregates_playerratingcorrection_changelist"), "permission": admin_perm("aggregates_playerratingcorrection")},
                    {"title": _("Поправки рейтинга команд"), "icon": "tune", "link": reverse_lazy("admin:aggregates_teamratingcorrection_changelist"), "permission": admin_perm("aggregates_teamratingcorrection")},
                    {"title": _("События аналитики"), "icon": "insights", "link": reverse_lazy("admin:analytics_analyticsevent_changelist"), "permission": admin_perm("analytics_analyticsevent")},
                ],
            },
            {
                "title": _("Партнёры и реклама"),
                "icon": "handshake",
                "collapsible": True,
                "items": [
                    {"title": _("Партнёры"), "icon": "business_center", "link": reverse_lazy("admin:partners_partner_changelist"), "permission": admin_perm("partners_partner")},
                    {"title": _("Баннеры"), "icon": "campaign", "link": reverse_lazy("admin:partners_banner_changelist"), "permission": admin_perm("partners_banner")},
                    {"title": _("Реклама и виджеты"), "icon": "code", "link": reverse_lazy("dashboard:ads"), "permission": dashboard_perm("ads")},
                ],
            },
            {
                "title": _("Системное"),
                "icon": "dns",
                "collapsible": True,
                "items": [
                    {"title": _("Запуски синка (парсер)"), "icon": "sync", "link": reverse_lazy("admin:parsers_parsersyncrun_changelist"), "permission": admin_perm("parsers_parsersyncrun")},
                    {"title": _("Аудит-лог staff (полный)"), "icon": "manage_history", "link": reverse_lazy("admin:dashboard_staffactionlog_changelist"), "permission": admin_perm("dashboard_staffactionlog")},
                    {"title": _("Попытки входа (axes)"), "icon": "lock_clock", "link": reverse_lazy("admin:axes_accessattempt_changelist"), "permission": admin_perm("axes_accessattempt")},
                    {"title": _("Журнал входов (axes)"), "icon": "login", "link": reverse_lazy("admin:axes_accesslog_changelist"), "permission": admin_perm("axes_accesslog")},
                    {"title": _("Неудачные входы (axes)"), "icon": "no_accounts", "link": reverse_lazy("admin:axes_accessfailurelog_changelist"), "permission": admin_perm("axes_accessfailurelog")},
                    {"title": _("Настройки платформы"), "icon": "tune", "link": reverse_lazy("admin:core_platformsetting_changelist"), "permission": admin_perm("core_platformsetting")},
                    {"title": _("Права сотрудников в дашборде"), "icon": "admin_panel_settings", "link": reverse_lazy("admin:dashboard_staffaccessgrant_changelist"), "permission": admin_perm("dashboard_staffaccessgrant")},
                    {"title": _("Группы прав /admin"), "icon": "group_work", "link": reverse_lazy("admin:auth_group_changelist"), "permission": admin_perm("auth_group")},
                    {"title": _("Расхождения импорта"), "icon": "difference", "link": reverse_lazy("admin:parsers_parserdiscrepancy_changelist"), "permission": admin_perm("parsers_parserdiscrepancy")},
                    {"title": _("Предложения ИИ по ФИО"), "icon": "auto_awesome", "link": reverse_lazy("admin:parsers_nameverificationsuggestion_changelist"), "permission": admin_perm("parsers_nameverificationsuggestion")},
                    {"title": _("Подтверждённые ФИО"), "icon": "spellcheck", "link": reverse_lazy("admin:parsers_confirmednamecorrection_changelist"), "permission": admin_perm("parsers_confirmednamecorrection")},
                    {"title": _("Дубли игроков"), "icon": "content_copy", "link": reverse_lazy("admin:players_potentialduplicateplayer_changelist"), "permission": admin_perm("players_potentialduplicateplayer")},
                    {"title": _("2FA: приложения"), "icon": "phonelink_lock", "link": reverse_lazy("admin:otp_totp_totpdevice_changelist"), "permission": admin_perm("otp_totp_totpdevice")},
                    {"title": _("2FA: резервные коды"), "icon": "password", "link": reverse_lazy("admin:otp_static_staticdevice_changelist"), "permission": admin_perm("otp_static_staticdevice")},
                ],
            },
        ],
    },
    "DASHBOARD_CALLBACK": "dashboard.admin_callback.dashboard_callback",
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # CSP-заголовок.
    'dopx.middleware.ContentSecurityPolicyMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # axes — после AuthenticationMiddleware.
    'axes.middleware.AxesMiddleware',
    # django-otp — после AuthenticationMiddleware.
    'django_otp.middleware.OTPMiddleware',
    # Обязательная 2FA для /admin/ и /staff/dashboard/ + idle-таймаут staff.
    'dashboard.middleware.StaffTwoFactorEnforcementMiddleware',
    # Права staff по разделам дашборда (после 2FA).
    'dashboard.middleware.DashboardSectionAccessMiddleware',
    'dopx.middleware.StaffSessionSecurityMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

if DEBUG:
    MIDDLEWARE += [
        'dopx.middleware.QueryCountMiddleware',
        'dopx.middleware.CacheHitMiddleware',
    ]

ROOT_URLCONF = 'dopx.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.indicator_tooltips',
                'core.context_processors.pwa_settings',
                'core.context_processors.current_round_squad',
                'core.context_processors.mobile_tabbar',
            ],
        },
    },
]

WSGI_APPLICATION = 'dopx.wsgi.application'

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "dopx"),
        "USER": os.getenv("DB_USER", "postgres"),
        "PASSWORD": os.getenv("DB_PASSWORD", "postgres"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
        "CONN_MAX_AGE": 600,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "connect_timeout": 10,
            "options": "-c statement_timeout=30000"
        }
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

AUTH_USER_MODEL = "users.User"

# Наш логин живёт на /users/login/.
LOGIN_URL = 'users:login'

# =============================================================================
# django-axes: AxesStandaloneBackend обязательно первым.
# =============================================================================
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# 5 неудачных попыток за час -> блокировка на час.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # часы
# Блокируем и по паре username+IP, и по одному username (ротация IP не помогает).
AXES_LOCKOUT_PARAMETERS = [['username'], ['username', 'ip_address']]
AXES_RESET_COOLOFF_ON_FAILURE_DURING_LOCKOUT = True
# Успешный вход сбрасывает счётчик.
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = None  # стандартный ответ axes (403)


LANGUAGE_CODE = 'ru'
# Свои переводы (в т.ч. строки темы Unfold, у которой нет русской локали).
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = "Asia/Almaty"
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Лимиты загрузки файлов. См. docs/adr/0017-upload-size-limits.md.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        # Только сессия: BasicAuth обходил 2FA и проверку подтверждения почты.
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        # Отдельный лимит для событий аналитики — вайзард легко даёт 15-20 событий за визит.
        'analytics_events': '300/hour',
    },
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

CSRF_COOKIE_HTTPONLY = False

# =============================================================================
# Безопасные cookie и заголовки (только в проде, без DEBUG).
# =============================================================================
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
# Обычная сессия — 2 недели. Для staff отдельный idle-таймаут (dopx/middleware.py).
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_SAVE_EVERY_REQUEST = True

X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'

# True — CSP только логирует нарушения (Report-Only).
CSP_REPORT_ONLY = os.getenv('CSP_REPORT_ONLY', 'False') == 'True'

# Кому можно встраивать наши виджеты в iframe. Пусто — всем (frame-ancestors *).
# Домены партнёров — через запятую в WIDGET_ALLOWED_ORIGINS.
WIDGET_ALLOWED_ORIGINS = [
    origin.strip() for origin in os.getenv('WIDGET_ALLOWED_ORIGINS', '').split(',') if origin.strip()
]

if not DEBUG:
    # HSTS и SSL-заголовок прокси — только в проде за TLS.
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# IP клиента берётся с конца X-Forwarded-For. См. docs/adr/0018-trusted-proxy-xff-parsing.md.
# За aaPanel -> nginx прокси два (docker-compose.yml задаёт 2).
TRUSTED_PROXY_COUNT = int(os.getenv('TRUSTED_PROXY_COUNT', 1))

# Голоса синтетических аккаунтов (core.utils.synthetic_users_q) в рейтингах — только вне прода.
COUNT_SYNTHETIC_VOTES = os.getenv('COUNT_SYNTHETIC_VOTES', str(not IS_PRODUCTION)) == 'True'
# Сид-команды ботов (seed_*, simulate_*, create_test_*) — только вне прода.
ALLOW_SEED_COMMANDS = os.getenv('ALLOW_SEED_COMMANDS', str(not IS_PRODUCTION)) == 'True'

# Idle-таймаут сессии только для staff.
STAFF_SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv('STAFF_SESSION_IDLE_TIMEOUT_SECONDS', 30 * 60))

# Обязательная 2FA для staff. STAFF_2FA_ENFORCED=False — аварийное отключение.
STAFF_2FA_ENFORCED = os.getenv('STAFF_2FA_ENFORCED', 'True') == 'True'

# Имя в приложении-аутентификаторе.
OTP_TOTP_ISSUER = 'DOPX'

SPECTACULAR_SETTINGS = {
    'TITLE': 'DOPX API',
    'DESCRIPTION': 'API для платформы оценки футбольных матчей',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# =============================================================================
# Celery Configuration
# =============================================================================
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = False
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
# Воркер не набирает задачи впрок — пуш не ждёт за чужой долгой задачей.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Live-опрос и пуши — в отдельной очереди realtime, её слушает отдельный воркер,
# чтобы долгие пересчёты не задерживали уведомления.
CELERY_TASK_ROUTES = {
    'parsers.sportmonks.tasks.sportmonks_update_live': {'queue': 'realtime'},
    'notifications.tasks.notify_followers_match_event': {'queue': 'realtime'},
    'notifications.tasks.notify_followers_match_started': {'queue': 'realtime'},
    'notifications.tasks.notify_followers_lineups_available': {'queue': 'realtime'},
    'notifications.tasks.notify_followers_match_activity': {'queue': 'realtime'},
    'notifications.tasks.notify_followers_match_changed': {'queue': 'realtime'},
    'notifications.tasks.send_push_task': {'queue': 'realtime'},
}

# В тестах Celery выполняет задачи синхронно, не трогая настоящую очередь.
if 'test' in sys.argv or 'pytest' in sys.modules:
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True

# Web Push: VAPID-ключи генерируются `vapid --gen`. Без них пуши не отправляются.
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_ADMIN_EMAIL = os.getenv('VAPID_ADMIN_EMAIL', 'admin@dopx.kz')

# Sportmonks — единственный источник данных матчей.
# SPORTMONKS_API_TOKEN — основной токен. SPORTMONKS_API_TOKEN_TEMP — временный:
# если задан, используется он. Вернуться на основной — убрать TEMP из .env и
# перезапустить runserver + Celery.
SPORTMONKS_API_TOKEN_MAIN = os.getenv('SPORTMONKS_API_TOKEN', '')
SPORTMONKS_API_TOKEN_TEMP = os.getenv('SPORTMONKS_API_TOKEN_TEMP', '')
SPORTMONKS_API_TOKEN = SPORTMONKS_API_TOKEN_TEMP or SPORTMONKS_API_TOKEN_MAIN
SPORTMONKS_API_TOKEN_SOURCE = 'TEMP' if SPORTMONKS_API_TOKEN_TEMP else 'MAIN'
SPORTMONKS_LEAGUE_ID = int(os.getenv('SPORTMONKS_LEAGUE_ID', '393'))  # Kazakhstan Premier League
SPORTMONKS_BASE_URL = 'https://api.sportmonks.com/v3/football'
SPORTMONKS_LOCALE = 'ru'

# Проверка ФИО через Gemini (parsers/name_ai.py). Без GEMINI_API_KEY функция отключена.
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
# Модель задаётся через env.
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')

CELERY_BEAT_SCHEDULE = {
    # =========================================================================
    # Celery Beat
    # =========================================================================
    # === Пересчёт таблицы (каждые 10 минут, все активные сезоны) ===
    'recalculate-standings': {
        'task': 'aggregates.tasks.recalculate_season_standings',
        'schedule': crontab(minute='*/10'),
    },
    # === Пересчёт агрегатов (каждые 10 минут) ===
    'recalculate-aggregates': {
        'task': 'aggregates.tasks.recalculate_all_aggregates',
        'schedule': crontab(minute='*/10'),
    },
    # === Уведомления (каждые 6 часов) ===
    'voting-closing-reminders': {
        'task': 'notifications.tasks.notify_voting_closing_soon',
        'schedule': crontab(minute='*/30'),
    },
    # Напоминание дооценить матч — за 2 часа до закрытия голосования.
    'unfinished-evaluation-reminders': {
        'task': 'notifications.tasks.notify_unfinished_evaluations',
        'schedule': crontab(minute='*/15'),
    },
    # Голосование закрылось — рейтинги открыты.
    'ratings-published': {
        'task': 'notifications.tasks.notify_ratings_published',
        'schedule': crontab(minute='*/15'),
    },
    # === Очистка старых данных (каждый день в 03:00) ===
    'cleanup-old-notifications-daily': {
        'task': 'notifications.tasks.cleanup_old_notifications',
        'schedule': crontab(hour=4, minute=0),
    },
    # === Отправка дайджеста уведомлений (каждый час) ===
    'notification-digest-hourly': {
        'task': 'notifications.tasks.send_notification_digest',
        'schedule': crontab(minute=0),
    },
    # === Мониторинг ошибок синхронизации (каждые 4 часа) ===
    'sync-error-monitor': {
        'task': 'parsers.tasks.check_sync_errors_and_alert',
        'schedule': crontab(minute=0, hour='*/4'),
    },
    # === Проверка ФИО (ИИ) — раз в месяц, только новые записи ===
    'verify-names-with-ai-monthly': {
        'task': 'parsers.tasks.verify_names_with_ai_monthly',
        'schedule': crontab(day_of_month=1, hour=3, minute=0),
    },
    # === Очистка просроченных CAPTCHA (раз в час) ===
    'cleanup-expired-captchas': {
        'task': 'core.tasks.cleanup_expired_captchas',
        'schedule': crontab(minute=15),
    },
    # === Антифрод: поиск кластеров аккаунтов с одного IP (каждые 6 часов) ===
    'detect-ip-clusters': {
        'task': 'users.tasks.detect_ip_clusters_task',
        'schedule': crontab(minute=30, hour='*/6'),
    },
    # === Всплески крайних оценок (окно детекта короткое, поэтому часто) ===
    'detect-vote-velocity-anomalies': {
        'task': 'aggregates.tasks.detect_vote_velocity_anomalies_task',
        'schedule': crontab(minute=45, hour='*/2'),
    },
    # === Калибровка антифрод-порогов по решениям модераторов (раз в неделю) ===
    'recalibrate-antifraud-thresholds': {
        'task': 'users.tasks.recalibrate_antifraud_thresholds',
        'schedule': crontab(minute=0, hour=4, day_of_week=1),
    },
    # === Автозакрытие старых слабых флагов (раз в сутки) ===
    'expire-stale-antifraud-flags': {
        'task': 'users.tasks.expire_stale_low_score_flags',
        'schedule': crontab(minute=20, hour=4),
    },
    # === Расхождение оценок команд со статистикой (раз в сутки) ===
    'detect-rating-stats-divergence': {
        'task': 'aggregates.tasks.detect_rating_stats_divergence_task',
        'schedule': crontab(minute=30, hour=5),
    },
    # === То же для игроков ===
    'detect-player-rating-stats-divergence': {
        'task': 'aggregates.tasks.detect_player_rating_stats_divergence_task',
        'schedule': crontab(minute=45, hour=5),
    },
    # === То же для тренеров (только флаг) и всплески оценок судьям ===
    'detect-coach-rating-stats-divergence': {
        'task': 'aggregates.tasks.detect_coach_rating_stats_divergence_task',
        'schedule': crontab(minute=0, hour=6),
    },
    'detect-referee-vote-spikes': {
        'task': 'aggregates.tasks.detect_referee_vote_spikes_task',
        'schedule': crontab(minute=20, hour='*/2'),
    },
    # === Бейдж «Чемпион месяца» — 1-го числа каждого месяца в 03:00 ===
    'award-monthly-champion-badge': {
        'task': 'users.tasks.award_monthly_champion_badge',
        'schedule': crontab(hour=3, minute=0, day_of_month=1),
    },
    # === Возврат trust_score к нейтральному (раз в месяц) ===
    'settle-trust-scores': {
        'task': 'users.tasks.settle_trust_scores_task',
        'schedule': crontab(minute='5,35'),
    },
    'decay-trust-scores': {
        'task': 'users.tasks.decay_trust_scores_task',
        'schedule': crontab(hour=3, minute=30, day_of_month=1),
    },
    # === Переоценка статусных достижений (раз в месяц) ===
    'revalidate-status-badges': {
        'task': 'users.tasks.revalidate_status_badges_task',
        'schedule': crontab(hour=3, minute=45, day_of_month=1),
    },
    # === Петли удержания ===
    # Напоминание о закрытии приёма прогнозов.
    'prediction-closing-reminders': {
        'task': 'notifications.tasks.notify_prediction_closing_soon',
        'schedule': crontab(minute='*/30'),
    },
    # Результат прогноза — дедупликация внутри задачи.
    'prediction-results': {
        'task': 'notifications.tasks.notify_prediction_results',
        'schedule': crontab(minute='*/30'),
    },
    # Персональная сводка недели — понедельник 10:00.
    'weekly-summary': {
        'task': 'notifications.tasks.send_weekly_summary',
        'schedule': crontab(day_of_week=1, hour=10, minute=0),
    },
    # Недельная сводка антифрода для модераторов — понедельник 09:00.
    'staff-antifraud-digest': {
        'task': 'notifications.tasks.send_staff_antifraud_digest',
        'schedule': crontab(day_of_week=1, hour=9, minute=0),
    },
    # === Сборная сезона (каждые 15 минут) ===
    'recompute-live-best-xi': {
        'task': 'season_squad.tasks.recompute_all_active_best_xi',
        'schedule': crontab(minute='*/15'),
    },
    # === Лучшие тура (каждые 15 минут) ===
    'recompute-round-of-week': {
        'task': 'round_squad.tasks.recompute_active_rounds',
        'schedule': crontab(minute='*/15'),
    },
    # =========================================================================
    # Sportmonks: лёгкий bulk-опрос ловит изменения, тяжёлая догрузка — только
    # для изменившихся матчей (parsers/sportmonks/tasks.py).
    # =========================================================================
    # === Live-опрос каждые 15 секунд ===
    # Один bulk-вызов на всю лигу: ~240 запросов/час при лимите 2000/час на entity.
    # Задача держит Redis-лок, чтобы тики не выполнялись параллельно.
    'sportmonks-update-live': {
        'task': 'parsers.sportmonks.tasks.sportmonks_update_live',
        'schedule': timedelta(seconds=15),
        # Не выполненный вовремя тик выбрасываем, чтобы не копились.
        'options': {'expires': 14},
    },
    # === Подтяжка составов для матчей в ближайшие 3 часа. ===
    'sportmonks-update-upcoming': {
        'task': 'parsers.sportmonks.tasks.sportmonks_update_upcoming',
        'schedule': crontab(minute='*/30'),
    },
    # === Досинк статистики недавно завершившихся матчей ===
    'sportmonks-resync-recent-stats': {
        'task': 'parsers.sportmonks.tasks.sportmonks_resync_recent_stats',
        'schedule': crontab(minute='*/15'),
    },
    # === Сверка календаря сезона (ночью) ===
    'sportmonks-sync-season': {
        'task': 'parsers.sportmonks.tasks.sportmonks_sync_season',
        'schedule': crontab(hour=3, minute=30),
    },
    # === Health check токена/квоты — каждые 2 часа. ===
    'sportmonks-health-check': {
        'task': 'parsers.sportmonks.tasks.sportmonks_health_check',
        'schedule': crontab(minute=15, hour='*/2'),
    },
    # === Недоступность игроков (травмы/дисквалификации) — раз в сутки. ===
    'sportmonks-sync-sidelined': {
        'task': 'parsers.sportmonks.tasks.sportmonks_sync_sidelined',
        'schedule': crontab(hour=4, minute=0),
    },
    # === Актуализация активности тренеров (без запросов к API) ===
    'sportmonks-sync-coach-activity': {
        'task': 'parsers.sportmonks.tasks.sportmonks_sync_coach_activity',
        'schedule': crontab(hour=4, minute=30),
    },
}

# =============================================================================
# CAPTCHA (django-simple-captcha) — self-hosted, без внешних ключей
# =============================================================================
CAPTCHA_LENGTH = 5
CAPTCHA_TIMEOUT = 5  # минут жизни челленджа
CAPTCHA_FONT_SIZE = 26
CAPTCHA_LETTER_ROTATION = (-35, 35)
CAPTCHA_FOREGROUND_COLOR = '#001100'
CAPTCHA_NOISE_FUNCTIONS = (
    'captcha.helpers.noise_arcs',
    'captcha.helpers.noise_dots',
)
CAPTCHA_CHALLENGE_FUNCT = 'captcha.helpers.random_char_challenge'

# =============================================================================
# Cache Configuration
# =============================================================================
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': os.getenv('REDIS_URL', 'redis://localhost:6379/1'),
        'KEY_PREFIX': 'dopx',
        'TIMEOUT': 600,
        'OPTIONS': {
            'max_connections': 50,
            'socket_connect_timeout': 5,
            'socket_timeout': 5,
        }
    },
    'aggregates': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        # Отдельная переменная, чтобы кэши не схлопнулись в одну БД Redis.
        'LOCATION': os.getenv('REDIS_AGGREGATES_URL', 'redis://localhost:6379/2'),
        'KEY_PREFIX': 'dopx_agg',
        'TIMEOUT': 300,
    }
}

# Тесты — свой кэш в памяти: не чистят и не засоряют Redis dev-сервера.
if 'test' in sys.argv or 'pytest' in sys.modules:
    CACHES = {
        'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'tests-default'},
        'aggregates': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'tests-aggregates'},
    }

# =============================================================================
# Logging
# =============================================================================
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

# Логи с ротацией: celery.log до 60 МБ (10 МБ × 6), errors.log до 40 МБ.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'celery_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'celery.log',
            'maxBytes': 10 * 1024 * 1024,  # 10 МБ
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'errors.log',
            'maxBytes': 10 * 1024 * 1024,  # 10 МБ
            'backupCount': 3,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'celery': {
            'handlers': ['celery_file', 'console'],
            'level': 'INFO',
            # propagate=False — чтобы не задваивать сообщения.
            'propagate': False,
        },
        'aggregates.tasks': {
            'handlers': ['celery_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
        'parsers.tasks': {
            'handlers': ['celery_file', 'console', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        # Логгеры дашборда и парсеров — чтобы их ошибки попадали в файлы.
        'dashboard': {
            'handlers': ['celery_file', 'console', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
        'parsers': {
            'handlers': ['celery_file', 'console', 'error_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

if DEBUG:
    INSTALLED_APPS += ['debug_toolbar']
    MIDDLEWARE += ['debug_toolbar.middleware.DebugToolbarMiddleware']
    INTERNAL_IPS = ['127.0.0.1']
    DEBUG_TOOLBAR_CONFIG = {
        # Тулбар только для INTERNAL_IPS.
        # Через туннель (ngrok) REMOTE_ADDR тоже 127.0.0.1 — отличаем по X-Forwarded-For.
        'SHOW_TOOLBAR_CALLBACK': lambda request: (
            request.META.get('HTTP_ACCEPT') != 'application/json'
            and request.META.get('REMOTE_ADDR') in INTERNAL_IPS
            and not request.META.get('HTTP_X_FORWARDED_FOR')
        ),
        # В тестах отключаем проверку тулбара (DEBUG там принудительно False).
        'IS_RUNNING_TESTS': False,
    }

# === Email Settings ===
EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@dopx.kz')
CONTACT_EMAIL = os.getenv('CONTACT_EMAIL', 'admin@dopx.kz')
ADMIN_ALERT_EMAIL = os.getenv('ADMIN_ALERT_EMAIL', CONTACT_EMAIL)
ENABLE_SYNC_ERROR_ALERTS = os.getenv('ENABLE_SYNC_ERROR_ALERTS', 'True') == 'True'
SITE_URL = os.getenv('SITE_URL', 'http://127.0.0.1:8000')

# === Admin Alert Settings ===
ADMIN_ALERT_EMAIL = os.getenv('ADMIN_ALERT_EMAIL', CONTACT_EMAIL)
ENABLE_SYNC_ERROR_ALERTS = os.getenv('ENABLE_SYNC_ERROR_ALERTS', 'True') == 'True'