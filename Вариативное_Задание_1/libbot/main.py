import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import telebot
from dotenv import load_dotenv
from telebot.apihelper import ApiTelegramException

load_dotenv()

SERVICE_NAME = "cbs_telegram_bot"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("SQLITE_DB_PATH", DATA_DIR / "bot.sqlite3"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_RETRIES = int(os.getenv("TELEGRAM_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.getenv("TELEGRAM_RETRY_DELAY", "1.5"))

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is not set")

DATA_DIR.mkdir(parents=True, exist_ok=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "message": record.getMessage(),
        }
        for field in ("user_id", "message_id", "chat_id", "event"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger(SERVICE_NAME)
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)
logger.setLevel(LOG_LEVEL)
logger.propagate = False


def sanitize_text(text: str | None) -> str:
    if not text:
        return ""
    if (re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
            or re.search(r"\+?\d[\d\s().-]{7,}", text)):
        return "[sensitive]"
    return text[:500]


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id INTEGER,
                username TEXT,
                chat_id INTEGER,
                query_text TEXT,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_actions_timestamp "
            "ON user_actions(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_actions_user_id "
            "ON user_actions(user_id)"
        )


def log_user_action(message: telebot.types.Message, status: str) -> None:
    user = message.from_user
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    media_type = next(
        (
            ct for ct in (
                "photo", "document", "video", "audio", "voice",
                "sticker", "animation", "location", "contact"
            )
            if getattr(message, ct, None)
        ),
        None,
    )
    query_text = sanitize_text(text) if text else (media_type or "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_actions
            (timestamp, user_id, username, chat_id, query_text, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                getattr(user, "id", None),
                getattr(user, "username", None),
                message.chat.id,
                query_text,
                status,
            ),
        )

    logger.info(
        "user_action",
        extra={
            "event": "user_action",
            "user_id": getattr(user, "id", None),
            "message_id": message.message_id,
            "chat_id": message.chat.id,
        },
    )


def with_retry(func: Callable[[], Any], *, attempts: int = MAX_RETRIES) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except ApiTelegramException as error:
            last_error = error
            logger.warning(
                "telegram_api_error",
                extra={"event": "telegram_api_error"},
                exc_info=True,
            )
            if attempt < attempts:
                time.sleep(RETRY_BASE_DELAY * attempt)
        except Exception as error:
            last_error = error
            logger.exception(
                "unexpected_error",
                extra={"event": "unexpected_error"}
            )
            if attempt < attempts:
                time.sleep(RETRY_BASE_DELAY * attempt)
    assert last_error is not None
    raise last_error


def send_safe_reply(message: telebot.types.Message, text: str) -> None:
    with_retry(lambda: bot.reply_to(message, text))


bot = telebot.TeleBot(BOT_TOKEN)


@bot.message_handler(commands=["start"])
def start_handler(message: telebot.types.Message) -> None:
    log_user_action(message, "start")
    send_safe_reply(
        message,
        (
            "Здравствуйте! Это бот ЦБС Петроградского района.\n"
            "Доступны сценарии: поиск книги, режим работы, продление, "
            "а также тестовый echo-режим."
        ),
    )


@bot.message_handler(commands=["book"])
def book_handler(message: telebot.types.Message) -> None:
    log_user_action(message, "book_search")
    send_safe_reply(message, "Заглушка: здесь будет сценарий «Поиск книги».")


@bot.message_handler(commands=["hours"])
def hours_handler(message: telebot.types.Message) -> None:
    log_user_action(message, "working_hours")
    send_safe_reply(message, "Заглушка: здесь будет сценарий «Режим работы».")


@bot.message_handler(commands=["renew"])
def renew_handler(message: telebot.types.Message) -> None:
    log_user_action(message, "renew")
    send_safe_reply(message, "Заглушка: здесь будет сценарий «Продление».")


@bot.message_handler(
    func=lambda message: (
        not (message.text and message.text.startswith('/'))
    ),
    content_types=[
        "text",
        "audio",
        "document",
        "photo",
        "sticker",
        "video",
        "voice",
        "location",
        "contact",
        "animation"
    ]
)
def echo_handler(message: telebot.types.Message) -> None:

    try:
        log_user_action(message, "echo")
        with_retry(
            lambda: bot.copy_message(
                chat_id=message.chat.id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        )

    except Exception:
        logger.exception(
            "message_processing_failed",
            extra={
                "event": "message_processing_failed",
                "user_id": getattr(message.from_user, "id", None),
                "message_id": message.message_id,
                "chat_id": message.chat.id,
            },
        )


def main() -> None:
    init_db()

    print(bot.get_me())

    logger.info("bot_started", extra={"event": "bot_started"})

    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        skip_pending=True
    )


if __name__ == "__main__":
    main()
