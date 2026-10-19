"""Small, readable runtime summaries alongside the existing SQLite audit."""

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo


class ShanghaiFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, ZoneInfo("Asia/Shanghai")).strftime(
            datefmt or "%Y-%m-%d %H:%M:%S%z"
        )


class ProjectLogFilter(logging.Filter):
    def filter(self, record):
        return record.levelno >= logging.WARNING or record.name.split(".")[0] in {
            "trip_assistant",
            "cli",
            "__main__",
            "agents",
            "context",
            "travel_data",
            "utils",
            "rag",
            "config",
        }


def configure_runtime_logging(
    log_dir=None, *, level="INFO", max_bytes=5 * 1024 * 1024, backup_count=5
):
    directory = (
        Path(log_dir)
        if log_dir is not None
        else Path(__file__).resolve().parents[2] / "data" / "logs"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "runtime.log").resolve()
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if getattr(handler, "_travel_runtime", False):
            if (
                Path(handler.baseFilename) == path
                and handler.maxBytes == max_bytes
                and handler.backupCount == backup_count
            ):
                handler.setLevel(level)
                root.setLevel(min(root.level or logging.INFO, logging._checkLevel(level)))
                return path
            root.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler._travel_runtime = True
    handler.setLevel(level)
    handler.setFormatter(ShanghaiFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(ProjectLogFilter())
    root.addHandler(handler)
    root.setLevel(min(root.level or logging.INFO, logging._checkLevel(level)))
    return path


def _scalar(value):
    return "unknown" if value is None else str(value).replace("\n", " ").replace("\r", " ")


def log_event(logger, event, **fields):
    # Call sites provide only scalar diagnostics, never transcript or payload.
    logger.info(
        "%s %s",
        event,
        " ".join(f"{key}={_scalar(value)}" for key, value in fields.items()),
    )
