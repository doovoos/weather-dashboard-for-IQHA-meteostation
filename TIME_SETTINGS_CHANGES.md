# Изменения в системе времени и часовых поясов

## Обзор изменений

Внесены три основные правки:

1. **Формат времени на графиках** — переведён с 12-часового на 24-часовой формат
2. **Начало суток в истории** — исправлено с 04:00 на 00:00
3. **Единая система настроек** — добавлены централизованные настройки для часового пояса и формата времени

## Детали изменений

### 1. Формат времени на графиках (charts.html)

**Файл:** `app/templates/charts.html`

**Изменения:**
- Добавлена поддержка глобальной настройки `window.APP_TIME_FORMAT`
- Chart.js теперь использует `hour12: false` для 24-часового формата
- Форматы отображения адаптируются к настройке:
  - 24h: `HH:mm` для часов, `dd.MM` для дней
  - 12h: `h:mm a` для часов, `MM/dd` для дней

**Пример:**
```javascript
const is24h = (window.APP_TIME_FORMAT || "24h") === "24h";
ticks: { hour12: !is24h }
```

### 2. Начало суток в истории (db.py)

**Файл:** `app/db.py`

**Проблема:**
SQL-запрос использовал `WHERE date(b.received_at) = date(?)`, который сравнивал даты в UTC, а не в локальном часовом поясе. Это приводило к смещению начала суток.

**Решение:**
```python
def get_history_for_date(target_date: str, conn: sqlite3.Connection | None = None):
    # Parse target_date as a local date, then convert to UTC bounds for the query
    try:
        target_day = date.fromisoformat(target_date)
    except ValueError:
        return []
    start_utc, end_utc = _local_day_bounds(target_day)
    
    # ... WHERE b.received_at >= ? AND b.received_at < ?
```

Теперь используется функция `_local_day_bounds()`, которая правильно конвертирует локальную дату в UTC-границы.

### 3. Единая система настроек времени

#### Новые настройки в БД

**Файл:** `app/settings.py`

Добавлены две новые настройки в секцию "Общие":

| Ключ | Название | Тип | По умолчанию | Описание |
|------|----------|-----|--------------|----------|
| `WEATHER_TIMEZONE` | Часовой пояс | string | UTC | Часовой пояс для отображения времени: UTC, Asia/Omsk, Europe/Moscow и т.д. |
| `TIME_FORMAT` | Формат времени | string | 24h | 24h — 24-часовой формат (14:30), 12h — 12-часовой (2:30 PM) |

#### Динамическое обновление часового пояса

**Файлы:** `app/db.py`, `app/settings.py`, `app/main.py`, `bot.py`

**Изменения:**
- Добавлена функция `_get_local_tz()` для ленивого получения часового пояса из БД
- Добавлена функция `refresh_local_tz()` для обновления часового пояса без перезапуска
- При изменении `WEATHER_TIMEZONE` через админку автоматически вызывается `refresh_local_tz()`
- Все компоненты (web, bot, db) теперь получают часовой пояс из БД с fallback на env-переменную

**Пример (db.py):**
```python
def _get_local_tz() -> ZoneInfo:
    """Get timezone from DB settings if available, otherwise use env default."""
    try:
        from .settings import get_string
        tz_name = get_string("WEATHER_TIMEZONE")
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass  # DB not ready yet
    try:
        return ZoneInfo(WEATHER_TIMEZONE)
    except ZoneInfoNotFoundError:
        return UTC

LOCAL_TZ = _get_local_tz()

def refresh_local_tz() -> None:
    """Refresh LOCAL_TZ after settings change."""
    global LOCAL_TZ
    LOCAL_TZ = _get_local_tz()
```

#### Передача настроек в frontend

**Файлы:** `app/main.py`, `app/templates/base.html`

**Изменения:**
- В `_site_globals()` добавлены переменные `time_format` и `weather_timezone`
- В `base.html` добавлена глобальная переменная `window.APP_TIME_FORMAT`
- Все шаблоны теперь имеют доступ к настройкам времени

**Пример (base.html):**
```html
<script>
    // Global time format setting from server
    window.APP_TIME_FORMAT = "{{ time_format }}";
</script>
```

#### Форматирование времени в зависимости от настройки

**Файлы:** `app/db.py`

**Изменения в `get_today_extremes()`:**
```python
# Get time format from settings
try:
    from . import settings as _settings
    time_format = _settings.get_string("TIME_FORMAT").strip().lower()
except Exception:
    time_format = "24h"
time_str = local_time.strftime("%H:%M" if time_format == "24h" else "%I:%M %p")
```

**Изменения в `get_history_for_date()`:**
```python
# Get time format from settings
try:
    from . import settings as _settings
    time_format = _settings.get_string("TIME_FORMAT").strip().lower()
except Exception:
    time_format = "24h"
time_fmt = "%H:%M:%S" if time_format == "24h" else "%I:%M:%S %p"
```

## Использование

### Настройка через админ-панель

1. Перейдите в `/admin/settings`
2. Найдите секцию "Общие"
3. Измените:
   - **Часовой пояс** — например, `Asia/Omsk` или `Europe/Moscow`
   - **Формат времени** — `24h` или `12h`
4. Сохраните изменения
5. Часовой пояс обновится автоматически, перезапуск не требуется

### Настройка через переменные окружения

При первом запуске (пустая БД) значения берутся из env:

```bash
WEATHER_TIMEZONE=Asia/Omsk
TIME_FORMAT=24h
```

После первого запуска значения из env игнорируются, используется БД.

## Тестирование

### Проверка синтаксиса
```bash
py -m py_compile app/db.py app/settings.py app/config.py app/main.py bot.py
```

### Проверка функциональности

1. **Графики:**
   - Откройте `/charts`
   - Убедитесь, что ось X показывает время в 24-часовом формате (например, `14:30`, а не `2:30 PM`)
   - Измените `TIME_FORMAT` на `12h` в админке
   - Обновите страницу — формат должен измениться

2. **История:**
   - Откройте `/history`
   - Выберите дату
   - Убедитесь, что записи начинаются с `00:00:00`, а не с `04:00:00`
   - Измените `WEATHER_TIMEZONE` в админке
   - Проверьте, что время корректно конвертируется

3. **Часовой пояс:**
   - Измените `WEATHER_TIMEZONE` в админке (например, `Asia/Omsk`)
   - Проверьте, что время на всех страницах обновилось без перезапуска
   - Проверьте, что Telegram-бот также использует новый часовой пояс

## Затронутые файлы

- `app/db.py` — исправление `get_history_for_date()`, добавление `refresh_local_tz()`, форматирование времени
- `app/settings.py` — добавлены настройки `WEATHER_TIMEZONE` и `TIME_FORMAT`
- `app/config.py` — обновлён `env_settings_dict()` для новых настроек
- `app/main.py` — динамическое получение часового пояса, передача настроек в шаблоны
- `app/templates/base.html` — добавлена `window.APP_TIME_FORMAT`
- `app/templates/charts.html` — использование настройки формата времени
- `bot.py` — динамическое получение часового пояса

## Совместимость

- **Обратная совместимость:** Сохранена. Env-переменная `WEATHER_TIMEZONE` используется как fallback
- **Миграция БД:** Не требуется. Новые настройки добавятся автоматически при первом обращении через `seed_defaults_if_empty()`
- **Существующие развёртывания:** Продолжают работать с текущими значениями из env до первого изменения через админку
