"""Tests for app.csv_import module."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.csv_import import CsvRow, parse_csv, save_csv_import, CsvImportResult

SAMPLE_CSV = """\
UNIXTIME;Дата;Время;Плотн воздха;Коэф смешивания;Дефиц давл пара;Давление;Влажность;Абс. влажность;VOLT;Точка росы;Ощущаем темп-ру;T5;Температура
1784664000;22.07.2026;00:00:00;1.2;12.47;0;757.95;100;14.84;12.15;17.43;19.98;47.78;17.43
1784664300;22.07.2026;00:05:00;1.2;12.3;0;757.88;100;14.65;12.15;17.21;19.68;47.22;17.22
1784664600;22.07.2026;00:10:00;1.2;12.16;0;757.88;100;14.5;12.15;17.04;19.44;47.22;17.04
"""


def test_parse_csv_basic():
    result = parse_csv(SAMPLE_CSV)
    assert result.error is None
    assert len(result.rows) == 3
    assert result.skipped_rows == []

    # Check column detection
    assert "Температура" in result.columns_found
    assert "Давление" in result.columns_found
    assert "Температура" in result.columns_mapped
    assert "Давление" in result.columns_mapped

    # Check first row data
    row = result.rows[0]
    assert row.unixtime == 1784664000
    assert "T1" in row.readings
    assert row.readings["T1"] == 17.43
    assert row.readings["PRESS"] == 757.95
    assert row.readings["RH"] == 100.0
    assert row.readings["VOLT"] == 12.15
    assert row.readings["DEW"] == 17.43
    assert row.readings["T5"] == 47.78


def test_parse_csv_skips_empty_rows():
    csv_text = """\
UNIXTIME;Температура;Давление
1784664000;17.43;757.95
;bad;row
1784664600;17.04;757.88
"""
    result = parse_csv(csv_text)
    assert result.error is None
    assert len(result.rows) == 2
    assert 3 in result.skipped_rows  # line 3 (1-based) is the bad row


def test_parse_csv_skips_unknown_columns():
    csv_text = """\
UNIXTIME;Дата;Время;Неизвестная колонка
1784664000;22.07.2026;00:00:00;42
"""
    result = parse_csv(csv_text)
    assert result.error is None
    assert len(result.rows) == 0  # no mappable sensors → row skipped
    assert "Неизвестная колонка" not in result.columns_mapped


def test_parse_csv_handles_cp1251_names():
    """Verify Cyrillic column names are matched correctly."""
    csv_text = """\
UNIXTIME;Давление;Влажность;Точка росы
1784664000;757.95;100;17.43
"""
    result = parse_csv(csv_text)
    assert result.error is None
    assert len(result.rows) == 1
    assert result.rows[0].readings == {
        "PRESS": 757.95,
        "RH": 100.0,
        "DEW": 17.43,
    }


def test_parse_csv_invalid_unixtime():
    csv_text = """\
UNIXTIME;Температура
not_a_number;17.43
1784664000;17.43
"""
    result = parse_csv(csv_text)
    assert result.error is None
    assert len(result.rows) == 1
    assert result.rows[0].readings["T1"] == 17.43


def test_parse_csv_empty_input():
    result = parse_csv("")
    assert result.error is None
    assert len(result.rows) == 0


def test_parse_csv_error_handling():
    """Passing non-string should not crash."""
    result = parse_csv(None)  # type: ignore[arg-type]
    # io.StringIO(None) creates empty buffer → no rows, no error
    assert result.error is None or isinstance(result.error, str)


if __name__ == "__main__":
    test_parse_csv_basic()
    test_parse_csv_skips_empty_rows()
    test_parse_csv_skips_unknown_columns()
    test_parse_csv_handles_cp1251_names()
    test_parse_csv_invalid_unixtime()
    test_parse_csv_empty_input()
    test_parse_csv_error_handling()
    print("All tests passed!")
