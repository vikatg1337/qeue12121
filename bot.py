import asyncio
import os
import random
import secrets
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден токен! Проверь переменную окружения BOT_TOKEN или файл .env")

# На сервере не используем локальный прокси компьютера.
session = AiohttpSession()
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

DB_FILE = os.getenv("DB_FILE", "queues.db")
ADMIN_ID = 1053596260
GROUP_CHAT_ID = -5496142597

JOKES = [
    "МЕМЕМЕМЕМЕМЕМЕМЕМЕМЕМЕМЕ",
    "жучило какой-то",
    "...",
    "Скачать вирусы без промоушен",
    "🐸 Кто рано записался, тот рано начал переживать",
    "67",
]

# Простые состояния диалога. Они хранятся в памяти до перезапуска бота.
waiting_for_queue_name = set()
waiting_for_queue_capacity = set()
pending_queue_name = {}
waiting_for_change = set()
waiting_for_change_queue = {}
waiting_for_proxy_name = {}       # user_id -> (queue_id, place)
waiting_for_seminar_title = set()
waiting_for_seminar_topics = {}   # user_id -> seminar title


def connect_db():
    db = sqlite3.connect(DB_FILE, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    """Create missing tables and add new columns without deleting existing data."""
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS queues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                places INTEGER NOT NULL DEFAULT 32
            )
        """)
        queue_columns = {row[1] for row in db.execute("PRAGMA table_info(queues)").fetchall()}
        if "places" not in queue_columns:
            db.execute("ALTER TABLE queues ADD COLUMN places INTEGER NOT NULL DEFAULT 32")
        db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                queue_id INTEGER NOT NULL,
                place INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                PRIMARY KEY (queue_id, place),
                UNIQUE (queue_id, user_id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                queue_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (queue_id, chat_id, message_id)
            )
        """)

        # Миграция: для записей за другого человека сохраняем ID того, кто сделал запись.
        columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)").fetchall()}
        if "booked_by" not in columns:
            db.execute("ALTER TABLE bookings ADD COLUMN booked_by INTEGER")

        db.execute("""
            CREATE TABLE IF NOT EXISTS seminars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS seminar_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seminar_id INTEGER NOT NULL,
                topic_name TEXT NOT NULL,
                user_id INTEGER,
                user_name TEXT,
                booked_at TEXT,
                FOREIGN KEY (seminar_id) REFERENCES seminars(id)
            )
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_topic_per_user_per_seminar
            ON seminar_topics(seminar_id, user_id) WHERE user_id IS NOT NULL
        """)


def get_queues():
    with connect_db() as db:
        return db.execute("SELECT id, name, places FROM queues ORDER BY id DESC").fetchall()


def get_queue(queue_id):
    with connect_db() as db:
        return db.execute("SELECT id, name, places FROM queues WHERE id = ?", (queue_id,)).fetchone()


def get_bookings(queue_id):
    with connect_db() as db:
        return db.execute(
            "SELECT place, user_id, user_name, booked_by FROM bookings WHERE queue_id = ? ORDER BY place",
            (queue_id,),
        ).fetchall()


def get_user_booking(queue_id, user_id):
    with connect_db() as db:
        return db.execute(
            "SELECT place FROM bookings WHERE queue_id = ? AND user_id = ? AND user_id > 0",
            (queue_id, user_id),
        ).fetchone()


async def update_queue_announcement(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return
    bookings = get_bookings(queue_id)
    free_places = queue["places"] - len(bookings)
    with connect_db() as db:
        announcements = db.execute(
            "SELECT chat_id, message_id FROM announcements WHERE queue_id = ?", (queue_id,)
        ).fetchall()

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=queue_{queue_id}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📝 Открыть очередь и записаться", url=link
    )]])
    for announcement in announcements:
        try:
            await bot.edit_message_text(
                chat_id=announcement["chat_id"],
                message_id=announcement["message_id"],
                text=(
                    "📢 ОБЪЯВЛЕНА НОВАЯ ОЧЕРЕДЬ!\n\n"
                    f"📚 {queue['name']}\n\n"
                    f"🟢 Свободно мест: {free_places}/{queue['places']}\n\n"
                    "🎓 Выбирайте свободное место в боте.\n"
                    "💀 Не тяните до последней минуты!"
                ),
                reply_markup=keyboard,
            )
        except Exception as error:
            print(f"Не удалось обновить объявление: {error}")


