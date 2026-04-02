import os
import sys

from .base import *
from .base import LOG_FORMATTERS

DEBUG = False
ALLOWED_HOSTS = ["*"]

# Runtime containers should not need direct PostgreSQL access.
# Any accidental ORM use should fail fast during the migration.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.dummy",
    }
}

IS_BOT_RUNTIME = True
DISABLE_ADMIN = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": LOG_FORMATTERS,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": os.getenv("ATTENDEE_LOG_FORMAT"),
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": os.getenv("ATTENDEE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
