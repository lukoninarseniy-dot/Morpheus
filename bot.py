"""
Бот «Режим сна» — Шаг 3.
Новое по сравнению с шагом 2:
- выбор часового пояса (/settz);
- напоминания по расписанию в твоём часовом поясе:
    ☕ дедлайн кофеина  (за 9 ч до отбоя)
    🌙 подготовка ко сну (за 1 ч до отбоя)
    😴 пора ложиться     (в момент отбоя)
    ☀️ доброе утро       (через 15 мин после подъёма)

Как работает планировщик: фоновый цикл раз в 30 секунд проверяет
у каждого пользователя его локальное время и шлёт напоминание, когда оно совпадает.
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

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

CYCLE_MIN = 90          # длительность цикла сна, минут
LATENCY_MIN = 15        # среднее время засыпания, минут
CAFFEINE_LEAD_MIN = 9 * 60   # за сколько до отбоя — дедлайн кофеина
WINDDOWN_LEAD_MIN = 60       # за сколько до отбоя — подготовка ко сну
MORNING_AFTER_MIN = 15       # через сколько после подъёма — утреннее сообщение

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# /fact — чтобы не повторять факт подряд (сбрасывается при рестарте)
_last_fact: dict[int, int] = {}
# защита от повторной отправки напоминания в тот же день: (uid, событие) -> "ГГГГ-ММ-ДД"
_sent: dict[tuple[int, str], str] = {}


# ---------- расчёт времени ----------
def parse_time(raw: str | None) -> str | None:
    """'7:0' / '07.00' -> '07:00'. Иначе None."""
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
    """Отбой = подъём − (циклы × 90 мин) − время засыпания."""
    h, m = map(int, wake.split(":"))
    wake_dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    bedtime = wake_dt - timedelta(minutes=cycles * CYCLE_MIN + latency)
    return bedtime.strftime("%H:%M")


def shift_time(hhmm: str, delta_min: int) -> str:
    """Сдвигает время на delta_min минут (можно отрицательно), по кругу за сутки."""
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + delta_min) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def effective_bedtime(user) -> str:
    """Свой отбой, если задан, иначе рекомендованный (5 циклов)."""
    return user["custom_bedtime"] or calc_bedtime(user["wake_time"], 5)


def valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


# ---------- тексты ----------
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


def format_full(user) -> str:
    """План + часовой пояс + времена напоминаний (для /me)."""
    wake = user["wake_time"]
    bed = effective_bedtime(user)
    text = format_plan(wake, user["custom_bedtime"])
    if user["timezone"]:
        text += (
            f"\n\n🌍 Часовой пояс: <b>{user['timezone']}</b>\n"
            f"🔔 Напоминания:\n"
            f"• ☕ дедлайн кофеина — {shift_time(bed, -CAFFEINE_LEAD_MIN)}\n"
            f"• 🌙 подготовка ко сну — {shift_time(bed, -WINDDOWN_LEAD_MIN)}\n"
            f"• 😴 отбой — {bed}\n"
            f"• ☀️ утро — {shift_time(wake, MORNING_AFTER_MIN)}"
        )
    else:
        text += "\n\n🔔 Чтобы включить напоминания, задай часовой пояс: /settz"
    return text


def build_reminder(event: str, bed: str, wake: str) -> str:
    if event == "caffeine":
        return (
            "☕ <b>Дедлайн кофеина</b>\n"
            "С этого момента лучше без кофе, чая, энергетиков и колы — кофеин "
            f"выводится 5–7 часов и крадёт глубокий сон. Отбой сегодня в {bed}."
        )
    if event == "winddown":
        return (
            f"🌙 <b>Через час — отбой ({bed})</b>\n"
            "Пора сворачиваться: приглуши яркий свет, отложи рабочие и тревожные "
            "ленты, можно принять тёплый душ. Плотно есть уже не стоит."
        )
    if event == "bedtime":
        return (
            f"😴 <b>Пора ложиться</b>\nОтбой в {bed}. Убери телефон подальше — "
            "спокойной ночи 🌟"
        )
    if event == "morning":
        return (
            "☀️ <b>Доброе утро!</b>\n"
            "Поймай дневной свет в первые 30 минут — это главный сигнал для "
            "биочасов, он помогает легче засыпать вечером.\n\n"
            "<i>Отметка сна и самочувствия появится на следующем шаге.</i>"
        )
    return ""


# ---------- клавиатуры ----------
def wake_keyboard():
    builder = InlineKeyboardBuilder()
    for t in ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]:
        builder.button(text=t, callback_data=f"wake:{t}")
    builder.button(text="✏️ Другое время", callback_data="wake:custom")
    builder.adjust(3)
    return builder.as_markup()


def adjust_keyboard(wake: str):
    early = calc_bedtime(wake, 6)
    late = calc_bedtime(wake, 4)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🛌 Раньше — {early} (9 ч)", callback_data=f"bed:{early}")
    builder.button(text=f"🌙 Позже — {late} (6 ч)", callback_data=f"bed:{late}")
    builder.button(text="✏️ Свой отбой", callback_data="bed:custom")
    builder.button(text="↩️ Вернуть рекомендованное", callback_data="bed:auto")
    builder.adjust(1)
    return builder.as_markup()


RU_TIMEZONES = [
    ("Калининград (UTC+2)", "Europe/Kaliningrad"),
    ("Москва (UTC+3)", "Europe/Moscow"),
    ("Самара (UTC+4)", "Europe/Samara"),
    ("Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
    ("Омск (UTC+6)", "Asia/Omsk"),
    ("Красноярск (UTC+7)", "Asia/Krasnoyarsk"),
    ("Иркутск (UTC+8)", "Asia/Irkutsk"),
    ("Якутск (UTC+9)", "Asia/Yakutsk"),
    ("Владивосток (UTC+10)", "Asia/Vladivostok"),
    ("Магадан (UTC+11)", "Asia/Magadan"),
    ("Камчатка (UTC+12)", "Asia/Kamchatka"),
]


def tz_keyboard():
    builder = InlineKeyboardBuilder()
    for label, iana in RU_TIMEZONES:
        builder.button(text=label, callback_data=f"tz:{iana}")
    builder.button(text="✏️ Другой пояс", callback_data="tz:custom")
    builder.adjust(2)
    return builder.as_markup()


# ---------- команды ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я помогу наладить режим сна.\n\n"
        "Выбери удобное время подъёма — я подскажу, когда ложиться, и буду "
        "присылать напоминания (для них нужен ещё часовой пояс — /settz).\n\n"
        "Все команды — /help",
        reply_markup=wake_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n"
        "/setwake — выбрать время подъёма\n"
        "/setwake 07:00 — сразу задать время\n"
        "/setbed 23:30 — задать свой отбой вручную\n"
        "/setbed auto — вернуть рекомендованный отбой\n"
        "/settz — выбрать часовой пояс (нужен для напоминаний)\n"
        "/me — мой режим и времена напоминаний\n"
        "/fact — случайный факт о сне\n"
        "/calc 07:00 — быстрый расчёт без сохранения\n"
        "/help — это сообщение"
    )


async def _prompt_tz_if_needed(message: Message, telegram_id: int):
    user = await db.get_user(telegram_id)
    if user and not user["timezone"]:
        await message.answer(
            "Чтобы я присылал напоминания, выбери часовой пояс:",
            reply_markup=tz_keyboard(),
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
    await _prompt_tz_if_needed(message, message.from_user.id)


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
    await _prompt_tz_if_needed(callback.message, callback.from_user.id)


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

    await db.set_bedtime(callback.from_user.id, value)
    await callback.message.answer("Сохранил твой отбой.\n\n"
                                  + format_plan(wake, value))
    await callback.answer("Готово")


@dp.message(Command("settz"))
async def cmd_settz(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Выбери часовой пояс:", reply_markup=tz_keyboard())
        return
    tz = command.args.strip()
    if not valid_tz(tz):
        await message.answer(
            "Не знаю такой пояс. Пример: <code>/settz Europe/Moscow</code>"
        )
        return
    await db.set_timezone(message.from_user.id, tz)
    user = await db.get_user(message.from_user.id)
    await message.answer(
        f"Часовой пояс сохранён: <b>{tz}</b>. Напоминания включены ✅\n\n"
        + format_full(user)
    )


@dp.callback_query(F.data.startswith("tz:"))
async def cb_tz(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await callback.message.answer(
            "Напиши пояс в формате Регион/Город, например: "
            "<code>/settz Asia/Novosibirsk</code>"
        )
        await callback.answer()
        return
    if not valid_tz(value):
        await callback.answer("Не знаю такой пояс", show_alert=True)
        return
    await db.set_timezone(callback.from_user.id, value)
    user = await db.get_user(callback.from_user.id)
    await callback.message.answer(
        f"Часовой пояс сохранён: <b>{value}</b>. Напоминания включены ✅\n\n"
        + format_full(user)
    )
    await callback.answer("Готово")


@dp.message(Command("me"))
async def cmd_me(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or not user["wake_time"]:
        await message.answer("Ты ещё не задал время подъёма. Нажми /setwake")
        return
    await message.answer(format_full(user), reply_markup=adjust_keyboard(user["wake_time"]))


@dp.message(Command("fact"))
async def cmd_fact(message: Message):
    uid = message.from_user.id
    idx = random.randrange(len(FACTS))
    if len(FACTS) > 1 and _last_fact.get(uid) == idx:
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


# ---------- планировщик напоминаний ----------
async def tick():
    """Один «такт»: проверяем у каждого пользователя его локальное время."""
    now_utc = datetime.now(dt_timezone.utc)
    try:
        users = await db.get_active_users()
    except Exception:
        logger.exception("Не удалось прочитать пользователей")
        return

    for user in users:
        try:
            tz = ZoneInfo(user["timezone"])
        except Exception:
            continue
        local = now_utc.astimezone(tz)
        hhmm = local.strftime("%H:%M")
        date_key = local.strftime("%Y-%m-%d")

        wake = user["wake_time"]
        bed = effective_bedtime(user)
        events = {
            "caffeine": shift_time(bed, -CAFFEINE_LEAD_MIN),
            "winddown": shift_time(bed, -WINDDOWN_LEAD_MIN),
            "bedtime": bed,
            "morning": shift_time(wake, MORNING_AFTER_MIN),
        }

        for event, event_time in events.items():
            if event_time != hhmm:
                continue
            guard = (user["telegram_id"], event)
            if _sent.get(guard) == date_key:   # уже отправляли сегодня
                continue
            _sent[guard] = date_key
            try:
                await bot.send_message(
                    user["telegram_id"], build_reminder(event, bed, wake)
                )
            except Exception as e:
                logger.warning(
                    "Не отправилось %s пользователю %s: %s",
                    event, user["telegram_id"], e,
                )


async def reminder_loop():
    logger.info("Планировщик напоминаний запущен")
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("Ошибка в tick()")
        await asyncio.sleep(30)


async def main():
    logger.info("Бот запускается...")
    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(reminder_loop())   # запускаем планировщик параллельно
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
