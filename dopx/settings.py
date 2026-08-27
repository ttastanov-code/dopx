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

# CSRF_TRUSTED_ORIGINS нигде в проекте не был задан. На локальной разработке
# (HTTP, DEBUG=True) это незаметно — Django сверяет CSRF только для HTTPS-
# запросов. На проде за nginx/HTTPS без этой настройки ЛЮБОЙ POST (вход,
# регистрация, отправка оценки матча, голосование) отвечал бы
# "CSRF verification failed" из-за несовпадения Origin/Referer с ожидаемым
# доменом. Формат — полные origin'ы через запятую в .env, например:
# CSRF_TRUSTED_ORIGINS=https://dopx.kz,https://www.dopx.kz
_csrf_trusted = os.getenv("CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in _csrf_trusted.split(",") if origin.strip()]

# Sentry — инициализация до импорта Django-приложений, чтобы ловить ошибки
# даже на этапе загрузки INSTALLED_APPS. Без SENTRY_DSN блок — no-op.
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
    'predictions',
    'lineups',
    'notifications',
    'dashboard',
    # "Живая сборная сезона" (продуктовая фича, 2026-08-21): расчёт лучшего
    # состава 4-3-3 сезона на основе оценок пользователей (Байес-сглаживание
    # по числу голосов) + скрапинг/обработка фото игроков с kffleague.kz.
    'season_squad',
    # "DOPX Лучшие тура" (продуктовый запрос 2026-08-22, по мотивам ревью
    # Codex по season_squad): "Сборная тура"/"Игрок тура" — та же механика,
    # но по одному туру, со сглаживанием по голосам, а не по числу матчей
    # (см. докстринг round_squad/models.py).
    'round_squad',
    # Партнёрская инфраструктура (продуктовый аудит "канал привлечения",
    # 2026-08-21): Partner + Banner, атрибуция рефералов, баннерная ротация.
    'partners',
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
        # False — не дублировать автосгенерированный алфавитный список apps
        # под кастомной навигацией: у каждой модели уже есть место в
        # бизнес-группах ниже.
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
                        "title": _("Реклама и виджеты"),
                        "icon": "code",
                        "link": reverse_lazy("dashboard:ads"),
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
                "title": _("Партнёры и реклама"),
                "icon": "handshake",
                "collapsible": True,
                "items": [
                    {"title": _("Партнёры"), "icon": "business_center", "link": reverse_lazy("admin:partners_partner_changelist")},
                    {"title": _("Баннеры"), "icon": "campaign", "link": reverse_lazy("admin:partners_banner_changelist")},
                    {"title": _("Реклама и виджеты"), "icon": "code", "link": reverse_lazy("dashboard:ads")},
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
    # CSP-заголовок — чистая функция от response, не завязана на
    # request.user/сессию, поэтому стоит рядом с SecurityMiddleware.
    'dopx.middleware.ContentSecurityPolicyMiddleware',
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
                'core.context_processors.current_round_squad',
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

# БАГ, найденный при аудите перед докеризацией: Django по умолчанию
# режет тело запроса на DATA_UPLOAD_MAX_MEMORY_SIZE / FILE_UPLOAD_MAX_MEMORY_SIZE
# = 2.5 МБ (собственный дефолт фреймворка, нигде в проекте раньше не
# переопределялся). При этом users/forms.py::MAX_AVATAR_SIZE_BYTES
# заявляет лимит на аватарку в 5 МБ — но любая аватарка размером от 2.5
# до 5 МБ (а это почти любое нормальное фото с телефона) отклонялась бы
# ДО того, как запрос вообще доходил до этой проверки, с общей ошибкой
# "Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE" вместо
# понятного сообщения формы. Поднимаем до 10 МБ — с запасом и под
# аватарки, и под баннеры (partners/models.py), и под вложения формы
# "право на ответ" (core), ни один из которых явного лимита не задавал
# и молча упирался в те же 2.5 МБ. nginx (docker/nginx.conf,
# client_max_body_size) стоит ещё выше — 20 МБ — так что именно это
# значение, а не nginx, было реальным узким местом.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

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

# True — заголовок уходит как Content-Security-Policy-Report-Only (браузер
# только логирует нарушения в консоль, ничего не блокирует). См.
# dopx/middleware.py::ContentSecurityPolicyMiddleware. Полезно включить на
# первый прогон после изменения политики — прежде чем блокировать по-настоящему.
CSP_REPORT_ONLY = os.getenv('CSP_REPORT_ONLY', 'False') == 'True'
if not DEBUG:
    # HSTS и proxy-заголовок SSL — только в проде за реальным TLS-терминатором
    # (nginx/ALB), на DEBUG-окружении без сертификата это уронит локальный сервер.
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True') == 'True'
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Сколько ДОВЕРЕННЫХ обратных прокси реально стоит перед Gunicorn — в
# типовой схеме nginx -> gunicorn это 1. core.utils.get_client_ip берёт
# IP-адрес на этой позиции С КОНЦА цепочки X-Forwarded-For, а не первый
# элемент: nginx с `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
# ДОПИСЫВАЕТ реальный IP клиента в конец заголовка, а не заменяет его —
# значит именно последний элемент нельзя подделать снаружи (всё, что
# ЛЕВЕЕ него в списке, атакующий мог вписать сам). Раньше брался первый
# элемент — целиком контролируется клиентом, обходит IP rate limit одной
# строкой заголовка. Это не заменяет требование ЗАКРЫТЬ прямой доступ к
# Gunicorn извне (файрвол/security group) — без этого шага даже правильный
# разбор XFF не спасает: у атакующего, зашедшего напрямую в обход nginx,
# в заголовке будет всего один (подделанный) элемент, и он и окажется
# "последним". См. docs/BACKLOG.md.
TRUSTED_PROXY_COUNT = int(os.getenv('TRUSTED_PROXY_COUNT', 1))

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

# Web Push. Ключи — одни на проект (не на пользователя), генерируются
# командой `vapid --gen` (py-vapid). Без них send_push_to_user
# (notifications/services.py) логирует и no-op'ает, а не роняет вызывающий код.
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_ADMIN_EMAIL = os.getenv('VAPID_ADMIN_EMAIL', 'admin@dopx.kz')

# Настройки парсера — какие турниры включены
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
    # === Anti-brigading: детект аномальных всплесков голосования
    # (сговор фан-базы), 2026-08-23 — чаще, чем IP-кластер (раз в 6 часов),
    # т.к. окно детекта самого всплеска короткое (VOTE_SPIKE_WINDOW_HOURS=2
    # в aggregates/tasks.py) — реже проверять означало бы пропускать
    # всплески между прогонами. ===
    'detect-vote-velocity-anomalies': {
        'task': 'aggregates.tasks.detect_vote_velocity_anomalies_task',
        'schedule': crontab(minute=45, hour='*/2'),
    },
    # === Самокалибровка порогов vote_spike/ip_cluster по решениям
    # модератора, 2026-08-23 (см. users/models.py::AntiFraudThreshold) —
    # раз в неделю: чаще бессмысленно (нужно накопить ANTIFRAUD_
    # RECALIBRATION_MIN_SAMPLE новых разобранных флагов, это не
    # событие одного дня), реже — калибровка отстаёт от реальности. ===
    'recalibrate-antifraud-thresholds': {
        'task': 'users.tasks.recalibrate_antifraud_thresholds',
        'schedule': crontab(minute=0, hour=4, day_of_week=1),
    },
    # 2026-08-24, продуктовый запрос "модерация антифрода должна быть
    # максимально простой и не затратной по времени" — раз в сутки чистит
    # старые слабые флаги, чтобы очередь не копилась вечно (см. докстринг
    # users.tasks.expire_stale_low_score_flags). До ежедневного
    # detect-rating-stats-divergence (05:30) — независимые друг от друга
    # задачи, порядок не важен, просто развели по времени.
    'expire-stale-antifraud-flags': {
        'task': 'users.tasks.expire_stale_low_score_flags',
        'schedule': crontab(minute=20, hour=4),
    },
    # === Независимый внешний сигнал — расхождение рейтинга сообщества с
    # объективной статистикой матчей от KFF (aggregates/tasks.py::
    # detect_rating_stats_divergence_task, см. её докстринг), 2026-08-23.
    # Раз в сутки: это МЕДЛЕННЫЙ трендовый сигнал (нужно несколько матчей
    # команды), не привязан к конкретному свежему событию, как vote_spike —
    # чаще пересчитывать бессмысленно. ===
    'detect-rating-stats-divergence': {
        'task': 'aggregates.tasks.detect_rating_stats_divergence_task',
        'schedule': crontab(minute=30, hour=5),
    },
    # === Бейдж «Чемпион месяца» — 1-го числа каждого месяца в 03:00 ===
    'award-monthly-champion-badge': {
        'task': 'users.tasks.award_monthly_champion_badge',
        'schedule': crontab(hour=3, minute=0, day_of_month=1),
    },
    # === 4 петли удержания (2026-08-21) ===
    # Loop 1: напоминание о закрытии приёма прогнозов — тот же интервал,
    # что и у voting-closing-reminders выше (симметричная задача, окно
    # закрытия «на другом конце» жизни матча — см. notify_prediction_closing_soon).
    'prediction-closing-reminders': {
        'task': 'notifications.tasks.notify_prediction_closing_soon',
        'schedule': crontab(minute='*/30'),
    },
    # Loop 3: «ваш прогноз vs результат» — раз в 30 минут достаточно: окно
    # дедупликации внутри задачи (Notification уже создан для пары
    # match+user) не даёт дублей при более частых прогонах, а более редкие
    # прогоны просто увеличили бы задержку между финальным свистком и письмом.
    'prediction-results': {
        'task': 'notifications.tasks.notify_prediction_results',
        'schedule': crontab(minute='*/30'),
    },
    # Loop 2: персональная сводка недели — раз в неделю, понедельник в
    # 10:00 (по аналогии с ежемесячным award-monthly-champion-badge выше,
    # но чаще — недельный, а не месячный ритм активности).
    'weekly-summary': {
        'task': 'notifications.tasks.send_weekly_summary',
        'schedule': crontab(day_of_week=1, hour=10, minute=0),
    },
    # 2026-08-24, продуктовый запрос "модерация антифрода должна быть
    # максимально простой и не затратной по времени" — раз в неделю письмо
    # с короткой сводкой новых сигналов, не нужно самому помнить зайти на
    # /staff/dashboard/antifraud/. Понедельник в 09:00, до weekly-summary
    # (10:00) и после ежесуточного detect-rating-stats-divergence (05:30) —
    # цифры в письме успевают учесть свежий прогон.
    'staff-antifraud-digest': {
        'task': 'notifications.tasks.send_staff_antifraud_digest',
        'schedule': crontab(day_of_week=1, hour=9, minute=0),
    },
    # === «Сборная DOPX» — пересчёт лучшего XI (каждые 15 минут) ===
    # Не привязан к сигналу "оценка сохранена" (как aggregates.signals) —
    # пересчёт всего состава недёшев (несколько GROUP BY по сезону), гонять
    # его на каждый голос — лишняя нагрузка. 15 минут — тот же порядок,
    # что у recalculate-standings/recalculate-aggregates выше, достаточно
    # часто, чтобы плашка "обновлено N минут назад" не выглядела мёртвой.
    'recompute-live-best-xi': {
        'task': 'season_squad.tasks.recompute_all_active_best_xi',
        'schedule': crontab(minute='*/15'),
    },
    # === «DOPX Лучшие тура» — тот же 15-минутный ритм, что у сборной сезона ===
    # (round_squad.tasks.recompute_active_rounds сам находит незакрытые
    # туры и дёшево пропускает уже зафиксированные — см. докстринг задачи).
    'recompute-round-of-week': {
        'task': 'round_squad.tasks.recompute_active_rounds',
        'schedule': crontab(minute='*/15'),
    },
    # === Синхронизация ID + позиции игроков с KFF (раз в 3 дня в 04:30) ===
    # Переименовано из sync-kff-photos (2026-08-21) — от автоматического
    # импорта ФОТО отказались (см. parsers/tasks.py::sync_kff_player_meta и
    # core/templatetags/avatar_extras.py), но привязка Player.kff_website_id
    # и бэкафилл пустой позиции по-прежнему полезны, поэтому задачу не
    # выключаем целиком, а сузили до метаданных. Не чаще: сайт-источник
    # чужой (вежливость + не хотим банов по UA), составы команд не меняются
    # ежедневно. 04:30 — не пересекается с sync-kff-premier-league-full
    # (03:00) и cleanup-old-notifications-daily (04:00).
    'sync-kff-player-meta': {
        'task': 'parsers.tasks.sync_kff_player_meta',
        'schedule': crontab(hour=4, minute=30, day_of_month='*/3'),
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
        # БАГ, найденный при аудите перед докеризацией: раньше здесь тоже
        # стоял os.getenv('REDIS_URL', ...) — та же переменная, что и у
        # 'default' выше, просто с другим дефолтным номером БД (/2 вместо
        # /1). Пока REDIS_URL не был задан явно нигде, дефолты и правда
        # расходились — но как только REDIS_URL присутствует в окружении
        # (а в докер-стеке он ЗАДАН явно, см. docker-compose.yml), ОБА
        # cache alias'а схлопывались на одну и ту же логическую БД Redis
        # (/1). Ключи не пересекались (разные KEY_PREFIX), но два
        # логически независимых кэша переставали быть физически
        # изолированными — например, FLUSHDB на этой БД задел бы оба
        # сразу. Отдельная переменная — правильное решение.
        'LOCATION': os.getenv('REDIS_AGGREGATES_URL', 'redis://localhost:6379/2'),
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
        # test runner форсит DEBUG=False на время тестов, из-за чего
        # debug_toolbar.E001 путает это с "тулбар остался в проде".
        # IS_RUNNING_TESTS=False — штатный флаг django-debug-toolbar,
        # отключающий именно эту проверку под manage.py test.
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