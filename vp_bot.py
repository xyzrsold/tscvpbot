# -*- coding: utf-8 -*-
"""
БОТ ДЛЯ ВЗАИМНОГО ПИАРА (ВП)

Как работает:
  1. Впшеры кидают посты в группу.
  2. До начала рабочего времени (22:15 МСК) бот только собирает посты и
     предлагает автору выбрать время (кнопками или ответом на пост: "23:40").
  3. В 22:15 бот расставляет время всем постам без времени (шаг 5 минут)
     и публикует их в канал строго по расписанию. Отложку Telegram НЕ использует.
  4. Смена = посты, отправленные с 09:51 до 09:50 следующих суток.
     Новая смена - новая пачка постов, дублей нет.
  5. Одинаковые по тексту посты на одной смене бот отклоняет (антидубль).
  6. /console (в личке бота) - консоль админов: статистика впшеров и статистика ВП
     за текущую и прошлую неделю. Админов консоли назначает только владелец (OWNER_ID).

Запуск:  pip install "aiogram>=3.7"   затем   python vp_bot.py
"""

import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =====================================================================
#  НАСТРОЙКИ - ЗАПОЛНИТЕ ЭТИ 5 СТРОК (текст внутри кавычек)
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")        # токен от @BotFather
GROUP_ID = int(os.getenv("GROUP_ID", "-1004484271989"))                             # ID группы впшеров (узнать командой /id)
CHANNEL_ID = os.getenv("CHANNEL_ID", "@tsconf")        # @username канала или число вида -100...
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# ADMIN_IDS - ID старших модераторов через запятую. Они (и владелец OWNER_ID) могут менять/отменять любые посты.
OWNER_ID = int(os.getenv("OWNER_ID", "5265325991"))                             # ID владельца бота (узнать командой /id). Только он назначает админов консоли

# =====================================================================
#  ПРАВИЛА ВРЕМЕНИ (менять только если изменятся правила ВП)
# =====================================================================
MSK = timezone(timedelta(hours=3))   # московское время (UTC+3, без перехода на летнее)
WORK_START = dtime(22, 15)           # первый пост смены
WORK_END = dtime(9, 50)              # последний возможный пост смены
SHIFT_BORDER = dtime(9, 51)          # граница смен: с 09:51 начинается новая смена
STEP = timedelta(minutes=5)          # интервал между постами
TICK_SECONDS = 10                    # как часто бот проверяет, не пора ли публиковать
MISSED_GRACE_MIN = 10                # если бот проспал время поста дольше 10 мин - пост переставится на ближайшее свободное
OVERFLOW_TIME = dtime(9, 55)         # если слоты кончились (140 шт.), остаток уходит пачкой в это время
OVERFLOW_GRACE = timedelta(minutes=60)  # сколько после OVERFLOW_TIME бот ещё дожимает пачку, потом смена закрывается
DB_FILE = "vp_bot.db"                # файл с базой (создастся сам)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
router = Router()

# =====================================================================
#  БАЗА ДАННЫХ (SQLite - простой файл, ничего устанавливать не нужно)
# =====================================================================
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row


def q(sql, args=()):
    """Прочитать данные из базы."""
    return conn.execute(sql, args).fetchall()


def run(sql, args=()):
    """Записать данные в базу."""
    cur = conn.execute(sql, args)
    conn.commit()
    return cur


