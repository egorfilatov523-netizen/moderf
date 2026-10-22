import asyncio
import os
import re
import random
import sqlite3
import time
from contextlib import contextmanager

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
)
from aiogram.exceptions import TelegramAPIError

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "bot.db")
WARNING_DELETE_SECONDS = 7

if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в файле .env")

router = Router()

DEFAULT_BAD_WORDS = [
    "бля", "блять", "блядь", "бляди", "сука", "суки", "сучка",
    "хуй", "хуя", "хуе", "хер", "пизда", "пиздец", "пизду",
    "пизды", "ебать", "ебал", "ебан", "ебись", "заебал",
    "заебись", "охуел", "мудак", "мудила", "долбоеб", "гандон",
    "шлюха",
]

LOOKALIKES = str.maketrans({
    "ё": "е", "a": "а", "c": "с", "e": "е", "o": "о",
    "p": "р", "x": "х", "y": "у", "k": "к", "m": "м",
    "t": "т", "h": "н", "b": "в", "0": "о", "3": "з",
})

# Пользовательские сценарии ввода: user_id -> action
pending: dict[int, str] = {}
admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize(text: str) -> str:
    text = text.lower().translate(LOOKALIKES)
    text = re.sub(r"[^а-яa-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_words (
                user_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                PRIMARY KEY (user_id, word)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS managed_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                message_id INTEGER,
                prize TEXT NOT NULL,
                winners_count INTEGER NOT NULL,
                ends_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_users (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                PRIMARY KEY (giveaway_id, user_id)
            )
        """)
        # Перенос старой глобальной таблицы в личные списки администраторов.
        old_table = conn.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='banned_words'
        """).fetchone()
        if old_table:
            old_words = conn.execute("SELECT word FROM banned_words").fetchall()
            for admin_id in ADMIN_IDS:
                for row in old_words:
                    word = normalize(row["word"])
                    if word:
                        conn.execute(
                            "INSERT OR IGNORE INTO user_words(user_id, word) VALUES (?, ?)",
                            (admin_id, word)
                        )
        # При первом запуске начальные слова получают администраторы из ADMIN_IDS.
        count = conn.execute("SELECT COUNT(*) FROM user_words").fetchone()[0]
        if count == 0:
            for admin_id in ADMIN_IDS:
                conn.executemany(
                    "INSERT OR IGNORE INTO user_words(user_id, word) VALUES (?, ?)",
                    [(admin_id, normalize(w)) for w in DEFAULT_BAD_WORDS]
                )


def ensure_default_words(user_id: int):
    """Создаёт личный стартовый список, если пользователь ещё не добавлял слова."""
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM user_words WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO user_words(user_id, word) VALUES (?, ?)",
                [(user_id, normalize(word)) for word in DEFAULT_BAD_WORDS]
            )


def get_words(owner_id: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT word FROM user_words WHERE user_id=? ORDER BY word",
            (owner_id,)
        ).fetchall()
    return [row["word"] for row in rows]


def find_bad_word(text: str, words: list[str]) -> str | None:
    normalized = normalize(text)
    if not normalized:
        return None
    compact = normalized.replace(" ", "")
    for word in sorted(words, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
            return word
        compact_word = word.replace(" ", "")
        if len(compact_word) >= 5 and compact_word in compact:
            return word
    return None


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить слово"),
             KeyboardButton(text="➖ Удалить слово")],
            [KeyboardButton(text="📋 Мои слова"),
             KeyboardButton(text="🛡 Модерация")],
            [KeyboardButton(text="🔗 Подключить чат"),
             KeyboardButton(text="🔌 Отключить чат")],
            [KeyboardButton(text="🎁 Розыгрыш"),
             KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие в меню",
    )


