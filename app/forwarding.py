"""Пересылка сырых пакетов ingest на внешний адрес.

Каждый пакет, успешно принятый /api/ingest, может дублироваться POST-запросом
с исходным телом на адрес из настройки FORWARD_RAW_URL. В отличие от публикации
во внешние погодные сервисы (со своими форматами), здесь пересылаются
исходные значения как есть.

Пересылка выполняется в фоне и не влияет на ответ станции: ошибки сети,
таймауты и плохие статусы логируются, но не пробрасываются наверх.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

from loguru import logger

from . import settings as app_settings

USER_AGENT = "weather-dashboard-forwarder/1.0"


def _forward_sync(body: bytes, url: str, timeout: int) -> None:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            if status >= 400:
                detail = response.read(512).decode("utf-8", errors="replace").strip()
                logger.warning("Forward: {} ответил статусом {} {}", url, status, detail)
            else:
                logger.debug("Forward: {} пакет ({} байт) отправлен, статус {}", url, len(body), status)
    except urllib.error.HTTPError as err:
        detail = err.read(512).decode("utf-8", errors="replace").strip()
        logger.warning("Forward: {} вернул HTTP {}: {} {}", url, err.code, err.reason, detail)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        logger.warning("Forward: не удалось отправить пакет на {}: {}", url, err)


def forward_raw_payload(body: bytes, conn: Any = None) -> bool:
    """Синхронно переслать исходное тело пакета, если пересылка включена.

    Возвращает True, если пересылка была выполнена (без гарантии успеха
    на стороне получателя — ошибки логируются). Никогда не бросает исключений.
    """
    try:
        if not app_settings.get_bool("FORWARD_RAW_ENABLED", conn=conn):
            return False
        url = (app_settings.get_string("FORWARD_RAW_URL", conn=conn) or "").strip()
        if not url:
            return False
        if not url.lower().startswith(("http://", "https://")):
            logger.warning("Forward: некорректный адрес пересылки {!r} (нужен http/https), пропускаю", url)
            return False
        timeout = max(1, app_settings.get_int("FORWARD_RAW_TIMEOUT_SECONDS", conn=conn) or 5)
        _forward_sync(body, url, timeout)
        return True
    except Exception as err:  # pragma: no cover — страховка на случай неожиданных ошибок
        logger.warning("Forward: неожиданная ошибка пересылки: {}", err)
        return False
