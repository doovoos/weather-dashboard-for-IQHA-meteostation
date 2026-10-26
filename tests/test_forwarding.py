"""Tests for app.forwarding module."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db
from app.forwarding import forward_raw_payload
from app import settings as app_settings


def _create_test_db() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = lambda cursor, row: {col[0]: row[i] for i, col in enumerate(cursor.description)}
    init_db(Path(tmp.name))
    return conn


class _Collector(BaseHTTPRequestHandler):
    received: list[bytes] = []

    def do_POST(self):  # noqa: N802 — stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Collector.received.append(body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _run_collector_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/ingest"
    return server, url


def main() -> None:
    conn = _create_test_db()

    # 1. Пересылка выключена — ничего не отправляется
    _Collector.received.clear()
    body = json.dumps({"devices": [{"mac": "AA:BB", "sensors": []}]}).encode()
    assert forward_raw_payload(body, conn) is False
    assert _Collector.received == []

    # 2. Включена с некорректным URL — не падает, ничего не отправляется
    app_settings.set_value("FORWARD_RAW_ENABLED", "1", conn=conn)
    app_settings.set_value("FORWARD_RAW_URL", "ftp://example.com", conn=conn)
    assert forward_raw_payload(body, conn) is False
    assert _Collector.received == []

    # 3. Включена с рабочим URL — пакет доставлен как есть
    server, url = _run_collector_server()
    try:
        app_settings.set_value("FORWARD_RAW_URL", url, conn=conn)
        assert forward_raw_payload(body, conn) is True
        assert _Collector.received == [body], "raw body must be forwarded unchanged"
    finally:
        server.shutdown()

    # 4. Недоступный адрес — не бросает исключений
    app_settings.set_value("FORWARD_RAW_URL", "http://127.0.0.1:1/nope", conn=conn)
    app_settings.set_value("FORWARD_RAW_TIMEOUT_SECONDS", "1", conn=conn)
    assert forward_raw_payload(body, conn) is True  # попытка была, ошибки проглочены

    # 5. Пустой URL при включённой пересылке — пропускаем тихо
    app_settings.set_value("FORWARD_RAW_URL", "", conn=conn)
    assert forward_raw_payload(body, conn) is False

    conn.close()
    print("test_forwarding: OK")


if __name__ == "__main__":
    main()
