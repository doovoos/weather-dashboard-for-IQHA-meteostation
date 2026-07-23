from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cache import cached, invalidate as cache_invalidate
from .config import DATABASE_PATH, WEATHER_TIMEZONE
from .migrations import run_migrations
from .sensor_map import PRIMARY_SENSOR_IDS, sensor_label, sensor_unit

# Lazy timezone resolution: try to get from DB settings, fallback to env
def _get_local_tz() -> ZoneInfo:
    """Get timezone from DB settings if available, otherwise use env default."""
    try:
        from .settings import get_string
        tz_name = get_string("WEATHER_TIMEZONE")
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass  # DB not ready yet or settings not seeded
    try:
        return ZoneInfo(WEATHER_TIMEZONE)
    except ZoneInfoNotFoundError:
        return UTC

LOCAL_TZ = _get_local_tz()


def refresh_local_tz() -> None:
    """Refresh LOCAL_TZ after settings change."""
    global LOCAL_TZ
    LOCAL_TZ = _get_local_tz()


TEMPERATURE_SENSOR_IDS = ("T1", "T2", "T3", "T4", "T5", "T6")
HUMIDITY_SENSOR_IDS = ("RH", "H1", "H2")
PRESSURE_SENSOR_IDS = ("PRESS", "HPA")


def dict_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = dict_factory
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@contextmanager
def use_connection(existing: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
    """Использует переданный connection или открывает временный.

    Если connection передан — caller отвечает за commit/close, мы только yield-им.
    Если нет — открываем свой через get_connection() (с авто-commit/close).
    """
    if existing is not None:
        yield existing
        return
    with get_connection() as connection:
        yield connection


def db_dependency() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: один connection на запрос.

    `check_same_thread=False` нужен, потому что sync-зависимость и
    async-эндпоинт (`/api/ingest`) могут оказаться в разных потоках
    (FastAPI запускает sync dep в threadpool, а async handler — в event
    loop). Конкурентного доступа к connection при этом нет: один conn
    живёт ровно один HTTP-запрос.
    """
    connection = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    connection.row_factory = dict_factory
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(db_path: Path | None = None) -> None:
    target = db_path or DATABASE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        run_migrations(connection)
    finally:
        connection.close()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_local_timestamp(value: str) -> datetime:
    return parse_timestamp(value).astimezone(LOCAL_TZ)


def _local_day_bounds(target_day: date) -> tuple[str, str]:
    start_local = datetime.combine(target_day, datetime.min.time(), tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC).isoformat(), end_local.astimezone(UTC).isoformat()


def format_relative_age(value: str, now: datetime | None = None) -> str:
    try:
        observed_at = parse_timestamp(value)
    except ValueError:
        return value

    current = now or datetime.now(UTC)
    seconds = max(0, int((current - observed_at).total_seconds()))
    if seconds < 60:
        return "только что"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"

    days = hours // 24
    return f"{days} дн назад"


def freshness_emoji(value: str, now: datetime | None = None) -> str:
    try:
        observed_at = parse_timestamp(value)
    except ValueError:
        return "⚪"
    current = now or datetime.now(UTC)
    seconds = max(0, int((current - observed_at).total_seconds()))
    if seconds <= 10 * 60:
        return "🟢"
    if seconds <= 60 * 60:
        return "🟡"
    return "🔴"


def save_payload(
    payload: dict[str, Any],
    received_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    timestamp = (received_at or datetime.now(UTC)).replace(microsecond=0)
    devices = payload.get("devices") or []
    inserted_batches = 0
    inserted_rows = 0

    with use_connection(conn) as connection:
        for device in devices:
            mac = str(device.get("mac") or "unknown")
            sensors = device.get("sensors") or []
            batch_cursor = connection.execute(
                """
                INSERT INTO ingest_batches(device_mac, received_at, payload_json)
                VALUES (?, ?, ?)
                """,
                (mac, timestamp.isoformat(), json.dumps(device, ensure_ascii=False)),
            )
            batch_id = int(batch_cursor.lastrowid)
            inserted_batches += 1

            for sensor in sensors:
                sensor_id = str(sensor.get("id") or "").strip().upper()
                value = sensor.get("value")
                if not sensor_id or value is None:
                    continue

                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    continue

                connection.execute(
                    """
                    INSERT INTO observations(
                        batch_id, device_mac, observed_at, sensor_id, sensor_name, value, unit
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        mac,
                        timestamp.isoformat(),
                        sensor_id,
                        sensor_label(sensor_id),
                        numeric_value,
                        sensor_unit(sensor_id, sensor.get("unit")),
                    ),
                )
                inserted_rows += 1

    cache_invalidate()

    return {
        "received_at": timestamp.isoformat(),
        "devices": inserted_batches,
        "measurements": inserted_rows,
    }


