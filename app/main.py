from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hmac
import json
import sqlite3

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

from . import csv_import, settings, stations as stations_repo
from .auth import admin_enabled, require_admin
from .config import (
    INGEST_MAX_BODY_BYTES,
    INGEST_MAX_DEVICES,
    INGEST_MAX_SENSORS_PER_DEVICE,
    INGEST_TOKEN,
    WEATHER_TIMEZONE,
    env_settings_dict,
)
from .db import (
    HUMIDITY_SENSOR_IDS,
    TEMPERATURE_SENSOR_IDS,
    db_dependency,
    get_anomaly_calendar,
    get_chart_series,
    get_comfort_risk,
    get_history_for_date,
    get_latest_snapshot,
    get_period_comparison,
    get_station_status,
    get_temperature_heatmap,
    get_today_extremes,
    get_today_temperature_extremes,
    get_uptime_monitor,
    init_db,
    save_payload,
)
from .logging_setup import setup_logging
from .schemas import (
    AnomalyCalendar,
    ChartSeries,
    ComfortRisk,
    CurrentResponse,
    Heatmap,
    IngestResult,
    PeriodComparison,
    StationStatus,
    StatusOk,
    UptimeMonitor,
)


app = FastAPI(title="Weather Station", version="1.2.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Lazy timezone resolution: try to get from DB settings, fallback to env
def _get_app_tz() -> ZoneInfo:
    """Get timezone from DB settings if available, otherwise use env default."""
    try:
        tz_name = settings.get_string("WEATHER_TIMEZONE")
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass  # DB not ready yet or settings not seeded
    try:
        return ZoneInfo(WEATHER_TIMEZONE)
    except ZoneInfoNotFoundError:
        return UTC

APP_TZ = _get_app_tz()


def _site_globals() -> dict[str, Any]:
    """Текущие user-facing значения для шаблонов. Читаются из app_settings."""
    yandex_id = settings.get_string("YANDEX_METRIKA_ID").strip()
    time_format = settings.get_string("TIME_FORMAT").strip().lower()
    return {
        "app_title": settings.get_string("APP_TITLE"),
        "site_brand": settings.get_string("SITE_BRAND"),
        "yandex_metrika_id": yandex_id,
        "yandex_metrika_enabled": bool(yandex_id),
        "admin_enabled": admin_enabled(),
        "time_format": time_format if time_format in ("12h", "24h") else "24h",
        "weather_timezone": settings.get_string("WEATHER_TIMEZONE"),
    }


def render_template(request: Request, template_name: str, context: dict[str, Any]) -> HTMLResponse:
    full_context = {"request": request, **_site_globals(), **context}
    try:
        # Starlette/FastAPI with request-first TemplateResponse signature.
        return templates.TemplateResponse(request=request, name=template_name, context=full_context)
    except TypeError:
        # Backward compatibility for name-first signature.
        return templates.TemplateResponse(template_name, full_context)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    inserted = settings.seed_defaults_if_empty(env_settings_dict())
    setup_logging("web")  # Call after DB init and seeding
    if inserted:
        logger.info("Seeded {} app_settings rows from env defaults", inserted)
    logger.info("Web service started")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("HTTP {} {}", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error on {} {}", request.method, request.url.path)
        raise
    logger.info("HTTP {} {} -> {}", request.method, request.url.path, response.status_code)
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    station: str | None = None,
    conn: sqlite3.Connection = Depends(db_dependency),
) -> HTMLResponse:
    enabled_stations = stations_repo.list_enabled(conn=conn)
    active_station: dict[str, Any] | None = None
    if station:
        # Ищем по mac среди включённых.
        active_station = next((s for s in enabled_stations if s["mac"] == station), None)
    if active_station is None and enabled_stations:
        # Primary first, иначе первая enabled.
        active_station = next((s for s in enabled_stations if s.get("is_primary")), enabled_stations[0])
    active_mac = active_station["mac"] if active_station else None

    snapshot = get_latest_snapshot(conn=conn, station_mac=active_mac)
    uptime = get_uptime_monitor(24, conn=conn)
    temp_extremes = get_today_extremes(TEMPERATURE_SENSOR_IDS, default_unit="°C", conn=conn)
    humidity_extremes = get_today_extremes(HUMIDITY_SENSOR_IDS, default_unit="%", conn=conn)
    comfort = get_comfort_risk(snapshot, conn=conn)
    comparison = get_period_comparison(conn=conn)
    chart_series = get_chart_series(days=1, conn=conn)
    return render_template(
        request,
        "index.html",
        {
            "snapshot": snapshot,
            "uptime": uptime,
            "temp_extremes": temp_extremes,
            "humidity_extremes": humidity_extremes,
            "comfort": comfort,
            "comparison": comparison,
            "chart_series": chart_series,
            "stations": enabled_stations,
            "active_station": active_station,
        },
    )


@app.get("/charts", response_class=HTMLResponse)
def charts_page(request: Request, days: int = 1) -> HTMLResponse:
    period = max(1, min(days, 90))
    return render_template(request, "charts.html", {"days": period})


@app.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    day: str | None = None,
    conn: sqlite3.Connection = Depends(db_dependency),
) -> HTMLResponse:
    selected_day = day or datetime.now(APP_TZ).date().isoformat()
    items = get_history_for_date(selected_day, conn=conn)
    return render_template(request, "history.html", {"selected_day": selected_day, "items": items})


