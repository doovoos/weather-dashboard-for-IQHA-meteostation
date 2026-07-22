"""Слой настроек: значения хранятся в таблице app_settings и редактируются
из админ-панели. Read-path использует in-memory кэш с инвалидацией при
записи.

Схема настроек определена статически в SETTINGS_SCHEMA — это позволяет
рендерить admin-форму, валидировать типы и хранить дефолты.

Дефолты при первом запуске берутся из env (см. config._defaults_from_env)
через функцию seed_defaults_if_empty(). Это обеспечивает беспроблемный
переход существующих развёртываний на новую систему.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable

from .db import use_connection


# ---------- Schema ----------

@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    type: str  # "string" | "int" | "bool" | "csv_int" | "csv_time"
    default: str
    section: str
    help: str = ""


SETTINGS_SCHEMA: tuple[SettingDef, ...] = (
    # ----- Общие -----
    SettingDef("APP_TITLE", "Внутреннее название", "string", "Weather Station", "Общие",
               "Title в <head>, заголовок сервиса."),
    SettingDef("SITE_BRAND", "Бренд в шапке", "string", "Weather Dashboard", "Общие",
               "Текст слева в верхней панели."),
    SettingDef("WEATHER_PLACE_NAME", "Имя локации", "string", "Локальная станция", "Общие",
               "Используется в сообщениях бота и в заголовках."),
    SettingDef("WEATHER_SITE_URL", "URL сайта", "string", "", "Общие",
               "Полный URL сайта — используется в Telegram-кнопках."),
    SettingDef("YANDEX_METRIKA_ID", "ID Яндекс.Метрики", "string", "", "Общие",
               "Только цифры. Пусто — Метрика отключена."),
    SettingDef("WEATHER_TIMEZONE", "Часовой пояс", "string", "UTC", "Общие",
               "Часовой пояс для отображения времени: UTC, Asia/Omsk, Europe/Moscow и т.д."),
    SettingDef("TIME_FORMAT", "Формат времени", "string", "24h", "Общие",
               "24h — 24-часовой формат (14:30), 12h — 12-часовой (2:30 PM)."),

    # ----- Telegram -----
    SettingDef("TELEGRAM_ADMIN_IDS", "ID админов в Telegram", "csv_int", "", "Telegram",
               "Список через запятую. Получают алерты о проблемах со станцией."),
    SettingDef("TELEGRAM_DAILY_USER_IDS", "ID пользователей для рассылки", "csv_int", "", "Telegram",
               "Кому отправляется ежедневная сводка."),
    SettingDef("TELEGRAM_DAILY_TIMES", "Времена ежедневной рассылки", "csv_time", "07:00,20:00", "Telegram",
               "Локальные времена через запятую: HH:MM,HH:MM."),
    SettingDef("TELEGRAM_STALE_MINUTES", "Порог 'нет данных' (мин)", "int", "5", "Telegram",
               "Через сколько минут без данных бот шлёт алерт админам."),
    SettingDef("TELEGRAM_MONITOR_INTERVAL_SECONDS", "Интервал проверки stale (сек)", "int", "60", "Telegram",
               "Как часто бот проверяет, что данные свежие."),
    SettingDef("TELEGRAM_DYNAMIC_NAME_ENABLED", "Динамическое имя бота", "bool", "1", "Telegram",
               "Если включено — бот пишет температуру в свой display name."),
    SettingDef("TELEGRAM_DYNAMIC_NAME_PREFIX", "Префикс имени бота", "string", "", "Telegram",
               "Что писать перед температурой. Пусто — берём 'Погода: <место>'."),
    SettingDef("TELEGRAM_DYNAMIC_NAME_INTERVAL_MINUTES", "Интервал обновления имени (мин)", "int", "10", "Telegram",
               "Telegram ограничивает setMyName ~1 раз в минуту, безопасный минимум — 10."),

    # ----- Стандарт ingestion -----
    SettingDef("AUTO_REGISTER_STATIONS", "Автоматически регистрировать новые станции", "bool", "0", "Станции",
               "Если включено — неизвестный mac создаёт запись (disabled). "
               "Иначе ingest от незарегистрированной станции отклоняется."),

    # ----- Логирование -----
    SettingDef("LOGGING_ENABLED", "Включить логирование в файл", "bool", "1", "Логирование",
               "Если выключено — логи пишутся только в консоль (stderr)."),
    SettingDef("LOGGING_LEVEL", "Уровень логирования", "string", "INFO", "Логирование",
               "DEBUG, INFO, WARNING, ERROR, CRITICAL. Чем выше уровень, тем меньше сообщений."),
    SettingDef("LOGGING_MAX_SIZE_MB", "Максимальный размер файла (МБ)", "int", "10", "Логирование",
               "При достижении этого размера создаётся новый файл. Старые файлы удаляются согласно LOGGING_BACKUP_COUNT."),
    SettingDef("LOGGING_BACKUP_COUNT", "Количество файлов в архиве", "int", "5", "Логирование",
               "Сколько rotated-файлов хранить (например, 5 файлов по 10 МБ = максимум 50 МБ логов)."),

    # ----- Агрегация -----
    SettingDef("AGGREGATION_MAX_AGE_DAYS", "Порог агрегации (дни)", "int", "45", "Агрегация",
               "Данные старше этого порога (в днях) будут агрегированы из 5-минутных в часовые средние."),
    SettingDef("AGGREGATION_INTERVAL_DAYS", "Интервал агрегации (дни)", "int", "10", "Агрегация",
               "Как часто запускать автоматическую агрегацию старых данных."),
    SettingDef("AGGREGATION_LAST_RUN_AT", "", "string", "", "Агрегация",
               ""),
)


SETTINGS_BY_KEY: dict[str, SettingDef] = {s.key: s for s in SETTINGS_SCHEMA}


def sections() -> list[str]:
    """Список секций в порядке появления в схеме (без дубликатов)."""
    seen: list[str] = []
    for s in SETTINGS_SCHEMA:
        if s.section not in seen:
            seen.append(s.section)
    return seen


# ---------- Cache ----------

_cache: dict[str, str] | None = None
_lock = threading.Lock()


def _invalidate_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def _load_cache(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def _ensure_cache(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
    with use_connection(conn) as connection:
        loaded = _load_cache(connection)
    with _lock:
        _cache = loaded
        return _cache


# ---------- Public read API ----------

def get_raw(key: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Сырое строковое значение из БД (без cast). None если ключа нет."""
    cache = _ensure_cache(conn=conn)
    return cache.get(key)


