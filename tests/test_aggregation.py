"""Tests for app.aggregation module."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.aggregation import aggregate_old_data
from app.db import init_db, save_payload


def _create_test_db() -> sqlite3.Connection:
    """Create a temporary test database with schema."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = lambda cursor, row: {col[0]: row[i] for i, col in enumerate(cursor.description)}
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(Path(tmp.name))
    return conn


def _insert_old_data(conn: sqlite3.Connection, mac: str, hours_ago: int, readings_per_hour: int = 12):
    """Insert test data for a given number of hours ago."""
    now = datetime.now(UTC)
    base_time = now - timedelta(hours=hours_ago)

    for hour_offset in range(readings_per_hour):
        ts = base_time + timedelta(minutes=5 * hour_offset)
        payload = {
            "devices": [{
                "mac": mac,
                "sensors": [
                    {"id": "T1", "value": 20.0 + hour_offset},
                    {"id": "RH", "value": 50.0 + hour_offset},
                ],
            }],
        }
        save_payload(payload, received_at=ts, conn=conn)
        conn.commit()


def test_aggregation_basic():
    """Test basic aggregation: old data gets aggregated into hourly averages."""
    conn = _create_test_db()
    mac = "TEST_MAC_001"

    # Insert data 50 days ago (should be aggregated)
    _insert_old_data(conn, mac, hours_ago=50 * 24, readings_per_hour=12)

    # Count before aggregation
    before_batches = conn.execute("SELECT COUNT(*) AS cnt FROM ingest_batches").fetchone()["cnt"]
    before_obs = conn.execute("SELECT COUNT(*) AS cnt FROM observations").fetchone()["cnt"]
    assert before_batches == 12
    assert before_obs == 24  # 12 batches × 2 sensors

    # Run aggregation with 45-day threshold
    result = aggregate_old_data(conn, max_age_days=45)

    assert result.error is None
    assert result.deleted_batches == 12
    assert result.deleted_rows == 24
    assert result.created_batches > 0
    assert result.created_rows > 0

    # Verify aggregated batches have _aggregated flag
    agg_batches = conn.execute(
        "SELECT payload_json FROM ingest_batches"
    ).fetchall()
    for b in agg_batches:
        payload = json.loads(b["payload_json"])
        assert payload.get("_aggregated") is True

    # Verify observations are hourly averages
    obs = conn.execute(
        "SELECT sensor_id, value FROM observations ORDER BY observed_at"
    ).fetchall()
    # Should have 2 sensors × number of unique hours
    assert len(obs) > 0
    assert all(o["sensor_id"] in ("T1", "RH") for o in obs)

    conn.close()


def test_aggregation_skips_recent_data():
    """Data newer than threshold should not be aggregated."""
    conn = _create_test_db()
    mac = "TEST_MAC_002"

    # Insert data 10 days ago (should NOT be aggregated)
    _insert_old_data(conn, mac, hours_ago=10 * 24, readings_per_hour=12)

    before_batches = conn.execute("SELECT COUNT(*) AS cnt FROM ingest_batches").fetchone()["cnt"]
    assert before_batches == 12

    result = aggregate_old_data(conn, max_age_days=45)

    assert result.error is None
    assert result.deleted_batches == 0
    assert result.created_batches == 0

    # Data should be unchanged
    after_batches = conn.execute("SELECT COUNT(*) AS cnt FROM ingest_batches").fetchone()["cnt"]
    assert after_batches == 12

    conn.close()


def test_aggregation_mixed_ages():
    """Only old data should be aggregated, recent data stays intact."""
    conn = _create_test_db()
    mac_old = "TEST_MAC_OLD"
    mac_new = "TEST_MAC_NEW"

    # Old data (60 days ago)
    _insert_old_data(conn, mac_old, hours_ago=60 * 24, readings_per_hour=6)
    # Recent data (5 days ago)
    _insert_old_data(conn, mac_new, hours_ago=5 * 24, readings_per_hour=6)

    total_before = conn.execute("SELECT COUNT(*) AS cnt FROM ingest_batches").fetchone()["cnt"]
    assert total_before == 12  # 6 old + 6 new

    result = aggregate_old_data(conn, max_age_days=45)

    assert result.error is None
    assert result.deleted_batches == 6  # only old ones
    assert result.created_batches > 0

    # Recent batches should still exist and NOT be aggregated
    recent = conn.execute(
        "SELECT payload_json FROM ingest_batches WHERE device_mac = ?",
        (mac_new,),
    ).fetchall()
    assert len(recent) == 6
    for b in recent:
        payload = json.loads(b["payload_json"])
        assert not payload.get("_aggregated")

    conn.close()


def test_aggregation_no_data():
    """Empty database should not cause errors."""
    conn = _create_test_db()
    result = aggregate_old_data(conn, max_age_days=45)
    assert result.error is None
    assert result.deleted_batches == 0
    assert result.created_batches == 0
    conn.close()


def test_aggregation_idempotent():
    """Running aggregation twice should not re-aggregate already aggregated data."""
    conn = _create_test_db()
    mac = "TEST_MAC_IDEMPOTENT"

    _insert_old_data(conn, mac, hours_ago=50 * 24, readings_per_hour=12)

    # First run
    result1 = aggregate_old_data(conn, max_age_days=45)
    assert result1.error is None
    assert result1.deleted_batches == 12

    # Second run — should find nothing to aggregate
    result2 = aggregate_old_data(conn, max_age_days=45)
    assert result2.error is None
    assert result2.deleted_batches == 0
    assert result2.created_batches == 0

    conn.close()


def test_aggregation_hourly_averages_correct():
    """Verify that hourly averages are computed correctly."""
    conn = _create_test_db()
    mac = "TEST_MAC_AVG"

    # Insert data for exactly 1 hour, 12 readings (every 5 min)
    now = datetime.now(UTC)
    # Align to hour boundary so all 12 readings (0-55 min) fit in one hour
    base_time = (now - timedelta(days=50)).replace(minute=0, second=0, microsecond=0)

    for i in range(12):
        ts = base_time + timedelta(minutes=5 * i)
        payload = {
            "devices": [{
                "mac": mac,
                "sensors": [
                    {"id": "T1", "value": 10.0 + i},  # 10, 11, ..., 21 → avg = 15.5
                ],
            }],
        }
        save_payload(payload, received_at=ts, conn=conn)
        conn.commit()

    result = aggregate_old_data(conn, max_age_days=45)
    assert result.error is None

    # Check the averaged value
    obs = conn.execute(
        "SELECT value FROM observations WHERE sensor_id = 'T1'"
    ).fetchall()
    assert len(obs) == 1
    assert abs(obs[0]["value"] - 15.5) < 0.01

    conn.close()


if __name__ == "__main__":
    test_aggregation_basic()
    print("  test_aggregation_basic OK")
    test_aggregation_skips_recent_data()
    print("  test_aggregation_skips_recent_data OK")
    test_aggregation_mixed_ages()
    print("  test_aggregation_mixed_ages OK")
    test_aggregation_no_data()
    print("  test_aggregation_no_data OK")
    test_aggregation_idempotent()
    print("  test_aggregation_idempotent OK")
    test_aggregation_hourly_averages_correct()
    print("  test_aggregation_hourly_averages_correct OK")
    print("\nAll aggregation tests passed!")
