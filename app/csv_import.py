"""Ретро-импорт показаний из CSV-файла (экспорт narodmon.ru).

Формат CSV (разделитель `;`):
  UNIXTIME;Дата;Время;Плотн воздха;Коэф смешивания;Дефиц давл пара;
  Давление;Влажность;Абс. влажность;VOLT;Точка росы;Ощущаем темп-ру;
  T5;Температура

Маппинг колонок → sensor_id:
  Температура   → T1
  T5            → T5
  Давление      → PRESS
  Влажность     → RH
  VOLT          → VOLT
  Точка росы    → DEW
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .cache import invalidate as cache_invalidate
from .db import use_connection
from .sensor_map import sensor_label, sensor_unit

# Колонки CSV → sensor_id.  Колонки, которых нет в этом словаре,
# игнорируются при импорте.
_CSV_COLUMN_TO_SENSOR_ID: dict[str, str] = {
    "Температура": "T1",
    "T5": "T5",
    "Давление": "PRESS",
    "Влажность": "RH",
    "VOLT": "VOLT",
    "Точка росы": "DEW",
}


@dataclass
class CsvRow:
    """Одна распарсенная строка CSV, готовая к импорту."""
    unixtime: int
    observed_at: str  # ISO-8601 UTC
    readings: dict[str, float] = field(default_factory=dict)


@dataclass
class CsvParseResult:
    """Результат парсинга CSV-файла."""
    rows: list[CsvRow] = field(default_factory=list)
    skipped_rows: list[int] = field(default_factory=list)  # 1-based line numbers
    columns_found: list[str] = field(default_factory=list)
    columns_mapped: list[str] = field(default_factory=list)
    error: str | None = None


def parse_csv(text: str) -> CsvParseResult:
    """Парсит CSV-текст (экспорт narodmon.ru) в список CsvRow.

    - Разделитель: `;`
    - Первая строка: заголовки колонок
    - UNIXTIME используется для observed_at (конвертируется в UTC ISO-8601)
    - Невалидные числовые значения пропускаются
    - Строки без unixtime или без хотя бы одного валидного сенсора — пропускаются
    """
    result = CsvParseResult()

    try:
        reader = csv.reader(io.StringIO(text), delimiter=";")
        headers: list[str] = []

        for line_idx, row in enumerate(reader):
            if line_idx == 0:
                headers = [cell.strip() for cell in row]
                result.columns_found = headers
                result.columns_mapped = [
                    h for h in headers if h in _CSV_COLUMN_TO_SENSOR_ID
                ]
                continue

            line_number = line_idx + 1  # 1-based for human-readable errors

            if not row or not row[0].strip():
                result.skipped_rows.append(line_number)
                continue

            # Парсим UNIXTIME
            try:
                unixtime = int(row[0].strip())
            except (ValueError, IndexError):
                result.skipped_rows.append(line_number)
                continue

            observed_at = datetime.fromtimestamp(unixtime, tz=UTC).isoformat()

            # Маппим колонки на sensor_id
            readings: dict[str, float] = {}
            header_to_index = {h: i for i, h in enumerate(headers)}

            for csv_col, sensor_id in _CSV_COLUMN_TO_SENSOR_ID.items():
                col_idx = header_to_index.get(csv_col)
                if col_idx is None or col_idx >= len(row):
                    continue
                raw_value = row[col_idx].strip()
                if not raw_value:
                    continue
                try:
                    readings[sensor_id] = float(raw_value)
                except ValueError:
                    continue

            if not readings:
                result.skipped_rows.append(line_number)
                continue

            result.rows.append(CsvRow(
                unixtime=unixtime,
                observed_at=observed_at,
                readings=readings,
            ))

    except Exception as exc:
        result.error = f"Ошибка парсинга CSV: {exc}"

    return result


def build_ingest_payload(
    rows: list[CsvRow],
    device_mac: str,
) -> dict[str, Any]:
    """Конвертирует CsvRow-ы в формат, совместимый с save_payload().

    Группирует строки по unixtime (если есть дубликаты — объединяет сенсоры).
    Возвращает dict: {"devices": [{"mac": ..., "sensors": [...]}]}
    Каждое уникальное время → один device-entry.
    """
    # Группируем по unixtime
    grouped: dict[int, list[CsvRow]] = {}
    for row in rows:
        grouped.setdefault(row.unixtime, []).append(row)

    devices: list[dict[str, Any]] = []
    for unixtime in sorted(grouped):
        group_rows = grouped[unixtime]
        observed_at = datetime.fromtimestamp(unixtime, tz=UTC).isoformat()

        # Объединяем readings из всех строк с одинаковым unixtime
        merged: dict[str, float] = {}
        for row in group_rows:
            merged.update(row.readings)

        sensors: list[dict[str, Any]] = []
        for sensor_id, value in sorted(merged.items()):
            sensors.append({
                "id": sensor_id,
                "value": value,
                "unit": sensor_unit(sensor_id),
            })

        devices.append({
            "mac": device_mac,
            "sensors": sensors,
            "_observed_at": observed_at,
        })

    return {"devices": devices}


@dataclass
class CsvImportResult:
    """Результат импорта CSV в БД."""
    inserted_batches: int = 0
    inserted_rows: int = 0
    skipped_batches: int = 0  # дубликаты (уже есть batch на это время)
    error: str | None = None


def save_csv_import(
    rows: list[CsvRow],
    device_mac: str,
    conn: sqlite3.Connection | None = None,
) -> CsvImportResult:
    """Сохраняет распарсенные CSV-строки в БД.

    Для каждой строки создаётся ingest_batch + observations.
    Дубликаты (batch с тем же mac и received_at) пропускаются.
    """
    result = CsvImportResult()

    with use_connection(conn) as connection:
        for csv_row in rows:
            timestamp = datetime.fromtimestamp(csv_row.unixtime, tz=UTC).replace(microsecond=0)
            ts_iso = timestamp.isoformat()

            # Проверяем дубликат: batch с тем же mac и received_at
            existing = connection.execute(
                "SELECT id FROM ingest_batches WHERE device_mac = ? AND received_at = ?",
                (device_mac, ts_iso),
            ).fetchone()
            if existing:
                result.skipped_batches += 1
                continue

            # Создаём batch
            payload_json = json.dumps({
                "mac": device_mac,
                "sensors": [
                    {"id": sid, "value": val}
                    for sid, val in sorted(csv_row.readings.items())
                ],
            }, ensure_ascii=False)

            batch_cursor = connection.execute(
                """
                INSERT INTO ingest_batches(device_mac, received_at, payload_json)
                VALUES (?, ?, ?)
                """,
                (device_mac, ts_iso, payload_json),
            )
            batch_id = int(batch_cursor.lastrowid)
            result.inserted_batches += 1

            # Создаём observations
            for sensor_id, value in sorted(csv_row.readings.items()):
                connection.execute(
                    """
                    INSERT INTO observations(
                        batch_id, device_mac, observed_at, sensor_id, sensor_name, value, unit
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        device_mac,
                        ts_iso,
                        sensor_id,
                        sensor_label(sensor_id),
                        value,
                        sensor_unit(sensor_id),
                    ),
                )
                result.inserted_rows += 1

    if result.inserted_batches > 0:
        cache_invalidate()

    return result
