from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from loguru import logger

from .config import LOG_DIR


def _get_logging_settings() -> tuple[bool, str, int, int]:
    """Get logging settings from database with fallback to defaults."""
    from . import settings

    logging_enabled = settings.get_bool("LOGGING_ENABLED")
    log_level = settings.get_string("LOGGING_LEVEL").strip().upper() or "INFO"
    max_size_mb = settings.get_int("LOGGING_MAX_SIZE_MB") or 10
    retention_days = settings.get_int("LOGGING_RETENTION_DAYS") or 25

    # Validate log level
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        log_level = "INFO"

    return logging_enabled, log_level, max_size_mb, retention_days


def setup_logging(service_name: str) -> None:
    """Configure logging with settings from database.

    Settings (from app_settings table):
    - LOGGING_ENABLED: bool (default: 1) — enable file logging
    - LOGGING_LEVEL: str (default: INFO) — log level
    - LOGGING_MAX_SIZE_MB: int (default: 10) — max file size before rotation
    - LOGGING_RETENTION_DAYS: int (default: 25) — days to keep rotated log files
    """
    logging_enabled, log_level, max_size_mb, retention_days = _get_logging_settings()
    retention_days = max(1, retention_days)

    log_path = Path(LOG_DIR)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove all existing handlers
    logger.remove()

    # Always add stderr handler (console output)
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>" + service_name + "</cyan> | {message}",
    )

    # Add file handler only if enabled
    if logging_enabled:
        logger.add(
            log_path / f"{service_name}.log",
            level=log_level,
            rotation=f"{max_size_mb} MB",
            retention=timedelta(days=retention_days),  # Delete files older than N days
            compression="zip",  # Compress rotated files to save space
            enqueue=True,
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | " + service_name + " | {message}",
        )
        logger.info(f"Logging configured: level={log_level}, max_size={max_size_mb}MB, retention={retention_days}d")
    else:
        logger.info("File logging disabled (only console output)")


def reconfigure_logging(service_name: str) -> None:
    """Reconfigure logging after settings change.
    
    This can be called when logging settings are changed via admin panel.
    Note: This only affects the current process. Other processes need to be
    restarted or call this function themselves.
    """
    setup_logging(service_name)
    logger.info("Logging reconfigured after settings change")