def get_string(key: str, conn: sqlite3.Connection | None = None) -> str:
    raw = get_raw(key, conn=conn)
    if raw is None:
        spec = SETTINGS_BY_KEY.get(key)
        return spec.default if spec else ""
    return raw


def get_int(key: str, conn: sqlite3.Connection | None = None) -> int:
    raw = get_string(key, conn=conn)
    try:
        return int(raw)
    except (TypeError, ValueError):
        spec = SETTINGS_BY_KEY.get(key)
        if spec:
            try:
                return int(spec.default)
            except (TypeError, ValueError):
                return 0
        return 0


def get_bool(key: str, conn: sqlite3.Connection | None = None) -> bool:
    raw = (get_string(key, conn=conn) or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "y", "да"}


def get_csv_int(key: str, conn: sqlite3.Connection | None = None) -> list[int]:
    raw = get_string(key, conn=conn)
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out


def get_csv_time(key: str, conn: sqlite3.Connection | None = None) -> list[str]:
    raw = get_string(key, conn=conn)
    out: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2:
            continue
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append(f"{h:02d}:{m:02d}")
    return out


def all_values(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """Все значения по схеме (с дефолтами)."""
    cache = _ensure_cache(conn=conn)
    return {s.key: cache.get(s.key, s.default) for s in SETTINGS_SCHEMA}


# ---------- Public write API ----------

def set_value(key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    """UPSERT значения. Сбрасывает кэш."""
    if key not in SETTINGS_BY_KEY:
        raise KeyError(f"Unknown setting: {key}")
    now = datetime.now(UTC).isoformat()
    with use_connection(conn) as connection:
        connection.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
    _invalidate_cache()
    # Refresh timezone if it changed
    if key == "WEATHER_TIMEZONE":
        from .db import refresh_local_tz
        refresh_local_tz()
    # Reconfigure logging if logging settings changed
    if key.startswith("LOGGING_"):
        from .logging_setup import reconfigure_logging
        import sys
        # Determine service name from command line
        service_name = "bot" if "bot.py" in sys.argv[0] else "web"
        reconfigure_logging(service_name)


def set_many(values: dict[str, str], conn: sqlite3.Connection | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    with use_connection(conn) as connection:
        for key, value in values.items():
            if key not in SETTINGS_BY_KEY:
                continue
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
    _invalidate_cache()
    # Refresh timezone if it changed
    if "WEATHER_TIMEZONE" in values:
        from .db import refresh_local_tz
        refresh_local_tz()
    # Reconfigure logging if logging settings changed
    if any(key.startswith("LOGGING_") for key in values):
        from .logging_setup import reconfigure_logging
        import sys
        service_name = "bot" if "bot.py" in sys.argv[0] else "web"
        reconfigure_logging(service_name)


# ---------- Seeding ----------

def seed_defaults_if_empty(
    env_overrides: dict[str, str],
    conn: sqlite3.Connection | None = None,
) -> int:
    """При первом запуске заполняет app_settings: env-значение либо schema-default.

    Идемпотентно: записывает только те ключи, которых ещё нет.
    Возвращает число вставленных строк.
    """
    now = datetime.now(UTC).isoformat()
    inserted = 0
    with use_connection(conn) as connection:
        existing_rows = connection.execute("SELECT key FROM app_settings").fetchall()
        existing = {row["key"] for row in existing_rows}
        for spec in SETTINGS_SCHEMA:
            if spec.key in existing:
                continue
            value = env_overrides.get(spec.key, spec.default)
            connection.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                (spec.key, value, now),
            )
            inserted += 1
    if inserted:
        _invalidate_cache()
    return inserted
