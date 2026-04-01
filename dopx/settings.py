# dopx/settings.py
import os
from dotenv import load_dotenv
from pathlib import Path
from celery.schedules import crontab

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-dev-key-change-in-prod")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third party
    'rest_framework',
    'drf_spectacular',
    'django_filters',
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
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
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
        'options': {'queue': 'default'}
    },
    # === ПОЛНАЯ синхронизация Премьер-Лиги (раз в сутки в 03:00) ===
    'sync-kff-premier-league-full': {
        'task': 'parsers.tasks.sync_kff_premier_league',
        'schedule': crontab(hour=3, minute=0),
        'options': {'queue': 'default'}
    },
    # === Обновление статусов матчей (каждый час) ===
    'update-match-statuses-hourly': {
        'task': 'parsers.tasks.update_match_statuses',
        'schedule': crontab(minute=0),
        'options': {'queue': 'default'}
    },
    # === Пересчёт таблицы (каждые 10 минут) — ✅ АВТО-СЕЗОН ===
    'recalculate-standings': {
        'task': 'aggregates.tasks.recalculate_season_standings',
        'schedule': crontab(minute='*/10'),
        # ✅ Убрано kwargs с season_id — теперь авто-детекция
        'options': {'queue': 'default'}
    },
    # === Пересчёт агрегатов (каждые 10 минут) ===
    'recalculate-aggregates': {
        'task': 'aggregates.tasks.recalculate_all_aggregates',
        'schedule': crontab(minute='*/10'),
        'options': {'queue': 'default'}
    },
    # === Уведомления (каждые 6 часов) ===
    'voting-reminders': {
        'task': 'notifications.tasks.cleanup_old_notifications',
        'schedule': crontab(minute=0, hour='*/6'),
        'options': {'queue': 'default'}
    },
    # === Очистка старых данных (каждый день в 03:00) ===
    'cleanup-old-sessions-daily': {
        'task': 'notifications.tasks.cleanup_old_sessions',
        'schedule': crontab(hour=3, minute=0),
        'options': {'queue': 'default'}
    },
    # === Проверка здоровья API (каждые 2 часа) ===
    'health-check-kff-api': {
        'task': 'parsers.tasks.health_check_kff_api',
        'schedule': crontab(minute=0, hour='*/2'),
        'options': {'queue': 'default'}
    },
    # === ✅ НОВОЕ: Мониторинг ошибок синхронизации (каждые 4 часа) ===
    'sync-error-monitor': {
        'task': 'parsers.tasks.check_sync_errors_and_alert',
        'schedule': crontab(minute=0, hour='*/4'),
        'options': {'queue': 'default'}
    },
}

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