def main_menu(user_id=None):
    rows = [
        [InlineKeyboardButton(text="➕ Создать очередь", callback_data="create")],
        [InlineKeyboardButton(text="📋 Все очереди", callback_data="queues")],
        [InlineKeyboardButton(text="📚 Темы семинаров", callback_data="seminars")],
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="➕ Создать семинар", callback_data="seminar_create")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queues_keyboard(user_id=None):
    rows = []
    for queue in get_queues():
        occupied = len(get_bookings(queue["id"]))
        rows.append([InlineKeyboardButton(
            text=f"📋 {queue['name']} ({occupied}/{queue['places']})", callback_data=f"view:{queue['id']}"
        )])
        if user_id == ADMIN_ID:
            rows.append([InlineKeyboardButton(
                text=f"🗑 Удалить: {queue['name']}", callback_data=f"delete:{queue['id']}"
            )])
            rows.append([InlineKeyboardButton(
                text=f"📢 Опубликовать: {queue['name']}", callback_data=f"publish:{queue['id']}"
            )])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_keyboard(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return InlineKeyboardMarkup(inline_keyboard=[])
    bookings = {row["place"] for row in get_bookings(queue_id)}
    rows, row = [], []
    for place in range(1, queue["places"] + 1):
        label = f"🔴 {place}" if place in bookings else f"🟢 {place}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"place:{queue_id}:{place}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend([
        [InlineKeyboardButton(text="❌ Отменить мою запись", callback_data=f"cancel:{queue_id}")],
        [InlineKeyboardButton(text="🔄 Сменить место", callback_data=f"change:{queue_id}")],
        [InlineKeyboardButton(text="🔃 Обновить", callback_data=f"view:{queue_id}"),
         InlineKeyboardButton(text="📋 Все очереди", callback_data="queues")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_text(queue_id):
    queue = get_queue(queue_id)
    if not queue:
        return "🛸 Очередь похищена инопланетянами. Не найдена."
    bookings = {row["place"]: row["user_name"] for row in get_bookings(queue_id)}
    capacity = queue["places"]
    free = capacity - len(bookings)
    lines = [
        f"📋 {queue['name']}", random.choice(JOKES), "",
        f"🔴 Занято: {len(bookings)}/{capacity}", f"🟢 Свободно: {free}", "",
    ]
    for place in range(1, capacity + 1):
        if place in bookings:
            lines.append(f"🔴 № {place} — {bookings[place]}")
        else:
            lines.append(f"🟢 № {place} — свободно, пока жизнь не настигла")
    lines.extend(["", "👇 Выбирай свободное место. Если записываешь другого человека, укажи его имя."])
    return "\n".join(lines)


def seminars_keyboard(user_id=None):
    rows = []
    with connect_db() as db:
        seminars = db.execute("SELECT id, title FROM seminars ORDER BY id DESC").fetchall()
    for seminar in seminars:
        with connect_db() as db:
            total = db.execute("SELECT COUNT(*) FROM seminar_topics WHERE seminar_id = ?", (seminar["id"],)).fetchone()[0]
            taken = db.execute("SELECT COUNT(*) FROM seminar_topics WHERE seminar_id = ? AND user_id IS NOT NULL", (seminar["id"],)).fetchone()[0]
        rows.append([InlineKeyboardButton(
            text=f"📚 {seminar['title']} ({taken}/{total})", callback_data=f"seminar_view:{seminar['id']}"
        )])
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="➕ Создать семинар", callback_data="seminar_create")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def seminar_topics_keyboard(seminar_id):
    with connect_db() as db:
        topics = db.execute(
            "SELECT id, topic_name, user_name FROM seminar_topics WHERE seminar_id = ? ORDER BY id",
            (seminar_id,),
        ).fetchall()
    rows = []
    for topic in topics:
        if topic["user_id"] is None:
            label = f"🟢 {topic['topic_name']} — свободна"
            callback = f"topic_book:{topic['id']}"
        else:
            label = f"🔴 {topic['topic_name']} — {topic['user_name']}"
            callback = f"topic_taken:{topic['id']}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=callback)])
    rows.append([InlineKeyboardButton(text="❌ Снять мою тему", callback_data=f"topic_cancel:{seminar_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К семинарам", callback_data="seminars")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "/myid")
async def show_my_id(message: Message):
    await message.answer(f"🪪 Твой Telegram ID: `{message.from_user.id}`\nЗапиши его — это пропуск в админку 😎", parse_mode="Markdown")


@dp.message(CommandStart())
async def start(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("queue_"):
        try:
            queue_id = int(parts[1].replace("queue_", "", 1))
        except ValueError:
            queue_id = 0
        if queue_id:
            if get_queue(queue_id):
                await message.answer("📩 Ты прибыл по секретной ссылке старосты!\nДобро пожаловать в академический хаос ☠️")
                await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
            else:
                await message.answer("👽 Эта очередь не найдена. Возможно, её уже удалили.")
            return
    await message.answer(
        "🤖 ВИКИН БОТ 3000 (название не я придумала)\n\n"
        "короче Вика навайбкодила бота такая она крутая ну!\n"
        "Короче очередь занимаем электронная не толпимся ноги поднимаем\n\nВыбирай действие:",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("🏠 Главное меню. Дедлайны подождут три секунды.", reply_markup=main_menu(callback.from_user.id))


@dp.callback_query(F.data == "create")
async def create_queue_start(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Создавать очереди может только староста!", show_alert=True)
        return
    waiting_for_queue_name.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer("Режим старосты активирован!\nКак назовём новую очередь? Напиши название сообщением. Для отмены — /cancel.")


@dp.callback_query(F.data == "queues")
async def show_queues(callback: CallbackQuery):
    await callback.answer()
    if not get_queues():
        await callback.message.answer("📭 Очередей пока нет. Можно создать первую!", reply_markup=main_menu(callback.from_user.id))
        return
    await callback.message.answer("📚 Все очереди — выбирай, где будем страдать:", reply_markup=queues_keyboard(callback.from_user.id))


@dp.callback_query(F.data.startswith("view:"))
async def view_queue(callback: CallbackQuery):
    await callback.answer()
    queue_id = int(callback.data.split(":")[1])
    await callback.message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))


@dp.callback_query(F.data.startswith("place:"))
async def choose_place(callback: CallbackQuery):
    _, queue_id_text, place_text = callback.data.split(":")
    queue_id, place = int(queue_id_text), int(place_text)
    if not get_queue(queue_id):
        await callback.answer("Очередь не найдена 👽", show_alert=True)
        return
    if not 1 <= place <= get_queue(queue_id)["places"]:
        await callback.answer("Такого места нет.", show_alert=True)
        return
    with connect_db() as db:
        taken = db.execute("SELECT user_name FROM bookings WHERE queue_id = ? AND place = ?", (queue_id, place)).fetchone()
    if taken:
        await callback.answer(f"Место уже занято: {taken['user_name']}", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Занять за себя", callback_data=f"bookself:{queue_id}:{place}")],
        [InlineKeyboardButton(text="✍️ Записать другого", callback_data=f"bookother:{queue_id}:{place}")],
        [InlineKeyboardButton(text="⬅️ Назад к очереди", callback_data=f"view:{queue_id}")],
    ])
    await callback.answer()
    await callback.message.answer(f"Свободное место № {place}. Кого записываем?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("bookself:"))
async def book_self(callback: CallbackQuery):
    _, q_text, p_text = callback.data.split(":")
    queue_id, place = int(q_text), int(p_text)
    queue = get_queue(queue_id)
    if not queue or not 1 <= place <= queue["places"]:
        await callback.answer("Такого места нет или очередь удалена.", show_alert=True)
        return
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name or "Участник"
    try:
        with connect_db() as db:
            db.execute("INSERT INTO bookings (queue_id, place, user_id, user_name, booked_by) VALUES (?, ?, ?, ?, ?)",
                       (queue_id, place, user_id, user_name, user_id))
    except sqlite3.IntegrityError:
        await callback.answer("⛔ Место занято или ты уже записан! Очередь — не королевская битва.", show_alert=True)
        return
    await update_queue_announcement(queue_id)
    await callback.answer(f"🎉 Место № {place} твоё!")
    await callback.message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))


@dp.callback_query(F.data.startswith("bookother:"))
async def book_other_start(callback: CallbackQuery):
    _, q_text, p_text = callback.data.split(":")
    queue_id, place = int(q_text), int(p_text)
    queue = get_queue(queue_id)
    if not queue or not 1 <= place <= queue["places"]:
        await callback.answer("Такого места нет или очередь удалена.", show_alert=True)
        return
    with connect_db() as db:
        taken = db.execute("SELECT 1 FROM bookings WHERE queue_id = ? AND place = ?", (queue_id, place)).fetchone()
    if taken:
        await callback.answer("Это место уже заняли. Обнови очередь.", show_alert=True)
        return
    waiting_for_proxy_name[callback.from_user.id] = (queue_id, place)
    await callback.answer()
    await callback.message.answer(f"✍️ Напиши имя человека для места № {place}. Можно указать имя и фамилию. Напиши /cancel, чтобы отменить.")


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    with connect_db() as db:
        cursor = db.execute("DELETE FROM bookings WHERE queue_id = ? AND user_id = ? AND user_id > 0", (queue_id, user_id))
    await callback.answer()
    if cursor.rowcount == 0:
        await callback.message.answer("🤷 У тебя здесь нет личной записи. Запись, сделанную за другого, этой кнопкой не отменить.")
        return
    await update_queue_announcement(queue_id)
    await callback.message.answer("🫡 Запись отменена. Место снова свободно!", reply_markup=main_menu(user_id))
    await callback.message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))


@dp.callback_query(F.data.startswith("change:"))
async def change_booking(callback: CallbackQuery):
    queue_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    if not get_user_booking(queue_id, user_id):
        await callback.answer()
        await callback.message.answer("👀 Сначала запишись в очередь за себя, потом меняй место.")
        return
    waiting_for_change.add(user_id)
    waiting_for_change_queue[user_id] = queue_id
    await callback.answer()
    await callback.message.answer(f"🔄 На какой номер пересаживаемся? Напиши число от 1 до {get_queue(queue_id)['places']}. Если место занято, запись не изменится.")


@dp.callback_query(F.data.startswith("delete:"))
async def delete_queue_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Только староста может удалять очереди!", show_alert=True)
        return
    queue_id = int(callback.data.split(":")[1])
    queue = get_queue(queue_id)
    if not queue:
        await callback.answer("Очередь уже не существует.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💥 Да, удалить", callback_data=f"delete_yes:{queue_id}"),
        InlineKeyboardButton(text="🛡 Отмена", callback_data=f"view:{queue_id}"),
    ]])
    await callback.answer()
    await callback.message.answer(f"⚠️ Точно удалить очередь «{queue['name']}»? Все записи в ней тоже будут удалены. Вернуть их будет нельзя.", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("delete_yes:"))
