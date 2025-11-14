# server-vision-pipeline/common/logging.py
from __future__ import annotations
import logging, os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

def _level_from_env(default: str = "INFO") -> int:
    return _LEVELS.get(os.getenv("LOG_LEVEL", default).upper(), logging.INFO)

def get_logger(name: str, log_dir: str = "logs", level: Optional[str] = None) -> logging.Logger:
    """
    Rotating file + console logger.
    - logs/<name>.log (5 MB x 5 files)
    - honors LOG_LEVEL env or provided level
    - idempotent (safe to call multiple times)
    """
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured
        return logger

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_level = _LEVELS.get(level.upper(), _level_from_env()) if level else _level_from_env()

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # File (rotating)
    fh = RotatingFileHandler(
        filename=str(Path(log_dir) / f"{name}.log"),
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(log_level)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(log_level)

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.setLevel(log_level)
    logger.propagate = False
    return logger
