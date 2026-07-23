# Weather Station Dashboard

Веб-интерфейс и Telegram-бот для домашней/локальной метеостанции:
- прием телеметрии через HTTP (`/api/ingest`)
- хранение в SQLite
- дашборд, графики, история, состояние станции
- Telegram-бот с кнопками, алертами и рассылкой

## Важно

Проект разработан совместно с **OpenAI Codex**.

## 1) Что нужно заранее

Минимум:
- Linux-сервер (Ubuntu/Debian)
- домен (например `weather.example.com`)
- открытые порты `80/443`

Для запуска в Docker:
- Docker + Docker Compose plugin

Для запуска без Docker:
- Python 3.12+
- `venv`, `pip`
- `systemd` (для автозапуска)

## 2) Быстрый запуск в Docker (рекомендуется)

### Шаг 1. Клонировать проект

```bash
git clone <YOUR_REPO_URL> weather-dashboard
cd weather-dashboard
```

### Шаг 2. Подготовить `.env`

```bash
cp .env.example .env
```

Откройте `.env` и заполните минимум:
- `TELEGRAM_BOT_TOKEN` (если нужен бот)
- `WEATHER_SITE_URL=https://your-domain.example`
- `WEATHER_PLACE_NAME=Ваша станция`
- `SITE_BRAND=Ваш бренд`

Если нужна Яндекс.Метрика:
- `YANDEX_METRIKA_ID=XXXXXXXX`

### Шаг 3. Запустить контейнеры

```bash
docker compose up -d --build
```

Проверка:
```bash
docker compose ps
curl http://127.0.0.1:18080/api/status
```

Ожидается:
```json
{"status":"ok"}
```

### Шаг 4. Подключить Nginx на хосте

Готовый пример:
- `deploy/nginx/weather.conf.example`

Пример (HTTP):

```nginx
server {
    listen 80;
    server_name weather.example.com;

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Проверка и перезапуск Nginx:
```bash
sudo nginx -t
sudo systemctl reload nginx
```

### Шаг 5. Включить HTTPS (Let’s Encrypt)

```bash
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d weather.example.com
```

## 3) Запуск без Docker (напрямую в системе)

### Шаг 1. Установить зависимости

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip nginx
```

### Шаг 2. Подготовить проект

```bash
git clone <YOUR_REPO_URL> weather-dashboard
cd weather-dashboard
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p data logs
```

### Шаг 3. Тестовый запуск

```bash
source .venv/bin/activate
set -a && source .env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 18080 --proxy-headers --forwarded-allow-ips="*"
```

### Шаг 4. Сделать автозапуск через systemd

Создайте `/etc/systemd/system/weather-web.service`:

```ini
[Unit]
Description=Weather Dashboard (FastAPI)
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/weather-dashboard
EnvironmentFile=/opt/weather-dashboard/.env
ExecStart=/opt/weather-dashboard/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18080 --proxy-headers --forwarded-allow-ips=*
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Если нужен бот, создайте `/etc/systemd/system/weather-bot.service`:

```ini
[Unit]
Description=Weather Telegram Bot
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/weather-dashboard
EnvironmentFile=/opt/weather-dashboard/.env
ExecStart=/opt/weather-dashboard/.venv/bin/python bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Применить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now weather-web
sudo systemctl enable --now weather-bot
sudo systemctl status weather-web --no-pager
sudo systemctl status weather-bot --no-pager
```

## 4) Что настроить в `.env`

Смотрите полный шаблон в `.env.example`.

Ключевые параметры:
- `WEATHER_DB_PATH` — путь к SQLite
- `WEATHER_TIMEZONE` — временная зона
- `LOG_DIR` — каталог логов
- `WEATHER_SITE_URL` — URL сайта для кнопки в Telegram
- `APP_TITLE` — внутреннее название приложения
- `SITE_BRAND` — надпись в шапке сайта
- `WEATHER_PLACE_NAME` — имя станции/локации в сообщениях
- `YANDEX_METRIKA_ID` — ID Метрики (пусто = выключено)

Telegram:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ADMIN_IDS`
- `TELEGRAM_DAILY_USER_IDS`
- `TELEGRAM_DAILY_TIMES`
- `TELEGRAM_STALE_MINUTES`
- `TELEGRAM_MONITOR_INTERVAL_SECONDS`
- `TELEGRAM_DYNAMIC_NAME_ENABLED`
- `TELEGRAM_DYNAMIC_NAME_PREFIX`
- `TELEGRAM_DYNAMIC_NAME_INTERVAL_MINUTES`

## 5) Публичный деплой: чек-лист безопасности

- не коммитить `.env`
- не хранить токены в коде
- держать веб на `127.0.0.1` за Nginx
- включить HTTPS
- ограничить доступ к серверу по SSH-ключам
- регулярно обновлять систему и зависимости
- делать бэкап `data/weather.sqlite3`
- проверять логи:
  - `logs/web.log`
  - `logs/bot.log`

## 6) Статические библиотеки фронтенда

Дизайн-система собственная — токены и стили в `app/static/dashboard.css` (палитра `oklch`, светлая/тёмная темы, дашбордные карточки и сетки).

Локально (без CDN):
- `app/static/vendor/tabler/icons/tabler-icons.min.css` — только иконки (используются в кнопке темы)
- `app/static/vendor/tabler/icons/fonts/*` — шрифты иконок
- `app/static/vendor/chartjs/chart.umd.min.js` — графики на странице `/charts`
- `app/static/vendor/chartjs/chartjs-adapter-date-fns.bundle.min.js`

Через CDN:
- Inter и JetBrains Mono с `fonts.googleapis.com` (подключены в `app/templates/base.html`). При желании можно добавить локально в `app/static/vendor/fonts/` и заменить `<link>`.

Проверенные актуальные версии на момент 2026-04-26:
- `@tabler/icons-webfont`: `3.41.1`
- `chart.js`: `4.5.1`
- `chartjs-adapter-date-fns`: `3.0.0`

## 7) Полезные команды

Docker:
```bash
docker compose up -d --build
docker compose logs -f web
docker compose logs -f bot
docker compose down
```

Проверка API:
```bash
curl http://127.0.0.1:18080/api/status
curl http://127.0.0.1:18080/api/current
```

## 8) Обновление кода на сервере

### Подтянуть изменения из GitHub

```bash
cd /путь/к/твоему/сайту
git pull origin main
```

Если были изменения в `requirements.txt` — переустановить зависимости:
```bash
source .venv/bin/activate
pip install -r requirements.txt
deactivate
```

### Перезапуск сервисов после изменения кода

После любых изменений в `.py`-файлах или шаблонах нужно перезапустить соответствующие сервисы:

```bash
# Перезапуск веб-сервера (после изменений в app/*.py, шаблонах, CSS)
sudo systemctl restart weather-web

# Перезапуск Telegram-бота (после изменений в bot.py)
sudo systemctl restart weather-bot

# Перезапуск обоих сразу
sudo systemctl restart weather-web weather-bot
```

### Проверка статуса и логов

```bash
# Статус сервисов
sudo systemctl status weather-web --no-pager
sudo systemctl status weather-bot --no-pager

# Логи в реальном времени
sudo journalctl -u weather-web -f --no-pager
sudo journalctl -u weather-bot -f --no-pager

# Последние 50 строк лога
sudo journalctl -u weather-web -n 50 --no-pager
```

### Полная последовательность обновления

```bash
cd /opt/weather-dashboard
git pull origin main
sudo systemctl restart weather-web weather-bot
sudo systemctl status weather-web weather-bot --no-pager
```