def get_latest_snapshot(
    conn: sqlite3.Connection | None = None,
    station_mac: str | None = None,
) -> dict[str, Any] | None:
    with use_connection(conn) as connection:
        if station_mac:
            batch = connection.execute(
                """
                SELECT id, device_mac, received_at
                FROM ingest_batches
                WHERE device_mac = ?
                ORDER BY received_at DESC, id DESC
                LIMIT 1
                """,
                (station_mac,),
            ).fetchone()
        else:
            batch = connection.execute(
                """
                SELECT id, device_mac, received_at
                FROM ingest_batches
                ORDER BY received_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if not batch:
            return None

        rows = connection.execute(
            """
            SELECT sensor_id, sensor_name, value, unit
            FROM observations
            WHERE batch_id = ?
            ORDER BY sensor_name
            """,
            (batch["id"],),
        ).fetchall()

    readings: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = {
            "sensor_id": row["sensor_id"],
            "sensor_name": row["sensor_name"],
            "value": row["value"],
            "unit": row["unit"],
        }
        readings.append(entry)
        lookup[row["sensor_id"].upper()] = entry

    primary = [lookup[sensor_id] for sensor_id in PRIMARY_SENSOR_IDS if sensor_id in lookup]
    local_dt = to_local_timestamp(batch["received_at"])
    return {
        "batch_id": batch["id"],
        "device_mac": batch["device_mac"],
        "received_at": batch["received_at"],
        "received_local": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "received_ago": format_relative_age(batch["received_at"]),
        "received_freshness": freshness_emoji(batch["received_at"]),
        "primary_readings": primary,
        "readings": readings,
    }


def _reading_value(snapshot: dict[str, Any] | None, ids: tuple[str, ...]) -> float | None:
    if not snapshot:
        return None
    lookup = {str(item.get("sensor_id", "")).upper(): item for item in snapshot.get("readings", [])}
    for sid in ids:
        item = lookup.get(sid)
        if not item:
            continue
        try:
            return float(item["value"])
        except (TypeError, ValueError, KeyError):
            continue
    return None


def get_comfort_risk(
    snapshot: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    current = snapshot or get_latest_snapshot(conn=conn)
    if not current:
        return {
            "level": "risk",
            "state": "Нет данных",
            "description": "Станция еще не передавала телеметрию.",
            "score": 0,
            "reasons": ["Нет телеметрии"],
            "temperature": {"value": 0.0, "unit": "°C"},
            "humidity": {"value": 0.0, "unit": "%"},
            "pressure": {"value": 0.0, "unit": "мм рт. ст."},
        }

    reasons: list[str] = []
    score = 100

    t1 = _reading_value(current, TEMPERATURE_SENSOR_IDS)
    rh = _reading_value(current, HUMIDITY_SENSOR_IDS)
    press = _reading_value(current, PRESSURE_SENSOR_IDS)

    if t1 is None:
        reasons.append("Нет температуры")
        score -= 25
    elif t1 < 16 or t1 > 28:
        reasons.append(f"Температура вне комфорта: {t1:.1f}°C")
        score -= 20

    if rh is None:
        reasons.append("Нет влажности")
        score -= 20
    elif rh < 30 or rh > 65:
        reasons.append(f"Влажность вне комфорта: {rh:.0f}%")
        score -= 20

    if press is None:
        reasons.append("Нет давления")
        score -= 10
    elif press < 730 or press > 780:
        reasons.append(f"Давление нестабильное: {press:.1f} мм рт. ст.")
        score -= 15

    score = max(0, min(100, score))
    if score >= 70:
        level = "good"
        state = "Комфортно"
        risk = "Низкий риск"
    elif score >= 40:
        level = "watch"
        state = "Требует внимания"
        risk = "Средний риск"
    else:
        level = "risk"
        state = "Некомфортно"
        risk = "Высокий риск"

    if not reasons:
        reasons = ["Показатели в норме"]

    return {
        "level": level,
        "state": state,
        "description": f"{risk}. " + "; ".join(reasons),
        "risk": risk,
        "score": score,
        "reasons": reasons,
        "temperature": {"value": t1 if t1 is not None else 0.0, "unit": "°C"},
        "humidity": {"value": rh if rh is not None else 0.0, "unit": "%"},
        "pressure": {"value": press if press is not None else 0.0, "unit": "мм рт. ст."},
    }


def get_chart_series(
    days: int | None = None,
    hours: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Серии измерений за последние N часов или дней.

    Совместимо с прежней сигнатурой: вызов get_chart_series(1) — 1 день.
    Для нового UI (1ч/24ч/7д/30д) удобнее передавать hours напрямую.
    """
    if hours is None and days is None:
        hours_eff = 24
    elif hours is not None:
        hours_eff = hours
    else:
        hours_eff = (days or 0) * 24
    hours_eff = max(1, min(hours_eff, 90 * 24))

    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT upper(sensor_id) AS sensor_id, sensor_name, observed_at, value, unit
            FROM observations
            WHERE julianday(observed_at) >= julianday('now', ?)
              AND sensor_id <> ''
            ORDER BY observed_at ASC
            """,
            (f"-{hours_eff} hours",),
        ).fetchall()

    series: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = series.setdefault(
            row["sensor_id"],
            {"label": row["sensor_name"], "unit": row["unit"], "points": []},
        )
        bucket["points"].append({"x": row["observed_at"], "y": row["value"]})

    return {"hours": hours_eff, "days": round(hours_eff / 24, 2), "series": series}


def _value_by_ids(reading_lookup: dict[str, dict[str, Any]], ids: tuple[str, ...]) -> str:
    for sensor_id in ids:
        item = reading_lookup.get(sensor_id)
        if item:
            unit = f" {item['unit']}" if item["unit"] else ""
            return f"{item['value']:.2f}{unit}"
    return "—"


def get_history_for_date(target_date: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    # Parse target_date as a local date, then convert to UTC bounds for the query
    try:
        target_day = date.fromisoformat(target_date)
    except ValueError:
        return []
    start_utc, end_utc = _local_day_bounds(target_day)

    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT
                b.id AS batch_id,
                b.device_mac,
                b.received_at,
                o.sensor_id,
                o.sensor_name,
                o.value,
                o.unit
            FROM ingest_batches AS b
            JOIN observations AS o ON o.batch_id = b.id
            WHERE b.received_at >= ? AND b.received_at < ?
            ORDER BY b.received_at ASC, o.sensor_name ASC
            """,
            (start_utc, end_utc),
        ).fetchall()

    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        bucket = grouped.setdefault(
            row["batch_id"],
            {
                "batch_id": row["batch_id"],
                "device_mac": row["device_mac"],
                "received_at": row["received_at"],
                "readings": [],
            },
        )
        bucket["readings"].append(
            {
                "sensor_id": row["sensor_id"].upper(),
                "sensor_name": row["sensor_name"],
                "value": row["value"],
                "unit": row["unit"],
            }
        )

    result: list[dict[str, Any]] = []
    core_ids = {"T1", "T2", "T3", "T4", "RH", "H1", "H2", "PRESS", "HPA"}
    all_other_sensors: dict[str, str] = {}  # {sensor_id: sensor_name}

    # Get time format from settings
    try:
        from . import settings as _settings
        time_format = _settings.get_string("TIME_FORMAT").strip().lower()
    except Exception:
        time_format = "24h"
    time_fmt = "%H:%M:%S" if time_format == "24h" else "%I:%M:%S %p"

    for item in grouped.values():
        reading_lookup = {reading["sensor_id"]: reading for reading in item["readings"]}
        local_dt = to_local_timestamp(item["received_at"])
        extra: list[dict[str, Any]] = []
        for reading in item["readings"]:
            if reading["sensor_id"] in core_ids:
                continue
            all_other_sensors[reading["sensor_id"]] = reading["sensor_name"]
            extra.append({
                "sensor_id": reading["sensor_id"],
                "label": reading["sensor_name"],
                "value": reading["value"],
                "unit": reading["unit"],
            })

        result.append(
            {
                **item,
                "date": local_dt.strftime("%Y-%m-%d"),
                "time": local_dt.strftime(time_fmt),
                "temperature": _value_by_ids(reading_lookup, TEMPERATURE_SENSOR_IDS),
                "humidity": _value_by_ids(reading_lookup, HUMIDITY_SENSOR_IDS),
                "pressure": _value_by_ids(reading_lookup, PRESSURE_SENSOR_IDS),
                "other_data": extra,
            }
        )

    return result, all_other_sensors


