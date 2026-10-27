"""Прогноз погоды по методу Замбретти (Negretti & Zambra, 1915).

Классический алгоритм локального прогноза по барометру:
- давление приводится к уровню моря (барометрическая формула),
- определяется тренд давления за 3 часа (>= 1.6 гПа — рост/падение),
- по формуле тренда вычисляется код Z и выбирается текст прогноза,
- направление ветра даёт небольшую поправку (юг +2, запад/восток +1, север +0).

Горизонт прогноза — 12–24 часа. Источники:
- https://github.com/sascommunities/iot-zambretti-weather-forcasting
- https://github.com/G6EJD/ESP32_Weather_Forecaster_TN061
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from loguru import logger

from . import settings as app_settings

# Порог смены тренда, гПа за 3 часа (1.6 мбар по классике)
TREND_THRESHOLD_HPA = 1.6
# Минимальный охват истории давления для устойчивого тренда, часов
MIN_SPAN_HOURS = 2.0
# Окно истории давления, часов
HISTORY_WINDOW_HOURS = 4.0

FORECAST_UNAVAILABLE_TEXT = (
    "Прогноз пока недоступен — не хватает данных: "
    "недостаточно данных для устойчивого прогноза."
)

# Тексты прогнозов по коду Z (1..32), перевод с оригинала Negretti & Zambra.
FORECAST_TEXTS: dict[int, str] = {
    # Падает (1-9)
    1: "Ясно, устойчиво",
    2: "Хорошая погода",
    3: "Ясно, становится менее устойчивой",
    4: "Довольно хорошо, позже возможны ливни",
    5: "Ливни, становится неустойчивее",
    6: "Неустойчиво, позже дождь",
    7: "Временами дождь, дальше хуже",
    8: "Временами дождь, становится очень неустойчиво",
    9: "Очень неустойчиво, дождь",
    # Стабильно (10-19)
    10: "Ясно, устойчиво",
    11: "Хорошая погода",
    12: "Ясно, возможны ливни",
    13: "Довольно хорошо, вероятны ливни",
    14: "Ливнево, с прояснениями",
    15: "Переменчиво, местами дождь",
    16: "Неустойчиво, временами дождь",
    17: "Дождь с частыми интервалами",
    18: "Очень неустойчиво, дождь",
    19: "Буря, сильный дождь",
    # Растёт (20-32)
    20: "Ясно, устойчиво",
    21: "Хорошая погода",
    22: "Становится лучше",
    23: "Довольно хорошо, улучшение",
    24: "Довольно хорошо, утром возможны ливни",
    25: "Утром ливни, затем улучшение",
    26: "Переменчиво, улучшение",
    27: "Местами неустойчиво, позже прояснение",
    28: "Неустойчиво, вероятно улучшение",
    29: "Неустойчиво, короткие прояснения",
    30: "Очень неустойчиво, временами лучше",
    31: "Буря, возможно улучшение",
    32: "Буря, сильный дождь",
}

TREND_LABELS = {
    "falling": "падает",
    "rising": "растёт",
    "steady": "стабильно",
}


def mmhg_to_hpa(value_mmhg: float) -> float:
    """мм рт.ст. -> гПа (мбар)."""
    return value_mmhg * 1.33322387415


def sea_level_pressure(pressure_hpa: float, temp_c: float, elevation_m: float) -> float:
    """Приведение давления к уровню моря (барометрическая формула)."""
    if elevation_m <= 0:
        return pressure_hpa
    denom = temp_c + 0.0065 * elevation_m + 273.15
    if denom <= 0:
        return pressure_hpa
    return pressure_hpa * (1 - (0.0065 * elevation_m) / denom) ** -5.257


def wind_adjustment(wind_dir_deg: float | None) -> int:
    """Поправка на ветер: юг +2, запад/восток +1, север +0."""
    if wind_dir_deg is None:
        return 0
    d = wind_dir_deg % 360
    if 135 <= d <= 225:  # юг
        return 2
    if 315 <= d or d <= 45:  # север
        return 0
    return 1  # запад/восток


def zambretti_code(p0_hpa: float, trend: str, wind_dir_deg: float | None) -> int:
    """Код прогноза Z по тренду и давлению на уровне моря."""
    wind = wind_adjustment(wind_dir_deg)
    if trend == "falling":
        z = math.floor(127 - 0.12 * p0_hpa) + wind
        return max(1, min(9, z))
    if trend == "rising":
        z = math.floor(185 - 0.16 * p0_hpa) + wind
        return max(20, min(32, z))
    z = math.floor(144 - 0.13 * p0_hpa) + wind
    return max(10, min(19, z))


def classify_trend(delta3h_hpa: float) -> str:
    if delta3h_hpa <= -TREND_THRESHOLD_HPA:
        return "falling"
    if delta3h_hpa >= TREND_THRESHOLD_HPA:
        return "rising"
    return "steady"


def compute_forecast(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Собрать прогноз по данным станции.

    Возвращает dict с текстом прогноза и деталями либо None, если данных
    не хватает для устойчивого прогноза.
    """
    from .db import get_zambretti_inputs

    try:
        inputs = get_zambretti_inputs(history_hours=HISTORY_WINDOW_HOURS, conn=conn)
    except Exception:
        logger.exception("Zambretti: не удалось получить данные из БД")
        return None

    elevation = (
        (app_settings.get_int("STATION_GROUND_ELEVATION_M", conn=conn) or 0)
        + (app_settings.get_int("STATION_MOUNT_HEIGHT_M", conn=conn) or 0)
    )

    readings = inputs.get("pressure_readings") or []
    temp_c = inputs.get("temp_c")
    wind_dir = inputs.get("wind_dir_deg")

    if len(readings) < 2:
        return None

    (t_past, p_past), (t_now, p_now) = readings[0], readings[-1]
    span_hours = (t_now - t_past).total_seconds() / 3600.0
    if span_hours < MIN_SPAN_HOURS:
        return None

    # Приведение обоих отсчётов к уровню моря (по текущей температуре)
    if elevation > 0:
        if temp_c is None:
            return None
        p0_past = sea_level_pressure(p_past, temp_c, elevation)
        p0_now = sea_level_pressure(p_now, temp_c, elevation)
    else:
        p0_past, p0_now = p_past, p_now

    # Изменение за 3 часа (масштабируем фактический интервал)
    delta3h = (p0_now - p0_past) / span_hours * 3.0
    trend = classify_trend(delta3h)
    z = zambretti_code(p0_now, trend, wind_dir)

    return {
        "available": True,
        "text": FORECAST_TEXTS[z],
        "z": z,
        "trend": trend,
        "trend_label": TREND_LABELS[trend],
        "delta3h_hpa": round(delta3h, 1),
        "pressure_sea_level_hpa": round(p0_now, 1),
        "wind_dir_deg": wind_dir,
        "elevation_m": elevation,
        "span_hours": round(span_hours, 1),
        "method": "Замбретти",
    }


def format_telegram_forecast(forecast: dict[str, Any] | None) -> str:
    """Строка прогноза для Telegram-бота."""
    if not forecast:
        return "🔮 " + FORECAST_UNAVAILABLE_TEXT
    lines = [f"🔮 Прогноз (Замбретти): {forecast['text']}"]
    details = f"Давление {forecast['pressure_sea_level_hpa']:.0f} гПа, за 3 ч {forecast['delta3h_hpa']:+.1f} гПа ({forecast['trend_label']})"
    if forecast.get("wind_dir_deg") is not None:
        details += f", ветер {forecast['wind_dir_deg']:.0f}°"
    lines.append(details)
    return "\n".join(lines)
