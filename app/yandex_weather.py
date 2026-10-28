"""Интеграция с API Яндекс Погоды (GraphQL, тариф «Умный дом»).

POST https://api.weather.yandex.ru/graphql/query
Заголовок: X-Yandex-Weather-Key: <ключ>

Бесплатный тариф «Умный дом» даёт: температуру, «ощущается как»,
скорость ветра и общие погодные условия (облачность/осадки).
Горизонт прогноза — 48 часов, лимиты: 30 запросов/сутки, 1000/месяц.
Ответ кэшируется в БД и обновляется не чаще раза в
YANDEX_WEATHER_REFRESH_MINUTES (по умолчанию 90 минут -> ~16 запросов/сутки),
при ошибках повторы приостанавливаются на YANDEX_WEATHER_ERROR_RETRY_MINUTES.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from . import settings as app_settings

API_URL = "https://api.weather.yandex.ru/graphql/query"

# Запрос в рамках бесплатного тарифа: температура, ощущается как, ветер, условие, иконка
FORECAST_QUERY = """
{
  weatherByPoint(request: { lat: %s, lon: %s }) {
    now {
      temperature
      feelsLike
      windSpeed
      windDirection
      condition
      icon(format: SVG, theme: DARK)
    }
    forecast {
      days(limit: 3) {
        hours {
          time
          temperature
          feelsLike
          windSpeed
          windDirection
          condition
          icon(format: SVG, theme: DARK)
        }
      }
    }
  }
}
"""

# Запрос без иконки — используется, если тариф не даёт поле icon
FORECAST_QUERY_NO_ICON = """
{
  weatherByPoint(request: { lat: %s, lon: %s }) {
    now {
      temperature
      feelsLike
      windSpeed
      windDirection
      condition
    }
    forecast {
      days(limit: 3) {
        hours {
          time
          temperature
          feelsLike
          windSpeed
          windDirection
          condition
        }
      }
    }
  }
}
"""

# Прогноз агрегируем по частям суток: границы в часах локального времени
PART_BUCKETS = (
    ("night", "ночью", 0, 5),
    ("morning", "утром", 6, 11),
    ("day", "днём", 12, 17),
    ("evening", "вечером", 18, 23),
)
# Хронологический порядок частей суток (для сортировки прогноза)
PART_ORDER = {"night": 0, "morning": 1, "day": 2, "evening": 3}
# Максимум частей суток в блоке прогноза (ближайшая отбрасывается)
MAX_PARTS = 6

CONDITION_RU: dict[str, str] = {
    "CLEAR": "ясно",
    "PARTLY_CLOUDY": "малооблачно",
    "CLOUDY": "облачно с прояснениями",
    "OVERCAST": "пасмурно",
    "LIGHT_RAIN": "небольшой дождь",
    "RAIN": "дождь",
    "HEAVY_RAIN": "сильный дождь",
    "SHOWERS": "ливень",
    "WET_SNOW": "дождь со снегом",
    "LIGHT_SNOW": "небольшой снег",
    "SNOW": "снег",
    "SNOW_SHOWERS": "снегопад",
    "HAIL": "град",
    "THUNDERSTORM": "гроза",
    "THUNDERSTORM_WITH_RAIN": "дождь с грозой",
    "THUNDERSTORM_WITH_HAIL": "гроза с градом",
    "FOG": "туман",
    "MIST": "дымка",
}

WIND_DIR_RU: dict[str, str] = {
    "NORTH_WEST": "СЗ", "NORTH": "С", "NORTH_EAST": "СВ", "EAST": "В",
    "SOUTH_EAST": "ЮВ", "SOUTH": "Ю", "SOUTH_WEST": "ЮЗ", "WEST": "З",
    "CALM": "штиль",
}


def _condition(code: str | None) -> str:
    if not code:
        return "—"
    # GraphQL отдаёт enum ("OVERCAST"), старый API — kebab-case ("overcast")
    normalized = code.upper().replace("-", "_")
    return CONDITION_RU.get(normalized, code)


def _wind_dir(code: str | None) -> str:
    if not code:
        return "—"
    normalized = code.upper().replace("-", "_")
    return WIND_DIR_RU.get(normalized, code)


def _to_local(dt: datetime) -> datetime:
    from .db import to_local_timestamp
    return to_local_timestamp(dt.astimezone(UTC).isoformat())


def _parse_response(raw: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any]:
    """Компактная структура для UI из ответа GraphQL API."""
    point = raw.get("data", {}).get("weatherByPoint") or {}
    now = point.get("now") or {}

    fact = {
        "temp": now.get("temperature"),
        "feels_like": now.get("feelsLike"),
        "condition": _condition(now.get("condition")),
        "icon_url": now.get("icon"),
        "wind_speed": now.get("windSpeed"),
        "wind_dir": _wind_dir(now.get("windDirection")),
    }

    # Почасовой прогноз -> агрегация по частям суток (сегодня/завтра/п/завтра)
    hours: list[dict[str, Any]] = []
    for day in (point.get("forecast") or {}).get("days") or []:
        hours.extend(day.get("hours") or [])

    now_local = datetime.now(UTC)
    today_local_date = _to_local(now_local).date()
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for hour in hours:
        try:
            t = datetime.fromisoformat(hour["time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if t <= now_local:
            continue  # только будущее
        if t > now_local + timedelta(hours=48):
            continue
        t_local = _to_local(t)
        day_offset = (t_local.date() - today_local_date).days
        if day_offset == 0:
            date_label = "сегодня"
        elif day_offset == 1:
            date_label = "завтра"
        elif day_offset == 2:
            date_label = "п/завтра"  # послезавтра — сокращённо, чтобы уместиться в колонку
        else:
            continue
        hour_num = t_local.hour
        for name, label, lo, hi in PART_BUCKETS:
            if lo <= hour_num <= hi:
                key = (t_local.date().isoformat() + " " + name, f"{date_label} {label}")
                break
        else:
            continue
        bucket = buckets.setdefault(
            key,
            {"part_key": key[0], "part_name": name, "part_label": key[1],
             "temps": [], "conditions": [], "icons": [], "wind": 0.0},
        )
        if hour.get("temperature") is not None:
            bucket["temps"].append(float(hour["temperature"]))
        if hour.get("condition"):
            bucket["conditions"].append(hour["condition"])
        if hour.get("icon"):
            bucket["icons"].append(hour["icon"])
        try:
            bucket["wind"] = max(bucket["wind"], float(hour.get("windSpeed") or 0))
        except (TypeError, ValueError):
            pass

    parts = []
    # Хронологический порядок: по дате, затем ночь -> утро -> день -> вечер
    for (_, _), bucket in sorted(
        buckets.items(),
        key=lambda kv: (kv[0][0].rsplit(" ", 1)[0], PART_ORDER.get(kv[0][0].rsplit(" ", 1)[1], 0)),
    ):
        dominant_condition = (
            Counter(bucket["conditions"]).most_common(1)[0][0]
            if bucket["conditions"] else None
        )
        dominant_icon = (
            Counter(bucket["icons"]).most_common(1)[0][0]
            if bucket["icons"] else None
        )
        parts.append({
            "part_name": bucket["part_name"],
            "part_label": bucket["part_label"],
            "temp_min": int(min(bucket["temps"])) if bucket["temps"] else None,
            "temp_max": int(max(bucket["temps"])) if bucket["temps"] else None,
            "condition": _condition(dominant_condition),
            "icon_url": dominant_icon,
            "wind_speed": round(bucket["wind"], 1) if bucket["wind"] else None,
        })

    # Максимум 6 частей: ближайшую по времени убираем — для ближнего горизонта
    # есть прогноз Замбретти, а строка должна оставаться в одну колонку сетки.
    if len(parts) > MAX_PARTS:
        parts = parts[-MAX_PARTS:]

    updated_local = None
    if fetched_at is not None:
        try:
            updated_local = _to_local(fetched_at).strftime("%d.%m %H:%M")
        except Exception:
            updated_local = None

    return {
        "available": True,
        "updated_utc": fetched_at.isoformat() if fetched_at else None,
        "updated_local": updated_local,
        "fact": fact,
        "forecast": {"parts": parts},
    }


def _post_query(api_key: str, query: str) -> dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query}).encode("utf-8"),
        method="POST",
        headers={
            "X-Yandex-Weather-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        # GraphQL-ошибки (недоступные поля тарифа и т.п.) приходят с HTTP 200
        messages = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise ValueError(f"GraphQL: {messages}")
    return payload


def _fetch(api_key: str, lat: str, lon: str) -> dict[str, Any]:
    try:
        return _post_query(api_key, FORECAST_QUERY % (lat, lon))
    except ValueError as err:
        if "icon" in str(err).lower():
            # Поле icon недоступно тарифу — повторяем без иконок
            logger.warning("Yandex.Weather: поле icon недоступно, запрос без иконок: {}", err)
            return _post_query(api_key, FORECAST_QUERY_NO_ICON % (lat, lon))
        raise


def _describe_fetch_error(err: Exception) -> str:
    """Человекочитаемое описание ошибки запроса к API."""
    if isinstance(err, urllib.error.HTTPError):
        detail = ""
        try:
            body = err.read(512).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
                detail = str(parsed.get("message") or parsed.get("detail") or body)
            except (json.JSONDecodeError, AttributeError):
                detail = body
        except Exception:
            detail = err.reason or ""
        return f"HTTP {err.code}" + (f": {detail}" if detail else "")
    return str(err)


CACHE_VERSION = 2


def _load_cache(conn: sqlite3.Connection | None) -> tuple[dict[str, Any] | None, datetime | None]:
    """Сырой ответ API из кэша и время его получения.

    Кэш хранит именно сырой ответ (не разобранный): при обновлении логики
    разбора старые данные переоформляются новым кодом автоматически.
    """
    raw = (app_settings.get_string("YANDEX_WEATHER_CACHE", conn=conn) or "").strip()
    updated_raw = (app_settings.get_string("YANDEX_WEATHER_UPDATED_AT", conn=conn) or "").strip()
    updated = None
    if updated_raw:
        try:
            updated = datetime.fromisoformat(updated_raw)
        except ValueError:
            updated = None
    if not raw:
        return None, updated
    try:
        wrapper = json.loads(raw)
    except json.JSONDecodeError:
        return None, updated
    if not isinstance(wrapper, dict) or wrapper.get("version") != CACHE_VERSION:
        return None, updated  # кэш старого формата — игнорируем
    return wrapper.get("raw"), updated


def _save_cache(raw: dict[str, Any], conn: sqlite3.Connection | None) -> None:
    wrapper = {"version": CACHE_VERSION, "raw": raw}
    app_settings.set_value("YANDEX_WEATHER_CACHE", json.dumps(wrapper, ensure_ascii=False), conn=conn)
    app_settings.set_value("YANDEX_WEATHER_UPDATED_AT", datetime.now(UTC).isoformat(), conn=conn)
    # Успех — сбрасываем маркеры ошибок
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR", "", conn=conn)
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR_AT", "", conn=conn)


def _load_error_state(conn: sqlite3.Connection | None) -> tuple[str | None, datetime | None]:
    """Последняя ошибка запроса и её время (для паузы между повторами)."""
    message = (app_settings.get_string("YANDEX_WEATHER_LAST_ERROR", conn=conn) or "").strip()
    at_raw = (app_settings.get_string("YANDEX_WEATHER_LAST_ERROR_AT", conn=conn) or "").strip()
    if not message or not at_raw:
        return None, None
    try:
        return message, datetime.fromisoformat(at_raw)
    except ValueError:
        return message, None


def _save_error_state(message: str, conn: sqlite3.Connection | None) -> None:
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR", message, conn=conn)
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR_AT", datetime.now(UTC).isoformat(), conn=conn)


def get_weather(
    conn: sqlite3.Connection | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Погода Яндекс с кэшированием.

    Контракт возвращаемого значения:
    - None — не настроено (нет ключа или координат): блок скрывается целиком;
    - {"available": True, ...} — данные есть (из кэша или свежий запрос);
    - {"available": False, "detail": "..."} — настроено, но данных пока нет.

    force=True — принудительный запрос мимо интервала обновления и паузы
    ошибок (ручное обновление по кнопке; расходует лимит API).

    Ошибки сети не бросает.
    """
    api_key = (app_settings.get_string("YANDEX_WEATHER_API_KEY", conn=conn) or "").strip()
    lat = (app_settings.get_string("STATION_LATITUDE", conn=conn) or "").strip().replace(",", ".")
    lon = (app_settings.get_string("STATION_LONGITUDE", conn=conn) or "").strip().replace(",", ".")
    refresh_minutes = max(15, app_settings.get_int("YANDEX_WEATHER_REFRESH_MINUTES", conn=conn) or 90)

    if not api_key or not lat or not lon:
        return None

    cached_raw, updated = _load_cache(conn)
    if not force and cached_raw is not None and updated is not None:
        age_minutes = (datetime.now(UTC) - updated).total_seconds() / 60.0
        if age_minutes < refresh_minutes:
            result = _parse_response(cached_raw, updated)
            result["stale"] = False
            return result

    # Пауза между повторами после ошибки, чтобы не сжигать лимиты API
    # (принудительный запрос её игнорирует)
    if not force:
        error_retry_minutes = max(5, app_settings.get_int("YANDEX_WEATHER_ERROR_RETRY_MINUTES", conn=conn) or 30)
        last_error, last_error_at = _load_error_state(conn)
        if last_error and last_error_at is not None:
            since_error = (datetime.now(UTC) - last_error_at).total_seconds() / 60.0
            if since_error < error_retry_minutes:
                retry_in = int(error_retry_minutes - since_error)
                detail = f"{last_error} (повтор через {retry_in} мин)"
                if cached_raw is not None:
                    result = _parse_response(cached_raw, updated)
                    result["stale"] = True
                    return result
                return {"available": False, "detail": detail}

    fetch_error: str | None = None
    try:
        fetched_at = datetime.now(UTC)
        raw = _fetch(api_key, lat, lon)
        result = _parse_response(raw, fetched_at)
        if not result["forecast"]["parts"] and result["fact"]["temp"] is None:
            raise ValueError("пустой ответ API")
        _save_cache(raw, conn=conn)
        result["stale"] = False
        return result
    except Exception as err:
        fetch_error = _describe_fetch_error(err)
        logger.warning("Yandex.Weather fetch failed: {}", fetch_error)
        _save_error_state(fetch_error, conn=conn)

    # Не удалось обновить — отдаём устаревший кэш, если есть
    if cached_raw is not None:
        result = _parse_response(cached_raw, updated)
        result["stale"] = True
        result["stale_reason"] = fetch_error
        return result
    return {"available": False, "detail": fetch_error}
