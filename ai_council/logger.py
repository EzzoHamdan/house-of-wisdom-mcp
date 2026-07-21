import json
import logging
from typing import Any, Optional


_TEXT_FMT = "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class _TextFormatter(logging.Formatter):
    """Human format: the base line, with structured ``data`` pretty-printed below."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        data = getattr(record, "acdata", None)
        if data is not None:
            base = f"{base}\n{json.dumps(data, indent=2, default=str)}"
        return base


class _JsonFormatter(logging.Formatter):
    """Machine format: one JSON object per line, ``data`` as a nested field."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        data = getattr(record, "acdata", None)
        if data is not None:
            payload["data"] = data
        return json.dumps(payload, default=str)


def _make_formatter(fmt: str) -> logging.Formatter:
    if fmt == "json":
        return _JsonFormatter(datefmt=_DATEFMT)
    return _TextFormatter(_TEXT_FMT, datefmt=_DATEFMT)


class AICouncilLogger:
    """Structured stderr logger for AI Council.

    Wraps the shared ``ai_council`` stdlib logger. Unlike a hard singleton,
    multiple instances may coexist — tests and embedders can hold their own —
    because every instance routes through the same *named* stdlib logger and a
    single handler. Level and format are therefore process-wide: set by whoever
    configures first (`__init__`) or calls `set_level` / `set_format` last.
    """

    def __init__(self, name: str = "ai_council", fmt: str = "text"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        # Add the handler (and announce startup) only once per named logger, so
        # constructing extra instances doesn't duplicate handlers or re-log.
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(_make_formatter(fmt))
            self.logger.addHandler(handler)
            self.info("AI Council Session Started")

    def _emit(self, level: int, message: str, data: Optional[Any]) -> None:
        # `data` rides along as a record attribute; the active formatter decides
        # how to render it (appended text vs a JSON field).
        self.logger.log(level, message, extra={"acdata": data})

    def log(self, message: str, data: Optional[Any] = None) -> None:
        self._emit(logging.INFO, message, data)

    def debug(self, message: str, data: Optional[Any] = None) -> None:
        self._emit(logging.DEBUG, message, data)

    def info(self, message: str, data: Optional[Any] = None) -> None:
        self._emit(logging.INFO, message, data)

    def warning(self, message: str, data: Optional[Any] = None) -> None:
        self._emit(logging.WARNING, message, data)

    def error(self, message: str, data: Optional[Any] = None) -> None:
        self._emit(logging.ERROR, message, data)

    def set_level(self, level: int) -> None:
        """Set the logging level using logging constants (e.g. logging.DEBUG)."""
        self.logger.setLevel(level)

    def set_format(self, fmt: str) -> None:
        """Swap the handler formatter between ``text`` and ``json`` (process-wide)."""
        for handler in self.logger.handlers:
            handler.setFormatter(_make_formatter(fmt))
