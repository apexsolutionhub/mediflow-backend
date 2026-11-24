"""
Django settings for BackEnd project.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pymysql
from decouple import Csv, config

pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY")
DEBUG = config("DEBUG", cast=bool, default=False)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "rest_framework_simplejwt",
    "api",
    "tenants",
    "clinic",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "clinic.node_middleware.ClinicNodeGuardMiddleware",
]

ROOT_URLCONF = "config.urls"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

db_options = {
    "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
    "charset": "utf8mb4",
}
if config("DB_SSL", default=False, cast=bool):
    db_options["ssl"] = {"ssl": True}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": config("DB_NAME", default="Clinical_DB"),
        "USER": config("DB_USER", default="root"),
        "PASSWORD": config("DB_PASSWORD", default=""),
        "HOST": config("DB_HOST", default="127.0.0.1"),
        "PORT": config("DB_PORT", default="3306"),
        "OPTIONS": db_options,
        # Lets two test runs use different throwaway databases (set TEST_DB_NAME for the second one).
        "TEST": {"NAME": config("TEST_DB_NAME", default="") or None},
    }
}

# Tests drive a clinic node and the cloud in one process: a second, throwaway database plays the node.
if "test" in sys.argv:
    # No migrations here: an old tenants migration is MySQL-only SQL. Tables come from the models.
    DATABASES["node"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"MIGRATE": False},
    }

# Offline sync (docs/OFFLINE_SYNC_CONTRACT.md). "cloud" is the SaaS deployment; "clinic" is a
# reception-PC node, which syncs to CLOUD_API_URL with its NODE_TOKEN.
NODE_MODE = config("NODE_MODE", default="cloud")
CLOUD_API_URL = config("CLOUD_API_URL", default="")
NODE_TOKEN = config("NODE_TOKEN", default="")
# Which clinic branch this server is. Must match the scope the node token was issued for.
NODE_CLINIC_TIN = config("NODE_CLINIC_TIN", default="")
NODE_BRANCH = config("NODE_BRANCH", default="Main")
# Local backups on a clinic server (clinic/backup.py). Empty BACKUP_DIR means a `backups` folder next to BackEnd.
BACKUP_DIR = config("BACKUP_DIR", default="")
# A second disk or USB drive that every backup is also copied to (recommended).
BACKUP_COPY_DIR = config("BACKUP_COPY_DIR", default="")
BACKUP_KEEP_DAYS = config("BACKUP_KEEP_DAYS", default=7, cast=int)
BACKUP_STALE_HOURS = config("BACKUP_STALE_HOURS", default=36, cast=int)
# A backup is taken automatically right before each manager-started sync.
BACKUP_BEFORE_SYNC = config("BACKUP_BEFORE_SYNC", default=True, cast=bool)
# Encrypt every backup with this passphrase (keep a copy with the manager: no passphrase, no restore).
BACKUP_PASSPHRASE = config("BACKUP_PASSPHRASE", default="")
# Optional off-site copy of encrypted backups to S3-compatible storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO).
BACKUP_S3_ENDPOINT = config("BACKUP_S3_ENDPOINT", default="")
BACKUP_S3_BUCKET = config("BACKUP_S3_BUCKET", default="")
BACKUP_S3_ACCESS_KEY = config("BACKUP_S3_ACCESS_KEY", default="")
BACKUP_S3_SECRET_KEY = config("BACKUP_S3_SECRET_KEY", default="")
BACKUP_S3_REGION = config("BACKUP_S3_REGION", default="us-east-1")
BACKUP_S3_PREFIX = config("BACKUP_S3_PREFIX", default="mediflow")
MYSQLDUMP_PATH = config("MYSQLDUMP_PATH", default="")
MYSQL_PATH = config("MYSQL_PATH", default="")
# The static web app a clinic server serves (made by `npm run build:node`).
FRONTEND_DIR = config("FRONTEND_DIR", default=str(BASE_DIR.parent / "dist-node"))

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:3000,http://localhost:3002",
    cast=Csv(),
)

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=2),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "PAGE_SIZE_QUERY_PARAM": "page_size",
    "DEFAULT_FILTER_BACKENDS": (
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
}
