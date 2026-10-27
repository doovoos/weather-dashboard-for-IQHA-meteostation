# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Self-hosted weather station dashboard: ingests telemetry from a local weather station over HTTP, stores it in SQLite, and exposes a FastAPI dashboard plus a Telegram bot. UI text and most user-facing strings are in Russian. Tests live in [tests/](tests/) (run with `python tests/test_*.py` directly — no test runner framework required): `test_aggregation.py`, `test_csv_import.py`, `test_forwarding.py`, `test_zambretti.py`, `test_yandex_weather.py`.

## Run / develop

Local dev (no Docker):
```bash
python3.12 -m venv .venv
source .venv/bin/activate            # POSIX / Git Bash
pip install -r requirements.txt
cp .env.example .env                 # then fill in values
mkdir -p data logs
set -a && source .env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 18080 --proxy-headers --forwarded-allow-ips="*"
python bot.py                        # separate process for the Telegram bot
```

On Windows PowerShell, `Activate.ps1` may be blocked by the default execution policy. Skip activation and call the venv's interpreter directly — it works the same:
```powershell
$env:WEATHER_DB_PATH = "$PWD\data\weather.sqlite3"
$env:LOG_DIR = "$PWD\logs"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18080 --proxy-headers --forwarded-allow-ips="*"
```

Docker (the deploy target):
```bash
docker compose up -d --build
docker compose logs -f web
docker compose logs -f bot
```

The web container listens on `8000` internally and is published on `127.0.0.1:${WEB_BIND_PORT:-18080}`. Nginx on the host terminates TLS and proxies to that port (see `deploy/nginx/weather.conf.example`).

Quick health checks:
```bash
curl http://127.0.0.1:18080/api/status
curl http://127.0.0.1:18080/api/current
```

## Architecture

Two long-running processes share one SQLite file (`WEATHER_DB_PATH`):

