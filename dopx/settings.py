# dopx/settings.py
import os
from dotenv import load_dotenv
from pathlib import Path
from celery.schedules import crontab
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-key-change-in-prod")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# =============================================================================
# Sentry — error tracking (продуктовый апгрейд: раньше единственным каналом
# были email-алерты из parsers/tasks.py::_send_sync_error_alert — легко
# пропустить, без стектрейса/контекста запроса, без дедупликации похожих
# ошибок в одну карточку). Инициализация ДО импорта Django-приложений ниже —
# sentry_sdk сам патчит логирование и умеет ловить ошибки, возникающие даже
# на этапе загрузки INSTALLED_APPS.
#
# БЕЗОПАСНЫЙ NO-OP: если SENTRY_DSN не задан в .env — блок просто не
# выполняется, проект работает как раньше. Ничего не ломается на локальной
# разработке без Sentry-аккаунта.
# =============================================================================
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
            # breadcrumb с уровня WARNING (обычные логи-заметки для контекста),
            # событие в Sentry — только с ERROR (не заваливаем проект каждым
            # logger.warning() из парсера, их и так много в штатной работе).
            LoggingIntegration(level=None, event_level="ERROR"),
        ],
        environment=ENVIRONMENT,
        # 10% трейсов достаточно для профиля производительности при
        # умеренном трафике staff-дашборда и матчей; не 100% — иначе на
        # каждый HTTP-запрос идёт лишний исходящий вызов к Sentry.
        traces_sample_rate=0.1,
        # PII (email, IP) в события НЕ отправляем по умолчанию — те же
        # соображения приватности, что и hash_ip() в analytics/services.py.
        send_default_pii=False,
    )

# Application definition
INSTALLED_APPS = [
    # django-unfold ДОЛЖЕН стоять ПЕРЕД django.contrib.admin — его шаблоны
    # (admin/base.html и т.д.) переопределяют стандартные через APP_DIRS,
    # порядок INSTALLED_APPS определяет приоритет поиска шаблонов между
    # приложениями. Существующие ModelAdmin-классы (30+ по проекту) не
    # требуют переписывания — Unfold работает поверх django.contrib.admin
    # без миграции, тема применяется даже без замены base admin.ModelAdmin.
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
    # Self-hosted CAPTCHA (django-simple-captcha) — рисует картинку самим
    # Pillow'ом, никакого внешнего сервиса/API-ключа не требует (в отличие
    # от Cloudflare Turnstile/hCaptcha/reCAPTCHA). См. блок CAPTCHA_* ниже.
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
    'lineups',
    'notifications',
    'dashboard',
    # Security-стек (продуктовый апгрейд, "защита на высшем уровне" для
    # staff): axes — брутфорс-лок логина, django_otp — 2FA (TOTP + backup-коды).
    'axes',
    'django_otp',
    'django_otp.plugins.otp_totp',
    'django_otp.plugins.otp_static',
]

