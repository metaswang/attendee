import os

from .base import *

DEBUG = True
SITE_DOMAIN = "localhost:8000"
ALLOWED_HOSTS = ["tendee-stripe-hooks.ngrok.io", "localhost"]

_default_db = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.getenv("POSTGRES_DB", "attendee_development"),
    "USER": os.getenv("POSTGRES_USER", "attendee_development_user"),
    "PASSWORD": os.getenv("POSTGRES_PASSWORD", "attendee_development_user"),
    "HOST": os.getenv("POSTGRES_HOST", "localhost"),
    "PORT": os.getenv("POSTGRES_PORT", "5432"),
}
if os.getenv("POSTGRES_SSLMODE"):
    _default_db = {**_default_db, "OPTIONS": {"sslmode": os.getenv("POSTGRES_SSLMODE")}}

DATABASES = {"default": _default_db}

# Log more stuff in development
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        # Uncomment to log database queries
        # "django.db.backends": {
        #    "handlers": ["console"],
        #    "level": "DEBUG",
        #    "propagate": False,
        # },
    },
}
