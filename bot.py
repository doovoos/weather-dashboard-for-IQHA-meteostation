from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app import settings
from app.config import (
    TELEGRAM_BOT_TOKEN,
    WEATHER_TIMEZONE,
    env_settings_dict,
)
from app.db import format_telegram_snapshot, get_latest_snapshot, init_db, parse_timestamp
from app.logging_setup import setup_logging

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

WEATHER_BUTTON = "Погода сейчас"


def keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(WEATHER_BUTTON)]]
    site_url = settings.get_string("WEATHER_SITE_URL").strip()
    if site_url:
        rows.append([KeyboardButton("Перейти на сайт", web_app=WebAppInfo(url=site_url))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def current_weather_text() -> str:
    return format_telegram_snapshot(get_latest_snapshot())


def build_dynamic_bot_name(snapshot: dict | None) -> str:
    suffix = "⚪ --"
    if snapshot:
        for reading in snapshot.get("readings", []):
            if str(reading.get("sensor_id", "")).upper() == "T1":
                try:
                    temperature = float(reading["value"])
                    icon = "☀️" if temperature >= 0 else "❄️"
                    suffix = f"{icon} {temperature:+.1f}°"
                except (TypeError, ValueError, KeyError):
                    suffix = "⚪ --"
                break
    prefix = settings.get_string("TELEGRAM_DYNAMIC_NAME_PREFIX").strip()
    if not prefix:
        place = settings.get_string("WEATHER_PLACE_NAME").strip() or "Локальная станция"
        prefix = f"Погода: {place}"
    title = f"{prefix} • {suffix}"
    if len(title) > 64:
        title = title[:64].rstrip()
    return title


async def send_text_to_chat(app: Application, chat_id: int, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard())
    except Exception:
        logger.exception("Failed to send telegram message to chat_id={}", chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    logger.info(
        "Telegram /start user_id={} chat_id={}",
        update.effective_user.id if update.effective_user else "?",
        update.effective_chat.id if update.effective_chat else "?",
    )
    await update.message.reply_text(
        "Нажмите кнопку, чтобы получить актуальную сводку погоды.",
        reply_markup=keyboard(),
    )


async def send_weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    text = (update.message.text or "").strip()
    logger.info(
        "Telegram request user_id={} chat_id={} text={!r}",
        update.effective_user.id if update.effective_user else "?",
        update.effective_chat.id if update.effective_chat else "?",
        text,
    )
    try:
        await update.message.reply_text(current_weather_text(), reply_markup=keyboard())
    except Exception:
        logger.exception("Failed to send weather message to chat_id={}", update.effective_chat.id if update.effective_chat else "?")
        await update.message.reply_text(
            "Не удалось получить данные погоды. Проверьте логи и подключение к базе.",
            reply_markup=keyboard(),
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    incoming = (update.message.text or "").strip().lower()
    if incoming in {WEATHER_BUTTON.lower(), "погода", "погода сейчас", "/weather"}:
        await send_weather(update, context)
        return
    await update.message.reply_text(
        "Используйте кнопку «Погода сейчас» или команду /weather.",
        reply_markup=keyboard(),
    )


async def stale_data_monitor_loop(app: Application) -> None:
    logger.info("Stale monitor started")
    notified = False
    while True:
        stale_minutes_threshold = settings.get_int("TELEGRAM_STALE_MINUTES")
        admin_ids = settings.get_csv_int("TELEGRAM_ADMIN_IDS")
        interval = max(15, settings.get_int("TELEGRAM_MONITOR_INTERVAL_SECONDS"))

        snapshot = get_latest_snapshot()
        stale_minutes = None
        if snapshot and snapshot.get("received_at"):
            observed_at = parse_timestamp(snapshot["received_at"])
            stale_minutes = (datetime.now(UTC) - observed_at).total_seconds() / 60.0

        is_stale = snapshot is None or stale_minutes is None or stale_minutes >= stale_minutes_threshold
        if is_stale and not notified and admin_ids:
            if snapshot and stale_minutes is not None:
                text = (
                    "⚠️ Нет новых данных от станции.\n"
                    f"Последнее обновление: {snapshot.get('received_ago', 'давно')}.\n"
                    f"Порог: {stale_minutes_threshold} мин."
                )
            else:
                text = (
                    "⚠️ Нет данных от станции.\n"
                    "В базе нет актуальных пакетов телеметрии."
                )
            logger.warning("Station data stale. Sending alert to admins.")
            for admin_id in admin_ids:
                await send_text_to_chat(app, admin_id, text)
            notified = True

        if not is_stale and notified:
            logger.info("Station data flow restored")
            notified = False

        await asyncio.sleep(interval)


async def daily_weather_broadcast_loop(app: Application) -> None:
    logger.info("Daily weather broadcast loop started")
    sent_keys: set[str] = set()
    while True:
        now_local = datetime.now(APP_TZ)
        hhmm = now_local.strftime("%H:%M")
        daily_times = settings.get_csv_time("TELEGRAM_DAILY_TIMES")
        daily_users = settings.get_csv_int("TELEGRAM_DAILY_USER_IDS")
        for schedule in daily_times:
            key = f"{now_local.date().isoformat()}-{schedule}"
            if hhmm == schedule and key not in sent_keys and daily_users:
                payload = current_weather_text()
                logger.info("Daily broadcast at {} for {} users", schedule, len(daily_users))
                for user_id in daily_users:
                    await send_text_to_chat(app, user_id, payload)
                sent_keys.add(key)

        today_prefix = f"{now_local.date().isoformat()}-"
        sent_keys = {key for key in sent_keys if key.startswith(today_prefix)}
        await asyncio.sleep(20)


async def dynamic_bot_name_loop(app: Application) -> None:
    logger.info("Dynamic bot name loop started")
    last_name = ""
    while True:
        if not settings.get_bool("TELEGRAM_DYNAMIC_NAME_ENABLED"):
            await asyncio.sleep(60)
            continue
        interval_minutes = max(1, settings.get_int("TELEGRAM_DYNAMIC_NAME_INTERVAL_MINUTES"))
        try:
            snapshot = get_latest_snapshot()
            new_name = build_dynamic_bot_name(snapshot)
            if new_name != last_name:
                await app.bot.set_my_name(name=new_name)
                logger.info("Bot name updated: {}", new_name)
                last_name = new_name
        except Exception:
            logger.exception("Failed to update bot name")
        await asyncio.sleep(interval_minutes * 60)


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the bot.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", send_weather))
    app.add_handler(MessageHandler(filters.Regex(f"^{WEATHER_BUTTON}$"), send_weather))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


async def main() -> None:
    init_db()
    inserted = settings.seed_defaults_if_empty(env_settings_dict())
    setup_logging("bot")  # Call after DB init and seeding
    if inserted:
        logger.info("Seeded {} app_settings rows from env defaults", inserted)
    logger.info("Bot service started")
    application = build_app()
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    monitor_task = asyncio.create_task(stale_data_monitor_loop(application))
    broadcast_task = asyncio.create_task(daily_weather_broadcast_loop(application))
    dynamic_name_task = asyncio.create_task(dynamic_bot_name_loop(application))
    try:
        await asyncio.Event().wait()
    finally:
        monitor_task.cancel()
        broadcast_task.cancel()
        dynamic_name_task.cancel()
        await asyncio.gather(monitor_task, broadcast_task, dynamic_name_task, return_exceptions=True)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