async def delete_queue_execute(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Только староста!", show_alert=True)
        return
    queue_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        db.execute("DELETE FROM bookings WHERE queue_id = ?", (queue_id,))
        db.execute("DELETE FROM announcements WHERE queue_id = ?", (queue_id,))
        cursor = db.execute("DELETE FROM queues WHERE id = ?", (queue_id,))
    await callback.answer()
    if cursor.rowcount:
        await callback.message.answer("💥 Очередь удалена. Одной академической проблемой меньше.", reply_markup=main_menu(callback.from_user.id))
    else:
        await callback.message.answer("Очередь уже удалена.")


@dp.callback_query(F.data.startswith("publish:"))
async def publish_queue(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Публиковать очереди может только староста!", show_alert=True)
        return
    queue_id = int(callback.data.split(":")[1])
    queue = get_queue(queue_id)
    if not queue:
        await callback.answer("Очередь не найдена!", show_alert=True)
        return
    try:
        me = await bot.get_me()
        if not me.username:
            await callback.answer("Не удалось получить имя бота.", show_alert=True)
            return
        link = f"https://t.me/{me.username}?start=queue_{queue_id}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Открыть очередь и записаться", url=link)]])
        sent_message = await bot.send_message(
            GROUP_CHAT_ID,
            f"📢 ОБЪЯВЛЕНА НОВАЯ ОЧЕРЕДЬ!\n\n📚 {queue['name']}\n\n"
            f"🟢 Свободно мест: {queue['places'] - len(get_bookings(queue_id))}/{queue['places']}\n\n"
            "🎓 Выбирайте свободное место в боте.\n💀 Не тяните до последней минуты!",
            reply_markup=keyboard,
        )
        with connect_db() as db:
            db.execute("INSERT OR IGNORE INTO announcements (queue_id, chat_id, message_id) VALUES (?, ?, ?)",
                       (queue_id, GROUP_CHAT_ID, sent_message.message_id))
        await callback.answer("Объявление опубликовано! 📢")
        await callback.message.answer(f"✅ Очередь «{queue['name']}» опубликована в группе!\nВикин IT-отдел доволен 😎")
    except Exception as error:
        print(f"Ошибка публикации: {error}")
        await callback.answer("Не получилось опубликовать. Проверь, что бот есть в группе.", show_alert=True)


