"""Агрегация старых 5-минутных показаний в часовые средние.

Для данных старше ``AGGREGATION_MAX_AGE_DAYS`` (по умолчанию 45 дней):
  1. Находит все неагрегированные batch-и (без флага ``_aggregated``).
  2. Вычисляет средние значения по каждому сенсору за каждый локальный час.
  3. Удаляет старые batch-и + observations (CASCADE).
  4. Создаёт новые batch-и с одним observation на (час × сенсор).

Агрегированные batch-и помечены ``_aggregated: true`` в payload_json,
чтобы не быть агрегированными повторно.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger

from .db import LOCAL_TZ, get_connection, use_connection
from .sensor_map import sensor_label, sensor_unit


@dataclass
class AggregationResult:
    deleted_batches: int = 0
    created_batches: int = 0
    deleted_rows: int = 0
    created_rows: int = 0
    hours_processed: int = 0
    error: str | None = None


def aggregate_old_data(
    conn: sqlite3.Connection,
    max_age_days: int = 45,
) -> AggregationResult:
    """Агрегует 5-минутные показания в часовые средние.

    Принимает существующее соединение с БД.
    Сама управляет транзакцией (BEGIN / COMMIT / ROLLBACK).
    """
    result = AggregationResult()

    now_utc = datetime.now(UTC)
    cutoff_utc = now_utc - timedelta(days=max_age_days)
    cutoff_iso = cutoff_utc.isoformat()

    # Проверяем, есть ли уже открытая транзакция
    in_transaction = conn.in_transaction

    try:
        if not in_transaction:
            conn.execute("BEGIN")

        # Находим неагрегированные batch-и до cutoff
        old_batches = conn.execute(
            """
            SELECT id, device_mac, received_at, payload_json
            FROM ingest_batches
            WHERE received_at < ?
            """,
            (cutoff_iso,),
        ).fetchall()

        # Фильтруем уже агрегированные
        non_aggregated = []
        for b in old_batches:
            try:
                payload = json.loads(b["payload_json"])
                if payload.get("_aggregated"):
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            non_aggregated.append(b)

        if not non_aggregated:
            conn.execute("COMMIT")
            return result

        batch_ids = [b["id"] for b in non_aggregated]
        result.deleted_batches = len(batch_ids)

        # Считаем observations до удаления
        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM observations WHERE batch_id IN ({})".format(
                ",".join("?" * len(batch_ids))
            ),
            batch_ids,
        ).fetchone()
        result.deleted_rows = int(count_row["cnt"]) if count_row else 0

        # Получаем все observations для этих batch-ей
        observations = conn.execute(
            """
            SELECT device_mac, observed_at, sensor_id, value
            FROM observations
            WHERE batch_id IN ({})
            ORDER BY observed_at ASC
            """.format(",".join("?" * len(batch_ids))),
            batch_ids,
        ).fetchall()

        # Группируем: (hour_iso, device_mac, sensor_id) → [values]
        hourly: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for obs in observations:
            # Обрезаем observed_at до часа (UTC)
            hour_iso = obs["observed_at"][:13] + ":00:00+00:00"
            key = (hour_iso, obs["device_mac"], obs["sensor_id"])
            try:
                hourly[key].append(float(obs["value"]))
            except (TypeError, ValueError):
                continue

        if not hourly:
            # Нет валидных observations — просто удаляем batch-и
            _delete_batches(conn, batch_ids)
            if not in_transaction:
                conn.execute("COMMIT")
            return result

        # Удаляем старые batch-и (observations удалятся CASCADE)
        _delete_batches(conn, batch_ids)

        # Группируем по (hour_iso, device_mac) для создания batch-ей
        hour_device_data: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        for (hour_iso, device_mac, sensor_id), values in hourly.items():
            avg_value = round(sum(values) / len(values), 4)
            hour_device_data[(hour_iso, device_mac)][sensor_id] = avg_value

        # Создаём новые агрегированные batch-и + observations
        for (hour_iso, device_mac), sensor_values in sorted(hour_device_data.items()):
            payload_json = json.dumps({
                "mac": device_mac,
                "_aggregated": True,
                "_avg_count": len(observations),  # approximate
            }, ensure_ascii=False)

            batch_cursor = conn.execute(
                """
                INSERT INTO ingest_batches(device_mac, received_at, payload_json)
                VALUES (?, ?, ?)
                """,
                (device_mac, hour_iso, payload_json),
            )
            batch_id = int(batch_cursor.lastrowid)
            result.created_batches += 1
            result.hours_processed += 1

            for sensor_id, avg_value in sorted(sensor_values.items()):
                conn.execute(
                    """
                    INSERT INTO observations(
                        batch_id, device_mac, observed_at, sensor_id, sensor_name, value, unit
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        device_mac,
                        hour_iso,
                        sensor_id,
                        sensor_label(sensor_id),
                        avg_value,
                        sensor_unit(sensor_id),
                    ),
                )
                result.created_rows += 1

        if not in_transaction:
            conn.execute("COMMIT")

    except Exception as exc:
        try:
            if not in_transaction:
                conn.execute("ROLLBACK")
        except Exception:
            pass
        result.error = str(exc)
        logger.exception("Aggregation failed")

    return result


def _delete_batches(conn: sqlite3.Connection, batch_ids: list[int]) -> None:
    """Удаляет batch-и по id. Observations удалятся через ON DELETE CASCADE."""
    placeholder = ",".join("?" * len(batch_ids))
    conn.execute(
        f"DELETE FROM ingest_batches WHERE id IN ({placeholder})",
        batch_ids,
    )


def run_aggregation_if_needed(conn: sqlite3.Connection) -> AggregationResult | None:
    """Проверяет, пора ли запускать агрегацию, и запускает если нужно.

    Логика:
      - Читает AGGREGATION_LAST_RUN_AT и AGGREGATION_INTERVAL_DAYS из настроек.
      - Если прошло больше AGGREGATION_INTERVAL_DAYS — запускает агрегацию.
      - После успешного завершения обновляет AGGREGATION_LAST_RUN_AT.

    Возвращает результат агрегации или None, если запуск не требовался.
    """
    from . import settings

    last_run_str = settings.get_string("AGGREGATION_LAST_RUN_AT")
    interval_days = settings.get_int("AGGREGATION_INTERVAL_DAYS") or 10
    max_age_days = settings.get_int("AGGREGATION_MAX_AGE_DAYS") or 45

    if last_run_str:
        try:
            last_run = datetime.fromisoformat(last_run_str)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=UTC)
            days_since = (datetime.now(UTC) - last_run).total_seconds() / 86400
            if days_since < interval_days:
                return None
        except (ValueError, TypeError):
            pass

    logger.info(
        "Starting aggregation: max_age_days={}, interval_days={}",
        max_age_days, interval_days,
    )
    result = aggregate_old_data(conn, max_age_days=max_age_days)

    if result.error:
        logger.error("Aggregation failed: {}", result.error)
    else:
        # Обновляем timestamp последнего запуска
        now_iso = datetime.now(UTC).isoformat()
        settings.set_value("AGGREGATION_LAST_RUN_AT", now_iso, conn=conn)
        logger.info(
            "Aggregation complete: deleted {} batches/{} rows, created {} batches/{} rows",
            result.deleted_batches, result.deleted_rows,
            result.created_batches, result.created_rows,
        )

    return result
