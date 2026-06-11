import logging
import os
import sys
from logging import Logger


def build_format(color):
    reset = "\x1b[0m"
    underline = "\x1b[3m"
    return f"{color}[%(asctime)s] %(levelname)s:{reset} %(message)s {underline}(%(filename)s:%(lineno)d:%(name)s){reset}"


class CustomFormatter(logging.Formatter):

    grey = "\x1b[1m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"

    FORMATS = {
        logging.DEBUG: build_format(grey),
        logging.INFO: build_format(green),
        logging.WARNING: build_format(yellow),
        logging.ERROR: build_format(red),
        logging.CRITICAL: build_format(bold_red),
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _resolve_level(log_level) -> int:
    """Resolve a level from an explicit value, the VLLM_ROUTER_LOG_LEVEL env var,
    or the default (INFO). DEBUG is opt-in: at production QPS the per-request
    DEBUG logging (full header dumps etc.) adds event-loop CPU and stdout I/O."""
    if log_level is None:
        log_level = os.environ.get("VLLM_ROUTER_LOG_LEVEL", "INFO")
    if isinstance(log_level, str):
        # uvicorn's "trace" maps to the most verbose logging level.
        if log_level.lower() == "trace":
            return logging.DEBUG
        return getattr(logging, log_level.upper(), logging.INFO)
    return log_level


def init_logger(name: str, log_level=None) -> Logger:
    level = _resolve_level(log_level)
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # The logger level is the single control point: stdout handler passes
    # everything (capped at INFO by the filter), stderr handler takes WARNING+.
    stdout_stream = logging.StreamHandler(sys.stdout)
    stdout_stream.setLevel(logging.NOTSET)
    stdout_stream.setFormatter(CustomFormatter())
    stdout_stream.addFilter(MaxLevelFilter(logging.INFO))
    logger.addHandler(stdout_stream)

    error_stream = logging.StreamHandler()
    error_stream.setLevel(logging.WARNING)
    error_stream.setFormatter(CustomFormatter())
    logger.addHandler(error_stream)
    logger.propagate = False

    return logger


def set_log_level(log_level) -> None:
    """Apply a log level to every vllm_router logger at runtime.

    Module loggers are created at import time (before args are parsed), so this
    lets --log-level take effect after the fact. The logger level is the single
    control point (handlers are level-agnostic), so setting it here is enough.
    """
    level = _resolve_level(log_level)
    for name, candidate in list(logging.root.manager.loggerDict.items()):
        if name.startswith("vllm_router") and isinstance(candidate, logging.Logger):
            candidate.setLevel(level)