@dp.callback_query(F.data == "seminars")
async def show_seminars(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📚 Семинары и темы. Выбирай семинар:", reply_markup=seminars_keyboard(callback.from_user.id))


@dp.callback_query(F.data == "seminar_create")
async def seminar_create_start(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Создавать семинары может только староста!", show_alert=True)
        return
    waiting_for_seminar_title.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer("➕ Напиши название семинара или предмета. Например: «История искусства — семинар 2».")


@dp.callback_query(F.data.startswith("seminar_view:"))
async def seminar_view(callback: CallbackQuery):
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        seminar = db.execute("SELECT id, title FROM seminars WHERE id = ?", (seminar_id,)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM seminar_topics WHERE seminar_id = ?", (seminar_id,)).fetchone()[0]
    await callback.answer()
    if not seminar:
        await callback.message.answer("Этот семинар не найден.")
        return
    if count == 0:
        await callback.message.answer(f"📚 {seminar['title']}\nПока тем нет.")
        return
    await callback.message.answer(f"📚 {seminar['title']}\nНажми на свободную тему, чтобы занять её.", reply_markup=seminar_topics_keyboard(seminar_id))


@dp.callback_query(F.data.startswith("topic_book:"))
async def topic_book(callback: CallbackQuery):
    topic_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    with connect_db() as db:
        topic = db.execute("SELECT id, seminar_id, topic_name, user_id FROM seminar_topics WHERE id = ?", (topic_id,)).fetchone()
        if not topic:
            await callback.answer("Тема не найдена.", show_alert=True)
            return
        if topic["user_id"] is not None:
            await callback.answer("Эту тему уже заняли. Обнови список.", show_alert=True)
            return
        existing = db.execute("SELECT topic_name FROM seminar_topics WHERE seminar_id = ? AND user_id = ?", (topic["seminar_id"], user_id)).fetchone()
        if existing:
            await callback.answer(f"Ты уже выбрал(а) тему: {existing['topic_name']}", show_alert=True)
            return
        db.execute("UPDATE seminar_topics SET user_id = ?, user_name = ?, booked_at = ? WHERE id = ? AND user_id IS NULL",
                   (user_id, callback.from_user.full_name or "Участник", datetime.utcnow().isoformat(timespec="seconds"), topic_id))
    await callback.answer("🎉 Тема за тобой!")
    await callback.message.answer("Тема занята!", reply_markup=seminar_topics_keyboard(topic["seminar_id"]))


@dp.callback_query(F.data.startswith("topic_taken:"))
async def topic_taken(callback: CallbackQuery):
    await callback.answer("Эта тема уже занята. Выбери свободную 🫡", show_alert=True)


@dp.callback_query(F.data.startswith("topic_cancel:"))
async def topic_cancel(callback: CallbackQuery):
    seminar_id = int(callback.data.split(":")[1])
    with connect_db() as db:
        cursor = db.execute("UPDATE seminar_topics SET user_id = NULL, user_name = NULL, booked_at = NULL WHERE seminar_id = ? AND user_id = ?",
                            (seminar_id, callback.from_user.id))
    await callback.answer()
    if cursor.rowcount:
        await callback.message.answer("🫡 Твоя тема снова свободна.", reply_markup=seminar_topics_keyboard(seminar_id))
    else:
        await callback.message.answer("У тебя нет выбранной темы в этом семинаре.")


@dp.message(F.text)
async def receive_text(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if text == "/cancel":
        waiting_for_proxy_name.pop(user_id, None)
        waiting_for_change.discard(user_id)
        waiting_for_change_queue.pop(user_id, None)
        waiting_for_queue_name.discard(user_id)
        waiting_for_queue_capacity.discard(user_id)
        pending_queue_name.pop(user_id, None)
        waiting_for_seminar_title.discard(user_id)
        waiting_for_seminar_topics.pop(user_id, None)
        staged_topics.pop(user_id, None)
        await message.answer("Окей, действие отменено.", reply_markup=main_menu(user_id))
        return

    if user_id in waiting_for_proxy_name:
        queue_id, place = waiting_for_proxy_name.pop(user_id)
        if not text or len(text) > 100:
            await message.answer("Имя должно быть от 1 до 100 символов. Попробуй ещё раз через выбор места.")
            return
        if not get_queue(queue_id):
            await message.answer("Очередь больше не существует.")
            return
        with connect_db() as db:
            taken = db.execute("SELECT 1 FROM bookings WHERE queue_id = ? AND place = ?", (queue_id, place)).fetchone()
            duplicate = db.execute("SELECT 1 FROM bookings WHERE queue_id = ? AND lower(user_name) = lower(?)", (queue_id, text)).fetchone()
            if taken:
                await message.answer("Место уже успели занять. Обнови очередь и выбери другое.")
                return
            if duplicate:
                await message.answer("Похоже, этот человек уже есть в очереди. Проверь список, чтобы не записать его дважды.")
                return
            # Negative synthetic ID keeps the legacy NOT NULL + UNIQUE constraints intact.
            # Check the generated ID separately so a place conflict cannot cause an endless retry.
            while True:
                synthetic_id = -secrets.randbelow(2_000_000_000) - 1
                id_taken = db.execute(
                    "SELECT 1 FROM bookings WHERE queue_id = ? AND user_id = ?",
                    (queue_id, synthetic_id),
                ).fetchone()
                if not id_taken:
                    break
            try:
                db.execute(
                    "INSERT INTO bookings (queue_id, place, user_id, user_name, booked_by) VALUES (?, ?, ?, ?, ?)",
                    (queue_id, place, synthetic_id, text, user_id),
                )
            except sqlite3.IntegrityError:
                await message.answer("Место только что заняли. Обнови очередь и попробуй другое.")
                return
        await update_queue_announcement(queue_id)
        await message.answer(f"✅ Записала «{text}» на место № {place}.", reply_markup=main_menu(user_id))
        await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
        return

    if user_id in waiting_for_change:
        try:
            place = int(text)
        except ValueError:
            await message.answer("🤨 Введи номер места цифрами.")
            return
        queue_id = waiting_for_change_queue.get(user_id)
        if queue_id is None:
            waiting_for_change.discard(user_id)
            await message.answer("Смена места отменена. Попробуй заново.")
            return
        queue = get_queue(queue_id) if queue_id is not None else None
        if not queue:
            waiting_for_change.discard(user_id)
            waiting_for_change_queue.pop(user_id, None)
            await message.answer("Очередь больше не существует.")
            return
        if not 1 <= place <= queue["places"]:
            await message.answer(f"Номер должен быть от 1 до {queue['places']}.")
            return
        current = get_user_booking(queue_id, user_id)
        if not current:
            waiting_for_change.discard(user_id)
            waiting_for_change_queue.pop(user_id, None)
            await message.answer("Сначала нужно занять место в этой очереди.")
            return
        if current[0] == place:
            await message.answer("😂 Ты уже сидишь на этом месте!")
            return
        try:
            with connect_db() as db:
                db.execute("UPDATE bookings SET place = ? WHERE queue_id = ? AND user_id = ? AND user_id > 0", (place, queue_id, user_id))
        except sqlite3.IntegrityError:
            await message.answer("💀 Место занято! Судьба оказалась быстрее.")
            return
        waiting_for_change.discard(user_id)
        waiting_for_change_queue.pop(user_id, None)
        await update_queue_announcement(queue_id)
        await message.answer(f"✨ Пересадка успешна! Новое место — № {place}", reply_markup=main_menu(user_id))
        await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
        return

    if user_id in waiting_for_queue_name:
        if user_id != ADMIN_ID:
            waiting_for_queue_name.discard(user_id)
            return
        if not text or len(text) > 100:
            await message.answer("Название должно быть от 1 до 100 символов.")
            return
        waiting_for_queue_name.discard(user_id)
        pending_queue_name[user_id] = text
        waiting_for_queue_capacity.add(user_id)
        await message.answer("🔢 Сколько участников должно быть в очереди? Напиши число от 1 до 200. Например, 32. Для отмены — /cancel.")
        return

    if user_id in waiting_for_queue_capacity:
        if user_id != ADMIN_ID:
            waiting_for_queue_capacity.discard(user_id)
            pending_queue_name.pop(user_id, None)
            return
        try:
            capacity = int(text)
        except ValueError:
            await message.answer("Нужно написать целое число от 1 до 200. Попробуй ещё раз.")
            return
        if not 1 <= capacity <= 200:
            await message.answer("Количество участников должно быть от 1 до 200. Попробуй ещё раз.")
            return
        name = pending_queue_name.pop(user_id, None)
        if not name:
            waiting_for_queue_capacity.discard(user_id)
            await message.answer("Название очереди потерялось. Начни создание заново.", reply_markup=main_menu(user_id))
            return
        with connect_db() as db:
            cursor = db.execute("INSERT INTO queues (name, places) VALUES (?, ?)", (name, capacity))
            queue_id = cursor.lastrowid
        waiting_for_queue_capacity.discard(user_id)
        await message.answer(f"🎉 Очередь создана! Вместимость: {capacity} участник(ов).", reply_markup=main_menu(user_id))
        await message.answer(queue_text(queue_id), reply_markup=queue_keyboard(queue_id))
        return

    if user_id in waiting_for_seminar_title:
        if user_id != ADMIN_ID:
            waiting_for_seminar_title.discard(user_id)
            return
        if not text or len(text) > 150:
            await message.answer("Название должно быть от 1 до 150 символов.")
            return
        waiting_for_seminar_title.discard(user_id)
        staged_topics[user_id] = []
        waiting_for_seminar_topics[user_id] = text
        await message.answer("Теперь отправь список тем — каждую тему с новой строки. Когда закончишь, напиши ГОТОВО. Для отмены — /cancel.")
        return

    if user_id in waiting_for_seminar_topics:
        if user_id != ADMIN_ID:
            waiting_for_seminar_topics.pop(user_id, None)
            return
        if text.upper() == "ГОТОВО":
            title = waiting_for_seminar_topics.pop(user_id)
            with connect_db() as db:
                cursor = db.execute("INSERT INTO seminars (title, created_by, created_at) VALUES (?, ?, ?)",
                                    (title, user_id, datetime.utcnow().isoformat(timespec="seconds")))
                seminar_id = cursor.lastrowid
            # Topics are staged in a temporary attribute per admin while entering the list.
            topics = staged_topics.pop(user_id, [])
            if not topics:
                await message.answer("Семинар создан, но тем не добавлено. Создай новый семинар со списком тем.", reply_markup=main_menu(user_id))
                return
            with connect_db() as db:
                db.executemany("INSERT INTO seminar_topics (seminar_id, topic_name) VALUES (?, ?)", [(seminar_id, topic) for topic in topics])
            await message.answer(f"🎉 Семинар «{title}» создан: тем — {len(topics)}.", reply_markup=main_menu(user_id))
            await message.answer("Список тем:", reply_markup=seminar_topics_keyboard(seminar_id))
            return
        if len(text) > 150:
            await message.answer("Одна тема должна быть не длиннее 150 символов.")
            return
        topics = staged_topics.setdefault(user_id, [])
        if len(topics) >= 50:
            await message.answer("Максимум 50 тем в одном семинаре.")
            return
        if text not in topics:
            topics.append(text)
        await message.answer(f"Добавлено тем: {len(topics)}. Отправь следующую тему или напиши ГОТОВО.")
        return


staged_topics = {}


async def main():
    init_db()
    print(f"Бот запускается... База данных: {os.path.abspath(DB_FILE)}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