def inline_word_keyboard(words: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🗑 {word}", callback_data=f"delword:{i}")]
        for i, word in enumerate(words[:80])
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chat_keyboard(chats: list[sqlite3.Row], action: str) -> InlineKeyboardMarkup:
    rows = []
    for chat in chats:
        rows.append([InlineKeyboardButton(
            text=f"{'🟢' if chat['enabled'] else '⚪'} {chat['title'][:45]}",
            callback_data=f"{action}:{chat['chat_id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = admin_cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = member.status in ("administrator", "creator")
        admin_cache[key] = (result, now + 30)
        return result
    except TelegramAPIError:
        return False


async def can_manage(message: Message) -> bool:
    if not message.from_user:
        return False
    if message.from_user.id in ADMIN_IDS:
        return True
    if message.chat.type in ("group", "supergroup"):
        return await is_group_admin(message.bot, message.chat.id, message.from_user.id)
    return False


async def delete_later(message: Message, delay: int = WARNING_DELETE_SECONDS):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def notify_admins(bot: Bot, chat_title: str, username: str,
                        user_id: int, bad_word: str, original_text: str):
    report = (
        "🚨 Нарушение правил\n\n"
        f"Группа: {chat_title}\nПользователь: {username}\n"
        f"ID: {user_id}\nСлово: {bad_word}\n"
        f"Сообщение: {original_text[:1000]}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report)
        except TelegramAPIError:
            pass


@router.message(Command("start"))
async def start(message: Message):
    if not message.from_user:
        return
    ensure_default_words(message.from_user.id)
    await message.answer(
        "🛡 Панель управления модерацией\n\n"
        "Выбирай действие большими кнопками внизу.\n"
        "Добавь бота в группу и выдай ему право удалять сообщения.",
        reply_markup=main_keyboard()
    )


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def help_handler(message: Message):
    await message.answer(
        "🛡 Как пользоваться:\n"
        "• «Добавить слово» — добавить слово в твой личный фильтр.\n"
        "• «Удалить слово» — выбрать слово для удаления.\n"
        "• «Мои слова» — посмотреть свой список.\n"
        "• «Подключить чат» — включить модерацию в группе, где бот администратор.\n"
        "• «Отключить чат» — выключить модерацию выбранной группы.\n"
        "• «Розыгрыш» — создать розыгрыш в подключённой группе.\n\n"
        "Управлять группой может её администратор или владелец бота.",
        reply_markup=main_keyboard()
    )


@router.message(F.text == "➕ Добавить слово")
async def add_word_start(message: Message):
    if not message.from_user:
        return
    pending[message.from_user.id] = "add_word"
    await message.answer("✍️ Отправь одним сообщением запрещённое слово или фразу.")


@router.message(F.text == "➖ Удалить слово")
async def delete_word_start(message: Message):
    if not message.from_user:
        return
    words = get_words(message.from_user.id)
    if not words:
        await message.answer("Твой список пуст.", reply_markup=main_keyboard())
        return
    await message.answer("Выбери слово, которое нужно удалить:", reply_markup=inline_word_keyboard(words))


@router.callback_query(F.data.startswith("delword:"))
async def delete_word_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    try:
        index = int(callback.data.split(":", 1)[1])
    except (ValueError, AttributeError):
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    words = get_words(callback.from_user.id)
    if index < 0 or index >= len(words):
        await callback.answer("Список уже изменился. Открой удаление заново.", show_alert=True)
        return
    word = words[index]
    with db() as conn:
        conn.execute(
            "DELETE FROM user_words WHERE user_id=? AND word=?",
            (callback.from_user.id, word)
        )
    await callback.answer(f"Удалено: {word}")
    try:
        await callback.message.edit_text(f"✅ Слово «{word}» удалено из твоего списка.")
    except TelegramAPIError:
        pass


@router.message(F.text == "📋 Мои слова")
async def my_words(message: Message):
    if not message.from_user:
        return
    words = get_words(message.from_user.id)
    if not words:
        await message.answer("Твой список запрещённых слов пуст.", reply_markup=main_keyboard())
        return
    text = "🚫 Твои запрещённые слова:\n\n" + "\n".join(f"• {w}" for w in words)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500], reply_markup=main_keyboard())


@router.message(F.text == "🛡 Модерация")
async def moderation_status(message: Message):
    if not message.from_user:
        return
    with db() as conn:
        chats = conn.execute(
            "SELECT * FROM managed_chats WHERE owner_id=? ORDER BY title",
            (message.from_user.id,)
        ).fetchall()
    if not chats:
        await message.answer(
            "У тебя пока нет подключённых групп.\n"
            "Добавь бота в группу, выдай ему право удалять сообщения, "
            "затем нажми «Подключить чат».",
            reply_markup=main_keyboard()
        )
        return
    lines = ["🛡 Статус модерации:"]
    lines.extend(
        f"{'🟢 Включена' if c['enabled'] else '⚪ Выключена'} — {c['title']}"
        for c in chats
    )
    await message.answer("\n".join(lines), reply_markup=main_keyboard())


@router.message(F.text == "🔗 Подключить чат")
async def connect_chat_start(message: Message):
    if not message.from_user:
        return
    with db() as conn:
        chats = conn.execute(
            "SELECT * FROM managed_chats WHERE owner_id=? AND enabled=0 ORDER BY title",
            (message.from_user.id,)
        ).fetchall()
    if not chats:
        await message.answer(
            "Не найдено отключённых групп, зарегистрированных за тобой.\n\n"
            "Чтобы подключить новую группу:\n"
            "1. Добавь бота в группу.\n"
            "2. Выдай ему право удалять сообщения.\n"
            "3. Отправь в этой группе команду /connect.",
            reply_markup=main_keyboard()
        )
        return
    await message.answer("Выбери группу для включения модерации:", reply_markup=chat_keyboard(chats, "enablechat"))


@router.my_chat_member()
async def bot_added_to_chat(event: ChatMemberUpdated):
    """Автоматически регистрирует группу, когда бота назначают администратором."""
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return
    new_status = event.new_chat_member.status
    if new_status != "administrator":
        return
    owner = event.from_user
    if not owner or owner.is_bot:
        return
    try:
        member = event.new_chat_member
        if not getattr(member, "can_delete_messages", False):
            return
    except Exception:
        return
    ensure_default_words(owner.id)
    with db() as conn:
        existing = conn.execute(
            "SELECT owner_id FROM managed_chats WHERE chat_id=?", (chat.id,)
        ).fetchone()
        if existing and existing["owner_id"] != owner.id and owner.id not in ADMIN_IDS:
            return
        conn.execute("""
            INSERT INTO managed_chats(chat_id, title, owner_id, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, enabled=1
        """, (chat.id, chat.title or str(chat.id), owner.id))
    try:
        await event.bot.send_message(
            owner.id,
            f"✅ Группа «{chat.title or chat.id}» подключена автоматически.\n"
            "Модерация включена. Открой меню бота для управления.",
            reply_markup=main_keyboard()
        )
    except TelegramAPIError:
        # Пользователь должен сначала открыть личный чат с ботом, чтобы бот мог написать.
        pass


@router.message(Command("connect"))
async def register_chat(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        await message.answer("Команду /connect нужно отправить в нужной группе.")
        return
    if not await can_manage(message):
        await message.answer("Подключить группу может её администратор.")
        return
    try:
        bot_member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        if bot_member.status != "administrator" or not getattr(bot_member, "can_delete_messages", False):
            await message.answer("Сначала выдай боту права администратора и разрешение удалять сообщения.")
            return
    except TelegramAPIError:
        await message.answer("Не удалось проверить права бота. Сделай его администратором с правом удаления сообщений.")
        return
    owner_id = message.from_user.id
    ensure_default_words(owner_id)
    with db() as conn:
        existing = conn.execute(
            "SELECT owner_id FROM managed_chats WHERE chat_id=?",
            (message.chat.id,)
        ).fetchone()
        if existing and existing["owner_id"] != owner_id and owner_id not in ADMIN_IDS:
            await message.answer("Эта группа уже подключена другим владельцем. Обратись к владельцу бота.")
            return
        conn.execute("""
            INSERT INTO managed_chats(chat_id, title, owner_id, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, enabled=1
        """, (message.chat.id, message.chat.title or str(message.chat.id), owner_id))
    await message.answer("✅ Группа подключена. Модерация включена.", reply_markup=main_keyboard())


@router.callback_query(F.data.startswith("enablechat:"))
async def enable_chat(callback: CallbackQuery):
    if not callback.from_user:
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная группа.", show_alert=True)
        return
    with db() as conn:
        cursor = conn.execute(
            "UPDATE managed_chats SET enabled=1 WHERE chat_id=? AND owner_id=?",
            (chat_id, callback.from_user.id)
        )
    if cursor.rowcount:
        await callback.answer("Модерация включена.")
        await callback.message.edit_text("✅ Модерация группы включена.")
    else:
        await callback.answer("Группа не найдена.", show_alert=True)


@router.message(F.text == "🔌 Отключить чат")
async def disconnect_chat_start(message: Message):
    if not message.from_user:
        return
    with db() as conn:
        chats = conn.execute(
            "SELECT * FROM managed_chats WHERE owner_id=? AND enabled=1 ORDER BY title",
            (message.from_user.id,)
        ).fetchall()
    if not chats:
        await message.answer("У тебя нет включённых групп.", reply_markup=main_keyboard())
        return
    await message.answer("Выбери группу для отключения модерации:", reply_markup=chat_keyboard(chats, "disablechat"))


@router.callback_query(F.data.startswith("disablechat:"))
async def disable_chat(callback: CallbackQuery):
    if not callback.from_user:
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная группа.", show_alert=True)
        return
    with db() as conn:
        cursor = conn.execute(
            "UPDATE managed_chats SET enabled=0 WHERE chat_id=? AND owner_id=?",
            (chat_id, callback.from_user.id)
        )
    if cursor.rowcount:
        await callback.answer("Модерация отключена.")
        await callback.message.edit_text("🔌 Модерация этой группы отключена.")
    else:
        await callback.answer("Группа не найдена.", show_alert=True)


@router.message(F.text == "🎁 Розыгрыш")
async def giveaway_start(message: Message):
    if not message.from_user:
        return
    with db() as conn:
        chats = conn.execute(
            "SELECT * FROM managed_chats WHERE owner_id=? AND enabled=1 ORDER BY title",
            (message.from_user.id,)
        ).fetchall()
    if not chats:
        await message.answer("Сначала подключи группу через /connect в самой группе.", reply_markup=main_keyboard())
        return
    await message.answer(
        "Чтобы создать розыгрыш, отправь одним сообщением:\n"
        "приз | длительность_в_минутах | количество_победителей\n\n"
        "Пример: Наушники | 60 | 2\n"
        "Затем выбери группу."
    )
    pending[message.from_user.id] = "giveaway_details"


@router.message(F.text == "⚙️ Настройки")
async def settings(message: Message):
    await message.answer(
        "⚙️ Настройки\n"
        "Слова хранятся отдельно для каждого пользователя. "
        "Каждая подключённая группа использует список своего владельца.\n"
        "Для регистрации новой группы отправь в ней /connect.",
        reply_markup=main_keyboard()
    )


@router.message(F.text, lambda message: bool(message.from_user and message.from_user.id in pending))
async def handle_pending_text(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    action = pending.get(user_id)
    if not action:
        return

    if action == "add_word":
        word = normalize(message.text)
        if not word:
            await message.answer("Не удалось распознать слово. Попробуй ещё раз.")
            return
        if len(word) > 100:
            await message.answer("Слово или фраза слишком длинные (максимум 100 символов).")
            return
        with db() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO user_words(user_id, word) VALUES (?, ?)",
                (user_id, word)
            )
        pending.pop(user_id, None)
        await message.answer(
            f"{'✅ Добавлено' if cursor.rowcount else 'ℹ️ Уже есть в списке'}: {word}",
            reply_markup=main_keyboard()
        )
        return

    if action == "giveaway_details":
        parts = [p.strip() for p in message.text.split("|")]
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("Формат: приз | минуты | победители\nПример: Наушники | 60 | 2")
            return
        prize, minutes_text, winners_text = parts
        minutes, winners_count = int(minutes_text), int(winners_text)
        if not prize or not 1 <= minutes <= 10080 or not 1 <= winners_count <= 50:
            await message.answer("Проверь приз, длительность (1–10080 минут) и число победителей (1–50).")
            return
        with db() as conn:
            chats = conn.execute(
                "SELECT * FROM managed_chats WHERE owner_id=? AND enabled=1 ORDER BY title",
                (user_id,)
            ).fetchall()
        if not chats:
            pending.pop(user_id, None)
            await message.answer("Нет подключённых групп.", reply_markup=main_keyboard())
            return
        # Store validated giveaway data and ask user to choose group.
        pending[user_id] = f"giveaway_chat|{minutes}|{winners_count}|{prize}"
        rows = [[InlineKeyboardButton(
            text=f"🎁 {chat['title'][:45]}",
            callback_data=f"gchat:{chat['chat_id']}"
        )] for chat in chats]
        await message.answer("В какой группе опубликовать розыгрыш?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return


@router.callback_query(F.data.startswith("gchat:"))
async def choose_giveaway_chat(callback: CallbackQuery):
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    action = pending.get(user_id, "")
    if not action.startswith("giveaway_chat|"):
        await callback.answer("Сначала начни создание розыгрыша заново.", show_alert=True)
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
        _, minutes_text, winners_text, prize = action.split("|", 3)
        minutes, winners_count = int(minutes_text), int(winners_text)
    except (ValueError, TypeError):
        await callback.answer("Не удалось прочитать настройки розыгрыша.", show_alert=True)
        return
    with db() as conn:
        chat = conn.execute(
            "SELECT title FROM managed_chats WHERE chat_id=? AND owner_id=? AND enabled=1",
            (chat_id, user_id)
        ).fetchone()
        if not chat:
            await callback.answer("Группа не найдена или отключена.", show_alert=True)
            return
        ends_at = time.time() + minutes * 60
        cursor = conn.execute("""
            INSERT INTO giveaways(chat_id, owner_id, prize, winners_count, ends_at)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, user_id, prize, winners_count, ends_at))
        giveaway_id = cursor.lastrowid
    text = (
        f"🎉 РОЗЫГРЫШ №{giveaway_id}\n\n"
        f"🎁 Приз: {prize}\n🏆 Победителей: {winners_count}\n"
        f"⏳ Завершение через {minutes} мин.\n\n"
        "Нажми кнопку ниже, чтобы участвовать!"
    )
    try:
        sent = await callback.bot.send_message(
            chat_id, text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎉 Участвовать", callback_data=f"join:{giveaway_id}")
            ]])
        )
        with db() as conn:
            conn.execute("UPDATE giveaways SET message_id=? WHERE id=?", (sent.message_id, giveaway_id))
        pending.pop(user_id, None)
        asyncio.create_task(finish_giveaway_later(callback.bot, giveaway_id, minutes * 60))
        await callback.answer("Розыгрыш опубликован!")
        await callback.message.edit_text(f"✅ Розыгрыш №{giveaway_id} опубликован в группе «{chat['title']}».")
    except TelegramAPIError as e:
        with db() as conn:
            conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (giveaway_id,))
        await callback.answer("Не удалось опубликовать розыгрыш.", show_alert=True)
        await callback.message.answer(f"Ошибка Telegram: {e}")


@router.callback_query(F.data.startswith("join:"))
async def join_giveaway(callback: CallbackQuery):
    if not callback.from_user:
        return
    try:
        giveaway_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректный розыгрыш.", show_alert=True)
        return
    with db() as conn:
        giveaway = conn.execute(
            "SELECT * FROM giveaways WHERE id=? AND active=1", (giveaway_id,)
        ).fetchone()
        if not giveaway or giveaway["ends_at"] <= time.time():
            await callback.answer("Розыгрыш уже завершён.", show_alert=True)
            return
        conn.execute("""
            INSERT OR IGNORE INTO giveaway_users(giveaway_id, user_id, username)
            VALUES (?, ?, ?)
        """, (giveaway_id, callback.from_user.id,
              callback.from_user.username or callback.from_user.full_name))
        count = conn.execute(
            "SELECT COUNT(*) FROM giveaway_users WHERE giveaway_id=?", (giveaway_id,)
        ).fetchone()[0]
    await callback.answer(f"Ты участвуешь! Участников: {count}", show_alert=True)


async def finish_giveaway_later(bot: Bot, giveaway_id: int, delay: int):
    await asyncio.sleep(delay)
    await finish_giveaway(bot, giveaway_id)


async def finish_giveaway(bot: Bot, giveaway_id: int):
    with db() as conn:
        giveaway = conn.execute(
            "SELECT * FROM giveaways WHERE id=? AND active=1", (giveaway_id,)
        ).fetchone()
        if not giveaway:
            return
        conn.execute("UPDATE giveaways SET active=0 WHERE id=?", (giveaway_id,))
        users = conn.execute(
            "SELECT user_id, username FROM giveaway_users WHERE giveaway_id=?",
            (giveaway_id,)
        ).fetchall()
    if not users:
        result = f"🎁 Розыгрыш №{giveaway_id} завершён.\nНикто не участвовал."
    else:
        winners = random.sample(list(users), min(giveaway["winners_count"], len(users)))
        lines = [
            f"{i}. {w['username'] or w['user_id']} (ID: {w['user_id']})"
            for i, w in enumerate(winners, 1)
        ]
        result = (
            f"🏁 РОЗЫГРЫШ №{giveaway_id} ЗАВЕРШЁН!\n\n"
            f"🎁 Приз: {giveaway['prize']}\n👥 Участников: {len(users)}\n\n"
            "🏆 Победители:\n" + "\n".join(lines)
        )
    try:
        await bot.send_message(giveaway["chat_id"], result)
        if giveaway["message_id"]:
            await bot.edit_message_reply_markup(
                chat_id=giveaway["chat_id"], message_id=giveaway["message_id"],
                reply_markup=None
            )
    except TelegramAPIError:
        pass


@router.message()
async def moderate(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    user = message.from_user
    if not user or user.is_bot:
        return
    with db() as conn:
        chat = conn.execute(
            "SELECT owner_id, enabled FROM managed_chats WHERE chat_id=?",
            (message.chat.id,)
        ).fetchone()
    if not chat or not chat["enabled"]:
        return
    if await is_group_admin(message.bot, message.chat.id, user.id):
        return
    text = message.text or message.caption or ""
    if not text or text.startswith("/"):
        return
    words = get_words(chat["owner_id"])
    bad_word = find_bad_word(text, words)
    if not bad_word:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        return
    username = f"@{user.username}" if user.username else user.full_name
    try:
        warning = await message.answer(
            f"⚠️ {username}, сообщение удалено за нарушение правил модерации."
        )
        asyncio.create_task(delete_later(warning))
    except TelegramAPIError:
        pass
    asyncio.create_task(notify_admins(
        message.bot, message.chat.title or str(message.chat.id),
        username, user.id, bad_word, text
    ))


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот модерации запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
