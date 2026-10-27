"""Tests for app.zambretti module."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, save_payload
from app import settings as app_settings
from app.zambretti import (
    FORECAST_TEXTS,
    classify_trend,
    compute_forecast,
    format_telegram_forecast,
    mmhg_to_hpa,
    sea_level_pressure,
    wind_adjustment,
    zambretti_code,
)


def _create_test_db() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = lambda cursor, row: {col[0]: row[i] for i, col in enumerate(cursor.description)}
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(Path(tmp.name))
    return conn


def _ingest(conn, mac, temp_c, press_mmhg, wind_dir, hours_ago):
    payload = {
        "devices": [{
            "mac": mac,
            "sensors": [
                {"id": "T1", "value": temp_c, "unit": "°C"},
                {"id": "PRESS", "value": press_mmhg, "unit": "мм рт.ст."},
                {"id": "1DIR", "value": wind_dir, "unit": "°"},
            ],
        }],
    }
    save_payload(payload, conn=conn)
    # Подменяем время на нужное (hours_ago) у всего последнего батча
    now = datetime.now(UTC) - timedelta(hours=hours_ago)
    ts = now.isoformat()
    conn.execute(
        "UPDATE observations SET observed_at = ? WHERE batch_id = (SELECT MAX(id) FROM ingest_batches)",
        (ts,),
    )
    conn.execute(
        "UPDATE ingest_batches SET received_at = ? WHERE id = (SELECT MAX(id) FROM ingest_batches)",
        (ts,),
    )
    conn.commit()


def main() -> None:
    # --- Чистые функции ---
    assert abs(mmhg_to_hpa(760) - 1013.25) < 0.1, "760 мм рт.ст. ~ 1013 гПа"

    # Приведение к уровню моря: на высоте давление ниже
    assert sea_level_pressure(1000.0, 15.0, 100.0) > 1000.0
    assert sea_level_pressure(1000.0, 15.0, 0.0) == 1000.0

    # Ветер: юг +2, север +0, запад/восток +1
    assert wind_adjustment(180) == 2
    assert wind_adjustment(0) == 0
    assert wind_adjustment(360) == 0
    assert wind_adjustment(270) == 1
    assert wind_adjustment(90) == 1
    assert wind_adjustment(None) == 0

    # Тренды
    assert classify_trend(-2.0) == "falling"
    assert classify_trend(2.0) == "rising"
    assert classify_trend(0.5) == "steady"
    assert classify_trend(-1.6) == "falling"

    # Коды Z в границах таблиц
    assert 1 <= zambretti_code(1015.0, "falling", None) <= 9
    assert 10 <= zambretti_code(1015.0, "steady", None) <= 19
    assert 20 <= zambretti_code(1015.0, "rising", None) <= 32

    # Полный набор текстов
    for z in range(1, 33):
        assert z in FORECAST_TEXTS, f"нет текста для Z={z}"

    # --- Интеграция с БД ---
    conn = _create_test_db()

    # Нет данных -> прогноз недоступен
    assert compute_forecast(conn=conn) is None
    assert "недостаточно данных" in format_telegram_forecast(None)

    app_settings.set_value("STATION_GROUND_ELEVATION_M", "100", conn=conn)
    app_settings.set_value("STATION_MOUNT_HEIGHT_M", "3", conn=conn)

    # Данных меньше 2 часов -> недоступен
    _ingest(conn, "AA:BB:CC:DD:EE:FF", 15.0, 750, 180, hours_ago=0.5)
    assert compute_forecast(conn=conn) is None

    # Полная история: давление росло (> 1.6 гПа за 3ч)
    _ingest(conn, "AA:BB:CC:DD:EE:FF", 15.0, 748, 180, hours_ago=3.0)
    _ingest(conn, "AA:BB:CC:DD:EE:FF", 15.0, 752, 180, hours_ago=0.0)

    forecast = compute_forecast(conn=conn)
    assert forecast is not None, "прогноз должен быть доступен"
    assert forecast["available"] is True
    assert forecast["trend"] == "rising"
    assert forecast["elevation_m"] == 103  # 100 + 3
    assert 20 <= forecast["z"] <= 32
    assert forecast["text"] == FORECAST_TEXTS[forecast["z"]]
    assert forecast["pressure_sea_level_hpa"] > 997  # 748-752 мм -> ~997-1003 гПа, приведённое к 103 м выше

    tg = format_telegram_forecast(forecast)
    assert "🔮" in tg and forecast["text"] in tg and "растёт" in tg

    # Падение давления
    conn2 = _create_test_db()
    _ingest(conn2, "AA:BB:CC:DD:EE:FF", 10.0, 760, 90, hours_ago=3.0)
    _ingest(conn2, "AA:BB:CC:DD:EE:FF", 10.0, 755, 90, hours_ago=0.0)
    f2 = compute_forecast(conn=conn2)
    assert f2 is not None and f2["trend"] == "falling"

    conn.close()
    conn2.close()
    print("test_zambretti: OK")


if __name__ == "__main__":
    main()