def init_db():
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            shift          TEXT NOT NULL,      -- дата смены (день, в который смена началась)
            chat_id        INTEGER NOT NULL,
            author_id      INTEGER,
            author_name    TEXT,
            media_group_id TEXT,               -- для постов-альбомов (несколько фото)
            created_at     INTEGER,
            scheduled_at   INTEGER,            -- время публикации (unix), пусто = ещё не назначено
            manual         INTEGER DEFAULT 0,  -- 1 = время выбрал сам автор
            status         TEXT DEFAULT 'pending',  -- pending / sent / failed / cancelled / expired / duplicate
            prompt_msg_id  INTEGER,            -- сообщение бота с кнопками выбора времени
            sent_at        INTEGER
        );
        CREATE TABLE IF NOT EXISTS post_messages (
            post_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            UNIQUE (post_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS shifts (
            shift   TEXT PRIMARY KEY,
            started INTEGER DEFAULT 0,         -- 1 = рабочее время уже началось, расписание объявлено
            closed  INTEGER DEFAULT 0          -- 1 = смена завершена, итог отправлен
        );
        CREATE TABLE IF NOT EXISTS shift_stats (
            shift    TEXT PRIMARY KEY,         -- дата смены
            vp_count INTEGER NOT NULL DEFAULT 0  -- сколько ВП (опубликованных постов) было за смену
        );
        CREATE TABLE IF NOT EXISTS console_admins (
            user_id  INTEGER PRIMARY KEY,      -- админы консоли (назначает владелец)
            name     TEXT,
            added_at INTEGER
        );
        """
    )
    # миграция для базы, созданной старой версией бота
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    if "text_key" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN text_key TEXT")  # отпечаток текста поста (антидубль)
    if "fwd" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN fwd INTEGER DEFAULT 0")  # 1 = в посте есть премиум-эмодзи, пересылаем
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_dedupe ON posts (shift, text_key)")
    conn.commit()


# Ссылка tg://user?id=... в личке с ботом Telegram показывает ссылкой только для тех, кто хоть раз
# писал боту или нажимал его кнопку. В консоли (личка) статистику читает админ, а впшеры могли
# никогда не писать боту - поэтому там ссылка другого вида. Поменять: "user" или "openmessage".
CONSOLE_LINK = "openmessage"


def mention(user_id, name, scheme="user"):
    """Кликабельный ник (HTML). ID в тексте не показывается."""
    label = html.escape((name or "").strip() or "впшер")
    if scheme == "openmessage":
        return f'<a href="tg://openmessage?user_id={user_id}">{label}</a>'
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def post_author(post):
    """Подпись автора поста: кликабельный ник по ID."""
    if post["author_id"] is None:
        return html.escape(post["author_name"] or "впшер")
    return mention(post["author_id"], post["author_name"])


def get_post(pid):
    rows = q("SELECT * FROM posts WHERE id=?", (pid,))
    return rows[0] if rows else None


# =====================================================================
#  РАБОТА СО ВРЕМЕНЕМ И СМЕНАМИ
# =====================================================================
def now_msk():
    return datetime.now(MSK)


def shift_of(dt):
    """
    ВАЖНАЯ ФУНКЦИЯ: к какой смене относится момент времени.
    Смена = с 09:51 до 09:50 следующих суток. Возвращает дату начала смены.
    Пример: 05.03 в 23:00 -> смена 05.03;  06.03 в 02:00 -> тоже смена 05.03;
            06.03 в 09:51 -> уже смена 06.03.
    """
    border = datetime.combine(dt.date(), SHIFT_BORDER, tzinfo=MSK)
    return dt.date() if dt >= border else dt.date() - timedelta(days=1)


def window_bounds(shift):
    """Рабочее окно смены: от 22:15 до 09:51 следующего дня (09:51 не включается)."""
    start = datetime.combine(shift, WORK_START, tzinfo=MSK)
    end_excl = datetime.combine(shift + timedelta(days=1), SHIFT_BORDER, tzinfo=MSK)
    return start, end_excl


def all_slots(shift):
    """Все возможные времена публикации смены: 22:15, 22:20, ... 09:50."""
    start, end_excl = window_bounds(shift)
    slots, t = [], start
    while t < end_excl:
        slots.append(t)
        t += STEP
    return slots


def shift_state(shift, now):
    """'before' - рабочее время ещё не началось, 'active' - идёт, 'closed' - закончилось."""
    start, end_excl = window_bounds(shift)
    if now < start:
        return "before"
    if now < end_excl:
        return "active"
    return "closed"


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")


def shift_title(shift):
    return f"ночь {shift:%d.%m} → {shift + timedelta(days=1):%d.%m}"


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def week_start(shift, offset=0):
    """
    Понедельник недели, в которую входит смена. offset=0 - эта неделя, 1 - прошлая.
    Неделя = смены с понедельника по воскресенье (по дню начала смены).
    """
    return shift - timedelta(days=shift.weekday()) - timedelta(days=7 * offset)


# =====================================================================
#  РАССТАНОВКА ВРЕМЕНИ ДЛЯ ПОСТОВ
# =====================================================================
def free_slots(shift, now, exclude_post=None):
    """Свободные времена смены. В рабочее время прошедшие минуты не предлагаются."""
    state = shift_state(shift, now)
    if state == "closed":
        return []
    rows = q(
        "SELECT scheduled_at FROM posts WHERE shift=? AND scheduled_at IS NOT NULL "
        "AND status IN ('pending','sent') AND id != ?",
        (shift.isoformat(), exclude_post or -1),
    )
    busy = {r["scheduled_at"] for r in rows}
    floor = now.replace(second=0, microsecond=0)
    result = []
    for s in all_slots(shift):
        if int(s.timestamp()) in busy:
            continue
        if state == "active" and s < floor:
            continue
        result.append(s)
    return result


def overflow_dt(shift):
    """Время, когда уходит пачка постов, которым не хватило слотов (09:55 следующих суток)."""
    return datetime.combine(shift + timedelta(days=1), OVERFLOW_TIME, tzinfo=MSK)


def set_overflow(pid, shift):
    """Поставить пост в пачку на 09:55 (manual=0, чтобы он мог переехать в слот, если тот освободится)."""
    run("UPDATE posts SET scheduled_at=?, manual=0 WHERE id=?", (int(overflow_dt(shift).timestamp()), pid))


def is_overflow(post, shift):
    return post["scheduled_at"] == int(overflow_dt(shift).timestamp())


def when_text(post, shift):
    """Фраза про время выхода поста."""
    if is_overflow(post, shift):
        return f"в {OVERFLOW_TIME:%H:%M} (свободные места на ночь закончились, уйдёт пачкой вместе с остальными)"
    return f"в {fmt_ts(post['scheduled_at'])}"


def plan_shift(shift, now):
    """
    ВАЖНАЯ ФУНКЦИЯ: автоматическая расстановка времени.
    Берёт посты смены без времени (в порядке поступления) и ставит каждому
    ближайшее свободное время с шагом 5 минут. Если мест не хватило - пост
    уходит в пачку на 09:55 (OVERFLOW_TIME), вместе с остальными такими же.
    Если слот освободился (например, отменили пост) - посты из пачки возвращаются в очередь.
    Возвращает (расставленные, ушедшие в пачку).
    """
    placed, overflow = [], []
    free = free_slots(shift, now)
    if free:  # есть место - пачку на 09:55 разбираем заново, по порядку поступления
        run("UPDATE posts SET scheduled_at=NULL WHERE shift=? AND status='pending' "
            "AND manual=0 AND scheduled_at=?", (shift.isoformat(), int(overflow_dt(shift).timestamp())))
    rows = q(
        "SELECT * FROM posts WHERE shift=? AND status='pending' AND scheduled_at IS NULL ORDER BY id",
        (shift.isoformat(),),
    )
    for r in rows:
        if free:
            slot = free.pop(0)
            run("UPDATE posts SET scheduled_at=? WHERE id=?", (int(slot.timestamp()), r["id"]))
            placed.append((r, slot))
        else:
            set_overflow(r["id"], shift)
            overflow.append(r)
    return placed, overflow


def reschedule_missed(shift, now):
    """Если бот был выключен и время поста давно прошло - сбросить время, чтобы пост встал заново."""
    limit = int((now - timedelta(minutes=MISSED_GRACE_MIN)).timestamp())
    run(
        "UPDATE posts SET scheduled_at=NULL WHERE shift=? AND status='pending' "
        "AND scheduled_at IS NOT NULL AND scheduled_at < ?",
        (shift.isoformat(), limit),
    )


def set_manual_time(pid, slot):
    run("UPDATE posts SET scheduled_at=?, manual=1 WHERE id=?", (int(slot.timestamp()), pid))


# =====================================================================
#  ПРИЁМ ПОСТОВ ИЗ ГРУППЫ
# =====================================================================
_INVISIBLE_RE = re.compile("[\u200b-\u200d\u2060\ufeff]")


def make_text_key(message):
    """
    Отпечаток содержания поста для антидубля: текст (или подпись к фото/видео) без учёта
    регистра, пробелов и переносов строк + адреса скрытых ссылок (текст со ссылкой внутри).
    Пост без текста (просто картинка) не сравнивается - вернёт None.
    """
    text = message.text or message.caption or ""
    norm = " ".join(_INVISIBLE_RE.sub("", text).split()).casefold()
    if not norm:
        return None
    ents = message.entities or message.caption_entities or []
    urls = sorted(e.url.strip().lower() for e in ents if e.type == "text_link" and e.url)
    return hashlib.sha256((norm + "\n" + "\n".join(urls)).encode("utf-8")).hexdigest()


def find_duplicate(shift_str, text_key, pid):
    """Есть ли на этой смене такой же пост (живой: ожидает или уже опубликован)? Возвращает его или None."""
    if not text_key:
        return None
    rows = q(
        "SELECT * FROM posts WHERE shift=? AND text_key=? AND id != ? "
        "AND status IN ('pending','sent') ORDER BY id LIMIT 1",
        (shift_str, text_key, pid),
    )
    return rows[0] if rows else None


def has_custom_emoji(message):
    """Есть ли в сообщении премиум (кастомные) эмодзи."""
    ents = list(message.entities or []) + list(message.caption_entities or [])
    return any(e.type == "custom_emoji" for e in ents)


def register_message(message, shift_str, text_key=None):
    """
    ВАЖНАЯ ФУНКЦИЯ: записать сообщение впшера как пост.
    Альбом (несколько фото одним постом) склеивается в один пост.
    Возвращает (id поста, это новый пост?).
    """
    mg = message.media_group_id
    fwd = 1 if has_custom_emoji(message) else 0
    if mg:
        rows = q("SELECT id FROM posts WHERE chat_id=? AND media_group_id=?", (message.chat.id, str(mg)))
        if rows:
            run("INSERT OR IGNORE INTO post_messages (post_id, message_id) VALUES (?, ?)",
                (rows[0]["id"], message.message_id))
            if fwd:
                run("UPDATE posts SET fwd=1 WHERE id=?", (rows[0]["id"],))
            return rows[0]["id"], False
    cur = run(
        "INSERT INTO posts (shift, chat_id, author_id, author_name, media_group_id, created_at, text_key, fwd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (shift_str, message.chat.id, message.from_user.id, message.from_user.full_name,
         str(mg) if mg else None, int(now_msk().timestamp()), text_key, fwd),
    )
    pid = cur.lastrowid
    run("INSERT INTO post_messages (post_id, message_id) VALUES (?, ?)", (pid, message.message_id))
    run("INSERT OR IGNORE INTO shifts (shift) VALUES (?)", (shift_str,))
    return pid, True


def find_post_by_message(chat_id, message_id):
    """Найти пост по сообщению (само сообщение поста или сообщение бота с кнопками)."""
    rows = q(
        "SELECT p.* FROM posts p JOIN post_messages m ON m.post_id = p.id "
        "WHERE p.chat_id=? AND m.message_id=?",
        (chat_id, message_id),
    )
    if rows:
        return rows[0]
    rows = q("SELECT * FROM posts WHERE chat_id=? AND prompt_msg_id=?", (chat_id, message_id))
    return rows[0] if rows else None


def can_edit(user_id, post):
    """Менять/отменять пост может автор, владелец бота (OWNER_ID) или админ из ADMIN_IDS."""
    return user_id == post["author_id"] or is_owner(user_id) or user_id in ADMIN_IDS


# =====================================================================
#  КЛАВИАТУРЫ ВЫБОРА ВРЕМЕНИ
# =====================================================================
def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def prompt_kb(pid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏰ Выбрать время", callback_data=f"vp:pick:{pid}"),
        InlineKeyboardButton(text="🤖 Пусть бот решит", callback_data=f"vp:auto:{pid}"),
    ]])


def hours_kb(pid, free):
    by_hour = {}
    for s in free:
        by_hour.setdefault(s.hour, []).append(s)
    buttons = [InlineKeyboardButton(text=f"{h:02d}:xx", callback_data=f"vp:hour:{pid}:{h}") for h in by_hour]
    rows = chunk(buttons, 4)
    rows.append([InlineKeyboardButton(text="🤖 Пусть бот решит", callback_data=f"vp:auto:{pid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def minutes_kb(pid, slots):
    buttons = [InlineKeyboardButton(text=s.strftime("%H:%M"), callback_data=f"vp:slot:{pid}:{int(s.timestamp())}")
               for s in slots]
    rows = chunk(buttons, 4)
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data=f"vp:pick:{pid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =====================================================================
#  КОМАНДЫ
# =====================================================================
HELP_TEXT = (
    "Как работает бот:\n"
    "1) Отправьте пост в рабочую группу.\n"
    "2) До 22:15 МСК бот просто собирает посты. Время можно выбрать кнопкой или ответом на свой пост, например: 23:40. Если ничего не выбрать - бот поставит ближайшее свободное время сам.\n"
    "3) С 22:15 до 09:50 бот публикует посты в канал с интервалом 5 минут.\n"
    "4) Пост, отправленный ночью, тоже уйдёт в эту же ночь.\n\n"
    "Команды:\n"
    "/queue - расписание текущей смены\n"
    "/cancel - ответьте этой командой на пост, чтобы отменить его\n"
)


@router.message(Command("start", "help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("id"))
async def cmd_id(message: Message):
    """Помогает узнать ID группы для настроек (работает в любом чате)."""
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"ID этого чата: {message.chat.id}\nВаш ID: {uid}")


@router.message(Command("queue"), F.chat.id == GROUP_ID)
async def cmd_queue(message: Message):
    """Показать расписание текущей смены."""
    now = now_msk()
    shift = shift_of(now)
    rows = q(
        "SELECT * FROM posts WHERE shift=? AND status IN ('pending','sent') "
        "ORDER BY (scheduled_at IS NULL), scheduled_at, id",
        (shift.isoformat(),),
    )
    if not rows:
        await message.answer(f"В смене ({shift_title(shift)}) постов пока нет.")
        return
    lines = [f"<b>Смена: {shift_title(shift)}</b>"]
    for r in rows:
        if r["status"] == "sent":
            lines.append(f"✅ {fmt_ts(r['scheduled_at'])} - {post_author(r)} (опубликован)")
        elif r["scheduled_at"]:
            lines.append(f"🕒 {fmt_ts(r['scheduled_at'])} - {post_author(r)}")
        else:
            lines.append(f"⏳ время назначит бот - {post_author(r)}")
    await send_long(message.bot, message.chat.id, "\n".join(lines))


@router.message(Command("cancel"), F.chat.id == GROUP_ID)
async def cmd_cancel(message: Message):
    """Отменить пост: ответьте на него командой /cancel."""
    if not message.reply_to_message:
        await message.reply("Ответьте командой /cancel на тот пост, который нужно отменить.")
        return
    post = find_post_by_message(message.chat.id, message.reply_to_message.message_id)
    if not post:
        await message.reply("Не нашёл такой пост.")
        return
    if not can_edit(message.from_user.id, post):
        await message.reply("Отменить пост может только его автор или админ.")
        return
    if post["status"] != "pending":
        await message.reply("Этот пост уже нельзя отменить (опубликован или обработан).")
        return
    run("UPDATE posts SET status='cancelled' WHERE id=?", (post["id"],))
    await message.reply("<b>🚫 Пост отменён</b>, в канал он не уйдёт.")


# =====================================================================
#  ВРЕМЯ, ЗАДАННОЕ ТЕКСТОМ (ответом на пост: "23:40")
# =====================================================================
TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*$")


@router.message(F.chat.id == GROUP_ID, F.reply_to_message, F.text.regexp(TIME_RE.pattern))
async def on_time_reply(message: Message):
    """
    ВАЖНАЯ ФУНКЦИЯ: впшер отвечает на свой пост временем (например 23:40).
    Если время занято или не попадает в сетку 5 минут - ставим на ближайшее свободное после него.
    """
    post = find_post_by_message(message.chat.id, message.reply_to_message.message_id)
    if not post:
        return
    if not can_edit(message.from_user.id, post):
        await message.reply("Менять время может только автор поста или админ.")
        return
    if post["status"] != "pending":
        await message.reply("Для этого поста время уже менять нельзя.")
        return

    m = TIME_RE.match(message.text)
    h, mi = int(m.group(1)), int(m.group(2))
    shift = date.fromisoformat(post["shift"])
    now = now_msk()
    if shift_state(shift, now) == "closed":
        await message.reply("Смена уже закончилась.")
        return

    day = shift if h >= 22 else shift + timedelta(days=1)
    want = datetime.combine(day, dtime(h, mi), tzinfo=MSK)
    start, end_excl = window_bounds(shift)
    if want < start or want >= end_excl:
        await message.reply("Время должно быть между 22:15 и 09:50 (МСК).")
        return

    slot = next((s for s in free_slots(shift, now, exclude_post=post["id"]) if s >= want), None)
    if slot is None:
        set_overflow(post["id"], shift)
        await message.reply(f"<b>⚠️ Свободных мест нет.</b> Пост уйдёт пачкой в {OVERFLOW_TIME:%H:%M}.")
        return
    set_manual_time(post["id"], slot)
    if slot == want:
        await message.reply(f"<b>✅ Пост выйдет в {slot:%H:%M}.</b>")
    else:
        await message.reply(f"<b>✅ Время изменено.</b> {want:%H:%M} занято или не попадает в сетку 5 минут. "
                            f"Поставил на ближайшее свободное: {slot:%H:%M}.")


# =====================================================================
#  КНОПКИ ВЫБОРА ВРЕМЕНИ
# =====================================================================
async def safe_edit(cb: CallbackQuery, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # например, текст не изменился - не страшно


@router.callback_query(F.data.startswith("vp:"))
async def on_callback(cb: CallbackQuery):
    parts = cb.data.split(":")
    action, pid = parts[1], int(parts[2])
    post = get_post(pid)
    if not post:
        await cb.answer("Пост не найден", show_alert=True)
        return
    if not can_edit(cb.from_user.id, post):
        await cb.answer("Это не ваш пост", show_alert=True)
        return
    if post["status"] != "pending":
        await cb.answer("Этот пост уже обработан", show_alert=True)
        return

    shift = date.fromisoformat(post["shift"])
    now = now_msk()
    free = free_slots(shift, now, exclude_post=pid)

    if action == "auto":
        # ближайшее свободное время; если мест нет - пачка на 09:55
        if free:
            slot = free[0]
            set_manual_time(pid, slot)
            await safe_edit(cb, f"<b>✅ Пост выйдет в {slot:%H:%M}</b> (ближайшее свободное время).")
            await cb.answer(f"Выйдет в {slot:%H:%M}")
        else:
            set_overflow(pid, shift)
            await safe_edit(cb, f"<b>⚠️ Свободных мест не осталось.</b> Пост уйдёт пачкой в {OVERFLOW_TIME:%H:%M}.")
            await cb.answer(f"Выйдет в {OVERFLOW_TIME:%H:%M}")
        return
    elif action == "pick":
        if not free:
            await safe_edit(cb, "Свободных мест нет.")
        else:
            await safe_edit(cb, "Выберите час (МСК):", hours_kb(pid, free))
    elif action == "hour":
        hour = int(parts[3])
        slots = [s for s in free if s.hour == hour]
        if not slots:
            await safe_edit(cb, "В этом часу свободных мест нет. Выберите другой час:", hours_kb(pid, free))
        else:
            await safe_edit(cb, f"Выберите время в {hour:02d}:xx:", minutes_kb(pid, slots))
    elif action == "slot":
        slot = datetime.fromtimestamp(int(parts[3]), MSK)
        if slot not in free:
            await cb.answer("Это время уже занято, выберите другое", show_alert=True)
            await safe_edit(cb, "Выберите час (МСК):", hours_kb(pid, free))
            return
        set_manual_time(pid, slot)
        await safe_edit(cb, f"<b>✅ Пост выйдет в {slot:%H:%M}.</b>")
    await cb.answer()


# =====================================================================
#  КОНСОЛЬ АДМИНОВ (только в личке бота: /console)
# =====================================================================
def is_owner(user_id):
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_console_admin(user_id):
    """Владелец или админ, которого назначил владелец."""
    return is_owner(user_id) or bool(q("SELECT 1 FROM console_admins WHERE user_id=?", (user_id,)))


def week_label(offset):
    mon = week_start(shift_of(now_msk()), offset)
    return f"{mon:%d.%m} Пн – {mon + timedelta(days=6):%d.%m} Вс"


def week_name(offset):
    return "текущая неделя" if offset == 0 else "прошлая неделя"


def authors_stats_text(offset):
    """
    Статистика впшеров за неделю: кто писал в рабочий чат и сколько ВП сделал
    (ВП = пост, который бот опубликовал в канал). Неделя: смены с понедельника по воскресенье.
    """
    mon = week_start(shift_of(now_msk()), offset)
    rows = q(
        "SELECT author_id, author_name, status FROM posts WHERE shift >= ? AND shift <= ? ORDER BY created_at, id",
        (mon.isoformat(), (mon + timedelta(days=6)).isoformat()),
    )
    stats = {}  # author_id -> [имя, кол-во ВП]
    for r in rows:
        if r["author_id"] is None:
            continue
        item = stats.setdefault(r["author_id"], [None, 0])
        item[0] = r["author_name"] or item[0]
        if r["status"] == "sent":
            item[1] += 1
    head = f"<b>📊 Статистика впшеров - {week_name(offset)}</b>\n{week_label(offset)}\n"
    if not stats:
        return head + "\nДанных пока нет."
    ordered = sorted(stats.items(), key=lambda kv: (-kv[1][1], (kv[1][0] or "").lower()))
    lines, size = [], len(head)
    for i, (uid, (name, cnt)) in enumerate(ordered, 1):
        line = f"{i}. {mention(uid, name, CONSOLE_LINK)} - {cnt} вп"
        if size + len(line) > 3800:  # лимит Telegram - 4096 символов
            lines.append(f"…и ещё {len(ordered) - i + 1}")
            break
        lines.append(line)
        size += len(line) + 1
    total = sum(v[1] for v in stats.values())
    return head + "\n" + "\n".join(lines) + f"\n\nВсего вп: {total}"


def vp_stats_text(offset):
    """Статистика ВП по сменам недели: «09.10 Пт - 103 вп». Итог смены записывается ботом при её закрытии."""
    now = now_msk()
    cur = shift_of(now)
    mon = week_start(cur, offset)
    lines, total = [], 0
    for i in range(7):
        d = mon + timedelta(days=i)
        if d > cur:
            break  # будущие смены не показываем
        rec = q("SELECT vp_count FROM shift_stats WHERE shift=?", (d.isoformat(),))
        note = ""
        if rec:
            n = rec[0]["vp_count"]
        else:  # итога ещё нет: считаем по постам (идущая смена или смена без постов)
            n = q("SELECT COUNT(*) AS c FROM posts WHERE shift=? AND status='sent'", (d.isoformat(),))[0]["c"]
            if d == cur:
                note = " (смена идёт)" if shift_state(d, now) == "active" else " (смена ещё не началась)"
        total += n
        lines.append(f"{d:%d.%m} {WEEKDAYS[d.weekday()]} - {n} вп{note}")
    head = f"<b>📈 Статистика ВП - {week_name(offset)}</b>\n{week_label(offset)}\n"
    return head + "\n" + "\n".join(lines) + f"\n\nИтого за неделю: {total} вп"


def console_kb(user_id):
    rows = [
        [InlineKeyboardButton(text="📊 Статистика впшеров", callback_data="cn:a:0")],
        [InlineKeyboardButton(text="📈 Статистика ВП", callback_data="cn:v:0")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton(text="👥 Админы консоли", callback_data="cn:adm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stats_kb(kind, offset):
    if offset == 0:
        switch = InlineKeyboardButton(text="⬅ Прошлая неделя", callback_data=f"cn:{kind}:1")
    else:
        switch = InlineKeyboardButton(text="Текущая неделя ➡", callback_data=f"cn:{kind}:0")
    return InlineKeyboardMarkup(inline_keyboard=[
        [switch],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="cn:menu")],
    ])


def admins_view():
    rows = q("SELECT user_id, name FROM console_admins ORDER BY added_at, user_id")
    lines = [f"<b>👥 Админы консоли</b>\n\nВладелец: {OWNER_ID}"]
    buttons = []
    if rows:
        lines.append("")
        for i, r in enumerate(rows, 1):
            label = r["name"] or str(r["user_id"])
            lines.append(f"{i}. {mention(r['user_id'], r['name'], CONSOLE_LINK)} (ID {r['user_id']})")
            buttons.append([InlineKeyboardButton(text=f"🗑 Убрать: {label}", callback_data=f"cn:del:{r['user_id']}")])
    else:
        lines.append("\nАдминов пока нет.")
    lines.append("\nДобавить: /addadmin ID (в личке бота) или ответьте командой /addadmin на сообщение человека в группе.")
    buttons.append([InlineKeyboardButton(text="🏠 В меню", callback_data="cn:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("console"))
async def cmd_console(message: Message):
    uid = message.from_user.id if message.from_user else None
    if uid is None:
        return
    if message.chat.type != "private":
        if is_console_admin(uid):
            await message.reply("Консоль открывается в личных сообщениях с ботом.")
        return
    if not is_console_admin(uid):
        await message.answer("⛔ У вас нет доступа к консоли.")
        return
    run("UPDATE console_admins SET name=? WHERE user_id=?", (message.from_user.full_name, uid))
    await message.answer("<b>🛠 Консоль админов</b>", reply_markup=console_kb(uid))


def target_from_command(message):
    """Кого назначаем/снимаем: автор сообщения, на которое ответили, либо ID после команды."""
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        return reply.from_user.id, reply.from_user.full_name
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].lstrip("-").isdigit():
        return int(parts[1]), None
    return None


@router.message(Command("addadmin", "deladmin"))
async def cmd_admin_manage(message: Message):
    """Назначить/снять админа консоли. Только владелец бота."""
    uid = message.from_user.id if message.from_user else None
    if uid is None or not is_owner(uid):
        if uid is not None and message.chat.type == "private":
            await message.answer("⛔ Назначать админов консоли может только владелец бота.")
        return
    adding = message.text.split()[0].lower().startswith("/addadmin")
    target = target_from_command(message)
    if not target:
        cmd = "/addadmin" if adding else "/deladmin"
        await message.reply(f"Укажите ID: {cmd} 123456789 - или ответьте командой {cmd} на сообщение человека в группе.")
        return
    tid, name = target
    if is_owner(tid):
        await message.reply("Владелец и так имеет полный доступ.")
        return
    if adding:
        run("INSERT INTO console_admins (user_id, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=COALESCE(excluded.name, name)",
            (tid, name, int(now_msk().timestamp())))
        await message.reply(f"✅ {mention(tid, name) if name else tid} назначен админом консоли. Консоль: /console в личке бота.")
    else:
        cur = run("DELETE FROM console_admins WHERE user_id=?", (tid,))
        await message.reply("✅ Админ снят." if cur.rowcount else "Такого админа в консоли нет.")


@router.callback_query(F.data.startswith("cn:"))
async def on_console(cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_console_admin(uid):
        await cb.answer("Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]
    if action == "menu":
        text, kb = "<b>🛠 Консоль админов</b>", console_kb(uid)
    elif action in ("a", "v"):
        offset = 1 if parts[2] == "1" else 0
        text = authors_stats_text(offset) if action == "a" else vp_stats_text(offset)
        kb = stats_kb(action, offset)
    elif action in ("adm", "del"):
        if not is_owner(uid):
            await cb.answer("Только владелец бота", show_alert=True)
            return
        if action == "del":
            run("DELETE FROM console_admins WHERE user_id=?", (int(parts[2]),))
        text, kb = admins_view()
    else:
        await cb.answer()
        return
    await safe_edit(cb, text, kb)
    await cb.answer()


# =====================================================================
#  ПРИЁМ ПОСТОВ (обработчик обычных сообщений в группе - стоит последним!)
# =====================================================================
POST_TYPES = {
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO, ContentType.ANIMATION,
    ContentType.DOCUMENT, ContentType.AUDIO, ContentType.VOICE, ContentType.VIDEO_NOTE,
}


@router.message(F.chat.id == GROUP_ID, F.content_type.in_(POST_TYPES))
async def on_post(message: Message):
    """
    ВАЖНАЯ ФУНКЦИЯ: любое сообщение впшера в группе считается постом.
    - До 22:15: бот предлагает выбрать время.
    - В рабочее время (22:15-09:50): сразу ставит ближайшее свободное время.
    - Если места кончились: пост уходит пачкой в 09:55.
    - Если рабочее окно смены уже закрыто: сообщает, что пост не уйдёт.
    - Если на этой смене уже есть пост с таким же текстом: отклоняет его (никуда не планируется).
    """
    if message.from_user is None or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return

    now = now_msk()
    shift = shift_of(message.date.astimezone(MSK))
    pid, is_new = register_message(message, shift.isoformat(), make_text_key(message))
    if not is_new:
        return  # продолжение альбома - отвечать не нужно

    # антидубль (между записью поста и проверкой нет await - два одинаковых поста не проскочат)
    dup = find_duplicate(shift.isoformat(), make_text_key(message), pid)
    if dup:
        run("UPDATE posts SET status='duplicate' WHERE id=?", (pid,))
        when = f", выйдет в {fmt_ts(dup['scheduled_at'])}" if dup["scheduled_at"] and dup["status"] == "pending" else ""
        await message.reply(
            f"<b>🔁 Такой пост уже есть на этой смене</b> (от {post_author(dup)}{when}). "
            "Дубль отклонён и в канал не уйдёт."
        )
        return

    state = shift_state(shift, now)
    if state == "before":
        sent = await message.reply(
            f"<b>✅ Пост принят ({shift_title(shift)})</b>\n"
            "Хотите выбрать время сами?",
            reply_markup=prompt_kb(pid),
        )
        run("UPDATE posts SET prompt_msg_id=? WHERE id=?", (sent.message_id, pid))
    elif state == "active":
        plan_shift(shift, now)
        post = get_post(pid)
        await message.reply(f"<b>✅ Пост принят</b>, выйдет {when_text(post, shift)}.")
    else:
        run("UPDATE posts SET status='expired' WHERE id=?", (pid,))
        await message.reply("<b>⛔ Рабочее окно этой смены уже закрыто</b>, пост не будет опубликован.")


# =====================================================================
#  ПУБЛИКАЦИЯ В КАНАЛ
# =====================================================================
def parse_chat(value):
    value = str(value).strip()
    return int(value) if value.lstrip("-").isdigit() else value


CHANNEL = parse_chat(CHANNEL_ID)


async def send_long(bot, chat_id, text):
    """Отправить длинный текст несколькими сообщениями (лимит Telegram - 4096 символов)."""
    try:
        chunk_text = ""
        for line in text.split("\n"):
            if len(chunk_text) + len(line) + 1 > 3800:
                await bot.send_message(chat_id, chunk_text)
                chunk_text = ""
            chunk_text += line + "\n"
        if chunk_text.strip():
            await bot.send_message(chat_id, chunk_text)
    except Exception:
        logging.exception("Не удалось отправить сообщение в чат")


async def send_post(bot, post):
    """
    ВАЖНАЯ ФУНКЦИЯ: публикация поста в канал.
    Пост пересылается (forward) из группы в канал.
    Берётся актуальный текст сообщения - если автор успел отредактировать пост, уйдёт исправленный.
    """
    mids = sorted(r["message_id"] for r in q("SELECT message_id FROM post_messages WHERE post_id=?", (post["id"],)))
    try:
        # всегда пересылка (forward): оформление, в том числе премиум-эмодзи, остаётся как в оригинале
        result = await bot.forward_messages(chat_id=CHANNEL, from_chat_id=post["chat_id"], message_ids=mids)
    except TelegramRetryAfter as e:
        logging.warning("Telegram просит подождать %s сек", e.retry_after)
        await asyncio.sleep(e.retry_after)
        return  # пост остался в очереди, попробуем на следующей проверке
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        run("UPDATE posts SET status='failed' WHERE id=?", (post["id"],))
        await send_long(bot, GROUP_ID, f"<b>❌ Не удалось опубликовать пост</b> от {post_author(post)}: {html.escape(str(e))}")
        return

    if not result:
        run("UPDATE posts SET status='failed' WHERE id=?", (post["id"],))
        await send_long(bot, GROUP_ID,
                        f"<b>❌ Пост не опубликован</b> (автор {post_author(post)}): сообщение не найдено (возможно, удалено).")
        return
    run("UPDATE posts SET status='sent', sent_at=? WHERE id=?", (int(now_msk().timestamp()), post["id"]))
    if len(result) < len(mids):
        await send_long(bot, GROUP_ID,
                        f"<b>⚠️ Пост опубликован не полностью</b> (автор {post_author(post)}; часть сообщений удалена).")


async def send_due(bot, shift, now):
    """Опубликовать все посты смены, время которых наступило."""
    due = q(
        "SELECT * FROM posts WHERE shift=? AND status='pending' AND scheduled_at IS NOT NULL "
        "AND scheduled_at <= ? ORDER BY scheduled_at, id",
        (shift.isoformat(), int(now.timestamp())),
    )
    for post in due:
        await send_post(bot, post)
        await asyncio.sleep(1)


async def announce_schedule(bot, shift, overflow):
    """Сообщение в группу в начале рабочего времени: расписание смены."""
    rows = q("SELECT * FROM posts WHERE shift=? AND status='pending' ORDER BY scheduled_at, id",
             (shift.isoformat(),))
    if not rows:
        return
    lines = [f"<b>🚀 Рабочее время началось ({shift_title(shift)})</b>\nРасписание:"]
    for r in rows:
        if r["scheduled_at"]:
            lines.append(f"{fmt_ts(r['scheduled_at'])} - {post_author(r)}")
    if overflow:
        lines.append(f"\n<b>⚠️ Не хватило мест</b>, эти посты уйдут пачкой в {OVERFLOW_TIME:%H:%M}: "
                     + ", ".join(post_author(r) for r in overflow))
    await send_long(bot, GROUP_ID, "\n".join(lines))


async def close_shift(bot, shift):
    """
    ВАЖНАЯ ФУНКЦИЯ: завершение смены (в 09:51).
    Все неопубликованные посты закрываются (в следующую смену они НЕ переходят),
    в группу уходит итог.
    """
    sh = shift.isoformat()
    run("UPDATE posts SET status='expired' WHERE shift=? AND status='pending'", (sh,))
    run("UPDATE shifts SET closed=1 WHERE shift=?", (sh,))
    # сохраняем итог смены для статистики: сколько ВП (опубликованных постов) за смену
    vp = q("SELECT COUNT(*) AS c FROM posts WHERE shift=? AND status='sent'", (sh,))[0]["c"]
    run("INSERT OR REPLACE INTO shift_stats (shift, vp_count) VALUES (?, ?)", (sh, vp))
    rows = q("SELECT status, author_id, author_name FROM posts WHERE shift=?", (sh,))
    if not rows:
        return
    count = lambda st: sum(1 for r in rows if r["status"] == st)
    text = (f"<b>🌅 Смена завершена ({shift_title(shift)})</b>\n"
            f"✅ Опубликовано: {count('sent')}\n"
            f"❌ Ошибки: {count('failed')}\n"
            f"⏳ Не успели выйти: {count('expired')}\n"
            f"🚫 Отменено: {count('cancelled')}")
    if count("duplicate"):
        text += f"\n🔁 Дублей отклонено: {count('duplicate')}"
    missed = [post_author(r) for r in rows if r["status"] == "expired"]
    if missed:
        text += "\nНе опубликованы: " + ", ".join(missed)
    await send_long(bot, GROUP_ID, text)


# =====================================================================
#  ПЛАНИРОВЩИК - ГЛАВНЫЙ ЦИКЛ БОТА
# =====================================================================
_last_cleanup = 0.0


def cleanup_old():
    """
    Хранение данных: только прошлая и текущая недели (две недели, Пн-Вс).
    Всё, что старше понедельника прошлой недели, удаляется. Проверка раз в час.
    """
    global _last_cleanup
    if time.time() - _last_cleanup < 3600:
        return
    _last_cleanup = time.time()
    cutoff = week_start(shift_of(now_msk()), 1).isoformat()
    run("DELETE FROM post_messages WHERE post_id IN (SELECT id FROM posts WHERE shift < ?)", (cutoff,))
    run("DELETE FROM posts WHERE shift < ?", (cutoff,))
    run("DELETE FROM shifts WHERE shift < ?", (cutoff,))
    run("DELETE FROM shift_stats WHERE shift < ?", (cutoff,))


async def tick(bot):
    """
    ВАЖНАЯ ФУНКЦИЯ: одна проверка (каждые 10 секунд).
    До 22:15 ничего не делает. В рабочее время: расставляет время, объявляет расписание
    и публикует посты, когда их время пришло. После 09:50 закрывает смену.
    """
    now = now_msk()
    cleanup_old()
    for s in q("SELECT shift, started FROM shifts WHERE closed=0 ORDER BY shift"):
        shift = date.fromisoformat(s["shift"])
        state = shift_state(shift, now)
        if state == "before":
            continue
        if state == "closed":
            # окно закончилось, но пачка на 09:55 ещё может ждать отправки
            if now <= overflow_dt(shift) + OVERFLOW_GRACE:
                plan_shift(shift, now)  # нераспределённые посты (если такие остались) - тоже в пачку
                await send_due(bot, shift, now)
                left = q("SELECT COUNT(*) AS c FROM posts WHERE shift=? AND status='pending'", (shift.isoformat(),))
                if left[0]["c"] == 0:
                    await close_shift(bot, shift)
            else:
                await close_shift(bot, shift)
            continue
        # рабочее время идёт
        reschedule_missed(shift, now)
        placed, overflow = plan_shift(shift, now)
        if not s["started"]:
            run("UPDATE shifts SET started=1 WHERE shift=?", (s["shift"],))
            await announce_schedule(bot, shift, overflow)
        await send_due(bot, shift, now)


async def scheduler(bot):
    while True:
        try:
            await tick(bot)
        except Exception:
            logging.exception("Ошибка в планировщике")
        await asyncio.sleep(TICK_SECONDS)


# =====================================================================
#  ЗАПУСК
# =====================================================================
async def main():
    if ":" not in BOT_TOKEN:
        print("Вставьте токен бота в строку BOT_TOKEN в начале файла.")
        return
    if GROUP_ID == 0:
        logging.warning("GROUP_ID не задан: бот пока отвечает только на /id. "
                        "Добавьте бота в группу, отправьте /id и впишите число в GROUP_ID.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    task = asyncio.create_task(scheduler(bot))  # noqa: F841 (держим ссылку, чтобы задача не пропала)
    logging.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
