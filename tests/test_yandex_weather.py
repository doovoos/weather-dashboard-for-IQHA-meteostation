"""Tests for app.yandex_weather module."""
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


SAMPLE = {
    "now": 1470220206,
    "now_dt": "2026-10-27T10:30:06Z",
    "info": {"lat": 55.75, "lon": 37.62, "url": "https://yandex.ru/pogoda/moscow"},
    "fact": {
        "temp": 5, "feels_like": 3, "condition": "overcast",
        "wind_speed": 3.2, "wind_dir": "nw", "pressure_mm": 748,
        "pressure_pa": 997, "humidity": 66, "daytime": "d",
    },
    "forecast": {
        "date": "2026-10-27",
        "parts": [
            {"part_name": "day", "temp_min": 5, "temp_max": 7, "temp_avg": 6,
             "condition": "light-rain", "wind_speed": 3.5, "prec_mm": 0.5, "prec_prob": 70},
            {"part_name": "evening", "temp_min": 2, "temp_max": 5, "temp_avg": 3,
             "condition": "cloudy", "wind_speed": 2.0, "prec_mm": 0, "prec_prob": 10},
        ],
    },
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

    # 2. Настроено, но без кэша и API недоступен -> "ещё загружается" с причиной
    original_fetch = yandex_weather._fetch
    yandex_weather._fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("network down"))
    result = yandex_weather.get_weather(conn=conn)
    assert result is not None, "настроенный блок должен существовать"
    assert result["available"] is False
    assert "network down" in result["detail"]

    # 2a. Повтор приостановлен: сразу после ошибки новый запрос не делается
    call_count = {"n": 0}
    def counting_fetch(*a, **k):
        call_count["n"] += 1
        raise OSError("should not be called")
    yandex_weather._fetch = counting_fetch
    result = yandex_weather.get_weather(conn=conn)
    assert call_count["n"] == 0, "повтор не должен выполняться в паузе"
    assert result["available"] is False
    assert "network down" in result["detail"] and "повтор через" in result["detail"]

    # 2b. После истечения паузы запрос выполняется снова
    app_settings.set_value(
        "YANDEX_WEATHER_LAST_ERROR_AT",
        (datetime.now(UTC) - timedelta(minutes=60)).isoformat(),
        conn=conn,
    )
    yandex_weather._fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("still down"))
    result = yandex_weather.get_weather(conn=conn)
    assert result["available"] is False and "still down" in result["detail"]

    # 3. Кэш свежий -> отдаётся без запроса к API (маркеры ошибки сброшены ранее нет — чистим)
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR", "", conn=conn)
    app_settings.set_value("YANDEX_WEATHER_LAST_ERROR_AT", "", conn=conn)
    yandex_weather._save_cache(SAMPLE, conn=conn)
    result = yandex_weather.get_weather(conn=conn)
    assert result is not None and result["available"]
    assert result["stale"] is False
    assert result["fact"]["condition"] == "пасмурно"
    assert result["fact"]["wind_dir"] == "СЗ"
    assert result["forecast"]["date"] == "2026-10-27"
    assert result["forecast"]["parts"][0]["part_label"] == "днём"
    assert result["forecast"]["parts"][0]["condition"] == "небольшой дождь"
    assert result["forecast"]["parts"][1]["part_label"] == "вечером"
    assert result["updated_local"] is not None

    # 4. Кэш устарел, API недоступен -> отдаём кэш с флагом stale
    app_settings.set_value(
        "YANDEX_WEATHER_UPDATED_AT",
        (datetime.now(UTC) - timedelta(minutes=200)).isoformat(),
        conn=conn,
    )
    result = yandex_weather.get_weather(conn=conn)
    assert result is not None and result["stale"] is True

    # 5. Кэш устарел, API работает -> обновление (после истечения паузы ошибок)
    app_settings.set_value(
        "YANDEX_WEATHER_LAST_ERROR_AT",
        (datetime.now(UTC) - timedelta(minutes=60)).isoformat(),
        conn=conn,
    )
    yandex_weather._fetch = lambda *a, **k: SAMPLE
    result = yandex_weather.get_weather(conn=conn)
    assert result is not None and result["stale"] is False

    # 6. HTTP-ошибка API описывается читаемо
    import urllib.error
    err = urllib.error.HTTPError(url="", code=403, msg="Forbidden",
                                 hdrs=None, fp=None)
    assert "403" in yandex_weather._describe_fetch_error(err)

    # 7. Русские переводы покрывают все коды условия
    for code in ["clear", "partly-cloudy", "cloudy", "overcast", "light-rain", "rain",
                 "heavy-rain", "showers", "wet-snow", "light-snow", "snow", "snow-showers",
                 "hail", "thunderstorm", "thunderstorm-with-rain", "thunderstorm-with-hail"]:
        assert code in yandex_weather.CONDITION_RU

    yandex_weather._fetch = original_fetch
    conn.close()
    print("test_yandex_weather: OK")


if __name__ == "__main__":
    main()