- **Web** ([app/main.py](app/main.py)) — FastAPI app. Calls `init_db()` on startup, mounts `/static`, renders Jinja2 templates from [app/templates/](app/templates/), and exposes both HTML pages (`/`, `/charts`, `/history`, `/station`, `/admin/settings`, `/admin/stations`, `/admin/import`) and JSON APIs (`/api/ingest`, `/api/current`, `/api/chart-data` (accepts `days` or `hours`), `/api/uptime`, `/api/comfort-risk`, `/api/period-comparison`, `/api/temperature-heatmap` (30-day window with `offset` pagination), `/api/anomaly-calendar`, `/api/station-status`, `/api/forecast` (Zambretti), `/api/yandex-weather`). Admin-only: `POST /admin/elevation-lookup` (Open-Meteo / Google Maps Elevation API). On startup, also launches `_aggregation_loop` as a background asyncio task for periodic old-data aggregation.
- **Bot** ([bot.py](bot.py)) — `python-telegram-bot` Application. **Optional** — only needed for Telegram notifications. On start it spawns three asyncio loops alongside the polling updater: `stale_data_monitor_loop` (alerts admins when no fresh telemetry), `daily_weather_broadcast_loop` (sends snapshots at `TELEGRAM_DAILY_TIMES`), and `dynamic_bot_name_loop` (rewrites the bot's display name with the current temperature). `current_weather_text()` appends the Zambretti forecast to every weather reply. The bot reads from the same DB the web writes to — there is no IPC.

Ingest flow: a device POSTs `{"devices": [{"mac": "...", "sensors": [{"id": "T1", "value": 23.4, "unit": "°C"}, ...]}, ...]}` to `/api/ingest`. `save_payload` writes one `ingest_batches` row per device and one `observations` row per numeric sensor reading. `received_at` is the server's UTC ingest time, not a device timestamp. After a successful save, the raw body is optionally forwarded (fire-and-forget, in a background thread) to `FORWARD_RAW_URL` — see [app/forwarding.py](app/forwarding.py). Forwarding never blocks or fails the ingest; errors are logged.

### Data model

Two tables, defined inline in `init_db()` in [app/db.py](app/db.py):

- `ingest_batches(id, device_mac, received_at, payload_json)` — raw payload archive, one row per device per POST.
- `observations(id, batch_id, device_mac, observed_at, sensor_id, sensor_name, value, unit)` — flattened readings. `sensor_id` is uppercased on write; `sensor_name`/`unit` are resolved through [app/sensor_map.py](app/sensor_map.py) at write time and stored denormalized.

Indexed by `(sensor_id, observed_at DESC)` and `(device_mac, observed_at DESC)`. WAL is enabled. All timestamps are stored as ISO-8601 UTC strings; conversion to `WEATHER_TIMEZONE` happens at read time via `to_local_timestamp()`.

### app/db.py is the analytics layer

[app/db.py](app/db.py) (~1100 lines) holds **all** SQL plus the derived metrics consumed by the dashboard widgets and the bot: `get_latest_snapshot`, `get_chart_series`, `get_history_for_date`, `get_uptime_monitor`, `get_today_extremes` (generic, picks first sensor with data from a list), `get_today_temperature_extremes` (thin wrapper), `get_comfort_risk`, `get_period_comparison` (returns both day/night aggregates and `series.{today,yesterday,monthAgo}` hourly arrays for the overlay sparkline), `get_temperature_heatmap(days, offset)` (offset shifts the window into the past), `get_anomaly_calendar`, `get_station_status`, `get_zambretti_inputs` (pressure history in hPa + current temp/wind direction for the forecast), `format_telegram_snapshot`. New widgets/endpoints belong here, not in `main.py`. Sensor-ID groupings (`TEMPERATURE_SENSOR_IDS`, `HUMIDITY_SENSOR_IDS`, `PRESSURE_SENSOR_IDS`, `WIND_DIR_SENSOR_IDS`) are defined at the top of the file and used to fall back across redundant sensors (e.g. T1→T2→…) — preserve that ordering when changing them. Pressure units are converted to hPa via `_pressure_to_hpa` (mmHg × 1.33322) depending on the stored `unit`.

### Sensor catalog

[app/sensor_map.py](app/sensor_map.py) is the source of truth for human-readable sensor labels and units. `PRIMARY_SENSOR_IDS` controls which readings appear in the dashboard's "primary" row and the Telegram summary. Adding a new sensor type means adding it here; unknown IDs flow through with the raw ID as the label and no unit.

### Config and runtime

[app/config.py](app/config.py) reads everything from environment variables (no settings file). Defaults are baked in for everything except `TELEGRAM_BOT_TOKEN` (required only by the bot, which raises on startup if missing) and `WEATHER_SITE_URL` (used to render the "Open site" button in Telegram). `WEATHER_TIMEZONE` defaults to `UTC`; both `app/main.py` and `app/db.py` independently fall back to UTC if the zone can't be loaded.

[app/settings.py](app/settings.py) provides a DB-backed settings layer (`app_settings` table) editable via `/admin/settings`. Settings are defined in `SETTINGS_SCHEMA` with typed defaults, sections, and help text. Read-path uses an in-memory cache with invalidation on write. `set_value`/`set_many` **commit immediately** when passed a request connection — this is deliberate: `reconfigure_logging()` and other post-write reads go through fresh connections, and a late commit would repopulate the cache with pre-write values (a bug that made settings appear to save only on the second attempt). Sections: Общие, Telegram, Станции, Пересылка, Логирование, Агрегация (+hidden-only «Служебные» which never renders). The `hidden` type marks internal keys (caches, timestamps) — they are skipped by the admin form and never wiped by a form save. `SECTION_DESCRIPTIONS` provides the one-line blurbs shown in the collapsible section headers. The settings page itself renders collapsible `<details>` sections with a chip table of contents and a sticky save bar.

[app/logging_setup.py](app/logging_setup.py) configures Loguru per-service. Each entrypoint calls `setup_logging("web")` or `setup_logging("bot")` to write to `LOG_DIR/{web,bot}.log` with 10MB rotation and `LOGGING_RETENTION_DAYS` (default 25) day retention. Docker containers cap their json-file logs at 10m × 3 files (see `docker-compose.yml`).

### Weather forecasts

Two forecast sources power the "Прогноз погоды" row on the dashboard:

- [app/zambretti.py](app/zambretti.py) — the local Negretti & Zambra algorithm: sea-level pressure correction (station elevation = `STATION_GROUND_ELEVATION_M` + `STATION_MOUNT_HEIGHT_M`), 3-hour pressure trend from the observations history, wind-direction adjustment, and one of 26 Russian forecast texts. Requires ≥2 pressure readings spanning ≥2h, otherwise the UI shows the "not enough data" text. Elevation can be auto-detected from coordinates via `POST /admin/elevation-lookup` (Open-Meteo, or Google Maps Elevation API when `GOOGLE_MAPS_API_KEY` is set). Used by the dashboard card, `/api/forecast`, and the bot's weather replies.
- [app/yandex_weather.py](app/yandex_weather.py) — Yandex Weather GraphQL API (`https://api.weather.yandex.ru/graphql/query`, header `X-Yandex-Weather-Key`), tariff «Умный дом»: temperature, feels-like, wind speed, condition, and icon URLs. Hourly data for 48h is aggregated into day parts (night/morning/day/evening, today/tomorrow). The response is cached (raw, `CACHE_VERSION`-tagged) in hidden settings and refreshed no more often than `YANDEX_WEATHER_REFRESH_MINUTES` (default 90 min ≈ 16 requests/day within the 30/day free limit). After a fetch error, retries pause for `YANDEX_WEATHER_ERROR_RETRY_MINUTES` (default 30) to protect the quota. Returns `None` when unconfigured (the block is hidden entirely), `{"available": False, "detail": …}` when configured but not yet fetched. GraphQL errors arrive with HTTP 200 — `_post_query` converts them to exceptions; if the `icon` field is rejected by the tariff, the query is retried without it (emoji fallback).

### Raw packet forwarding

### Raw packet forwarding

[app/forwarding.py](app/forwarding.py) duplicates every successfully ingested packet — the raw request body, unchanged — as a `POST` to `FORWARD_RAW_URL` (e.g. the IQHA hub at `https://hub.iqha.ru/ingest`; note the path, the old `/api/ingest` hub endpoint answers 410 Gone). Runs in a background thread (`asyncio.to_thread` from the async ingest handler), never blocks or fails the ingest. Failure details including the remote response body are logged.

### CSV retro-import

[app/csv_import.py](app/csv_import.py) handles retrospective data import from narodmon.ru CSV exports. The admin page at `/admin/import` ([app/templates/admin/import.html](app/templates/admin/import.html)) provides:
1. File upload with station selection
2. Preview with configurable column→sensor mapping (dropdowns for every CSV column)
3. Import with duplicate detection (skips batches with matching mac + timestamp)

The parser stores `raw_values` for all numeric CSV columns, enabling user remapping of arbitrarily-named columns to any sensor_id from `SENSOR_MAP`. Auto-detected mappings are pre-selected; unknown columns default to "skip".

### Data aggregation

[app/aggregation.py](app/aggregation.py) reduces storage for old data by converting 5-minute readings into hourly averages. Controlled by settings:
- `AGGREGATION_MAX_AGE_DAYS` (default 45) — data older than this is eligible
- `AGGREGATION_INTERVAL_DAYS` (default 10) — how often to run
- `AGGREGATION_LAST_RUN_AT` — internal timestamp of last successful run

The `_aggregation_loop` in [app/main.py](app/main.py) checks every 6 hours whether aggregation is due. Aggregated batches are marked with `"_aggregated": true` in `payload_json` to prevent re-aggregation. The process is transactional and idempotent.

### Frontend

Server-rendered Jinja2 with a custom design system in [app/static/dashboard.css](app/static/dashboard.css) — `oklch` palette, light/dark theme on `data-bs-theme`, dense-grid dashboard ported from a Claude Design handoff (variation A "плотная сетка"). Tabler is **not** used for layout — only `tabler-icons.min.css` for icons (theme toggle, admin gear icon). Chart.js still powers the line graphs on `/charts`; everywhere else (sparklines, comfort gauge, min/max bars, period overlay) charts are inline SVG drawn by the small JS in `index.html`.

The dashboard home page (`index.html`) opens with a forecast row: Zambretti card (1/3 width, 12–24h horizon) plus the Yandex Weather card (2/3, or the row collapses to a single full-width Zambretti card when Yandex is unconfigured). The chart range switcher on both `/` and `/charts` supports 1/3/12/24-hour and multi-day windows (`/charts?hours=N` or `?days=N`). The `/charts` page also has the "Климат" card (DEW, absolute humidity `1`, VPD, MIX_RATIO, AIR_D), the T1+T6 temperature overlay, and the heatmap's 30-day window navigation (`‹`/`›` buttons; `›` is disabled on the current window and the window is marked `disabled` in markup from first paint). Yandex icons are remote `<img>` from the URLs the API returns, with emoji fallbacks.

[app/templates/base.html](app/templates/base.html) renders the topbar (brand + nav + admin gear icon + theme toggle), wraps `{% block content %}` in `<div class="dashboard">`, and ships the optional Yandex.Metrika tag (enabled iff `YANDEX_METRIKA_ID` is set). All pages extend it and use the shared classes (`.card`, `.page-header`, `.seg`, `.data-table`, `.kpi-value`, `.alert-{good,warn,bad}`).

Admin pages ([app/templates/admin/](app/templates/admin/)) extend `admin/_layout.html` which provides segmented tab navigation (Настройки / Станции / Импорт). The import page features a two-step flow: upload → preview with configurable column mapping → import.

The history page ([app/templates/history.html](app/templates/history.html)) includes a client-side sensor filter for "other data" readings. Filter state persists in `localStorage`. Default visible sensors: absolute humidity (`1`), dew point (`DEW`), perceived temperature (`T6`).

Inter and JetBrains Mono are loaded from Google Fonts CDN; everything else (Tabler icons, Chart.js) is vendored in `app/static/vendor/`. When bumping vendored versions, also update the table in [README.md](README.md).

`render_template()` in [app/main.py](app/main.py) wraps `TemplateResponse` to support both the request-first and name-first signatures across Starlette versions — keep using it instead of calling `templates.TemplateResponse` directly.

**Jinja gotcha:** when looping to find the first available sensor, use `{% set ns = namespace(found=none) %}` and assign to `ns.found` inside the loop. A plain `{% set foo = ... %}` inside `{% for %}` only lives in the loop's scope and is `None` outside it (this caused current-value cards to render `—` once already).

## Conventions

- Russian is the user-facing language (templates, bot replies, log messages, comfort-risk reasons, relative-age strings like "5 мин назад"). Keep new user-visible strings in Russian.
- Timestamps: store UTC ISO strings, convert to local only for display.
- Sensor IDs are uppercase everywhere after ingest — compare with `.upper()` and define new groupings in `sensor_map.py`/`db.py` in uppercase.
- Don't commit `data/weather.sqlite3`, `.env`, or anything in `logs/` (already in `.gitignore`).
