"""Интеграция с API Яндекс.Погоды (тариф «Погода на вашем сайте»).

GET https://api.weather.yandex.ru/v2/informers?lat=..&lon=..&lang=ru_RU
Заголовок: X-Yandex-Weather-Key: <ключ>

Ограничения бесплатного тарифа: горизонт 48 часов, 30 запросов/сутки,
1000 запросов/месяц. Поэтому ответ кэшируется в БД и обновляется не чаще
раз в YANDEX_WEATHER_REFRESH_MINUTES (по умолчанию 90 минут -> ~16 запросов/сутки).
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from . import settings as app_settings

API_URL = "https://api.weather.yandex.ru/v2/informers"

CONDITION_RU: dict[str, str] = {
    "clear": "ясно",
    "partly-cloudy": "малооблачно",
    "cloudy": "облачно с прояснениями",
    "overcast": "пасмурно",
    "light-rain": "небольшой дождь",
    "rain": "дождь",
    "heavy-rain": "сильный дождь",
    "showers": "ливень",
    "wet-snow": "дождь со снегом",
    "light-snow": "небольшой снег",
    "snow": "снег",
    "snow-showers": "снегопад",
    "hail": "град",
    "thunderstorm": "гроза",
    "thunderstorm-with-rain": "дождь с грозой",
    "thunderstorm-with-hail": "гроза с градом",
}

WIND_DIR_RU: dict[str, str] = {
    "nw": "СЗ", "n": "С", "ne": "СВ", "e": "В",
    "se": "ЮВ", "s": "Ю", "sw": "ЮЗ", "w": "З", "c": "штиль",
}

PART_RU: dict[str, str] = {
    "night": "ночью",
    "morning": "утром",
    "day": "днём",
    "evening": "вечером",
}


def _condition(code: str | None) -> str:
    if not code:
        return "—"
    return CONDITION_RU.get(code, code)


def _wind_dir(code: str | None) -> str:
    if not code:
        return "—"
    return WIND_DIR_RU.get(code, code)


def _parse_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Компактная структура для UI из сырого ответа API."""
    fact = raw.get("fact") or {}
    forecast = raw.get("forecast") or {}
    parts = []
    for part in forecast.get("parts") or []:
        parts.append({
            "part_name": part.get("part_name"),
            "part_label": PART_RU.get(part.get("part_name"), part.get("part_name", "")),
            "temp_min": part.get("temp_min"),
            "temp_max": part.get("temp_max"),
            "feels_like": part.get("feels_like"),
            "condition": _condition(part.get("condition")),
            "wind_speed": part.get("wind_speed"),
            "prec_mm": part.get("prec_mm"),
            "prec_prob": part.get("prec_prob"),
        })
    now_dt = raw.get("now_dt")
    updated = None
    updated_local = None
    if now_dt:
        try:
            updated = datetime.fromisoformat(now_dt.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            updated = None
    if updated is not None:
        try:
            from .db import to_local_timestamp
            updated_local = to_local_timestamp(updated.isoformat()).strftime("%d.%m %H:%M")
        except Exception:
            updated_local = None
    return {
        "available": True,
        "updated_utc": updated.isoformat() if updated else None,
        "updated_local": updated_local,
        "fact": {
            "temp": fact.get("temp"),
            "feels_like": fact.get("feels_like"),
            "condition": _condition(fact.get("condition")),
            "wind_speed": fact.get("wind_speed"),
            "wind_dir": _wind_dir(fact.get("wind_dir")),
            "pressure_mm": fact.get("pressure_mm"),
            "humidity": fact.get("humidity"),
        },
        "forecast": {
            "date": forecast.get("date"),
            "parts": parts,
        },
    }


def _fetch(api_key: str, lat: str, lon: str) -> dict[str, Any]:
    url = f"{API_URL}?lat={lat}&lon={lon}&lang=ru_RU"
    request = urllib.request.Request(url, headers={"X-Yandex-Weather-Key": api_key})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _load_cache(conn: sqlite3.Connection | None) -> tuple[dict[str, Any] | None, datetime | None]:
    raw = (app_settings.get_string("YANDEX_WEATHER_CACHE", conn=conn) or "").strip()
    updated_raw = (app_settings.get_string("YANDEX_WEATHER_UPDATED_AT", conn=conn) or "").strip()
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    updated = None
    if updated_raw:
        try:
            updated = datetime.fromisoformat(updated_raw)
        except ValueError:
            updated = None
    return data, updated


def _save_cache(raw: dict[str, Any], conn: sqlite3.Connection | None) -> None:
    app_settings.set_value("YANDEX_WEATHER_CACHE", json.dumps(raw, ensure_ascii=False), conn=conn)
    app_settings.set_value("YANDEX_WEATHER_UPDATED_AT", datetime.now(UTC).isoformat(), conn=conn)


def get_weather(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Погода Яндекс с кэшированием.

    Возвращает структуру для UI либо None (не настроено / нет данных
    и получить не удалось). Ошибки сети не бросает.
    """
    api_key = (app_settings.get_string("YANDEX_WEATHER_API_KEY", conn=conn) or "").strip()
    lat = (app_settings.get_string("STATION_LATITUDE", conn=conn) or "").strip().replace(",", ".")
    lon = (app_settings.get_string("STATION_LONGITUDE", conn=conn) or "").strip().replace(",", ".")
    refresh_minutes = max(15, app_settings.get_int("YANDEX_WEATHER_REFRESH_MINUTES", conn=conn) or 90)

    if not api_key or not lat or not lon:
        return None

    cached_raw, updated = _load_cache(conn)
    if cached_raw is not None and updated is not None:
        age_minutes = (datetime.now(UTC) - updated).total_seconds() / 60.0
        if age_minutes < refresh_minutes:
            result = _parse_response(cached_raw)
            result["stale"] = False
            return result

    try:
        raw = _fetch(api_key, lat, lon)
        _save_cache(raw, conn=conn)
        result = _parse_response(raw)
        result["stale"] = False
        return result
    except Exception as err:
        logger.warning("Yandex.Weather fetch failed: {}", err)

    # Не удалось обновить — отдаём устаревший кэш, если есть
    if cached_raw is not None:
        result = _parse_response(cached_raw)
        result["stale"] = True
        return result
    return None
