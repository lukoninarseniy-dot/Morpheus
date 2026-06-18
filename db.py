"""Хранилище данных пользователей (SQLite через aiosqlite).

Таблица users:
- telegram_id    — id пользователя в Telegram (ключ)
- wake_time      — желаемое время подъёма "ЧЧ:ММ"
- custom_bedtime — свой отбой "ЧЧ:ММ", если рекомендованный не подошёл (или NULL)
- created_at     — когда запись создана/обновлена
"""

import os
from datetime import datetime

import aiosqlite

# Путь к файлу базы. Локально — рядом с кодом; на Railway зададим через
# переменную DB_PATH, указав путь на постоянный диск (Volume).
DB_PATH = os.environ.get("DB_PATH", "sleep_bot.db")


async def init_db():
    """Создаёт таблицу, если её ещё нет. Вызывается один раз при старте."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id    INTEGER PRIMARY KEY,
                wake_time      TEXT,
                custom_bedtime TEXT,
                created_at     TEXT
            )
            """
        )
        await conn.commit()


async def get_user(telegram_id: int):
    """Возвращает запись пользователя (как словарь-подобный объект) или None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone()


async def set_wake(telegram_id: int, wake_time: str):
    """Сохраняет время подъёма. При смене подъёма свой отбой сбрасывается,
    чтобы снова показывался рекомендованный."""
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
    """Сохраняет свой отбой. Передай None, чтобы вернуть рекомендованный."""
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