@app.get("/station", response_class=HTMLResponse)
def station_page(request: Request, conn: sqlite3.Connection = Depends(db_dependency)) -> HTMLResponse:
    status = get_station_status(conn=conn)
    return render_template(request, "station.html", {"status": status})


@app.post("/api/ingest", response_model=IngestResult)
async def ingest(request: Request, conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    if INGEST_TOKEN:
        provided = request.headers.get("X-Ingest-Token", "")
        if not hmac.compare_digest(provided, INGEST_TOKEN):
            logger.warning("Ingest rejected: bad or missing X-Ingest-Token from {}", request.client.host if request.client else "?")
            raise HTTPException(status_code=401, detail="Invalid ingest token.")

    body = await request.body()
    if len(body) > INGEST_MAX_BODY_BYTES:
        logger.warning("Ingest rejected: body size {} > limit {}", len(body), INGEST_MAX_BODY_BYTES)
        raise HTTPException(status_code=413, detail=f"Payload too large (limit {INGEST_MAX_BODY_BYTES} bytes).")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {err}") from err

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object.")

    devices = payload.get("devices")
    if not isinstance(devices, list) or not devices:
        raise HTTPException(status_code=400, detail="Payload must contain a non-empty 'devices' array.")

    if len(devices) > INGEST_MAX_DEVICES:
        raise HTTPException(status_code=400, detail=f"Too many devices in payload (max {INGEST_MAX_DEVICES}).")

    auto_register = settings.get_bool("AUTO_REGISTER_STATIONS", conn=conn)
    for device in devices:
        sensors = device.get("sensors") or []
        if not isinstance(sensors, list):
            raise HTTPException(status_code=400, detail="device.sensors must be an array.")
        if len(sensors) > INGEST_MAX_SENSORS_PER_DEVICE:
            raise HTTPException(status_code=400, detail=f"Too many sensors in one device (max {INGEST_MAX_SENSORS_PER_DEVICE}).")

        mac = str(device.get("mac") or "").strip()
        if not mac:
            raise HTTPException(status_code=400, detail="device.mac is required.")

        station = stations_repo.get_by_mac(mac, conn=conn)
        if station is None:
            if auto_register:
                station = stations_repo.upsert_unknown(mac, conn=conn)
                # Коммитим явно: HTTPException ниже отменит коммит из
                # db_dependency, а нам нужно, чтобы факт регистрации mac
                # остался в БД и владелец увидел его в /admin/stations.
                conn.commit()
                logger.info("Auto-registered new station {} (disabled until owner enables it)", mac)
                raise HTTPException(
                    status_code=403,
                    detail=f"Station {mac} registered but disabled. Enable it in /admin/stations.",
                )
            logger.warning("Ingest rejected: unknown mac {!r}", mac)
            raise HTTPException(status_code=403, detail=f"Station {mac} is not registered.")
        if not station.get("enabled"):
            logger.warning("Ingest rejected: mac {!r} is disabled", mac)
            raise HTTPException(status_code=403, detail=f"Station {mac} is disabled.")

    result = save_payload(payload, conn=conn)
    return {"status": "ok", **result}


@app.get("/api/status", response_model=StatusOk)
def api_status() -> dict[str, Any]:
    return {"status": "ok"}


# ---------- Admin (HTTP Basic Auth) ----------

@app.get("/admin/whoami")
def admin_whoami(user: str = Depends(require_admin)) -> dict[str, Any]:
    return {"user": user, "admin_enabled": admin_enabled()}


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_root(_: str = Depends(require_admin)) -> RedirectResponse:
    return RedirectResponse(url="/admin/settings", status_code=302)


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_get(
    request: Request,
    saved: int = 0,
    _: str = Depends(require_admin),
    conn: sqlite3.Connection = Depends(db_dependency),
) -> HTMLResponse:
    flash = {"tone": "good", "text": "Настройки сохранены."} if saved else None
    return render_template(
        request,
        "admin/settings.html",
        {
            "schema": list(settings.SETTINGS_SCHEMA),
            "sections": settings.sections(),
            "values": settings.all_values(conn=conn),
            "flash": flash,
        },
    )


@app.post("/admin/settings", response_class=HTMLResponse)
async def admin_settings_post(
    request: Request,
    _: str = Depends(require_admin),
    conn: sqlite3.Connection = Depends(db_dependency),
) -> RedirectResponse:
    form = await request.form()
    submitted: dict[str, str] = {}
    for spec in settings.SETTINGS_SCHEMA:
        if spec.type == "bool":
            submitted[spec.key] = "1" if form.get(spec.key) else "0"
        else:
            value = str(form.get(spec.key, "")).strip()
            submitted[spec.key] = value
    settings.set_many(submitted, conn=conn)
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@app.get("/admin/stations", response_class=HTMLResponse)
def admin_stations_get(
    request: Request,
    saved: str | None = None,
    _: str = Depends(require_admin),
    conn: sqlite3.Connection = Depends(db_dependency),
) -> HTMLResponse:
    flash = None
    if saved == "created":
        flash = {"tone": "good", "text": "Станция добавлена."}
    elif saved == "updated":
        flash = {"tone": "good", "text": "Станция обновлена."}
    elif saved == "deleted":
        flash = {"tone": "good", "text": "Станция удалена."}
    elif saved == "exists":
        flash = {"tone": "warn", "text": "Станция с таким mac уже есть."}
    elif saved == "error":
        flash = {"tone": "bad", "text": "Не удалось сохранить — проверьте поля."}
    return render_template(
        request,
        "admin/stations.html",
        {"stations": stations_repo.list_stations(conn=conn), "flash": flash},
    )


@app.post("/admin/stations", response_class=HTMLResponse)
async def admin_stations_post(
    request: Request,
    _: str = Depends(require_admin),
    conn: sqlite3.Connection = Depends(db_dependency),
) -> RedirectResponse:
    form = await request.form()
    action = str(form.get("action") or "").strip()
    try:
        if action == "create":
            mac = str(form.get("mac") or "").strip()
            existing = stations_repo.get_by_mac(mac, conn=conn) if mac else None
            if existing:
                return RedirectResponse(url="/admin/stations?saved=exists", status_code=303)
            stations_repo.create(
                mac=mac,
                name=str(form.get("name") or "").strip(),
                sensor=str(form.get("sensor") or "").strip(),
                location=str(form.get("location") or "").strip(),
                enabled=bool(form.get("enabled")),
                conn=conn,
            )
            return RedirectResponse(url="/admin/stations?saved=created", status_code=303)

        if action == "update":
            station_id = int(str(form.get("id") or "0"))
            stations_repo.update(
                station_id,
                name=str(form.get("name") or "").strip(),
                sensor=str(form.get("sensor") or "").strip(),
                location=str(form.get("location") or "").strip(),
                enabled=bool(form.get("enabled")),
                is_primary=bool(form.get("is_primary")),
                conn=conn,
            )
            return RedirectResponse(url="/admin/stations?saved=updated", status_code=303)

        if action == "delete":
            station_id = int(str(form.get("id") or "0"))
            stations_repo.delete(station_id, conn=conn)
            return RedirectResponse(url="/admin/stations?saved=deleted", status_code=303)
    except (ValueError, TypeError) as err:
        logger.warning("Admin stations form error: {}", err)
        return RedirectResponse(url="/admin/stations?saved=error", status_code=303)

    return RedirectResponse(url="/admin/stations", status_code=303)


# ---------- CSV Import (admin) ----------

# In-memory store for preview data between GET→POST cycle.
# Key: preview token (str), Value: dict with parsed rows and metadata.
_preview_store: dict[str, dict[str, Any]] = {}
_PREVIEW_MAX_ENTRIES = 20


def _store_preview(data: dict[str, Any]) -> str:
    """Store preview data, evict oldest if over limit. Returns token."""
    import uuid
    if len(_preview_store) >= _PREVIEW_MAX_ENTRIES:
        oldest_key = next(iter(_preview_store))
        del _preview_store[oldest_key]
    token = uuid.uuid4().hex[:12]
    _preview_store[token] = data
    return token


@app.get("/admin/import", response_class=HTMLResponse)
def admin_import_get(
    request: Request,
    imported: str | None = None,
    _: str = Depends(require_admin),
    conn: sqlite3.Connection = Depends(db_dependency),
) -> HTMLResponse:
    flash = None
    if imported == "ok":
        flash = {"tone": "good", "text": "Данные успешно импортированы."}
    elif imported == "empty":
        flash = {"tone": "warn", "text": "CSV-файл не содержит данных для импорта."}
    elif imported == "error":
        flash = {"tone": "bad", "text": "Ошибка при импорте — проверьте файл."}
    return render_template(
        request,
        "admin/import.html",
        {
            "stations": stations_repo.list_stations(conn=conn),
            "flash": flash,
            "preview": None,
        },
    )


@app.post("/admin/import", response_class=HTMLResponse)
async def admin_import_post(
    request: Request,
    _: str = Depends(require_admin),
    conn: sqlite3.Connection = Depends(db_dependency),
) -> HTMLResponse:
    form = await request.form()
    action = str(form.get("action") or "").strip()

    # --- Action: import (from preview) ---
    if action == "import":
        token = str(form.get("preview_token") or "").strip()
        preview_data = _preview_store.pop(token, None)
        if not preview_data:
            return render_template(
                request, "admin/import.html",
                {
                    "stations": stations_repo.list_stations(conn=conn),
                    "flash": {"tone": "bad", "text": "Данные предпросмотра устарели. Загрузите файл заново."},
                    "preview": None,
                },
            )
        rows = preview_data["rows"]
        device_mac = preview_data["device_mac"]

        # Читаем пользовательский маппинг из формы: mapping_<csv_col> = sensor_id
        custom_mapping: dict[str, str] = {}
        for key, value in form.multi_items():
            if key.startswith("mapping_"):
                csv_col = key[len("mapping_"):]
                sensor_id = str(value).strip()
                if csv_col and sensor_id:
                    custom_mapping[csv_col] = sensor_id

        if custom_mapping:
            rows = csv_import.remap_rows(rows, custom_mapping)

        if not rows:
            return RedirectResponse(url="/admin/import?imported=empty", status_code=303)
        result = csv_import.save_csv_import(rows, device_mac, conn=conn)
        logger.info(
            "CSV import: mac={}, inserted_batches={}, inserted_rows={}, skipped={}",
            device_mac, result.inserted_batches, result.inserted_rows, result.skipped_batches,
        )
        return RedirectResponse(url="/admin/import?imported=ok", status_code=303)

    # --- Action: preview (file upload) ---
    upload: UploadFile | None = form.get("file")  # type: ignore[assignment]
    device_mac = str(form.get("device_mac") or "").strip()
    if not upload or not upload.filename:
        return render_template(
            request, "admin/import.html",
            {
                "stations": stations_repo.list_stations(conn=conn),
                "flash": {"tone": "bad", "text": "Выберите CSV-файл для загрузки."},
                "preview": None,
            },
        )
    if not device_mac:
        return render_template(
            request, "admin/import.html",
            {
                "stations": stations_repo.list_stations(conn=conn),
                "flash": {"tone": "bad", "text": "Выберите станцию для импорта."},
                "preview": None,
            },
        )

    raw_bytes = await upload.read()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("cp1251")
        except UnicodeDecodeError:
            return render_template(
                request, "admin/import.html",
                {
                    "stations": stations_repo.list_stations(conn=conn),
                    "flash": {"tone": "bad", "text": "Не удалось прочитать файл: неверная кодировка."},
                    "preview": None,
                },
            )

    parse_result = csv_import.parse_csv(text)
    if parse_result.error:
        return render_template(
            request, "admin/import.html",
            {
                "stations": stations_repo.list_stations(conn=conn),
                "flash": {"tone": "bad", "text": parse_result.error},
                "preview": None,
            },
        )
    if not parse_result.rows:
        return render_template(
            request, "admin/import.html",
            {
                "stations": stations_repo.list_stations(conn=conn),
                "flash": {"tone": "warn", "text": "CSV-файл не содержит валидных строк для импорта."},
                "preview": None,
            },
        )

    # Store preview data and render preview page
    token = _store_preview({
        "rows": parse_result.rows,
        "device_mac": device_mac,
    })

    # Build preview rows for template (limit to first 50)
    from .db import to_local_timestamp
    preview_rows = []
    for row in parse_result.rows[:50]:
        try:
            local_dt = to_local_timestamp(row.observed_at)
            time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            time_str = row.observed_at
        preview_rows.append({
            "time": time_str,
            "readings": [
                {"sensor_id": sid, "value": val}
                for sid, val in sorted(row.readings.items())
            ],
        })

    station = stations_repo.get_by_mac(device_mac, conn=conn)
    station_name = station["name"] if station else device_mac

    # Маппинг CSV-колонка → sensor_id для шаблона предпросмотра
    # Включаем ВСЕ колонки из CSV, а не только авто-распознанные
    auto_mapping = csv_import._CSV_COLUMN_TO_SENSOR_ID
    col_to_sensor = {}
    for col in parse_result.columns_found:
        if col in ("UNIXTIME", "Дата", "Время"):
            continue
        col_to_sensor[col] = auto_mapping.get(col, "")

    # Все доступные sensor_id для выпадающих списков
    from .sensor_map import SENSOR_MAP
    available_sensors = sorted(SENSOR_MAP.keys())

    # Все колонки для отображения (исключая служебные)
    data_columns = [c for c in parse_result.columns_found if c not in ("UNIXTIME", "Дата", "Время")]
    logger.info("CSV import debug: columns_found={!r}, data_columns={!r}", parse_result.columns_found, data_columns)

    return render_template(
        request,
        "admin/import.html",
        {
            "stations": stations_repo.list_stations(conn=conn),
            "flash": None,
            "preview": {
                "token": token,
                "filename": upload.filename,
                "station_mac": device_mac,
                "station_name": station_name,
                "total_rows": len(parse_result.rows),
                "skipped_count": len(parse_result.skipped_rows),
                "columns_found": parse_result.columns_found,
                "columns_mapped": parse_result.columns_mapped,
                "col_to_sensor": col_to_sensor,
                "available_sensors": available_sensors,
                "data_columns": data_columns,
                "rows": preview_rows,
                "truncated": len(parse_result.rows) > 50,
            },
        },
    )


@app.get("/api/current", response_model=CurrentResponse)
def current_weather(conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    snapshot = get_latest_snapshot(conn=conn)
    if not snapshot:
        return {"status": "empty", "snapshot": None}
    return {"status": "ok", "snapshot": snapshot}


@app.get("/api/chart-data", response_model=ChartSeries)
def chart_data(
    days: int | None = None,
    hours: int | None = None,
    conn: sqlite3.Connection = Depends(db_dependency),
) -> dict[str, Any]:
    return get_chart_series(days=days, hours=hours, conn=conn)


@app.get("/api/uptime", response_model=UptimeMonitor)
def api_uptime(hours: int = 24, conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    return get_uptime_monitor(hours, conn=conn)


@app.get("/api/comfort-risk", response_model=ComfortRisk)
def api_comfort_risk(conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    return get_comfort_risk(conn=conn)


@app.get("/api/period-comparison", response_model=PeriodComparison)
def api_period_comparison(conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    return get_period_comparison(conn=conn)


@app.get("/api/temperature-heatmap", response_model=Heatmap)
def api_temperature_heatmap(days: int = 30, conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    return get_temperature_heatmap(days, conn=conn)


@app.get("/api/anomaly-calendar", response_model=AnomalyCalendar)
def api_anomaly_calendar(
    month: str | None = None,
    conn: sqlite3.Connection = Depends(db_dependency),
) -> dict[str, Any]:
    return get_anomaly_calendar(month, conn=conn)


@app.get("/api/station-status", response_model=StationStatus)
def api_station_status(conn: sqlite3.Connection = Depends(db_dependency)) -> dict[str, Any]:
    return get_station_status(conn=conn)


def _favicon_temp(snapshot: dict[str, Any] | None) -> float | None:
    if not snapshot:
        return None
    for sensor_id in ("T1", "T2", "T3", "T4", "T5", "T6"):
        for reading in snapshot.get("readings", []):
            if str(reading.get("sensor_id", "")).upper() == sensor_id:
                try:
                    return float(reading["value"])
                except (TypeError, ValueError, KeyError):
                    return None
    return None


@app.get("/favicon.ico")
def favicon_ico() -> RedirectResponse:
    return RedirectResponse(url="/favicon.svg", status_code=307)


@app.get("/favicon.svg")
def favicon_svg(conn: sqlite3.Connection = Depends(db_dependency)) -> Response:
    temp = _favicon_temp(get_latest_snapshot(conn=conn))
    if temp is None:
        label = "--"
        bg = "#334155"
    elif temp < 0:
        label = f"{temp:.0f}"
        bg = "#2563eb"
    elif temp < 20:
        label = f"{temp:.0f}"
        bg = "#0ea5e9"
    elif temp < 30:
        label = f"{temp:.0f}"
        bg = "#f59e0b"
    else:
        label = f"{temp:.0f}"
        bg = "#ef4444"

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
<rect x="2" y="2" width="60" height="60" rx="14" fill="{bg}"/>
<text x="32" y="39" text-anchor="middle" font-family="Arial, sans-serif" font-size="22" font-weight="700" fill="#ffffff">{label}</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