def get_uptime_monitor(hours: int = 24, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    period = max(6, min(hours, 168))
    now_utc = datetime.now(UTC)
    start_utc = now_utc - timedelta(hours=period)

    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT received_at
            FROM ingest_batches
            WHERE julianday(received_at) >= julianday(?)
            ORDER BY received_at ASC
            """,
            (start_utc.isoformat(),),
        ).fetchall()
        latest = connection.execute(
            """
            SELECT received_at
            FROM ingest_batches
            ORDER BY received_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    now_local = now_utc.astimezone(LOCAL_TZ)
    hour_starts = [now_local.replace(minute=0, second=0, microsecond=0) - timedelta(hours=period - 1 - i) for i in range(period)]
    labels = [moment.strftime("%H:%M") for moment in hour_starts]
    points = [0 for _ in range(period)]

    for row in rows:
        observed_local = to_local_timestamp(row["received_at"]).replace(minute=0, second=0, microsecond=0)
        delta = int((observed_local - hour_starts[0]).total_seconds() // 3600)
        if 0 <= delta < period:
            points[delta] += 1

    active_hours = sum(1 for value in points if value > 0)
    availability = round((active_hours / period) * 100, 1)
    latest_received = latest["received_at"] if latest else None

    return {
        "hours": period,
        "labels": labels,
        "points": points,
        "availability": availability,
        "last_seen_ago": format_relative_age(latest_received) if latest_received else "нет данных",
        "freshness": freshness_emoji(latest_received) if latest_received else "⚪",
    }


def get_today_extremes(
    sensor_ids: tuple[str, ...],
    default_unit: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Min/max за сегодня по первому sensor_id из списка, по которому есть данные."""
    now_local = datetime.now(LOCAL_TZ)
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(UTC).isoformat()
    day_end_utc = day_end_local.astimezone(UTC).isoformat()

    resolved_id: str | None = None
    min_row = None
    max_row = None
    with use_connection(conn) as connection:
        for sid in sensor_ids:
            row_min = connection.execute(
                """
                SELECT observed_at, value, unit
                FROM observations
                WHERE upper(sensor_id) = ?
                  AND julianday(observed_at) >= julianday(?)
                  AND julianday(observed_at) < julianday(?)
                ORDER BY value ASC, observed_at ASC
                LIMIT 1
                """,
                (sid.upper(), day_start_utc, day_end_utc),
            ).fetchone()
            if not row_min:
                continue
            row_max = connection.execute(
                """
                SELECT observed_at, value, unit
                FROM observations
                WHERE upper(sensor_id) = ?
                  AND julianday(observed_at) >= julianday(?)
                  AND julianday(observed_at) < julianday(?)
                ORDER BY value DESC, observed_at ASC
                LIMIT 1
                """,
                (sid.upper(), day_start_utc, day_end_utc),
            ).fetchone()
            resolved_id = sid.upper()
            min_row = row_min
            max_row = row_max
            break

    def serialize(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        local_time = to_local_timestamp(row["observed_at"])
        # Get time format from settings
        try:
            from . import settings as _settings
            time_format = _settings.get_string("TIME_FORMAT").strip().lower()
        except Exception:
            time_format = "24h"
        time_str = local_time.strftime("%H:%M" if time_format == "24h" else "%I:%M %p")
        return {
            "value": row["value"],
            "unit": row["unit"] or default_unit,
            "time": time_str,
            "datetime_local": local_time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    return {
        "date": day_start_local.strftime("%Y-%m-%d"),
        "sensor_id": resolved_id,
        "min": serialize(min_row),
        "max": serialize(max_row),
    }


def get_today_temperature_extremes(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    return get_today_extremes(TEMPERATURE_SENSOR_IDS, default_unit="°C", conn=conn)


def _period_stats_t1(
    start_utc: str,
    end_utc: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with use_connection(conn) as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS cnt,
                MIN(value) AS min_v,
                MAX(value) AS max_v,
                AVG(value) AS avg_v
            FROM observations
            WHERE upper(sensor_id) = 'T1'
              AND julianday(observed_at) >= julianday(?)
              AND julianday(observed_at) < julianday(?)
            """,
            (start_utc, end_utc),
        ).fetchone()
    if not row or int(row["cnt"] or 0) == 0:
        return None
    return {
        "count": int(row["cnt"]),
        "min": float(row["min_v"]),
        "max": float(row["max_v"]),
        "avg": float(row["avg_v"]),
        "amp": float(row["max_v"]) - float(row["min_v"]),
    }


def _bucket_hourly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bucket: dict[int, list[float]] = {h: [] for h in range(24)}
    for row in rows:
        local_dt = to_local_timestamp(row["observed_at"])
        bucket[local_dt.hour].append(float(row["value"]))
    return [
        {"hour": h, "value": round(sum(bucket[h]) / len(bucket[h]), 2) if bucket[h] else None}
        for h in range(24)
    ]


def _period_hourly_series_t1_batch(
    target_days: list[date],
    connection: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """Среднее T1 по часам локального дня для нескольких дней одним SQL.

    UNION ALL даёт оптимизатору SQLite три узких индексных диапазона по
    (sensor_id, observed_at) — это лучше, чем один range-scan между min(start)
    и max(end), особенно когда между датами большой разрыв (год).
    """
    if not target_days:
        return {}

    selects = []
    params: list[Any] = []
    for day in target_days:
        s, e = _local_day_bounds(day)
        selects.append(
            "SELECT ? AS bucket, observed_at, value FROM observations "
            "WHERE upper(sensor_id) = 'T1' "
            "AND julianday(observed_at) >= julianday(?) "
            "AND julianday(observed_at) < julianday(?)"
        )
        params.extend([day.isoformat(), s, e])
    sql = " UNION ALL ".join(selects) + " ORDER BY observed_at ASC"
    rows = connection.execute(sql, params).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {d.isoformat(): [] for d in target_days}
    for row in rows:
        grouped[row["bucket"]].append(row)

    return {key: _bucket_hourly(rows) for key, rows in grouped.items()}


def _period_hourly_series_t1(
    target_day: date,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Среднее T1 по часам локального дня. 24 точки, пропуски — null."""
    day_start_utc, day_end_utc = _local_day_bounds(target_day)
    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT observed_at, value
            FROM observations
            WHERE upper(sensor_id) = 'T1'
              AND julianday(observed_at) >= julianday(?)
              AND julianday(observed_at) < julianday(?)
            ORDER BY observed_at ASC
            """,
            (day_start_utc, day_end_utc),
        ).fetchall()

    bucket: dict[int, list[float]] = {h: [] for h in range(24)}
    for row in rows:
        local_dt = to_local_timestamp(row["observed_at"])
        bucket[local_dt.hour].append(float(row["value"]))

    return [
        {"hour": h, "value": round(sum(bucket[h]) / len(bucket[h]), 2) if bucket[h] else None}
        for h in range(24)
    ]


def _period_day_night_stats_t1(
    target_day: date,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    day_start_utc, day_end_utc = _local_day_bounds(target_day)
    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT observed_at, value
            FROM observations
            WHERE upper(sensor_id) = 'T1'
              AND julianday(observed_at) >= julianday(?)
              AND julianday(observed_at) < julianday(?)
            ORDER BY observed_at ASC
            """,
            (day_start_utc, day_end_utc),
        ).fetchall()

    day_values: list[float] = []
    night_values: list[float] = []
    all_values: list[float] = []

    # Day: 07:00-18:59, Night: 19:00-06:59
    for row in rows:
        value = float(row["value"])
        local_hour = to_local_timestamp(row["observed_at"]).hour
        all_values.append(value)
        if 7 <= local_hour < 19:
            day_values.append(value)
        else:
            night_values.append(value)

    def avg(values: list[float]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "day_avg": avg(day_values),
        "night_avg": avg(night_values),
        "avg": avg(all_values),
        "day_count": len(day_values),
        "night_count": len(night_values),
        "count": len(all_values),
    }


@cached(ttl_seconds=300)
def get_period_comparison(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    with use_connection(conn) as connection:
        return _get_period_comparison_impl(connection)


def _get_period_comparison_impl(connection: sqlite3.Connection) -> dict[str, Any]:
    today_local = datetime.now(LOCAL_TZ).date()
    periods = [
        ("Вчера", today_local - timedelta(days=1)),
        ("Месяц назад", today_local - timedelta(days=30)),
        ("Год назад", today_local - timedelta(days=365)),
    ]

    today_stats = _period_day_night_stats_t1(today_local, conn=connection)

    rows = []
    for label, day in periods:
        stats = _period_day_night_stats_t1(day, conn=connection)
        delta_day = None
        delta_night = None
        if today_stats["day_avg"] is not None and stats["day_avg"] is not None:
            delta_day = today_stats["day_avg"] - stats["day_avg"]
        if today_stats["night_avg"] is not None and stats["night_avg"] is not None:
            delta_night = today_stats["night_avg"] - stats["night_avg"]

        rows.append(
            {
                "label": label,
                "date": day.isoformat(),
                "stats": stats,
                "delta_day": delta_day,
                "delta_night": delta_night,
            }
        )

    yesterday = today_local - timedelta(days=1)
    month_ago = today_local - timedelta(days=30)
    batch = _period_hourly_series_t1_batch([today_local, yesterday, month_ago], connection)
    series = {
        "today": batch[today_local.isoformat()],
        "yesterday": batch[yesterday.isoformat()],
        "monthAgo": batch[month_ago.isoformat()],
    }

    return {
        "today_date": today_local.isoformat(),
        "today": {
            "avg": today_stats["avg"],
            "count": today_stats["count"],
        },
        "day": {
            "today_avg": today_stats["day_avg"],
            "today_count": today_stats["day_count"],
            "rows": [
                {
                    "label": row["label"],
                    "date": row["date"],
                    "avg": row["stats"]["day_avg"],
                    "count": row["stats"]["day_count"],
                    "delta": row["delta_day"],
                }
                for row in rows
            ],
        },
        "night": {
            "today_avg": today_stats["night_avg"],
            "today_count": today_stats["night_count"],
            "rows": [
                {
                    "label": row["label"],
                    "date": row["date"],
                    "avg": row["stats"]["night_avg"],
                    "count": row["stats"]["night_count"],
                    "delta": row["delta_night"],
                }
                for row in rows
            ],
        },
        "rows": rows,
        "series": series,
    }


@cached(ttl_seconds=300)
def get_temperature_heatmap(days: int = 30, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    period = max(7, min(days, 90))
    today_local = datetime.now(LOCAL_TZ).date()
    start_day = today_local - timedelta(days=period - 1)
    start_utc, _ = _local_day_bounds(start_day)

    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT observed_at, value
            FROM observations
            WHERE upper(sensor_id) = 'T1'
              AND julianday(observed_at) >= julianday(?)
            ORDER BY observed_at ASC
            """,
            (start_utc,),
        ).fetchall()

    bucket: dict[str, dict[int, list[float]]] = {}
    for row in rows:
        local_dt = to_local_timestamp(row["observed_at"])
        day_key = local_dt.date().isoformat()
        day_bucket = bucket.setdefault(day_key, {h: [] for h in range(24)})
        day_bucket[local_dt.hour].append(float(row["value"]))

    labels: list[str] = []
    matrix: list[list[float | None]] = []
    for i in range(period):
        day = start_day + timedelta(days=i)
        day_key = day.isoformat()
        labels.append(day_key)
        hours = []
        src = bucket.get(day_key, {})
        for h in range(24):
            vals = src.get(h, [])
            hours.append(round(sum(vals) / len(vals), 2) if vals else None)
        matrix.append(hours)

    return {
        "labels": labels,
        "hours": [f"{h:02d}:00" for h in range(24)],
        "matrix": matrix,
    }


@cached(ttl_seconds=300)
def get_anomaly_calendar(month: str | None = None, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    now_local = datetime.now(LOCAL_TZ)
    if month:
        try:
            month_start = datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
        except ValueError:
            month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)

    days_count = (next_month.date() - month_start.date()).days
    days: list[dict[str, Any]] = []

    with use_connection(conn) as connection:
        for idx in range(days_count):
            day = month_start.date() + timedelta(days=idx)
            s, e = _local_day_bounds(day)
            stats = _period_stats_t1(s, e, conn=connection)

            packets = connection.execute(
                """
                SELECT received_at
                FROM ingest_batches
                WHERE julianday(received_at) >= julianday(?)
                  AND julianday(received_at) < julianday(?)
                ORDER BY received_at ASC
                """,
                (s, e),
            ).fetchall()

            gaps = 0
            prev_dt: datetime | None = None
            for row in packets:
                dt = parse_timestamp(row["received_at"])
                if prev_dt and (dt - prev_dt).total_seconds() > 30 * 60:
                    gaps += 1
                prev_dt = dt

            level = "ok"
            reasons: list[str] = []
            if stats:
                if stats["min"] < -25 or stats["max"] > 35:
                    level = "high"
                    reasons.append("Экстремальная температура")
                elif stats["amp"] > 15:
                    level = "medium"
                    reasons.append("Резкий суточный перепад")
            if gaps >= 3:
                level = "high"
                reasons.append("Много пропусков данных")
            elif gaps > 0 and level == "ok":
                level = "medium"
                reasons.append("Есть пропуски данных")

            days.append(
                {
                    "date": day.isoformat(),
                    "day": day.day,
                    "level": level,
                    "reasons": reasons,
                    "has_data": bool(stats),
                }
            )

    return {
        "month": month_start.strftime("%Y-%m"),
        "days": days,
    }


def get_t5_chart_series(hours: int = 24, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Возвращает точки T5 (температура чипа) за последние N часов."""
    hours = max(1, min(hours, 168))
    with use_connection(conn) as connection:
        rows = connection.execute(
            """
            SELECT observed_at, value
            FROM observations
            WHERE upper(sensor_id) = 'T5'
              AND julianday(observed_at) >= julianday('now', ?)
            ORDER BY observed_at ASC
            """,
            (f"-{hours} hours",),
        ).fetchall()
    result = []
    for row in rows:
        local_dt = to_local_timestamp(row["observed_at"])
        result.append({
            "time": local_dt.strftime("%H:%M"),
            "datetime": local_dt.strftime("%Y-%m-%d %H:%M"),
            "value": round(float(row["value"]), 1),
        })
    return result


def get_station_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    with use_connection(conn) as connection:
        snapshot = get_latest_snapshot(conn=connection)
        now = datetime.now(UTC)
        day_ago = (now - timedelta(days=1)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()
        packets_24h = connection.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM ingest_batches
            WHERE julianday(received_at) >= julianday(?)
            """,
            (day_ago,),
        ).fetchone()["cnt"]
        packets_7d = connection.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM ingest_batches
            WHERE julianday(received_at) >= julianday(?)
            """,
            (week_ago,),
        ).fetchone()["cnt"]
        recent = connection.execute(
            """
            SELECT received_at
            FROM ingest_batches
            WHERE julianday(received_at) >= julianday(?)
            ORDER BY received_at ASC
            """,
            (day_ago,),
        ).fetchall()

    avg_interval = None
    gaps = 0
    gap_details: list[dict[str, Any]] = []
    if len(recent) >= 2:
        intervals: list[float] = []
        prev = parse_timestamp(recent[0]["received_at"])
        prev_raw = recent[0]["received_at"]
        for row in recent[1:]:
            cur = parse_timestamp(row["received_at"])
            delta_min = (cur - prev).total_seconds() / 60.0
            intervals.append(delta_min)
            if delta_min > 30:
                gaps += 1
                gap_details.append({
                    "start": to_local_timestamp(prev_raw).strftime("%H:%M"),
                    "end": to_local_timestamp(row["received_at"]).strftime("%H:%M"),
                    "duration_min": round(delta_min, 1),
                })
            prev = cur
            prev_raw = row["received_at"]
        avg_interval = round(sum(intervals) / len(intervals), 1) if intervals else None

    missing_primary = []
    if snapshot:
        present = {str(r.get("sensor_id", "")).upper() for r in snapshot.get("readings", [])}
        missing_primary = [sid for sid in PRIMARY_SENSOR_IDS if sid not in present]

    return {
        "last_seen": snapshot["received_local"] if snapshot else "нет данных",
        "last_seen_ago": snapshot["received_ago"] if snapshot else "нет данных",
        "freshness": snapshot["received_freshness"] if snapshot else "⚪",
        "packets_24h": int(packets_24h or 0),
        "packets_7d": int(packets_7d or 0),
        "avg_interval_min": avg_interval,
        "gaps_24h": gaps,
        "gap_details": gap_details,
        "sensor_count": len(snapshot.get("readings", [])) if snapshot else 0,
        "missing_primary": missing_primary,
    }


def _reading_emoji(sensor_id: str) -> str:
    sid = sensor_id.upper()
    if sid.startswith("T"):
        return "🌡️"
    if sid in {"RH", "H1", "H2"}:
        return "💧"
    if sid in {"PRESS", "HPA"}:
        return "🧭"
    if sid.startswith("WS") or sid == "1DIR":
        return "💨"
    if sid.startswith("RAIN"):
        return "🌧️"
    if sid.startswith("PM"):
        return "🌫️"
    return "•"


def _format_temp_with_icon(value: float) -> str:
    icon = "☀️" if value >= 0 else "❄️"
    return f"{icon} {value:+.2f}°"


def format_telegram_snapshot(snapshot: dict[str, Any] | None) -> str:
    # Локальный импорт, чтобы не создать цикл при импортах в настройках/боте.
    from . import settings as _settings
    place = _settings.get_string("WEATHER_PLACE_NAME").strip() or "Локальная станция"
    title = f'🏡 Погода: {place}'
    if not snapshot:
        return f"{title}\n⚪ --\n\n📭 Данных пока нет. Станция еще ничего не отправляла."

    freshness = snapshot.get("received_freshness") or freshness_emoji(snapshot["received_at"])
    updated_ago = snapshot.get("received_ago") or format_relative_age(snapshot["received_at"])
    lines = [
        title,
        f"🕒 Обновлено: {freshness} {updated_ago}",
        "",
        "Показатели:",
    ]

    for reading in snapshot["primary_readings"]:
        sensor_id = str(reading.get("sensor_id", "")).upper()
        if sensor_id == "T1":
            lines.append(f"{_reading_emoji(sensor_id)} {reading['sensor_name']}: {_format_temp_with_icon(float(reading['value']))}")
        else:
            unit = f" {reading['unit']}" if reading["unit"] else ""
            lines.append(f"{_reading_emoji(sensor_id)} {reading['sensor_name']}: {reading['value']:.2f}{unit}")

    extremes = get_today_temperature_extremes()
    if extremes.get("min") and extremes.get("max"):
        t_min = extremes["min"]
        t_max = extremes["max"]
        lines.extend(
            [
                "",
                "📉📈 T1 за сегодня:",
                f"Мин: {_format_temp_with_icon(float(t_min['value']))} в {t_min['time']}",
                f"Макс: {_format_temp_with_icon(float(t_max['value']))} в {t_max['time']}",
            ]
        )

    return "\n".join(lines)
