"""Конфиг разделён на два слоя:

1. **Из env** (этот файл): секреты и параметры развёртывания —
   путь к БД и логам, токен Telegram-бота, таймзона, admin-учётка,
   ingest-лимиты. Эти значения читаются один раз при старте.

2. **Из БД** (см. app/settings.py): user-facing настройки — название,
   бренд, тексты, расписания, флаги. Их редактирует владелец из
   /admin/settings, после изменения немедленно подхватываются веб-частью.

Env-значения для (2) используются только при первом запуске пустой БД
(см. seed_defaults_if_empty). После — БД источник истины.
"""
from __future__ import annotations

import os
from pathlib import Path


def _parse_str_list(value: str) -> list[str]:
    return [chunk.strip() for chunk in value.split(",") if chunk.strip()]


def _load_dotenv(path: Path) -> None:
    """Подгружает переменные из .env, если файл существует.

    Существующие переменные окружения не перезаписываются — env приоритетнее
    .env (важно для Docker, где env_file уже всё прокинул). Это нужно, чтобы
    локальный запуск через `uvicorn app.main:app` без `set -a; source .env`
    тоже работал из коробки. Поддерживаются строки вида KEY=VALUE,
    комментарии (#) и одинарные/двойные кавычки вокруг значения.
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


# ---------- Deployment / paths ----------

BASE_DIR = Path(__file__).resolve().parent.parent
_load_dotenv(BASE_DIR / ".env")
DATABASE_PATH = Path(os.getenv("WEATHER_DB_PATH", BASE_DIR / "weather.sqlite3"))
LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
WEATHER_TIMEZONE = os.getenv("WEATHER_TIMEZONE", "UTC")

# ---------- Secrets ----------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ---------- Admin auth (HTTP Basic) ----------
# Если задан ADMIN_PASSWORD_HASH (bcrypt-хэш) — он используется как есть.
# Иначе если задан ADMIN_PASSWORD — он хешируется в память на старте.
# Если ни то, ни другое — admin-страницы отключены.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()

# ---------- Ingest security ----------

INGEST_TOKEN = os.getenv("INGEST_TOKEN", "").strip()
INGEST_MAX_BODY_BYTES = int(os.getenv("INGEST_MAX_BODY_BYTES", str(512 * 1024)))
INGEST_MAX_DEVICES = int(os.getenv("INGEST_MAX_DEVICES", "5"))
INGEST_MAX_SENSORS_PER_DEVICE = int(os.getenv("INGEST_MAX_SENSORS_PER_DEVICE", "128"))
# Deprecated: с миграцией v2 список разрешённых mac хранится в таблице
# stations. Оставлено для обратной совместимости первого запуска: на этапе
# seed мы перенесём env-значение как имя первой записи stations.
INGEST_ALLOWED_MACS = _parse_str_list(os.getenv("INGEST_ALLOWED_MACS", ""))


# ---------- Seed для app_settings (только для первого запуска) ----------

def env_settings_dict() -> dict[str, str]:
    """Возвращает только те env-переменные из SETTINGS_SCHEMA, что заданы.

    Используется в seed_defaults_if_empty: ключи без env-значения
    получат default из самой схемы.
    """
    keys = (
        "APP_TITLE",
        "SITE_BRAND",
        "WEATHER_PLACE_NAME",
        "WEATHER_SITE_URL",
        "YANDEX_METRIKA_ID",
        "WEATHER_TIMEZONE",
        "TIME_FORMAT",
        "TELEGRAM_ADMIN_IDS",
        "TELEGRAM_DAILY_USER_IDS",
        "TELEGRAM_DAILY_TIMES",
        "TELEGRAM_STALE_MINUTES",
        "TELEGRAM_MONITOR_INTERVAL_SECONDS",
        "TELEGRAM_DYNAMIC_NAME_ENABLED",
        "TELEGRAM_DYNAMIC_NAME_PREFIX",
        "TELEGRAM_DYNAMIC_NAME_INTERVAL_MINUTES",
        "AUTO_REGISTER_STATIONS",
        "LOGGING_ENABLED",
        "LOGGING_LEVEL",
        "LOGGING_MAX_SIZE_MB",
        "LOGGING_BACKUP_COUNT",
    )
    overrides: dict[str, str] = {}
    for key in keys:
        raw = os.getenv(key)
        if raw is not None and raw != "":
            overrides[key] = raw
    return overrides
