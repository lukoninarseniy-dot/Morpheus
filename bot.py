"""
Бот «Режим сна» — Шаг 4 (финальный).
Новое по сравнению с шагом 3:
- постоянное меню-кнопки внизу вместо запоминания команд;
- утренний опрос: лёг/встал вовремя и оценка самочувствия, всё пишется в дневник;
- сводка за неделю и за месяц (по расписанию и по запросу);
- отдельный переключатель уведомлений (включить/выключить);
- минимум смайликов в текстах.
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
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
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

CYCLE_MIN = 90
LATENCY_MIN = 15
CAFFEINE_LEAD_MIN = 9 * 60
WINDDOWN_LEAD_MIN = 60
MORNING_AFTER_MIN = 15
SUMMARY_TIME = "21:00"      # когда присылать недельную/месячную сводку

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_last_fact: dict[int, int] = {}
_sent: dict[tuple[int, str], str] = {}   # защита от повторной отправки за день

# Подписи кнопок меню (используются и как фильтры, и как сама клавиатура)
BTN_ME = "Мой режим"
BTN_LOG = "Отметить сон"
BTN_WAKE = "Время подъёма"
BTN_BED = "Отбой"
BTN_TZ = "Часовой пояс"
BTN_NOTIFY = "Уведомления"
BTN_WEEK = "Итоги недели"
BTN_MONTH = "Итоги месяца"
BTN_FACT = "Совет о сне"


# ============ расчёт времени ============
def parse_time(raw: str | None) -> str | None:
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
    h, m = map(int, wake.split(":"))
    wake_dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    bedtime = wake_dt - timedelta(minutes=cycles * CYCLE_MIN + latency)
    return bedtime.strftime("%H:%M")


def shift_time(hhmm: str, delta_min: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + delta_min) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def effective_bedtime(user) -> str:
    return user["custom_bedtime"] or calc_bedtime(user["wake_time"], 5)


def valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def user_now(user) -> datetime:
    tz = user["timezone"]
    return datetime.now(ZoneInfo(tz)) if tz else datetime.now(dt_timezone.utc)


# ============ тексты режима ============
def format_plan(wake: str, custom_bed: str | None) -> str:
    rec = calc_bedtime(wake, 5)
    if custom_bed:
        eff = custom_bed
        note = (f"Рекомендованный отбой — <b>{rec}</b> (5 циклов), "
                f"но у тебя выбран свой: <b>{custom_bed}</b>.")
    else:
        eff = rec
        note = f"Рекомендованный отбой — <b>{rec}</b> (5 циклов, около 7,5 часа)."
    return (
        f"Подъём: <b>{wake}</b>\n"
        f"Ложиться: <b>{eff}</b>\n\n"
        f"{note}\n\n"
        f"Варианты от подъёма {wake}:\n"
        f"• {calc_bedtime(wake, 6)} — 6 циклов (около 9 часов)\n"
        f"• {calc_bedtime(wake, 5)} — 5 циклов (около 7,5 часа), рекомендуется\n"
        f"• {calc_bedtime(wake, 4)} — 4 цикла (около 6 часов)"
    )


def format_full(user) -> str:
    wake = user["wake_time"]
    bed = effective_bedtime(user)
    text = format_plan(wake, user["custom_bedtime"])
    if user["timezone"]:
        state = "включены" if user["notify_enabled"] else "выключены"
        text += (
            f"\n\nЧасовой пояс: <b>{user['timezone']}</b>\n"
            f"Уведомления: <b>{state}</b>\n"
            f"Напоминания:\n"
            f"• дедлайн кофеина — {shift_time(bed, -CAFFEINE_LEAD_MIN)}\n"
            f"• подготовка ко сну — {shift_time(bed, -WINDDOWN_LEAD_MIN)}\n"
            f"• отбой — {bed}\n"
            f"• утро — {shift_time(wake, MORNING_AFTER_MIN)}"
        )
    else:
        text += "\n\nЧтобы включить напоминания, задай часовой пояс."
    return text


def build_reminder(event: str, bed: str, wake: str) -> str:
    if event == "caffeine":
        return (
            "<b>Дедлайн кофеина.</b> С этого момента лучше без кофе, чая, "
            "энергетиков и колы — кофеин выводится 5–7 часов и крадёт глубокий "
            f"сон. Отбой сегодня в {bed}."
        )
    if event == "winddown":
        return (
            f"<b>Через час отбой ({bed}).</b> Пора сворачиваться: приглуши яркий "
            "свет, отложи рабочие и тревожные ленты, можно принять тёплый душ. "
            "Плотно есть уже не стоит."
        )
    if event == "bedtime":
        return f"<b>Пора ложиться.</b> Отбой в {bed}. Убери телефон подальше, спокойной ночи."
    return ""


# ============ клавиатуры ============
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ME), KeyboardButton(text=BTN_LOG)],
            [KeyboardButton(text=BTN_WAKE), KeyboardButton(text=BTN_BED)],
            [KeyboardButton(text=BTN_TZ), KeyboardButton(text=BTN_NOTIFY)],
            [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_MONTH)],
            [KeyboardButton(text=BTN_FACT)],
        ],
        resize_keyboard=True,
    )


def wake_keyboard():
    b = InlineKeyboardBuilder()
    for t in ["06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]:
        b.button(text=t, callback_data=f"wake:{t}")
    b.button(text="Другое время", callback_data="wake:custom")
    b.adjust(3)
    return b.as_markup()


def adjust_keyboard(wake: str):
    early = calc_bedtime(wake, 6)
    late = calc_bedtime(wake, 4)
    b = InlineKeyboardBuilder()
    b.button(text=f"Раньше — {early} (9 ч)", callback_data=f"bed:{early}")
    b.button(text=f"Позже — {late} (6 ч)", callback_data=f"bed:{late}")
    b.button(text="Свой отбой", callback_data="bed:custom")
    b.button(text="Вернуть рекомендованное", callback_data="bed:auto")
    b.adjust(1)
    return b.as_markup()


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
    b = InlineKeyboardBuilder()
    for label, iana in RU_TIMEZONES:
        b.button(text=label, callback_data=f"tz:{iana}")
    b.button(text="Другой пояс", callback_data="tz:custom")
    b.adjust(2)
    return b.as_markup()


def notify_keyboard(enabled: bool):
    b = InlineKeyboardBuilder()
    if enabled:
        b.button(text="Выключить", callback_data="notify:off")
    else:
        b.button(text="Включить", callback_data="notify:on")
    return b.as_markup()


# ============ утренний дневник ============
def _ans_ru(v: str | None) -> str:
    return {"yes": "да", "almost": "почти", "no": "нет"}.get(v, str(v))


def _yesno_kb(field: str, date: str):
    b = InlineKeyboardBuilder()
    b.button(text="Да", callback_data=f"log:{field}:yes:{date}")
    b.button(text="Почти", callback_data=f"log:{field}:almost:{date}")
    b.button(text="Нет", callback_data=f"log:{field}:no:{date}")
    b.adjust(3)
    return b.as_markup()


def _scale_kb(field: str, date: str):
    b = InlineKeyboardBuilder()
    for n in range(1, 6):
        b.button(text=str(n), callback_data=f"log:{field}:{n}:{date}")
    b.adjust(5)
    return b.as_markup()


def render_checkin(log, date: str):
    """Возвращает (текст, клавиатура) для текущего шага утреннего опроса."""
    bed = log["on_time_bed"] if log else None
    wake = log["on_time_wake"] if log else None
    well = log["wellbeing"] if log else None
    d = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m")

    if bed is None:
        return ("Доброе утро. Отметим, как прошла ночь.\n\nЛёг спать вовремя?",
                _yesno_kb("bed", date))
    if wake is None:
        return (f"Ночь {d}. Лёг вовремя: {_ans_ru(bed)}.\n\nВстал вовремя?",
                _yesno_kb("wake", date))
    if well is None:
        return (f"Ночь {d}. Лёг: {_ans_ru(bed)}, встал: {_ans_ru(wake)}.\n\n"
                "Насколько выспался? (1 — совсем разбит, 5 — отлично)",
                _scale_kb("well", date))
    return (f"Записал ночь {d}: лёг — {_ans_ru(bed)}, встал — {_ans_ru(wake)}, "
            f"самочувствие — {well} из 5. Хорошего дня.", None)


# ============ сводки ============
def summarize(logs) -> dict:
    def adherence(vals):
        if not vals:
            return None
        score = sum({"yes": 1.0, "almost": 0.5, "no": 0.0}[v] for v in vals)
        return round(100 * score / len(vals))

    well_vals = [l["wellbeing"] for l in logs if l["wellbeing"] is not None]
    bed_vals = [l["on_time_bed"] for l in logs if l["on_time_bed"]]
    wake_vals = [l["on_time_wake"] for l in logs if l["on_time_wake"]]
    return {
        "nights": len(logs),
        "avg_well": round(sum(well_vals) / len(well_vals), 1) if well_vals else None,
        "bed": adherence(bed_vals),
        "wake": adherence(wake_vals),
    }


def _takeaway(s: dict) -> str:
    if s["nights"] < 3:
        return ("Маловато отметок для выводов. Чем регулярнее отмечаешься по "
                "утрам, тем полезнее будет сводка.")
    if s["bed"] is not None and s["bed"] < 50:
        return ("Отход ко сну часто сдвигался. Стабильное время отбоя — самое "
                "важное для качества сна, попробуй держать его ровнее.")
    if s["avg_well"] is not None and s["avg_well"] >= 4:
        return "Самочувствие высокое — режим работает, так держать."
    if s["avg_well"] is not None and s["avg_well"] < 3:
        return ("Самочувствие невысокое. Если часов сна хватает, обрати внимание "
                "на кофеин во второй половине дня, алкоголь и экраны перед сном.")
    return "Картина неплохая. Главное — держать режим стабильным изо дня в день."


def build_summary(title: str, period: str, stats: dict) -> str:
    lines = [f"<b>{title}</b> ({period})"]
    if stats["nights"] == 0:
        lines.append("За этот период нет ни одной отметки. Отмечайся по утрам — "
                     "и в следующий раз будет полная картина.")
        return "\n".join(lines)
    lines.append(f"Отмечено ночей: {stats['nights']}")
    if stats["bed"] is not None:
        lines.append(f"Ложился вовремя: {stats['bed']}%")
    if stats["wake"] is not None:
        lines.append(f"Вставал вовремя: {stats['wake']}%")
    if stats["avg_well"] is not None:
        lines.append(f"Среднее самочувствие: {stats['avg_well']} из 5")
    lines.append("")
    lines.append(_takeaway(stats))
    return "\n".join(lines)


def _week_bounds(d):
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _month_bounds(d):
    first = d.replace(day=1)
    nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
        else first.replace(month=first.month + 1)
    return first, nxt - timedelta(days=1)


async def week_summary_text(user) -> str:
    today = user_now(user).date()
    start, end = _week_bounds(today)
    logs = await db.get_logs_between(user["telegram_id"], start.isoformat(), end.isoformat())
    period = f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}"
    return build_summary("Итоги недели", period, summarize(logs))


async def month_summary_text(user) -> str:
    today = user_now(user).date()
    start, end = _month_bounds(today)
    logs = await db.get_logs_between(user["telegram_id"], start.isoformat(), end.isoformat())
    period = start.strftime("%B %Y")
    return build_summary("Итоги месяца", period, summarize(logs))


# ============ общие действия (для команд и кнопок меню) ============
async def show_me(message: Message, uid: int):
    user = await db.get_user(uid)
    if not user or not user["wake_time"]:
        await message.answer("Время подъёма ещё не задано. Нажми «Время подъёма».")
        return
    await message.answer(format_full(user), reply_markup=adjust_keyboard(user["wake_time"]))


async def show_bed(message: Message, uid: int):
    user = await db.get_user(uid)
    if not user or not user["wake_time"]:
        await message.answer("Сначала задай время подъёма.")
        return
    await message.answer(format_plan(user["wake_time"], user["custom_bedtime"]),
                         reply_markup=adjust_keyboard(user["wake_time"]))


async def send_fact(message: Message, uid: int):
    idx = random.randrange(len(FACTS))
    if len(FACTS) > 1 and _last_fact.get(uid) == idx:
        idx = (idx + 1) % len(FACTS)
    _last_fact[uid] = idx
    await message.answer(f"<b>Факт о сне</b>\n\n{FACTS[idx]}")


async def show_notify(message: Message, uid: int):
    user = await db.get_user(uid)
    enabled = bool(user["notify_enabled"]) if user else True
    state = "включены" if enabled else "выключены"
    await message.answer(f"Уведомления сейчас {state}.", reply_markup=notify_keyboard(enabled))


async def start_log(message: Message, uid: int):
    user = await db.get_user(uid)
    date = user_now(user).strftime("%Y-%m-%d") if user else \
        datetime.now(dt_timezone.utc).strftime("%Y-%m-%d")
    await db.ensure_log(uid, date)
    log = await db.get_log(uid, date)
    text, kb = render_checkin(log, date)
    await message.answer(text, reply_markup=kb)


async def send_week(message: Message, uid: int):
    user = await db.get_user(uid)
    if not user:
        await message.answer("Пока нечего показать. Начни с «Время подъёма».")
        return
    await message.answer(await week_summary_text(user))


async def send_month(message: Message, uid: int):
    user = await db.get_user(uid)
    if not user:
        await message.answer("Пока нечего показать. Начни с «Время подъёма».")
        return
    await message.answer(await month_summary_text(user))


# ============ команды и кнопки ============
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет. Я помогу наладить режим сна: подскажу время отбоя, напомню "
        "ложиться и собирать утренние отметки, а в конце недели и месяца пришлю "
        "сводку.\n\nНачни с кнопки «Время подъёма» внизу.",
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Пользуйся кнопками меню внизу. Если меню скрылось — нажми на значок "
        "клавиатуры в поле ввода.\n\n"
        "«Время подъёма» — задать подъём и увидеть отбой\n"
        "«Отбой» — изменить время отбоя\n"
        "«Часовой пояс» — нужен для напоминаний\n"
        "«Уведомления» — включить или выключить\n"
        "«Отметить сон» — утренняя отметка вручную\n"
        "«Итоги недели» / «Итоги месяца» — сводки\n"
        "«Совет о сне» — случайный факт",
        reply_markup=main_menu(),
    )


# --- режим ---
@dp.message(Command("me"))
async def cmd_me(message: Message):
    await show_me(message, message.from_user.id)


@dp.message(F.text == BTN_ME)
async def btn_me(message: Message):
    await show_me(message, message.from_user.id)


# --- время подъёма ---
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


@dp.message(F.text == BTN_WAKE)
async def btn_wake(message: Message):
    await message.answer("Выбери время подъёма:", reply_markup=wake_keyboard())


@dp.callback_query(F.data.startswith("wake:"))
async def cb_wake(callback: CallbackQuery):
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await callback.message.answer("Напиши время так: <code>/setwake 06:45</code>")
        await callback.answer()
        return
    await db.set_wake(callback.from_user.id, value)
    await callback.message.answer(format_plan(value, None), reply_markup=adjust_keyboard(value))
    await callback.answer("Время подъёма сохранено")
    await _prompt_tz_if_needed(callback.message, callback.from_user.id)


# --- отбой ---
@dp.message(Command("setbed"))
async def cmd_setbed(message: Message, command: CommandObject):
    user = await db.get_user(message.from_user.id)
    if not user or not user["wake_time"]:
        await message.answer("Сначала задай время подъёма.")
        return
    arg = (command.args or "").strip().lower()
    if arg in ("auto", "авто", "сброс"):
        await db.set_bedtime(message.from_user.id, None)
        await message.answer("Вернул рекомендованный отбой.\n\n"
                             + format_plan(user["wake_time"], None))
        return
    bed = parse_time(command.args)
    if not bed:
        await message.answer("Формат: <code>/setbed 23:30</code> или <code>/setbed auto</code>")
        return
    await db.set_bedtime(message.from_user.id, bed)
    await message.answer("Сохранил твой отбой.\n\n" + format_plan(user["wake_time"], bed))


@dp.message(F.text == BTN_BED)
async def btn_bed(message: Message):
    await show_bed(message, message.from_user.id)


@dp.callback_query(F.data.startswith("bed:"))
async def cb_bed(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["wake_time"]:
        await callback.message.answer("Сначала задай время подъёма.")
        await callback.answer()
        return
    wake = user["wake_time"]
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        await callback.message.answer("Напиши отбой так: <code>/setbed 23:30</code>")
        await callback.answer()
        return
    if value == "auto":
        await db.set_bedtime(callback.from_user.id, None)
        await callback.message.answer("Вернул рекомендованное.\n\n" + format_plan(wake, None))
        await callback.answer("Готово")
        return
    await db.set_bedtime(callback.from_user.id, value)
    await callback.message.answer("Сохранил твой отбой.\n\n" + format_plan(wake, value))
    await callback.answer("Готово")


# --- часовой пояс ---
async def _prompt_tz_if_needed(message: Message, uid: int):
    user = await db.get_user(uid)
    if user and not user["timezone"]:
        await message.answer("Чтобы я присылал напоминания, выбери часовой пояс:",
                             reply_markup=tz_keyboard())


@dp.message(Command("settz"))
async def cmd_settz(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Выбери часовой пояс:", reply_markup=tz_keyboard())
        return
    tz = command.args.strip()
    if not valid_tz(tz):
        await message.answer("Не знаю такой пояс. Пример: <code>/settz Europe/Moscow</code>")
        return
    await db.set_timezone(message.from_user.id, tz)
    user = await db.get_user(message.from_user.id)
    await message.answer(f"Часовой пояс сохранён: <b>{tz}</b>. Напоминания включены.\n\n"
                         + format_full(user))


@dp.message(F.text == BTN_TZ)
async def btn_tz(message: Message):
    await message.answer("Выбери часовой пояс:", reply_markup=tz_keyboard())


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
        f"Часовой пояс сохранён: <b>{value}</b>. Напоминания включены.\n\n"
        + format_full(user)
    )
    await callback.answer("Готово")


# --- уведомления ---
@dp.message(Command("notifications"))
async def cmd_notifications(message: Message):
    await show_notify(message, message.from_user.id)


@dp.message(F.text == BTN_NOTIFY)
async def btn_notify(message: Message):
    await show_notify(message, message.from_user.id)


@dp.callback_query(F.data.startswith("notify:"))
async def cb_notify(callback: CallbackQuery):
    enabled = callback.data.split(":", 1)[1] == "on"
    await db.set_notify(callback.from_user.id, enabled)
    state = "включены" if enabled else "выключены"
    await callback.message.edit_text(f"Уведомления {state}.",
                                     reply_markup=notify_keyboard(enabled))
    await callback.answer("Готово")


# --- дневник сна ---
@dp.message(Command("log"))
async def cmd_log(message: Message):
    await start_log(message, message.from_user.id)


@dp.message(F.text == BTN_LOG)
async def btn_log(message: Message):
    await start_log(message, message.from_user.id)


@dp.callback_query(F.data.startswith("log:"))
async def cb_log(callback: CallbackQuery):
    _, field, value, date = callback.data.split(":", 3)
    uid = callback.from_user.id
    column = {"bed": "on_time_bed", "wake": "on_time_wake", "well": "wellbeing"}[field]
    stored = int(value) if field == "well" else value

    await db.ensure_log(uid, date)
    await db.update_log_field(uid, date, column, stored)
    log = await db.get_log(uid, date)
    text, kb = render_checkin(log, date)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# --- сводки и факты ---
@dp.message(Command("week"))
async def cmd_week(message: Message):
    await send_week(message, message.from_user.id)


@dp.message(F.text == BTN_WEEK)
async def btn_week(message: Message):
    await send_week(message, message.from_user.id)


@dp.message(Command("month"))
async def cmd_month(message: Message):
    await send_month(message, message.from_user.id)


@dp.message(F.text == BTN_MONTH)
async def btn_month(message: Message):
    await send_month(message, message.from_user.id)


@dp.message(Command("fact"))
async def cmd_fact(message: Message):
    await send_fact(message, message.from_user.id)


@dp.message(F.text == BTN_FACT)
async def btn_fact(message: Message):
    await send_fact(message, message.from_user.id)


@dp.message(Command("calc"))
async def cmd_calc(message: Message, command: CommandObject):
    wake = parse_time(command.args)
    if not wake:
        await message.answer("Формат: <code>/calc 07:00</code>")
        return
    await message.answer(format_plan(wake, None))


# --- запасной обработчик любого другого текста ---
@dp.message(F.text)
async def fallback(message: Message):
    await message.answer("Не понял. Пользуйся кнопками меню внизу или /help.",
                         reply_markup=main_menu())


# ============ планировщик ============
async def _send_summary_if_due(user, local, hhmm, date_key):
    if hhmm != SUMMARY_TIME:
        return
    uid = user["telegram_id"]
    # недельная — в воскресенье
    if local.weekday() == 6:
        guard = (uid, "weekly")
        if _sent.get(guard) != date_key:
            _sent[guard] = date_key
            try:
                await bot.send_message(uid, await week_summary_text(user))
            except Exception as e:
                logger.warning("weekly не отправилась %s: %s", uid, e)
    # месячная — в последний день месяца
    if (local + timedelta(days=1)).month != local.month:
        guard = (uid, "monthly")
        if _sent.get(guard) != date_key:
            _sent[guard] = date_key
            try:
                await bot.send_message(uid, await month_summary_text(user))
            except Exception as e:
                logger.warning("monthly не отправилась %s: %s", uid, e)


async def tick():
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
        uid = user["telegram_id"]

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
            guard = (uid, event)
            if _sent.get(guard) == date_key:
                continue
            _sent[guard] = date_key
            try:
                if event == "morning":
                    await db.ensure_log(uid, date_key)
                    log = await db.get_log(uid, date_key)
                    text, kb = render_checkin(log, date_key)
                    await bot.send_message(uid, text, reply_markup=kb)
                else:
                    await bot.send_message(uid, build_reminder(event, bed, wake))
            except Exception as e:
                logger.warning("Не отправилось %s пользователю %s: %s", event, uid, e)

        await _send_summary_if_due(user, local, hhmm, date_key)


async def reminder_loop():
    logger.info("Планировщик запущен")
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("Ошибка в tick()")
        await asyncio.sleep(30)


async def setup_commands():
    await bot.set_my_commands([
        BotCommand(command="me", description="Мой режим"),
        BotCommand(command="log", description="Отметить сон"),
        BotCommand(command="setwake", description="Время подъёма"),
        BotCommand(command="settz", description="Часовой пояс"),
        BotCommand(command="notifications", description="Уведомления"),
        BotCommand(command="week", description="Итоги недели"),
        BotCommand(command="month", description="Итоги месяца"),
        BotCommand(command="fact", description="Совет о сне"),
        BotCommand(command="help", description="Помощь"),
    ])


async def main():
    logger.info("Бот запускается...")
    await db.init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await setup_commands()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
