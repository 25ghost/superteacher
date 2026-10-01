"""Application logging (M9).

A single ``dictConfig`` applied exactly once at startup (from
``app.main``) so every log line in the API shares one single-line shape:

    2026-09-30 14:05:09 INFO app.services.auth_service successful login

Guarantees:

- single line per record — message text can never be split across lines,
  so greps and log shippers see one event per line;
- ``extra={...}`` context fields are *not* interpolated (the formatter
  only knows timestamp/level/logger/message), which structurally prevents
  a sensitive value from leaking through an unexpected key;
- passwords, tokens and token digests are never passed to any logger —
  only identifiers (user ids) and non-secret metadata are logged.
"""
from __future__ import annotations

import logging.config

#: One line: timestamp, level, logger name, message.
SINGLE_LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
SINGLE_LINE_DATEFMT = "%Y-%m-%d %H:%M:%S"

LOGGING_CONFIG: dict = {
    "version": 1,
    # Never touch loggers configured by pytest or an embedding server.
    "disable_existing_loggers": False,
    "formatters": {
        "single_line": {
            "format": SINGLE_LINE_FORMAT,
            "datefmt": SINGLE_LINE_DATEFMT,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "single_line",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
}


def configure_logging(level: str = "INFO") -> None:
    """Apply the application logging configuration.

    Idempotent by construction: ``dictConfig`` replaces the configuration
    wholesale, and calling it again with the same arguments changes nothing,
    so importing ``app.main`` twice (tests, embedding) stays harmless.
    """
    config = {
        **LOGGING_CONFIG,
        "root": {**LOGGING_CONFIG["root"], "level": level.upper()},
    }
    logging.config.dictConfig(config)
