import os

from .base import *

DEBUG = True
SITE_DOMAIN = os.getenv("SITE_DOMAIN", "localhost:8100")
_allowed_hosts = ["tendee-stripe-hooks.ngrok.io", "localhost", "127.0.0.1"]
_extra_allowed_hosts = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "").split(",") if host.strip()]
ALLOWED_HOSTS = list(dict.fromkeys(_allowed_hosts + _extra_allowed_hosts))

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "attendee_development"),
        "USER": os.getenv("POSTGRES_USER", "attendee_development_user"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "attendee_development_user"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": "5432",
    }
}

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
