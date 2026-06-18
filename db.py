"""Хранилище данных пользователей (SQLite через aiosqlite).

Таблица users:
- telegram_id    — id пользователя в Telegram (ключ)
- wake_time      — желаемое время подъёма "ЧЧ:ММ"
- custom_bedtime — свой отбой "ЧЧ:ММ" (или NULL — тогда берём рекомендованный)
- timezone       — часовой пояс, напр. "Europe/Moscow" (нужен для напоминаний)
- created_at     — когда запись создана/обновлена
"""

import os
from datetime import datetime

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "sleep_bot.db")


async def _ensure_column(conn, table: str, column: str, coltype: str):
    """Добавляет колонку, если её ещё нет (для баз, созданных на прошлых шагах)."""
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        existing = [row[1] for row in await cur.fetchall()]
    if column not in existing:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id    INTEGER PRIMARY KEY,
                wake_time      TEXT,
                custom_bedtime TEXT,
                timezone       TEXT,
                created_at     TEXT
            )
            """
        )
        # если база осталась с шага 2 — дольём недостающую колонку
        await _ensure_column(conn, "users", "timezone", "TEXT")
        await conn.commit()


async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone()


async def get_active_users():
    """Все, у кого заданы и время подъёма, и часовой пояс — им шлём напоминания."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users WHERE wake_time IS NOT NULL AND timezone IS NOT NULL"
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
