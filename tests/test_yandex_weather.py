"""Tests for app.yandex_weather module (GraphQL API)."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db
from app import settings as app_settings
from app import yandex_weather


def _hours(base: datetime, temps: list[int], condition: str = "OVERCAST") -> list[dict]:
    return [
        {"time": (base + timedelta(hours=i)).isoformat(),
         "temperature": t, "feelsLike": t - 1, "windSpeed": 2.0 + i * 0.1,
         "windDirection": "NORTH_EAST", "condition": condition}
        for i, t in enumerate(temps)
    ]


def _sample() -> dict:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # Ближайшие 6 часов (останутся в прогнозе) + прошедшие 2 часа (отбрасываются)
    day_hours = _hours(now - timedelta(hours=2), [5, 4, 5, 6, 7, 8, 9, 8], "OVERCAST")
    tomorrow_hours = _hours(now + timedelta(hours=6), [3, 2, 1, 0, 1, 2, 3, 4, 5, 6, 7, 8], "LIGHT_RAIN")
    return {
        "data": {
            "weatherByPoint": {
                "now": {"temperature": 6, "feelsLike": 4, "windSpeed": 3.2,
                        "windDirection": "NORTH_EAST", "condition": "OVERCAST"},
                "forecast": {"days": [
                    {"hours": day_hours},
                    {"hours": tomorrow_hours},
                ]},
            }
        }
    }


def _create_test_db() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = lambda cursor, row: {col[0]: row[i] for i, col in enumerate(cursor.description)}
    init_db(Path(tmp.name))
    return conn


def main() -> None:
    conn = _create_test_db()

    # 1. Не настроено -> None
    assert yandex_weather.get_weather(conn=conn) is None

    app_settings.set_value("YANDEX_WEATHER_API_KEY", "test-key", conn=conn)
    app_settings.set_value("STATION_LATITUDE", "55.75", conn=conn)
    app_settings.set_value("STATION_LONGITUDE", "37.62", conn=conn)

    # 2. Парсинг GraphQL-ответа
    fetched_at = datetime.now(UTC)
    result = yandex_weather._parse_response(_sample(), fetched_at)
    assert result["available"] is True
    assert result["fact"]["temp"] == 6
    assert result["fact"]["condition"] == "пасмурно"
    assert result["fact"]["wind_dir"] == "СВ"
    assert result["fact"]["feels_like"] == 4
    parts = result["forecast"]["parts"]
    assert parts, "должны быть части суток"
    labels = [p["part_label"] for p in parts]
    assert all(l.startswith("сегодня") or l.startswith("завтра") for l in labels), labels
    for p in parts:
        assert p["temp_min"] <= p["temp_max"]
        assert p["condition"] in ("пасмурно", "небольшой дождь")
    assert result["updated_local"] is not None

    # 3. Кэш свежий -> без запроса к API
    yandex_weather._save_cache(result, conn=conn)
    got = yandex_weather.get_weather(conn=conn)
    assert got is not None and got["available"] and got["stale"] is False

    # 4. Ошибка + нет кэша -> available=False с причиной (сначала чистим кэш)
    app_settings.set_value("YANDEX_WEATHER_CACHE", "", conn=conn)
    original_fetch = yandex_weather._fetch
    yandex_weather._fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("network down"))
    got = yandex_weather.get_weather(conn=conn)
    assert got is not None and got["available"] is False
    assert "network down" in got["detail"]

    # 4a. Пауза: повтор не выполняется
    calls = {"n": 0}
    def counting_fetch(*a, **k):
        calls["n"] += 1
        raise OSError("nope")
    yandex_weather._fetch = counting_fetch
    got = yandex_weather.get_weather(conn=conn)
    assert calls["n"] == 0 and "повтор через" in got["detail"]

    # 4b. После паузы запрос выполняется, успех сбрасывает маркеры
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR_AT",
                           (datetime.now(UTC) - timedelta(minutes=60)).isoformat(), conn=conn)
    yandex_weather._fetch = lambda *a, **k: _sample()
    got = yandex_weather.get_weather(conn=conn)
    assert got["available"] is True and got["stale"] is False

    # 5. GraphQL-ошибка (HTTP 200, errors в теле) описывается читаемо
    import urllib.error
    err = urllib.error.HTTPError(url="", code=403, msg="Forbidden", hdrs=None, fp=None)
    assert "403" in yandex_weather._describe_fetch_error(err)

    # 6. Устаревший кэш + ошибка API -> кэш с флагом stale
    app_settings.set_value("YANDEX_WEATHER_UPDATED_AT",
                           (datetime.now(UTC) - timedelta(minutes=200)).isoformat(), conn=conn)
    yandex_weather._fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    got = yandex_weather.get_weather(conn=conn)
    assert got["available"] is True and got["stale"] is True

    # 7. Переводы покрывают все условия
    for code in ["CLEAR", "PARTLY_CLOUDY", "CLOUDY", "OVERCAST", "LIGHT_RAIN", "RAIN",
                 "HEAVY_RAIN", "SHOWERS", "WET_SNOW", "LIGHT_SNOW", "SNOW", "SNOW_SHOWERS",
                 "HAIL", "THUNDERSTORM", "THUNDERSTORM_WITH_RAIN", "THUNDERSTORM_WITH_HAIL",
                 "FOG", "MIST"]:
        assert code in yandex_weather.CONDITION_RU

    yandex_weather._fetch = original_fetch
    conn.close()
    print("test_yandex_weather: OK")


if __name__ == "__main__":
    main()
