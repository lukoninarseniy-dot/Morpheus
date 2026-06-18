"""
Бот «Режим сна» — Шаг 2.
Новое по сравнению с шагом 1:
- бот запоминает время подъёма (база данных SQLite);
- считает рекомендованный отбой и даёт изменить его вручную;
- команда /fact — случайный факт о сне.
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import db
from facts import FACTS

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Локально — в файл .env, на Railway — в Variables."
    )

CYCLE_MIN = 90        # длительность цикла сна, минут
LATENCY_MIN = 15      # среднее время засыпания, минут

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Чтобы /fact не повторял один и тот же факт подряд (сбрасывается при рестарте).
_last_fact: dict[int, int] = {}


# ---------- расчёт сна ----------
def parse_time(raw: str | None) -> str | None:
    """Превращает текст вида '7:0', '07.00' в '07:00'. Иначе — None."""
    if not raw:
        return None
    cleaned = raw.strip().replace(".", ":")
    try:
        hh, mm = cleaned.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return f"{h:02d}:{m:02d}"
    return None


def calc_bedtime(wake: str, cycles: int = 5, latency: int = LATENCY_MIN) -> str:
    """Время отбоя = подъём − (циклы × 90 мин) − время засыпания."""
    h, m = map(int, wake.split(":"))
    wake_dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    bedtime = wake_dt - timedelta(minutes=cycles * CYCLE_MIN + latency)
    return bedtime.strftime("%H:%M")


def format_plan(wake: str, custom_bed: str | None) -> str:
    rec = calc_bedtime(wake, 5)
    if custom_bed:
        eff = custom_bed
        note = (f"Рекомендованный отбой — <b>{rec}</b> (5 циклов), "
                f"но у тебя выбран свой: <b>{custom_bed}</b>.")
    else:
        eff = rec
        note = f"Рекомендованный отбой — <b>{rec}</b> (5 циклов, ~7,5 ч)."
    return (
        f"⏰ Подъём: <b>{wake}</b>\n"
        f"🛏 Ложиться: <b>{eff}</b>\n\n"
        f"{note}\n\n"
        f"Варианты от подъёма {wake}:\n"
        f"• {calc_bedtime(wake, 6)} — 6 циклов (~9 ч)\n"
        f"• {calc_bedtime(wake, 5)} — 5 циклов (~7,5 ч) ⭐\n"
        f"• {calc_bedtime(wake, 4)} — 4 цикла (~6 ч)"
    )


# ---------- клавиатуры ----------
def wake_keyboard():
    builder = InlineKeyboardBuilder()
    for t in ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]:
        builder.button(text=t, callback_data=f"wake:{t}")
    builder.button(text="✏️ Другое время", callback_data="wake:custom")
    builder.adjust(3)
    return builder.as_markup()


def adjust_keyboard(wake: str):
    early = calc_bedtime(wake, 6)   # 9 часов сна — лечь раньше
    late = calc_bedtime(wake, 4)    # 6 часов сна — лечь позже
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🛌 Раньше — {early} (9 ч)", callback_data=f"bed:{early}")
    builder.button(text=f"🌙 Позже — {late} (6 ч)", callback_data=f"bed:{late}")
    builder.button(text="✏️ Свой отбой", callback_data="bed:custom")
    builder.button(text="↩️ Вернуть рекомендованное", callback_data="bed:auto")
    builder.adjust(1)
    return builder.as_markup()


# ---------- команды ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я помогу наладить режим сна.\n\n"
        "Выбери удобное время подъёма — и я подскажу, когда ложиться, чтобы "
        "просыпаться в конце цикла сна и чувствовать себя бодрее.\n\n"
        "Все команды — /help",
        reply_markup=wake_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/setwake — выбрать время подъёма (кнопками)\n"
        "/setwake 07:00 — сразу задать время\n"
        "/setbed 23:30 — задать свой отбой вручную\n"
        "/setbed auto — вернуть рекомендованный отбой\n"
        "/me — мой текущий режим\n"
        "/fact — случайный факт о сне\n"
        "/calc 07:00 — быстрый расчёт без сохранения\n"
        "/help — это сообщение"
    )


@dp.message(Command("setwake"))
async def cmd_setwake(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Выбери время подъёма:", reply_markup=wake_keyboard())
        return
    wake = parse_time(command.args)
    if not wake:
        await message.answer("Не понял время. Формат: <code>/setwake 07:00</code>")
        return
    await db.set_wake(message.from_user.id, wake)
    await message.answer(format_plan(wake, None), reply_markup=adjust_keyboard(wake))


@dp.callback_query(F.data.startswith("wake:"))
async def cb_wake(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await callback.message.answer(
            "Напиши своё время подъёма так: <code>/setwake 06:45</code>"
        )
        await callback.answer()
        return
    await db.set_wake(callback.from_user.id, value)
    await callback.message.answer(
        format_plan(value, None), reply_markup=adjust_keyboard(value)
    )
    await callback.answer("Время подъёма сохранено")


@dp.message(Command("setbed"))
async def cmd_setbed(message: Message, command: CommandObject):
    user = await db.get_user(message.from_user.id)
    if not user or not user["wake_time"]:
        await message.answer("Сначала выбери время подъёма: /setwake")
        return
    arg = (command.args or "").strip().lower()
    if arg in ("auto", "авто", "сброс"):
        await db.set_bedtime(message.from_user.id, None)
        await message.answer("Вернул рекомендованный отбой.\n\n"
                             + format_plan(user["wake_time"], None))
        return
    bed = parse_time(command.args)
    if not bed:
        await message.answer(
            "Формат: <code>/setbed 23:30</code> или <code>/setbed auto</code>"
        )
        return
    await db.set_bedtime(message.from_user.id, bed)
    await message.answer("Сохранил твой отбой.\n\n"
                         + format_plan(user["wake_time"], bed))


@dp.callback_query(F.data.startswith("bed:"))
async def cb_bed(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["wake_time"]:
        await callback.message.answer("Сначала выбери время подъёма: /setwake")
        await callback.answer()
        return
    wake = user["wake_time"]
    value = callback.data.split(":", 1)[1]

    if value == "custom":
        await callback.message.answer("Напиши свой отбой так: <code>/setbed 23:30</code>")
        await callback.answer()
        return
    if value == "auto":
        await db.set_bedtime(callback.from_user.id, None)
        await callback.message.answer("Вернул рекомендованный отбой.\n\n"
                                      + format_plan(wake, None))
        await callback.answer("Готово")
        return

    # value — это конкретное время "ЧЧ:ММ" с кнопки «раньше/позже»
    await db.set_bedtime(callback.from_user.id, value)
    await callback.message.answer("Сохранил твой отбой.\n\n"
                                  + format_plan(wake, value))
    await callback.answer("Готово")


@dp.message(Command("me"))
async def cmd_me(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or not user["wake_time"]:
        await message.answer("Ты ещё не задал время подъёма. Нажми /setwake")
        return
    await message.answer(
        format_plan(user["wake_time"], user["custom_bedtime"]),
        reply_markup=adjust_keyboard(user["wake_time"]),
    )


@dp.message(Command("fact"))
async def cmd_fact(message: Message):
    uid = message.from_user.id
    idx = random.randrange(len(FACTS))
    if len(FACTS) > 1 and _last_fact.get(uid) == idx:   # не повторяем предыдущий
        idx = (idx + 1) % len(FACTS)
    _last_fact[uid] = idx
    await message.answer(f"💡 <b>Факт о сне</b>\n\n{FACTS[idx]}")


@dp.message(Command("calc"))
async def cmd_calc(message: Message, command: CommandObject):
    wake = parse_time(command.args)
    if not wake:
        await message.answer("Формат: <code>/calc 07:00</code>")
        return
    await message.answer(format_plan(wake, None))


async def main():
    logger.info("Бот запускается...")
    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
