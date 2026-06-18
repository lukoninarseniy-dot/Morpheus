"""Хранилище данных (SQLite через aiosqlite).

Таблицы:
  users      — настройки пользователя
  sleep_logs — дневник сна (одна запись на ночь)
"""

import os
from datetime import datetime

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "sleep_bot.db")

# Колонки дневника, которые можно обновлять (защита от подстановки в SQL)
LOG_COLUMNS = ("on_time_bed", "on_time_wake", "wellbeing")


async def _ensure_column(conn, table: str, column: str, definition: str):
    """Добавляет колонку, если её ещё нет (миграция старых баз)."""
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        existing = [row[1] for row in await cur.fetchall()]
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id    INTEGER PRIMARY KEY,
                wake_time      TEXT,
                custom_bedtime TEXT,
                timezone       TEXT,
                notify_enabled INTEGER DEFAULT 1,
                created_at     TEXT
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sleep_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL,
                log_date     TEXT NOT NULL,
                on_time_bed  TEXT,
                on_time_wake TEXT,
                wellbeing    INTEGER,
                created_at   TEXT,
                UNIQUE(telegram_id, log_date)
            )
            """
        )
        # миграции для баз с прошлых шагов
        await _ensure_column(conn, "users", "timezone", "TEXT")
        await _ensure_column(conn, "users", "notify_enabled", "INTEGER DEFAULT 1")
        await conn.commit()


# ---------- users ----------
async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone()


async def get_active_users():
    """Кому шлём напоминания: задан подъём и пояс, уведомления включены."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users "
            "WHERE wake_time IS NOT NULL AND timezone IS NOT NULL "
            "AND notify_enabled = 1"
        ) as cur:
            return await cur.fetchall()


async def set_wake(telegram_id: int, wake_time: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, wake_time, custom_bedtime, created_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                wake_time = excluded.wake_time,
                custom_bedtime = NULL
            """,
            (telegram_id, wake_time, datetime.now().isoformat()),
        )
        await conn.commit()


async def set_bedtime(telegram_id: int, custom_bedtime: str | None):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, custom_bedtime, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                custom_bedtime = excluded.custom_bedtime
            """,
            (telegram_id, custom_bedtime, datetime.now().isoformat()),
        )
        await conn.commit()


async def set_timezone(telegram_id: int, timezone: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, timezone, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                timezone = excluded.timezone
            """,
            (telegram_id, timezone, datetime.now().isoformat()),
        )
        await conn.commit()


async def set_notify(telegram_id: int, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, notify_enabled, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                notify_enabled = excluded.notify_enabled
            """,
            (telegram_id, 1 if enabled else 0, datetime.now().isoformat()),
        )
        await conn.commit()


# ---------- sleep_logs ----------
async def ensure_log(telegram_id: int, log_date: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO sleep_logs (telegram_id, log_date, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id, log_date) DO NOTHING
            """,
            (telegram_id, log_date, datetime.now().isoformat()),
        )
        await conn.commit()


async def update_log_field(telegram_id: int, log_date: str, column: str, value):
    if column not in LOG_COLUMNS:
        raise ValueError(f"Недопустимая колонка: {column}")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE sleep_logs SET {column} = ? "
            "WHERE telegram_id = ? AND log_date = ?",
            (value, telegram_id, log_date),
        )
        await conn.commit()


async def get_log(telegram_id: int, log_date: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM sleep_logs WHERE telegram_id = ? AND log_date = ?",
            (telegram_id, log_date),
        ) as cur:
            return await cur.fetchone()


async def get_logs_between(telegram_id: int, start_date: str, end_date: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM sleep_logs "
            "WHERE telegram_id = ? AND log_date BETWEEN ? AND ? "
            "ORDER BY log_date",
            (telegram_id, start_date, end_date),
        ) as cur:
            return await cur.fetchall()