# ============================================================
# django-unfold — тема Django admin (продуктовый апгрейд, "оба: admin +
# ссылка на dashboard" → полноценный редизайн). Работает ПОВЕРХ обычного
# django.contrib.admin, существующие ModelAdmin-классы (30+ по проекту)
# не требуют переписывания под unfold.admin.ModelAdmin — тема применяется
# автоматически. SIDEBAR ниже — официальный способ Unfold добавлять свои
# пункты меню, вместо кастомного templates/admin/base_site.html (убран —
# конфликтовал бы с шаблонами unfold, см. git-историю этого файла).
# ============================================================
UNFOLD = {
    "SITE_TITLE": "DOPX — администрирование",
    "SITE_HEADER": "DOPX",
    "SITE_SUBHEADER": "Крауд-рейтинг Премьер-Лиги Казахстана",
    "SITE_SYMBOL": "sports_soccer",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "SHOW_BACK_BUTTON": True,
    # Палитра под daisyUI-тему сайта (primary — indigo/violet), см.
    # templates/base.html / daisyui@5 CDN. Формат — RGB-триплеты без
    # запятых, как того требует Unfold (CSS-переменные rgb(var(--c) / a)).
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
        # show_all_applications=False — раньше был True, показывал ПОД
        # нашей кастомной навигацией ещё и сырой автосгенерированный список
        # Django-приложений (тот самый "Aggregates, Analytics, Axes,
        # Coaches..." по алфавиту) — теперь у каждой модели есть осмысленное
        # место в бизнес-группах ниже, дублировать его плоским списком
        # приложений незачем (продуктовый апгрейд — "переиграть отображение
        # apps в админке более понятно").
        "show_all_applications": False,
        "navigation": [
            {
                "title": _("Staff-инструменты"),
                "separator": True,
                "items": [
                    {
                        "title": _("Дашборд — обзор"),
                        "icon": "dashboard",
                        "link": reverse_lazy("dashboard:overview"),
                    },
                    {
                        "title": _("Трафик"),
                        "icon": "language",
                        "link": reverse_lazy("dashboard:traffic"),
                    },
                    {
                        "title": _("Здоровье данных"),
                        "icon": "monitor_heart",
                        "link": reverse_lazy("dashboard:data_health"),
                    },
                    {
                        "title": _("Антифрод"),
                        "icon": "shield_moon",
                        "link": reverse_lazy("dashboard:antifraud"),
                    },
                    {
                        "title": _("Парсер KFF"),
                        "icon": "cable",
                        "link": reverse_lazy("dashboard:parser_tools"),
                    },
                    {
                        "title": _("Аудит-лог"),
                        "icon": "history",
                        "link": reverse_lazy("dashboard:audit_log"),
                    },
                    {
                        "title": _("На сайт"),
                        "icon": "open_in_new",
                        "link": reverse_lazy("core:home"),
                    },
                ],
            },
            # ------------------------------------------------------------
            # Дальше — бизнес-группировка Django-моделей (продуктовый
            # апгрейд). Раньше сайдбар admin показывал сырой список
            # Django-приложений в алфавитном порядке (Aggregates, Analytics,
            # Axes, Coaches...) — технически корректно, но staff приходится
            # знать НАЗВАНИЕ ПРИЛОЖЕНИЯ, чтобы найти нужную модель. Ниже —
            # те же ~30 моделей, сгруппированные по СМЫСЛУ использования.
            # collapsible=True — группы свёрнуты по умолчанию, разворачивать
            # по мере надобности, а не листать длинный список каждый раз.
            # ------------------------------------------------------------
            {
                "title": _("Справочники"),
                "icon": "category",
                "collapsible": True,
                "items": [
                    {"title": _("Лиги"), "icon": "emoji_events", "link": reverse_lazy("admin:leagues_league_changelist")},
                    {"title": _("Сезоны"), "icon": "calendar_month", "link": reverse_lazy("admin:seasons_season_changelist")},
                    {"title": _("Команды"), "icon": "groups", "link": reverse_lazy("admin:teams_team_changelist")},
                    {"title": _("Команды в сезоне"), "icon": "table_rows", "link": reverse_lazy("admin:teams_teamseason_changelist")},
                    {"title": _("Игроки"), "icon": "sports", "link": reverse_lazy("admin:players_player_changelist")},
                    {"title": _("Тренеры"), "icon": "assignment_ind", "link": reverse_lazy("admin:coaches_coach_changelist")},
                    {"title": _("Судьи"), "icon": "sports_score", "link": reverse_lazy("admin:referees_referee_changelist")},
                    {"title": _("Стадионы"), "icon": "stadium", "link": reverse_lazy("admin:core_stadium_changelist")},
                ],
            },
            {
                "title": _("Матчи и данные"),
                "icon": "scoreboard",
                "collapsible": True,
                "items": [
                    {"title": _("Матчи"), "icon": "sports_soccer", "link": reverse_lazy("admin:matches_match_changelist")},
                    {"title": _("Составы"), "icon": "assignment", "link": reverse_lazy("admin:lineups_matchlineup_changelist")},
                    {"title": _("События матчей"), "icon": "bolt", "link": reverse_lazy("admin:events_matchevent_changelist")},
                    {"title": _("Реакции на события"), "icon": "mood", "link": reverse_lazy("admin:events_eventreaction_changelist")},
                ],
            },
            {
                "title": _("Оценки и вовлечённость"),
                "icon": "star_rate",
                "collapsible": True,
                "items": [
                    {"title": _("Контекст оценки"), "icon": "visibility", "link": reverse_lazy("admin:evaluations_contextevaluation_changelist")},
                    {"title": _("Оценки команд"), "icon": "shield", "link": reverse_lazy("admin:evaluations_teamevaluation_changelist")},
                    {"title": _("Оценки игроков"), "icon": "person", "link": reverse_lazy("admin:evaluations_playerevaluation_changelist")},
                    {"title": _("Оценки тренеров"), "icon": "badge", "link": reverse_lazy("admin:evaluations_coachevaluation_changelist")},
                    {"title": _("Оценки судей"), "icon": "gavel", "link": reverse_lazy("admin:evaluations_refereeevaluation_changelist")},
                    {"title": _("Оценки матча"), "icon": "reviews", "link": reverse_lazy("admin:evaluations_matchevaluation_changelist")},
                ],
            },
            {
                "title": _("Пользователи"),
                "icon": "group",
                "collapsible": True,
                "items": [
                    {"title": _("Пользователи"), "icon": "person", "link": reverse_lazy("admin:users_user_changelist")},
                    {"title": _("Бейджи"), "icon": "military_tech", "link": reverse_lazy("admin:users_userbadge_changelist")},
                    {"title": _("XP и уровни"), "icon": "trending_up", "link": reverse_lazy("admin:users_userxp_changelist")},
                    {"title": _("Подписки (follow)"), "icon": "favorite", "link": reverse_lazy("admin:users_follow_changelist")},
                    {"title": _("Push-подписки"), "icon": "notifications_active", "link": reverse_lazy("admin:users_pushsubscription_changelist")},
                ],
            },
            {
                "title": _("Антифрод и обращения"),
                "icon": "gpp_maybe",
                "collapsible": True,
                "items": [
                    {"title": _("Подозрительная активность"), "icon": "warning", "link": reverse_lazy("admin:users_suspiciousactivityflag_changelist")},
                    {"title": _("Обращения"), "icon": "mail", "link": reverse_lazy("admin:notifications_contactsubmission_changelist")},
                    {"title": _("Уведомления"), "icon": "notifications", "link": reverse_lazy("admin:notifications_notification_changelist")},
                ],
            },
            {
                "title": _("Аналитика и агрегаты"),
                "icon": "monitoring",
                "collapsible": True,
                "items": [
                    {"title": _("Агрегаты игроков"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_playermatchaggregate_changelist")},
                    {"title": _("Агрегаты тренеров"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_coachmatchaggregate_changelist")},
                    {"title": _("Агрегаты матчей"), "icon": "query_stats", "link": reverse_lazy("admin:aggregates_matchaggregate_changelist")},
                    {"title": _("События аналитики"), "icon": "insights", "link": reverse_lazy("admin:analytics_analyticsevent_changelist")},
                ],
            },
            {
                "title": _("Системное"),
                "icon": "dns",
                "collapsible": True,
                "items": [
                    {"title": _("Запуски синка KFF"), "icon": "sync", "link": reverse_lazy("admin:parsers_parsersyncrun_changelist")},
                    {"title": _("Аудит-лог staff (полный)"), "icon": "manage_history", "link": reverse_lazy("admin:dashboard_staffactionlog_changelist")},
                    {"title": _("Попытки входа (axes)"), "icon": "lock_clock", "link": reverse_lazy("admin:axes_accessattempt_changelist")},
                ],
            },
        ],
    },
    "DASHBOARD_CALLBACK": "dashboard.admin_callback.dashboard_callback",
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # django-axes — блокировка брутфорса логина. Обязательно ПОСЛЕ
    # AuthenticationMiddleware (нужен request.user для отслеживания попыток).
    'axes.middleware.AxesMiddleware',
    # django-otp — прикрепляет OTP-состояние (request.user.is_verified())
    # к сессии. ПОСЛЕ AuthenticationMiddleware, ДО нашей проверки ниже.
    'django_otp.middleware.OTPMiddleware',
    # Принудительная OTP-проверка для /admin/ и /staff/dashboard/ + sliding
    # idle-таймаут сессии staff — см. dopx/middleware.py. ПОСЛЕДНИЙ из
    # security-мидлварей: должен видеть и request.user, и is_verified().
    'dashboard.middleware.StaffTwoFactorEnforcementMiddleware',
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

# LOGIN_URL не был задан НИГДЕ в проекте — Django молча падал на дефолт
# '/accounts/login/', которого в проекте не существует (сайт использует
# кастомный логин на /users/login/, см. users/urls.py). Это означало, что
# ЛЮБОЙ @login_required-вью (не только новые 2FA-страницы) для неавторизованного
# пользователя отдавал 404 вместо редиректа на форму входа — найдено при
# отладке 2FA (dashboard/views_2fa.py), но баг системный, чинится здесь один раз.
LOGIN_URL = 'users:login'

# =============================================================================
# django-axes — блокировка брутфорса логина (продуктовый апгрейд, "высший
# уровень" защиты staff-доступа). AxesStandaloneBackend ОБЯЗАТЕЛЬНО ПЕРВЫМ —
# он перехватывает попытку аутентификации ДО ModelBackend и отклоняет её,
# если по этому username/IP уже превышен лимит неудачных попыток, независимо
# от того, правильный пароль или нет. ModelBackend ниже — стандартный
# бэкенд, до сих пор неявно применявшийся по умолчанию (AUTHENTICATION_BACKENDS
# не был задан явно нигде в проекте) — явно перечисляем, чтобы не потерять.
# =============================================================================
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# 5 неудачных попыток за 1 час → блокировка на 1 час. Блокируем по паре
# username+IP (COOLOFF применяется к комбинации) — так один скомпрометированный
# пароль не блокирует легитимного сотрудника с другого IP, но и не даёт
# атакующему перебирать пароли с одного IP по разным username бесконечно.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # часы
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_RESET_COOLOFF_ON_FAILURE_DURING_LOCKOUT = True
# Сбрасывать счётчик попыток при успешном входе — иначе одна забытая старая
# неудачная попытка месяц назад тихо накапливалась бы к следующей блокировке.
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = None  # используем дефолтный ответ axes (403 + сообщение), без кастомного шаблона


LANGUAGE_CODE = 'ru'
TIME_ZONE = "Asia/Almaty"
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        # Отдельный, более щедрый лимит для событийной аналитики
        # (analytics/views.py::ClientEventThrottle) — на 6-шаговом
        # вайзарде легитимный юзер легко даёт 15-20 событий за визит,
        # глобального anon-лимита в 100/hour ему не хватит.
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
# Security hardening (продуктовый апгрейд — "защита на высшем уровне" для
# staff-доступа). Django НЕ включает Secure/SameSite-флаги на cookie по
# умолчанию — их приходится выставлять явно. `not DEBUG` — на локальной
# разработке (HTTP, без TLS) Secure-cookie просто не отправлялся бы браузером
# вообще, залогиниться было бы невозможно; в проде (DEBUG=False, обязательно
# за HTTPS) это критичный минимум.
# =============================================================================
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
# Обычная сессия — 2 недели (типичный UX сайта с оценками матчей, никто не
# хочет логиниться каждый день). Для staff — отдельный, СИЛЬНО более короткий
# sliding-таймаут накладывается через StaffSessionSecurityMiddleware ниже
# (dopx/middleware.py), а не через этот глобальный SESSION_COOKIE_AGE — он
# бьёт по ВСЕМ пользователям одинаково, укорачивать его ради staff нельзя.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_SAVE_EVERY_REQUEST = True  # нужно, чтобы sliding-таймаут ниже реально скользил

X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
if not DEBUG:
    # HSTS и proxy-заголовок SSL — только в проде за реальным TLS-терминатором
    # (nginx/ALB), на DEBUG-окружении без сертификата это уронит локальный сервер.
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Idle-таймаут сессии ТОЛЬКО для staff (is_staff=True) — обычные пользователи
# сайта под этот лимит не попадают. См. dopx/middleware.py::StaffSessionSecurityMiddleware.
STAFF_SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv('STAFF_SESSION_IDLE_TIMEOUT_SECONDS', 30 * 60))

# Feature-флаг для 2FA (dashboard/middleware.py::StaffTwoFactorEnforcementMiddleware).
# По умолчанию ВКЛЮЧЕНО — таково явное требование задачи. Флаг существует
# как аварийный рубильник: если после деплоя что-то пойдёт не так с QR/TOTP
# и staff массово не может зайти, можно временно выставить
# STAFF_2FA_ENFORCED=False в .env и перезапустить сервер БЕЗ отката кода,
# пока разбираемся — типовая enterprise-практика для рискованных security-фич.
STAFF_2FA_ENFORCED = os.getenv('STAFF_2FA_ENFORCED', 'True') == 'True'

# Название, которое увидит staff в приложении-аутентификаторе (Google
# Authenticator/Authy) рядом с кодом — иначе там был бы generic "unknown".
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

# =============================================================================
# Web Push (продуктовый аудит, раздел 5c "PWA + Web Push")
# =============================================================================
# Ключи генерируются ОДИН РАЗ на проект (не на пользователя!) командой
# `vapid --gen` из пакета `py-vapid` (устанавливается автоматически как
# зависимость `pywebpush`, см. requirements.txt) и кладутся в переменные
# окружения — как и CELERY_BROKER_URL выше, НЕ хардкодятся в settings.py.
# Пока переменные не заданы, `notifications/services.py::send_push_to_user`
# логирует предупреждение и no-op'ает вместо падения — фича должна
# деградировать мягко на окружениях, где push ещё не настроен (например,
# локальная разработка), а не ронять весь `notify_followers_match_activity`.
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_ADMIN_EMAIL = os.getenv('VAPID_ADMIN_EMAIL', 'admin@dopx.kz')

# ✅ НОВОЕ: Настройки парсера (легко включать/выключать турниры)
PARSER_SETTINGS = {
    'ENABLED_TOURNAMENTS': ['pl'],  # ['pl', '1l', '2l', 'cup'] - добавить при необходимости
    'DEFAULT_TOURNAMENT': 'pl',
    'AUTO_CREATE_SEASONS': True,
    'SYNC_RECENT_LIMIT': 10,
}

CELERY_BEAT_SCHEDULE = {
    # === БЫСТРАЯ синхронизация последних ЗАВЕРШЁННЫХ матчей Премьер-Лиги (каждые 30 мин) ===
    'sync-kff-recent-premier': {
        'task': 'parsers.tasks.sync_recent_matches',
        'schedule': crontab(minute='*/30'),
        'kwargs': {
            'tournament_code': 'pl',
            'limit': 10,
        },
        # 'options': {'queue': 'default'}
    },
    # === ПОЛНАЯ синхронизация Премьер-Лиги (раз в сутки в 03:00) ===
    'sync-kff-premier-league-full': {
        'task': 'parsers.tasks.sync_kff_premier_league',
        'schedule': crontab(hour=3, minute=0),
        # 'options': {'queue': 'default'}
    },
    # === Частое обновление для LIVE матчей ===
    'update-live-matches': {
        'task': 'parsers.tasks.update_match_statuses',
        'schedule': crontab(minute='*/2'),  # Каждые 2 минуты для live
        # 'options': {'queue': 'default'}
    },
    
    # === Реже для scheduled матчей ===
    'update-scheduled-matches': {
        'task': 'parsers.tasks.update_match_statuses',
        'schedule': crontab(minute='*/10'),  # Каждые 10 минут
        # 'options': {'queue': 'default'}
    },
    # === Пересчёт таблицы (каждые 10 минут) — ✅ АВТО-СЕЗОН ===
    'recalculate-standings': {
        'task': 'aggregates.tasks.recalculate_season_standings',
        'schedule': crontab(minute='*/10'),
        # ✅ Убрано kwargs с season_id — теперь авто-детекция
        # 'options': {'queue': 'default'}
    },
    # === Пересчёт агрегатов (каждые 10 минут) ===
    'recalculate-aggregates': {
        'task': 'aggregates.tasks.recalculate_all_aggregates',
        'schedule': crontab(minute='*/10'),
        # 'options': {'queue': 'default'}
    },
    # === Уведомления (каждые 6 часов) ===
    'voting-closing-reminders': {
        'task': 'notifications.tasks.notify_voting_closing_soon',
        'schedule': crontab(minute='*/30'),
    },
    # === Очистка старых данных (каждый день в 03:00) ===
    'cleanup-old-notifications-daily': {
        'task': 'notifications.tasks.cleanup_old_notifications',
        'schedule': crontab(hour=4, minute=0),
    },
    # === Отправка дайджеста уведомлений (каждый час) ===
    'notification-digest-hourly': {
        'task': 'notifications.tasks.send_notification_digest',
        'schedule': crontab(minute=0),  # раз в час, на весь час
    },
    # === Проверка здоровья API (каждые 2 часа) ===
    'health-check-kff-api': {
        'task': 'parsers.tasks.health_check_kff_api',
        'schedule': crontab(minute=0, hour='*/2'),
        # 'options': {'queue': 'default'}
    },
    # === Мониторинг ошибок синхронизации (каждые 4 часа) ===
    'sync-error-monitor': {
        'task': 'parsers.tasks.check_sync_errors_and_alert',
        'schedule': crontab(minute=0, hour='*/4'),
        # 'options': {'queue': 'default'}
    },
    # === Очистка просроченных CAPTCHA-записей (раз в час) ===
    # django-simple-captcha сама не удаляет истёкшие челленджи — таблица
    # captcha_captchastore росла бы бесконечно без этой задачи.
    'cleanup-expired-captchas': {
        'task': 'core.tasks.cleanup_expired_captchas',
        'schedule': crontab(minute=15),  # раз в час, со сдвигом от дайджеста
    },
    # === Антифрод: поиск кластеров аккаунтов с одного IP (каждые 6 часов) ===
    'detect-ip-clusters': {
        'task': 'users.tasks.detect_ip_clusters_task',
        'schedule': crontab(minute=30, hour='*/6'),
    },
    # === Бейдж «Чемпион месяца» — 1-го числа каждого месяца в 03:00 ===
    'award-monthly-champion-badge': {
        'task': 'users.tasks.award_monthly_champion_badge',
        'schedule': crontab(hour=3, minute=0, day_of_month=1),
    },
}

# =============================================================================
# CAPTCHA (django-simple-captcha) — self-hosted, без внешних API-ключей
# =============================================================================
# Почему не Cloudflare Turnstile/hCaptcha/reCAPTCHA: оба требуют регистрации
# аккаунта у стороннего провайдера и получения API-ключей — сознательно
# отказались от этого варианта (см. просьбу пользователя). django-simple-
# captcha рисует картинку самим Pillow'ом (уже есть в requirements.txt)
# прямо на сервере, без внешних сетевых вызовов и без передачи данных
# пользователей третьей стороне.
CAPTCHA_LENGTH = 5
CAPTCHA_TIMEOUT = 5  # минут — сколько живёт сгенерированный челлендж
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
        'LOCATION': os.getenv('REDIS_URL', 'redis://localhost:6379/2'),
        'KEY_PREFIX': 'dopx_agg',
        'TIMEOUT': 300,
    }
}

# =============================================================================
# Logging
# =============================================================================
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

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
            'class': 'logging.FileHandler',
            'filename': LOGS_DIR / 'celery.log',
            'formatter': 'verbose',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': LOGS_DIR / 'errors.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'celery': {
            'handlers': ['celery_file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
        'aggregates.tasks': {
            'handlers': ['celery_file', 'console'],
            'level': 'INFO',
            'propagate': True,
        },
        'parsers.tasks': {
            'handlers': ['celery_file', 'console', 'error_file'],
            'level': 'INFO',
            'propagate': True,
        },
    },
}

if DEBUG:
    INSTALLED_APPS += ['debug_toolbar']
    MIDDLEWARE += ['debug_toolbar.middleware.DebugToolbarMiddleware']
    INTERNAL_IPS = ['127.0.0.1']
    DEBUG_TOOLBAR_CONFIG = {
        'SHOW_TOOLBAR_CALLBACK': lambda request: request.META.get('HTTP_ACCEPT') != 'application/json',
        # ИСПРАВЛЕНО (найдено при первом прогоне manage.py test): Django
        # test runner принудительно выставляет settings.DEBUG=False на
        # время тестов — debug_toolbar.E001 видит "toolbar в INSTALLED_APPS,
        # но DEBUG=False" и считает это ошибкой конфигурации (в проде
        # тулбар быть включённым не должен). `manage.py test` НЕ равно
        # "тулбар случайно остался включён в проде" — это ожидаемое
        # поведение самого test runner'а, а не баг. IS_RUNNING_TESTS=False
        # — официальный флаг django-debug-toolbar, отключающий именно этот
        # check для тестовых прогонов.
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