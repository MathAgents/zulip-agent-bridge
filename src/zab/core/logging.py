"""Metadata-only rotating logs (SPEC §14)."""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": self.formatTime(record),
                "level": record.levelname,
                "bot": getattr(record, "bot", None),
                "session": getattr(record, "session", None),
                "turn": getattr(record, "turn", None),
                "event": getattr(record, "event", record.name),
                "msg": record.getMessage(),
                **{
                    key: getattr(record, key)
                    for key in (
                        "backend",
                        "metrics",
                        "window_s",
                        "latency_s",
                        "latency_e2e_s",
                        "message_id",
                        "retry_after",
                    )
                    if hasattr(record, key)
                },
            }
        )


def configure(state_dir: str) -> list[logging.Handler]:
    directory = Path(state_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    file = RotatingFileHandler(directory / "zab.log", maxBytes=10 * 1024**2, backupCount=4)
    file.setFormatter(JsonFormatter())
    handlers = [console, file]
    logger = logging.getLogger("zab")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in handlers:
        logger.addHandler(handler)
    return handlers


def close(handlers: list[logging.Handler]) -> None:
    logger = logging.getLogger("zab")
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()
