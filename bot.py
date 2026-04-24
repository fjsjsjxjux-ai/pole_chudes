#!/usr/bin/env python3
"""
Поле Чудес - Telegram Bot
Одиночный режим, мультиплеер в ЛС и группах,
профили, ранги, инвентарь, система уровней.
"""

import asyncio
import logging
import random
import os
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from words import WORDS_BY_CATEGORY, ALL_CATEGORIES
from game_logic import (
    GameRoom, SinglePlayerGame,
    WHEEL_SECTORS, DIFFICULTY_SETTINGS,
    spin_wheel, format_word_display, ALPHABET
)
from db import (
    init_db, ensure_user, get_user, add_score_and_xp,
    use_free_hint, add_free_hints, use_skip_skip, add_skip_skips, set_active_title,
    get_leaderboard_xp, get_leaderboard_score, get_users_count,
    get_user_position_xp, get_user_position_score,
    get_rank_for_xp, get_next_rank,
    RANKS
)

# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8622943091:AAEa19llqG6GVYyh11TzH5lWWoPOYaUG2IU")

# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------
class CreateRoom(StatesGroup):
    waiting_rounds     = State()
    waiting_players    = State()
    waiting_difficulty = State()
    waiting_category   = State()

class CreateGroupRoom(StatesGroup):
    waiting_rounds     = State()
    waiting_players    = State()
    waiting_difficulty = State()
    waiting_category   = State()

class JoinRoom(StatesGroup):
    waiting_room_id = State()

class SinglePlay(StatesGroup):
    choosing_difficulty = State()
    choosing_category   = State()
    playing             = State()

class ProfileState(StatesGroup):
    choosing_title = State()

# ---------------------------------------------------------------------------
# Хранилища
# ---------------------------------------------------------------------------
rooms:        dict[str, GameRoom]         = {}
single_games: dict[int, SinglePlayerGame] = {}
# user_id -> (difficulty, category)
last_single_settings: dict[int, tuple[str, str]] = {}
# group_chat_id -> room_id
group_rooms:  dict[int, str]              = {}

TURN_TIMEOUT_SEC = 45
turn_timer_tasks: dict[str, asyncio.Task] = {}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())

# ===========================================================================
# КЛАВИАТУРЫ
# ===========================================================================

def kb_group_menu() -> InlineKeyboardMarkup:
    """Меню в группе — только 2 кнопки."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎡 Создать игру", callback_data="group_create_room")],
        [InlineKeyboardButton(text="📊 Рейтинг",      callback_data="leaderboard")],
    ])

def kb_main_menu() -> InlineKeyboardMarkup:
    """Меню в ЛС."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Одиночная игра",   callback_data="single_play")],
        [InlineKeyboardButton(text="👥 Мультиплеер (ЛС)", callback_data="multi_play")],
        [InlineKeyboardButton(text="👤 Профиль",          callback_data="profile")],
        [InlineKeyboardButton(text="🎒 Инвентарь",        callback_data="inventory")],
        [InlineKeyboardButton(text="📊 Рейтинг",          callback_data="leaderboard")],
        [InlineKeyboardButton(text="📖 Правила",          callback_data="rules")],
    ])

def kb_difficulty(prefix: str = "diff") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Лёгкий  (3-5 букв)",  callback_data=f"{prefix}_easy")],
        [InlineKeyboardButton(text="🟡 Средний (6-8 букв)",  callback_data=f"{prefix}_medium")],
        [InlineKeyboardButton(text="🔴 Сложный (9+ букв)",   callback_data=f"{prefix}_hard")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])

def kb_categories(prefix: str = "cat") -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, cat in enumerate(ALL_CATEGORIES):
        row.append(InlineKeyboardButton(text=cat, callback_data=f"{prefix}_{cat}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🎲 Случайная", callback_data=f"{prefix}_random")])
    buttons.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_rounds(prefix: str = "rounds") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}_{i}") for i in range(1, 4)],
        [InlineKeyboardButton(text=str(i), callback_data=f"{prefix}_{i}") for i in range(4, 6)],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])

def kb_multi_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать комнату", callback_data="create_room")],
        [InlineKeyboardButton(text="🚪 Войти в комнату", callback_data="join_room")],
        [InlineKeyboardButton(text="🏠 Меню",            callback_data="main_menu")],
    ])

def kb_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])

def kb_rematch(room_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Играть ещё (те же условия)", callback_data=f"rematch_{room_id}")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])

def kb_single_rematch() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔁 Играть ещё (те же условия)", callback_data="single_rematch")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])

def kb_spin(room: GameRoom, uid: int) -> InlineKeyboardMarkup:
    u = get_user(uid)
    rows = [[InlineKeyboardButton(text="🎡 Крутить барабан!", callback_data="spin_wheel")]]
    rows.append([InlineKeyboardButton(text="🔤 Назвать слово целиком", callback_data="guess_word_multi")])
    if u and u["free_hints"] > 0:
        rows.append([InlineKeyboardButton(
            text=f"💡 Бесплатная подсказка ({u['free_hints']} шт)",
            callback_data="use_free_hint_multi"
        )])
    rows.append([InlineKeyboardButton(text="🏳️ Сдаться", callback_data=f"surrender_{room.room_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_group_active(room_id: str) -> InlineKeyboardMarkup:
    """Кнопки в группе во время активной игры."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏳️ Сдаться", callback_data=f"surrender_{room_id}")],
    ])

def kb_group_lobby(room_id: str) -> InlineKeyboardMarkup:
    """Кнопки в группе при ожидании игроков."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Войти в игру",      callback_data=f"gjoin_{room_id}")],
        [InlineKeyboardButton(text="▶️ Начать досрочно",   callback_data=f"gstart_{room_id}")],
        [InlineKeyboardButton(text="❌ Отменить игру",      callback_data=f"gcancel_{room_id}")],
    ])

def kb_host_ls(room_id: str) -> InlineKeyboardMarkup:
    """Кнопки хоста в ЛС при групповой игре."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать досрочно",   callback_data=f"gstart_{room_id}")],
        [InlineKeyboardButton(text="❌ Отменить игру",      callback_data=f"gcancel_ls_{room_id}")],
    ])

def kb_player_ls(room_id: str) -> InlineKeyboardMarkup:
    """Кнопки обычного игрока в ЛС при групповой игре."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Покинуть игру", callback_data=f"gleave_{room_id}")],
    ])

def kb_host_room(room_id: str) -> InlineKeyboardMarkup:
    """Кнопки хоста в ЛС-приватной комнате."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать игру досрочно", callback_data=f"start_game_{room_id}")],
        [InlineKeyboardButton(text="❌ Удалить комнату",       callback_data=f"delete_room_{room_id}")],
        [InlineKeyboardButton(text="🚪 Выйти из игры",         callback_data=f"leave_room_{room_id}")],
    ])

def kb_player_room(room_id: str) -> InlineKeyboardMarkup:
    """Кнопки обычного игрока в ЛС-приватной комнате."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Выйти из игры", callback_data=f"leave_room_{room_id}")],
    ])

def kb_single_alphabet(used: set, show_free_hint: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, letter in enumerate(ALPHABET):
        if letter in used:
            row.append(InlineKeyboardButton(text="·", callback_data="used_letter"))
        else:
            row.append(InlineKeyboardButton(text=letter, callback_data=f"sletter_{letter}"))
        if len(row) == 6:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    extra = []
    if show_free_hint:
        extra.append(InlineKeyboardButton(text="💡 Бесплатная подсказка!", callback_data="sfree_hint"))
    extra.append(InlineKeyboardButton(text="💡 Подсказка (-50 очков)", callback_data="shint"))
    buttons.append(extra)
    buttons.append([InlineKeyboardButton(text="🔤 Назвать слово целиком", callback_data="sguess_word")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ===========================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ===========================================================================

def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{name}</a>'

async def notify_all_in_room(room: GameRoom, text: str, reply_markup=None):
    for uid in room.player_ids:
        try:
            await bot.send_message(uid, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"notify_all error {uid}: {e}")

def build_round_status(room: GameRoom) -> str:
    display = format_word_display(room.current_word, room.guessed_letters)
    cfg_label = DIFFICULTY_SETTINGS[room.difficulty]["label"]
    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>Раунд {room.current_round}/{room.total_rounds}</b>  {cfg_label}",
        f"📚 Категория: <b>{room.current_category}</b>",
        f"💬 Подсказка: <i>{room.current_hint}</i>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🔤 <code>{display}</code>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📝 Буквы: {' '.join(sorted(room.guessed_letters)) or '—'}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "💰 <b>Счёт:</b>"
    ]
    for uid in room.player_ids:
        name    = room.player_names[uid]
        score   = room.scores[uid]
        r_score = room.round_scores[uid]
        marker  = "👑" if uid == room.current_player_id else "  "
        lines.append(f"{marker} {name}: <b>{score}</b>  (+{r_score} раунд)")
    return "\n".join(lines)

def build_single_status(game: SinglePlayerGame) -> str:
    display   = format_word_display(game.word, game.guessed_letters)
    lives_str = "❤️" * game.lives + "🖤" * (game.max_lives - game.lives)
    cfg_label = DIFFICULTY_SETTINGS[game.difficulty]["label"]
    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>Слово {game.word_index}/{game.total_words}</b>  {cfg_label}",
        f"📚 Категория: <b>{game.category}</b>",
        f"💬 Подсказка: <i>{game.hint}</i>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"🔤 <code>{display}</code>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"❤️ Жизни: {lives_str}",
        f"💰 Очки: <b>{game.score}</b>",
        f"📝 Буквы: {' '.join(sorted(game.guessed_letters)) or '—'}",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)

def _find_room_by_player(uid: int) -> Optional[GameRoom]:
    for room in rooms.values():
        if uid in room.player_ids and room.active:
            return room
    return None

def _find_any_room_by_player(uid: int) -> Optional[GameRoom]:
    for room in rooms.values():
        if uid in room.player_ids:
            return room
    return None

# ===========================================================================
# ТАЙМЕР ХОДА
# ===========================================================================

def _cancel_turn_timer(room_id: str):
    task = turn_timer_tasks.pop(room_id, None)
    if task and not task.done():
        task.cancel()

def restart_turn_timer(room: GameRoom):
    """
    Перезапускает таймер хода для текущего игрока.
    Нужен, чтобы не было "время вышло", если игрок активен (крутит/пишет буквы вовремя).
    """
    _cancel_turn_timer(room.room_id)
    room.turn_timer_token += 1
    token = room.turn_timer_token
    uid   = room.current_player_id
    turn_timer_tasks[room.room_id] = asyncio.create_task(player_turn_timer(room.room_id, uid, token))

async def player_turn_timer(room_id: str, player_id: int, token: int):
    await asyncio.sleep(TURN_TIMEOUT_SEC)
    room = rooms.get(room_id)
    if not room or not room.active:
        return
    if room.current_player_id != player_id or room.turn_timer_token != token:
        return
    name = room.player_names[player_id]
    room.next_player()
    next_name = room.player_names[room.current_player_id]

    if room.room_type == "group":
        try:
            await bot.send_message(
                room.group_chat_id,
                f"⏰ <b>Время вышло!</b> {name} пропускает ход.\n👉 Ходит: <b>{next_name}</b>",
            )
        except Exception:
            pass
    else:
        await notify_all_in_room(
            room,
            f"⏰ <b>Время вышло!</b> {name} пропускает ход.\n👉 Ходит: <b>{next_name}</b>",
        )
    await send_turn_message(room)

async def send_turn_message(room: GameRoom):
    status       = build_round_status(room)
    current_uid  = room.current_player_id
    current_name = room.player_names[current_uid]
    # Обновляем время последней активности для таймера бездействия
    room.last_activity = time.time()

    if room.room_type == "group":
        try:
            sent = await bot.send_message(
                room.group_chat_id,
                status + f"\n\n👉 Ход: <b>{mention(current_uid, current_name)}</b>\n"
                         f"⏰ {TURN_TIMEOUT_SEC} секунд. Напишите букву в чат!",
                reply_markup=kb_group_active(room.room_id),
            )
            room.group_message_id = sent.message_id
        except Exception as e:
            logger.warning(f"send_turn_message group error: {e}")
        # Уведомить текущего игрока в ЛС если есть подсказки
        u = get_user(current_uid)
        if u and u["free_hints"] > 0:
            try:
                await bot.send_message(
                    current_uid,
                    f"💡 Твой ход в группе! У тебя <b>{u['free_hints']}</b> бесплатных подсказок.\n"
                    f"Напиши в чат группы /hint_{room.room_id} чтобы использовать.",
                )
            except Exception:
                pass
    else:
        for uid in room.player_ids:
            try:
                if uid == current_uid:
                    await bot.send_message(
                        uid,
                        status + f"\n\n🎡 <b>Ваш ход!</b> Крутите барабан!\n⏰ {TURN_TIMEOUT_SEC} секунд.",
                        reply_markup=kb_spin(room, uid),
                    )
                else:
                    await bot.send_message(
                        uid,
                        status + f"\n\n⏳ Ходит <b>{current_name}</b>...",
                    )
            except Exception as e:
                logger.warning(f"send_turn_message private error {uid}: {e}")

    restart_turn_timer(room)

# ===========================================================================
# /start, /menu, /single, /multi — команды
# ===========================================================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid   = message.from_user.id
    uname = message.from_user.full_name
    ensure_user(uid, uname)

    if message.chat.type in ("group", "supergroup"):
        # В группе — краткое представление с 2 командами
        await message.answer(
            f"🎡 <b>Поле Чудес</b> готов к игре!\n\n"
            f"Доступные команды:\n"
            f"🎮 /single — одиночная игра (в ЛС)\n"
            f"👥 /multi — мультиплеер в этой группе\n\n"
            f"Или нажми кнопку ниже:",
            reply_markup=kb_group_menu(),
        )
        return

    u    = get_user(uid)
    rank = get_rank_for_xp(u["xp"])
    await message.answer(
        f"🎡 <b>Поле Чудес!</b>\n\n"
        f"Привет, <b>{uname}</b>! {rank['name']}\n\n"
        f"Угадывай слова, зарабатывай очки и повышай уровень!\n\n"
        f"Выбери режим:",
        reply_markup=kb_main_menu(),
    )

@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    if message.chat.type in ("group", "supergroup"):
        await message.answer("🎡 <b>Поле Чудес</b>", reply_markup=kb_group_menu())
    else:
        await message.answer("🏠 <b>Главное меню</b>", reply_markup=kb_main_menu())

@dp.message(Command("single"))
async def cmd_single(message: Message, state: FSMContext):
    """Одиночная игра — работает только в ЛС."""
    uid   = message.from_user.id
    uname = message.from_user.full_name
    ensure_user(uid, uname)

    if message.chat.type in ("group", "supergroup"):
        # Отправляем ссылку на ЛС
        me = await bot.get_me()
        await message.answer(
            f"🎮 Одиночная игра запускается в личных сообщениях!\n"
            f"👉 <a href='https://t.me/{me.username}?start=single'>Перейти в ЛС</a>",
        )
        return

    await state.set_state(SinglePlay.choosing_difficulty)
    await message.answer(
        "🎮 <b>Одиночная игра</b>\n\nВыбери сложность:",
        reply_markup=kb_difficulty("sdiff"),
    )

@dp.message(Command("multi"))
async def cmd_multi(message: Message, state: FSMContext):
    """Создать игру в группе или открыть мультиплеер в ЛС."""
    uid   = message.from_user.id
    uname = message.from_user.full_name
    ensure_user(uid, uname)

    if message.chat.type in ("group", "supergroup"):
        # Сразу начинаем создание групповой игры
        await state.set_state(CreateGroupRoom.waiting_rounds)
        await message.answer(
            "🎯 <b>Создание игры в группе</b>\n\nСколько раундов?",
            reply_markup=kb_rounds("grrooms"),
        )
        return

    await state.clear()
    await message.answer(
        "👥 <b>Мультиплеер (ЛС)</b>\n\nСоздай комнату или войди:",
        reply_markup=kb_multi_menu(),
    )

# Подсказка через чат группы
@dp.message(F.text.regexp(r'^/hint_([A-Z0-9]{6})$'))
async def cmd_hint_group(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    room_id = message.text.split("_")[1]
    room = rooms.get(room_id)
    if not room or not room.active:
        return
    uid = message.from_user.id
    if room.current_player_id != uid:
        await message.reply("⚠️ Сейчас не твой ход!")
        return
    u = get_user(uid)
    if not u or u["free_hints"] <= 0:
        await message.reply("❌ У тебя нет бесплатных подсказок.")
        return
    hidden = [c for c in set(room.current_word) if c.isalpha() and c not in room.guessed_letters]
    if not hidden:
        await message.reply("Все буквы уже открыты!")
        return
    letter = random.choice(hidden)
    room.guessed_letters.add(letter)
    use_free_hint(uid)
    await message.reply(f"💡 Подсказка: буква <b>«{letter}»</b> открыта!")
    if room.is_round_complete():
        await finish_round(room)
    else:
        status = build_round_status(room)
        try:
            await bot.send_message(room.group_chat_id, status)
        except Exception:
            pass

# ===========================================================================
# ПРОФИЛЬ
# ===========================================================================

@dp.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    uid = call.from_user.id
    u   = get_user(uid)
    if not u:
        await call.answer("Профиль не найден.", show_alert=True)
        return

    rank      = get_rank_for_xp(u["xp"])
    next_rank = get_next_rank(u["xp"])
    progress  = ""
    if next_rank:
        need = next_rank["xp_needed"] - rank["xp_needed"]
        have = u["xp"] - rank["xp_needed"]
        pct  = min(int(have / need * 10), 10)
        bar  = "█" * pct + "░" * (10 - pct)
        progress = f"\n📈 До следующего уровня: [{bar}] {have}/{need} XP"

    title_line = f"\n🏷 Титул: <b>{u['active_title']}</b>" if u.get("active_title") else ""
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: <b>{u['username']}</b>{title_line}\n"
        f"Уровень: <b>{rank['level']}</b> — {rank['name']}\n"
        f"XP: <b>{u['xp']}</b>{progress}\n\n"
        f"🎮 Игр сыграно: <b>{u['games_played']}</b>\n"
        f"🔤 Слов угадано: <b>{u['words_guessed']}</b>\n"
        f"💰 Очков всего: <b>{u['total_score']}</b>\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷 Сменить титул", callback_data="change_title")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])
    await call.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "change_title")
async def cb_change_title(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    u   = get_user(uid)
    if not u or not u["titles"]:
        await call.answer("У тебя пока нет титулов! Повышай уровень.", show_alert=True)
        return

    buttons = [[InlineKeyboardButton(text=t, callback_data=f"settitle_{t}")] for t in u["titles"]]
    buttons.append([InlineKeyboardButton(text="🚫 Убрать титул", callback_data="settitle_none")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="profile")])
    await state.set_state(ProfileState.choosing_title)
    await call.message.edit_text(
        "🏷 <b>Выбери активный титул:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )

@dp.callback_query(F.data.startswith("settitle_"), ProfileState.choosing_title)
async def cb_set_title(call: CallbackQuery, state: FSMContext):
    await state.clear()
    uid   = call.from_user.id
    title = call.data[9:]
    if title == "none":
        from db import DB_PATH
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET active_title='' WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        await call.answer("Титул убран.")
    else:
        set_active_title(uid, title)
        await call.answer(f"Титул «{title}» активирован!")
    await cb_profile(call)

# ===========================================================================
# ИНВЕНТАРЬ
# ===========================================================================

@dp.callback_query(F.data == "inventory")
async def cb_inventory(call: CallbackQuery):
    uid = call.from_user.id
    u   = get_user(uid)
    if not u:
        await call.answer("Профиль не найден.", show_alert=True)
        return

    text = (
        f"🎒 <b>Инвентарь</b>\n\n"
        f"💡 Бесплатные подсказки: <b>{u['free_hints']}</b>\n"
        f"   └ Открывает случайную букву без штрафа\n\n"
        f"🛡 Защита от ПРОПУСКА: <b>{u['skip_skips']}</b>\n"
        f"   └ Автоматически спасает от сектора ПРОПУСК\n\n"
        f"🏷 Титулы: {len(u['titles'])} шт.\n"
        f"   └ {', '.join(u['titles']) if u['titles'] else 'пока нет'}\n\n"
        f"<i>Бонусы получают за повышение уровня!</i>"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton(text="🏠 Меню",    callback_data="main_menu")],
        ]),
    )

# ===========================================================================
# РЕЙТИНГ
# ===========================================================================

LEADERBOARD_PAGE_SIZE = 20

def kb_leaderboard_root() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏆 Топ XP", callback_data="lb_xp10"),
            InlineKeyboardButton(text="💰 Топ очки", callback_data="lb_score10"),
        ],
        [
            InlineKeyboardButton(text="🌍 XP глобально", callback_data="lb_xp_page_1"),
            InlineKeyboardButton(text="🌍 Очки глобально", callback_data="lb_score_page_1"),
        ],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])

def kb_leaderboard_pager(metric: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"lb_{metric}_page_{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"lb_{metric}_page_{page+1}"))
    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="leaderboard")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def _show_leaderboard(call: CallbackQuery, metric: str, limit: int, offset: int, page: Optional[int] = None):
    uid = call.from_user.id
    if metric == "xp":
        rows = get_leaderboard_xp(limit=limit, offset=offset)
        pos  = get_user_position_xp(uid)
        title = "XP"
    else:
        rows = get_leaderboard_score(limit=limit, offset=offset)
        pos  = get_user_position_score(uid)
        title = "очкам"

    lines = []
    if page is None:
        lines.append(f"📊 <b>Топ-10 по {title}</b>\n")
    else:
        total = get_users_count()
        total_pages = max(1, (total + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)
        lines.append(f"📊 <b>Глобальный рейтинг по {title}</b>\nСтраница <b>{page}/{total_pages}</b>\n")

    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(rows):
        absolute_rank = offset + i + 1
        mark = " 👈" if p["user_id"] == uid else ""
        if page is None and i < 3:
            prefix = medals[i]
        else:
            prefix = f"{absolute_rank}."

        if metric == "xp":
            rank = get_rank_for_xp(p["xp"])
            lines.append(f"{prefix} {p['username']} — <b>{p['xp']}</b> XP  {rank['name']}{mark}")
        else:
            lines.append(f"{prefix} {p['username']} — <b>{p['total_score']}</b> очков{mark}")

    if pos:
        lines.append(f"\n👤 Твоё место: <b>#{pos}</b>")

    if page is None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="leaderboard")],
        ])
    else:
        total = get_users_count()
        total_pages = max(1, (total + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)
        kb = kb_leaderboard_pager(metric, page, total_pages)

    await call.message.edit_text("\n".join(lines), reply_markup=kb)

@dp.callback_query(F.data == "leaderboard")
async def cb_leaderboard(call: CallbackQuery):
    await call.message.edit_text("📊 <b>Рейтинг</b>\n\nВыбери, что показать:", reply_markup=kb_leaderboard_root())

@dp.callback_query(F.data == "lb_xp10")
async def cb_lb_xp10(call: CallbackQuery):
    await _show_leaderboard(call, metric="xp", limit=10, offset=0, page=None)

@dp.callback_query(F.data == "lb_score10")
async def cb_lb_score10(call: CallbackQuery):
    await _show_leaderboard(call, metric="score", limit=10, offset=0, page=None)

@dp.callback_query(F.data.startswith("lb_xp_page_"))
async def cb_lb_xp_page(call: CallbackQuery):
    try:
        page = int(call.data.split("_")[-1])
    except Exception:
        page = 1
    page = max(1, page)
    offset = (page - 1) * LEADERBOARD_PAGE_SIZE
    await _show_leaderboard(call, metric="xp", limit=LEADERBOARD_PAGE_SIZE, offset=offset, page=page)

@dp.callback_query(F.data.startswith("lb_score_page_"))
async def cb_lb_score_page(call: CallbackQuery):
    try:
        page = int(call.data.split("_")[-1])
    except Exception:
        page = 1
    page = max(1, page)
    offset = (page - 1) * LEADERBOARD_PAGE_SIZE
    await _show_leaderboard(call, metric="score", limit=LEADERBOARD_PAGE_SIZE, offset=offset, page=page)

# ===========================================================================
# ГЛАВНОЕ МЕНЮ (callback)
# ===========================================================================

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    is_group = call.message.chat.type in ("group", "supergroup")
    if is_group:
        await call.message.edit_text(
            "🎡 <b>Поле Чудес</b>",
            reply_markup=kb_group_menu(),
        )
    else:
        await call.message.edit_text(
            "🎡 <b>Поле Чудес</b> — Главное меню",
            reply_markup=kb_main_menu(),
        )

@dp.callback_query(F.data == "rules")
async def cb_rules(call: CallbackQuery):
    text = (
        "📖 <b>Правила Поля Чудес</b>\n\n"
        "🎯 <b>Цель:</b> угадать загаданное слово по буквам.\n\n"
        "🎡 <b>Барабан</b> (только в мультиплеере ЛС):\n"
        "• Очки (50-700) — умножаются на количество букв\n"
        "• ⭐ ПРИЗ — удвоение очков раунда\n"
        "• 🎁 БОНУС — +200 очков раунда\n"
        "• 💡 ПОДСКАЗКА — +1 бесплатная подсказка\n"
        "• 🛡 ЩИТ — +1 защита от ПРОПУСКА\n"
        "• 🎰 ДЖЕКПОТ — +500 при завершении слова\n"
        "• 🧨 МИНУС — -200 очков раунда\n"
        "• 💀 БАНКРОТ — теряешь очки раунда\n"
        "• ⏩ ПРОПУСК — пропускаешь ход\n\n"
        "🔤 <b>Ввод букв:</b>\n"
        "• В группе — пишешь буквы текстом в чат\n"
        "• В одиночной — нажимаешь на кнопки алфавита\n"
        "• Кулдаун 0.5 сек между буквами\n\n"
        "💡 <b>Бесплатные подсказки</b> — открывают букву без штрафа!\n"
        "🛡 <b>Защита от ПРОПУСКА</b> — срабатывает автоматически.\n\n"
        f"⏰ <b>{TURN_TIMEOUT_SEC} секунд</b> на ход, иначе пропуск!\n\n"
        "📈 <b>Опыт:</b> 100 очков = 10 XP. Повышай уровень, получай бонусы!"
    )
    await call.message.edit_text(text, reply_markup=kb_back_menu())

# ===========================================================================
# ОДИНОЧНАЯ ИГРА
# ===========================================================================

@dp.callback_query(F.data == "single_play")
async def cb_single_play(call: CallbackQuery, state: FSMContext):
    await state.set_state(SinglePlay.choosing_difficulty)
    await call.message.edit_text(
        "🎮 <b>Одиночная игра</b>\n\nВыбери сложность:",
        reply_markup=kb_difficulty("sdiff"),
    )

@dp.callback_query(F.data.startswith("sdiff_"), SinglePlay.choosing_difficulty)
async def cb_s_difficulty(call: CallbackQuery, state: FSMContext):
    diff = call.data[6:]
    await state.update_data(difficulty=diff)
    await state.set_state(SinglePlay.choosing_category)
    dlabel = DIFFICULTY_SETTINGS[diff]["label"]
    await call.message.edit_text(
        f"Сложность: <b>{dlabel}</b>\n\nВыбери категорию:",
        reply_markup=kb_categories("scat"),
    )

@dp.callback_query(F.data.startswith("scat_"), SinglePlay.choosing_category)
async def cb_s_category(call: CallbackQuery, state: FSMContext):
    cat_raw    = call.data[5:]
    data       = await state.get_data()
    difficulty = data["difficulty"]
    category   = random.choice(ALL_CATEGORIES) if cat_raw == "random" else cat_raw

    uid   = call.from_user.id
    uname = call.from_user.full_name
    ensure_user(uid, uname)
    game  = SinglePlayerGame(uid, difficulty, category)
    if not game.load_words():
        await call.message.edit_text("❌ Нет слов для этого выбора.", reply_markup=kb_back_menu())
        return

    single_games[uid] = game
    last_single_settings[uid] = (difficulty, category)
    await state.set_state(SinglePlay.playing)

    u = get_user(uid)
    has_free = u and u["free_hints"] > 0
    status   = build_single_status(game)
    await call.message.edit_text(
        f"🚀 <b>Игра началась!</b>\n\n{status}\n\nНажми букву:",
        reply_markup=kb_single_alphabet(game.guessed_letters, show_free_hint=has_free),
    )

@dp.callback_query(F.data.startswith("sletter_"), SinglePlay.playing)
async def cb_s_letter(call: CallbackQuery, state: FSMContext):
    uid    = call.from_user.id
    game   = single_games.get(uid)
    if not game:
        await call.answer("Игра не найдена. /start", show_alert=True)
        return

    letter = call.data[8:]
    if letter in game.guessed_letters:
        await call.answer("Уже называл!", show_alert=False)
        return

    count = game.guess_letter(letter)
    if count > 0:
        await call.answer(f"✅ «{letter}» — {count} раз(а)! +{count*10} очков")
    else:
        await call.answer(f"❌ «{letter}» — нет такой буквы")

    await _s_update(call, game, state)

@dp.callback_query(F.data == "shint", SinglePlay.playing)
async def cb_s_hint(call: CallbackQuery, state: FSMContext):
    uid  = call.from_user.id
    game = single_games.get(uid)
    if not game:
        await call.answer("Игра не найдена.", show_alert=True)
        return
    if game.score < 50:
        await call.answer("❌ Недостаточно очков (нужно 50)!", show_alert=True)
        return
    letter = game.use_hint(free=False)
    if not letter:
        await call.answer("Все буквы открыты!", show_alert=True)
        return
    await call.answer(f"💡 «{letter}» (-50 очков)")
    await _s_update(call, game, state)

@dp.callback_query(F.data == "sfree_hint", SinglePlay.playing)
async def cb_s_free_hint(call: CallbackQuery, state: FSMContext):
    uid  = call.from_user.id
    game = single_games.get(uid)
    if not game:
        await call.answer("Игра не найдена.", show_alert=True)
        return
    u = get_user(uid)
    if not u or u["free_hints"] <= 0:
        await call.answer("❌ Бесплатных подсказок нет!", show_alert=True)
        return
    letter = game.use_hint(free=True)
    if not letter:
        await call.answer("Все буквы открыты!", show_alert=True)
        return
    use_free_hint(uid)
    await call.answer(f"💡 Бесплатная подсказка: «{letter}» (без штрафа!)", show_alert=True)
    await _s_update(call, game, state)

@dp.callback_query(F.data == "sguess_word", SinglePlay.playing)
async def cb_s_guess_word_prompt(call: CallbackQuery):
    await call.answer()
    await call.message.answer("🔤 Напиши слово целиком:")

@dp.message(SinglePlay.playing)
async def msg_s_guess_word(message: Message, state: FSMContext):
    uid  = message.from_user.id
    game = single_games.get(uid)
    if not game:
        return
    guess = message.text.strip().upper()
    if guess == game.word:
        game.score += 100
        game.word_guessed = True
        await message.answer(f"🎉 <b>Верно! +100 очков!</b>")
        await _s_next_or_finish(message, game, state)
    else:
        game.lives -= 1
        if game.lives <= 0:
            await message.answer(f"💀 Слово было: <b>{game.word}</b>")
            await _s_next_or_finish(message, game, state)
        else:
            status = build_single_status(game)
            u = get_user(uid)
            has_free = u and u["free_hints"] > 0
            await message.answer(
                f"❌ Неверно!\n\n{status}",
                reply_markup=kb_single_alphabet(game.guessed_letters, show_free_hint=has_free),
            )

async def _s_update(call: CallbackQuery, game: SinglePlayerGame, state: FSMContext):
    if game.is_word_complete():
        game.score += 50
        game.word_guessed = True
        await call.message.edit_text(
            f"🎊 <b>Слово угадано! +50 бонус</b>\nСлово: <b>{game.word}</b>",
        )
        await _s_next_or_finish(call.message, game, state)
        return
    if game.lives <= 0:
        await call.message.edit_text(
            f"💀 <b>Жизни кончились!</b>\nСлово: <b>{game.word}</b>",
        )
        await _s_next_or_finish(call.message, game, state)
        return
    uid      = game.user_id
    u        = get_user(uid)
    has_free = u and u["free_hints"] > 0
    status   = build_single_status(game)
    await call.message.edit_text(
        status + "\n\nНажми букву:",
        reply_markup=kb_single_alphabet(game.guessed_letters, show_free_hint=has_free),
    )

async def _s_next_or_finish(message: Message, game: SinglePlayerGame, state: FSMContext):
    await asyncio.sleep(2)
    if game.word_guessed:
        game.words_guessed += 1
    if game.next_word():
        uid      = game.user_id
        u        = get_user(uid)
        has_free = u and u["free_hints"] > 0
        status   = build_single_status(game)
        await message.answer(
            f"➡️ <b>Следующее слово!</b>\n\n{status}\n\nНажми букву:",
            reply_markup=kb_single_alphabet(game.guessed_letters, show_free_hint=has_free),
        )
    else:
        uid = game.user_id
        single_games.pop(uid, None)
        await state.clear()
        result = add_score_and_xp(uid, game.score, game.words_guessed)
        stars  = "⭐" * min(game.score // 100, 5)
        reward_text = ""
        if result.get("leveled_up"):
            lvl = result["new_level"]
            rname = result["new_rank_name"]
            rewards = result.get("rewards", {})
            reward_text = (
                f"\n\n🎉 <b>НОВЫЙ УРОВЕНЬ {lvl}!</b>\n"
                f"Ранг: {rname}\n"
            )
            if rewards.get("hints"):
                reward_text += f"💡 +{rewards['hints']} подсказок\n"
            if rewards.get("skip_skips"):
                reward_text += f"🛡 +{rewards['skip_skips']} защит от пропуска\n"
            if rewards.get("titles"):
                reward_text += f"🏷 Новый титул: {', '.join(rewards['titles'])}\n"
        await message.answer(
            f"🏁 <b>Игра окончена!</b>\n\n"
            f"🎯 Слов угадано: {game.words_guessed}/{game.total_words}\n"
            f"💰 Счёт: <b>{game.score}</b> очков\n"
            f"📈 +{result.get('gained_xp', 0)} XP\n"
            f"{stars}{reward_text}",
            reply_markup=kb_single_rematch(),
        )

@dp.callback_query(F.data == "used_letter")
async def cb_used_letter(call: CallbackQuery):
    await call.answer("Уже названа!", show_alert=False)

@dp.callback_query(F.data == "single_rematch")
async def cb_single_rematch(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    uname = call.from_user.full_name
    settings = last_single_settings.get(uid)
    if not settings:
        await call.answer("Нет сохранённых условий. Запусти /single.", show_alert=True)
        return
    difficulty, category = settings

    ensure_user(uid, uname)
    game = SinglePlayerGame(uid, difficulty, category)
    if not game.load_words():
        await call.message.edit_text("❌ Не удалось загрузить слова для этих условий.", reply_markup=kb_back_menu())
        return

    single_games[uid] = game
    await state.clear()
    await state.set_state(SinglePlay.playing)

    u = get_user(uid)
    has_free = u and u["free_hints"] > 0
    status = build_single_status(game)
    await call.message.edit_text(
        f"🚀 <b>Игра началась!</b>\n\n{status}\n\nНажми букву:",
        reply_markup=kb_single_alphabet(game.guessed_letters, show_free_hint=has_free),
    )

# ===========================================================================
# МУЛЬТИПЛЕЕР В ЛС — СОЗДАНИЕ КОМНАТЫ
# ===========================================================================

@dp.callback_query(F.data == "multi_play")
async def cb_multi_play(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "👥 <b>Мультиплеер (ЛС)</b>\n\nСоздай комнату или войди:",
        reply_markup=kb_multi_menu(),
    )

@dp.callback_query(F.data == "create_room")
async def cb_create_room(call: CallbackQuery, state: FSMContext):
    await state.set_state(CreateRoom.waiting_rounds)
    await call.message.edit_text(
        "🎯 <b>Создание комнаты</b>\n\nСколько раундов?",
        reply_markup=kb_rounds("mrooms"),
    )

@dp.callback_query(F.data.startswith("mrooms_"), CreateRoom.waiting_rounds)
async def cb_m_rounds(call: CallbackQuery, state: FSMContext):
    rounds = int(call.data.split("_")[1])
    await state.update_data(rounds=rounds)
    await state.set_state(CreateRoom.waiting_players)
    await call.message.edit_text(
        f"✅ Раундов: <b>{rounds}</b>\n\nСколько игроков? (2-10)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="2", callback_data="mplayers_2"),
             InlineKeyboardButton(text="3", callback_data="mplayers_3"),
             InlineKeyboardButton(text="4", callback_data="mplayers_4")],
            [InlineKeyboardButton(text="5", callback_data="mplayers_5"),
             InlineKeyboardButton(text="6", callback_data="mplayers_6"),
             InlineKeyboardButton(text="10", callback_data="mplayers_10")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
    )

@dp.callback_query(F.data.startswith("mplayers_"), CreateRoom.waiting_players)
async def cb_m_players_btn(call: CallbackQuery, state: FSMContext):
    await state.update_data(max_players=int(call.data.split("_")[1]))
    await state.set_state(CreateRoom.waiting_difficulty)
    await call.message.edit_text(
        "🎯 Выбери сложность для комнаты:",
        reply_markup=kb_difficulty("mdiff"),
    )

@dp.message(CreateRoom.waiting_players)
async def msg_m_players(message: Message, state: FSMContext):
    try:
        n = int(message.text.strip())
        if n < 2 or n > 100:
            raise ValueError()
    except ValueError:
        await message.answer("❌ Введи число от 2 до 100.")
        return
    await state.update_data(max_players=n)
    await state.set_state(CreateRoom.waiting_difficulty)
    await message.answer("🎯 Выбери сложность:", reply_markup=kb_difficulty("mdiff"))

@dp.callback_query(F.data.startswith("mdiff_"), CreateRoom.waiting_difficulty)
async def cb_m_difficulty(call: CallbackQuery, state: FSMContext):
    diff = call.data[6:]
    await state.update_data(difficulty=diff)
    await state.set_state(CreateRoom.waiting_category)
    dlabel = DIFFICULTY_SETTINGS[diff]["label"]
    await call.message.edit_text(
        f"Сложность: <b>{dlabel}</b>\n\nВыбери категорию:",
        reply_markup=kb_categories("mcat"),
    )

@dp.callback_query(F.data.startswith("mcat_"), CreateRoom.waiting_category)
async def cb_m_category(call: CallbackQuery, state: FSMContext):
    cat_raw  = call.data[5:]
    category = random.choice(ALL_CATEGORIES) if cat_raw == "random" else cat_raw
    data     = await state.get_data()

    uid   = call.from_user.id
    uname = call.from_user.full_name
    ensure_user(uid, uname)

    room = GameRoom(
        host_id=uid, host_name=uname,
        total_rounds=data["rounds"],
        max_players=data["max_players"],
        category=category,
        difficulty=data["difficulty"],
        room_type="private"
    )
    rooms[room.room_id] = room
    await state.clear()

    await call.message.edit_text(
        f"🏠 <b>Комната создана!</b>\n\n"
        f"🔑 Код: <code>{room.room_id}</code>\n"
        f"📚 Категория: <b>{category}</b>\n"
        f"🎯 Раундов: <b>{room.total_rounds}</b>\n"
        f"👥 Игроков: 1/{room.max_players}\n"
        f"🎮 Сложность: {DIFFICULTY_SETTINGS[room.difficulty]['label']}\n\n"
        f"Поделись кодом с друзьями!\nОни входят через ЛС бота → Мультиплеер → Войти.",
        reply_markup=kb_host_room(room.room_id),
    )

# ===========================================================================
# ВХОД В ЛС-КОМНАТУ
# ===========================================================================

@dp.callback_query(F.data == "join_room")
async def cb_join_room(call: CallbackQuery, state: FSMContext):
    await state.set_state(JoinRoom.waiting_room_id)
    await call.message.edit_text("🚪 Введи код комнаты:", reply_markup=kb_back_menu())

@dp.message(JoinRoom.waiting_room_id)
async def msg_join_room(message: Message, state: FSMContext):
    room_id = message.text.strip().upper()
    room    = rooms.get(room_id)

    if not room:
        await message.answer("❌ Комната не найдена. Проверь код.")
        return
    if room.active:
        await message.answer("❌ Игра уже началась!")
        return
    if room.room_type != "private":
        await message.answer("❌ Это групповая комната. Войди через группу.")
        return

    uid   = message.from_user.id
    uname = message.from_user.full_name
    ensure_user(uid, uname)

    if uid in room.player_ids:
        await message.answer("⚠️ Ты уже в этой комнате!")
        await state.clear()
        return
    if room.is_full:
        await message.answer("❌ Комната заполнена!")
        return

    room.add_player(uid, uname)
    await state.clear()

    players_list = "\n".join([f"{i+1}. {room.player_names[p]}" for i, p in enumerate(room.player_ids)])

    for existing_uid in room.player_ids:
        try:
            kb = kb_host_room(room.room_id) if existing_uid == room.host_id else kb_player_room(room.room_id)
            await bot.send_message(
                existing_uid,
                f"👋 <b>{uname}</b> вошёл в комнату!\n\n"
                f"👥 Игроки ({len(room.player_ids)}/{room.max_players}):\n{players_list}",
                reply_markup=kb,
            )
        except Exception:
            pass

    if room.is_full:
        await asyncio.sleep(1)
        await start_multi_game(room)

# ---------------------------------------------------------------------------
# Кнопки управления ЛС-комнатой
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("start_game_"))
async def cb_start_game(call: CallbackQuery):
    room_id = call.data[11:]
    room    = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена!", show_alert=True)
        return
    if call.from_user.id != room.host_id:
        await call.answer("Только создатель может начать!", show_alert=True)
        return
    if len(room.player_ids) < 2:
        await call.answer("Нужно минимум 2 игрока!", show_alert=True)
        return
    await call.answer()
    await start_multi_game(room)

@dp.callback_query(F.data.startswith("delete_room_"))
async def cb_delete_room(call: CallbackQuery):
    room_id = call.data[12:]
    room    = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена!", show_alert=True)
        return
    if call.from_user.id != room.host_id:
        await call.answer("Только создатель может удалить комнату!", show_alert=True)
        return
    await notify_all_in_room(room, "❌ Создатель удалил комнату. Игра отменена.")
    rooms.pop(room_id, None)
    await call.answer("Комната удалена.")
    await call.message.edit_text("❌ Комната удалена.", reply_markup=kb_back_menu())

@dp.callback_query(F.data.startswith("leave_room_"))
async def cb_leave_room(call: CallbackQuery):
    room_id = call.data[11:]
    room    = rooms.get(room_id)
    uid     = call.from_user.id
    if not room or uid not in room.player_ids:
        await call.answer("Ты не в этой комнате.", show_alert=True)
        return
    if room.active:
        await call.answer("Нельзя выйти из активной игры.", show_alert=True)
        return
    uname = room.player_names[uid]
    room.remove_player(uid)
    await call.answer("Ты вышел из комнаты.")
    await call.message.edit_text("🚪 Ты вышел из комнаты.", reply_markup=kb_back_menu())
    for other in room.player_ids:
        try:
            await bot.send_message(other, f"🚪 <b>{uname}</b> вышел из комнаты.")
        except Exception:
            pass

# ===========================================================================
# ГРУППОВЫЕ КОМНАТЫ — СОЗДАНИЕ
# ===========================================================================

@dp.callback_query(F.data == "group_create_room")
async def cb_group_create_start(call: CallbackQuery, state: FSMContext):
    if call.message.chat.type not in ("group", "supergroup"):
        await call.answer("Эта кнопка работает только в группах!", show_alert=True)
        return
    await state.set_state(CreateGroupRoom.waiting_rounds)
    await call.message.edit_text(
        "🎯 <b>Создание игры в группе</b>\n\nСколько раундов?",
        reply_markup=kb_rounds("grrooms"),
    )

@dp.callback_query(F.data.startswith("grrooms_"), CreateGroupRoom.waiting_rounds)
async def cb_gr_rounds(call: CallbackQuery, state: FSMContext):
    rounds = int(call.data.split("_")[1])
    await state.update_data(rounds=rounds)
    await state.set_state(CreateGroupRoom.waiting_players)
    await call.message.edit_text(
        f"✅ Раундов: <b>{rounds}</b>\n\n"
        f"Максимум игроков? (0 = без ограничений)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="∞ Без ограничений", callback_data="grplayers_0")],
            [InlineKeyboardButton(text="4",  callback_data="grplayers_4"),
             InlineKeyboardButton(text="6",  callback_data="grplayers_6"),
             InlineKeyboardButton(text="10", callback_data="grplayers_10")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
    )

@dp.callback_query(F.data.startswith("grplayers_"), CreateGroupRoom.waiting_players)
async def cb_gr_players_btn(call: CallbackQuery, state: FSMContext):
    await state.update_data(max_players=int(call.data.split("_")[1]))
    await state.set_state(CreateGroupRoom.waiting_difficulty)
    await call.message.edit_text(
        "🎯 Выбери сложность:",
        reply_markup=kb_difficulty("grdiff"),
    )

@dp.message(CreateGroupRoom.waiting_players)
async def msg_gr_players(message: Message, state: FSMContext):
    if message.chat.type not in ("group", "supergroup"):
        return
    try:
        n = int(message.text.strip())
        if n < 0 or n > 100:
            raise ValueError()
    except ValueError:
        await message.reply("❌ Введи число от 0 до 100 (0 = без ограничений).")
        return
    await state.update_data(max_players=n)
    await state.set_state(CreateGroupRoom.waiting_difficulty)
    await message.answer("🎯 Выбери сложность:", reply_markup=kb_difficulty("grdiff"))

@dp.callback_query(F.data.startswith("grdiff_"), CreateGroupRoom.waiting_difficulty)
async def cb_gr_difficulty(call: CallbackQuery, state: FSMContext):
    diff = call.data[7:]
    await state.update_data(difficulty=diff)
    await state.set_state(CreateGroupRoom.waiting_category)
    dlabel = DIFFICULTY_SETTINGS[diff]["label"]
    await call.message.edit_text(
        f"Сложность: <b>{dlabel}</b>\n\nВыбери категорию:",
        reply_markup=kb_categories("grcat"),
    )

@dp.callback_query(F.data.startswith("grcat_"), CreateGroupRoom.waiting_category)
async def cb_gr_category(call: CallbackQuery, state: FSMContext):
    cat_raw  = call.data[6:]
    category = random.choice(ALL_CATEGORIES) if cat_raw == "random" else cat_raw
    data     = await state.get_data()

    uid      = call.from_user.id
    uname    = call.from_user.full_name
    chat_id  = call.message.chat.id
    ensure_user(uid, uname)

    if chat_id in group_rooms:
        old_room_id = group_rooms[chat_id]
        if old_room_id in rooms:
            await call.message.edit_text(
                f"❌ В этой группе уже есть активная комната!\n"
                f"Код: <code>{old_room_id}</code>",
            )
            await state.clear()
            return

    max_players = data.get("max_players", 0)
    room = GameRoom(
        host_id=uid, host_name=uname,
        total_rounds=data["rounds"],
        max_players=max_players,
        category=category,
        difficulty=data["difficulty"],
        room_type="group",
        group_chat_id=chat_id,
    )
    rooms[room.room_id]  = room
    group_rooms[chat_id] = room.room_id
    await state.clear()

    max_text = str(max_players) if max_players > 0 else "∞"
    sent = await call.message.edit_text(
        f"🎡 <b>Игра создана!</b>\n\n"
        f"🔑 Код: <code>{room.room_id}</code>\n"
        f"📚 Категория: <b>{category}</b>\n"
        f"🎯 Раундов: <b>{room.total_rounds}</b>\n"
        f"👥 Игроков: 1/{max_text}\n"
        f"🎮 Сложность: {DIFFICULTY_SETTINGS[room.difficulty]['label']}\n\n"
        f"⏰ 5 минут на вход. Нажимайте ✅ Войти:",
        reply_markup=kb_group_lobby(room.room_id),
    )
    room.group_message_id = sent.message_id

    # Уведомить хоста в ЛС
    await _notify_player_ls(uid, uname, room, is_host=True)

    asyncio.create_task(_group_room_deadline(room.room_id, chat_id))

async def _notify_player_ls(uid: int, uname: str, room: GameRoom, is_host: bool):
    """Отправить игроку уведомление в ЛС о том что он в групповой игре."""
    role = "создатель комнаты" if is_host else "участник"
    kb   = kb_host_ls(room.room_id) if is_host else kb_player_ls(room.room_id)
    try:
        await bot.send_message(
            uid,
            f"🎡 <b>Ты {'создал' if is_host else 'вошёл в'} игру в группе!</b>\n\n"
            f"🔑 Комната: <code>{room.room_id}</code>\n"
            f"📚 Категория: <b>{room.current_category}</b>\n"
            f"🎮 Сложность: {DIFFICULTY_SETTINGS[room.difficulty]['label']}\n\n"
            f"Роль: <b>{role}</b>\n\n"
            f"<i>Игра идёт в группе — заходи и следи там за ходами!\n"
            f"{'Ты можешь начать досрочно или отменить игру.' if is_host else 'Если захочешь — можешь покинуть игру.'}</i>",
            reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить ЛС {uid}: {e}")

async def _group_room_deadline(room_id: str, chat_id: int):
    await asyncio.sleep(300)
    room = rooms.get(room_id)
    if not room or room.active:
        return
    if len(room.player_ids) < 2:
        await bot.send_message(chat_id, "⏰ Время вышло. Недостаточно игроков, игра отменена.")
        rooms.pop(room_id, None)
        group_rooms.pop(chat_id, None)
        return
    await bot.send_message(chat_id, "⏰ Время набора игроков истекло! Начинаем...")
    await start_multi_game(room)

# ===========================================================================
# ГРУППОВЫЕ КОМНАТЫ — ВХОД / УПРАВЛЕНИЕ
# ===========================================================================

@dp.callback_query(F.data.startswith("gjoin_"))
async def cb_group_join(call: CallbackQuery):
    room_id = call.data[6:]
    room    = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена!", show_alert=True)
        return
    if room.active:
        await call.answer("Игра уже началась!", show_alert=True)
        return

    uid   = call.from_user.id
    uname = call.from_user.full_name
    ensure_user(uid, uname)

    if uid in room.player_ids:
        await call.answer("Ты уже в игре!", show_alert=False)
        return
    if room.is_full:
        await call.answer("Комната заполнена!", show_alert=True)
        return

    room.add_player(uid, uname)
    await call.answer(f"✅ {uname} вошёл в игру!")

    max_text     = str(room.max_players) if room.max_players > 0 else "∞"
    players_list = "\n".join([f"{i+1}. {room.player_names[p]}" for i, p in enumerate(room.player_ids)])

    try:
        await call.message.edit_text(
            f"🎡 <b>Ожидаем игроков...</b>\n\n"
            f"🔑 Код: <code>{room.room_id}</code>\n"
            f"📚 {room.current_category}  |  {DIFFICULTY_SETTINGS[room.difficulty]['label']}\n"
            f"👥 {len(room.player_ids)}/{max_text}:\n{players_list}\n\n"
            f"⏰ 5 мин на вход. Хост может начать досрочно.",
            reply_markup=kb_group_lobby(room.room_id),
        )
    except Exception:
        pass

    # Уведомить нового игрока в ЛС
    await _notify_player_ls(uid, uname, room, is_host=False)

    if room.is_full:
        await asyncio.sleep(1)
        await start_multi_game(room)

@dp.callback_query(F.data.startswith("gstart_"))
async def cb_group_start(call: CallbackQuery):
    room_id = call.data[7:]
    room    = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена!", show_alert=True)
        return
    if call.from_user.id != room.host_id:
        await call.answer("Только создатель может начать досрочно!", show_alert=True)
        return
    if len(room.player_ids) < 2:
        await call.answer("Нужно минимум 2 игрока!", show_alert=True)
        return
    await call.answer()
    await start_multi_game(room)

@dp.callback_query(F.data.startswith("gcancel_ls_"))
async def cb_group_cancel_ls(call: CallbackQuery):
    """Отмена групповой игры через кнопку в ЛС хоста."""
    room_id = call.data[11:]
    room    = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена!", show_alert=True)
        return
    if call.from_user.id != room.host_id:
        await call.answer("Только создатель может отменить!", show_alert=True)
        return
    if room.active:
        await call.answer("Игра уже идёт, нельзя отменить.", show_alert=True)
        return
    try:
        await bot.send_message(room.group_chat_id, "❌ Хост отменил игру.")
    except Exception:
        pass
    # Уведомить всех участников в ЛС
    for uid in room.player_ids:
        if uid == room.host_id:
            continue
        try:
            await bot.send_message(uid, f"❌ Хост отменил игру <code>{room_id}</code>.")
        except Exception:
            pass
    rooms.pop(room_id, None)
    group_rooms.pop(room.group_chat_id, None)
    await call.answer("Игра отменена.")
    await call.message.edit_text("❌ Игра отменена.", reply_markup=kb_back_menu())

@dp.callback_query(F.data.startswith("gcancel_"))
async def cb_group_cancel(call: CallbackQuery):
    """Отмена игры через кнопку в группе."""
    room_id = call.data[8:]
    # Не обрабатываем gcancel_ls_ здесь
    if room_id.startswith("ls_"):
        return
    room = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена!", show_alert=True)
        return
    if call.from_user.id != room.host_id:
        await call.answer("Только создатель может отменить!", show_alert=True)
        return
    for uid in room.player_ids:
        try:
            await bot.send_message(uid, f"❌ Игра в группе отменена хостом.")
        except Exception:
            pass
    rooms.pop(room_id, None)
    group_rooms.pop(room.group_chat_id, None)
    await call.message.edit_text("❌ Игра отменена создателем.")
    await call.answer("Игра отменена.")

@dp.callback_query(F.data.startswith("gleave_"))
async def cb_group_leave_ls(call: CallbackQuery):
    """Выход игрока из групповой игры через кнопку в ЛС."""
    room_id = call.data[7:]
    room    = rooms.get(room_id)
    uid     = call.from_user.id
    if not room or uid not in room.player_ids:
        await call.answer("Ты не в этой игре.", show_alert=True)
        return
    if room.active:
        await call.answer("Нельзя выйти из активной игры.", show_alert=True)
        return
    uname = room.player_names[uid]
    room.remove_player(uid)
    await call.answer("Ты покинул игру.")
    await call.message.edit_text("🚪 Ты покинул игру.", reply_markup=kb_back_menu())
    try:
        await bot.send_message(
            room.group_chat_id,
            f"🚪 <b>{uname}</b> покинул игру.",
        )
    except Exception:
        pass
    for other in room.player_ids:
        try:
            await bot.send_message(other, f"🚪 <b>{uname}</b> покинул игру.")
        except Exception:
            pass

# ===========================================================================
# ЗАПУСК МУЛЬТИПЛЕЕРНОЙ ИГРЫ
# ===========================================================================

# ===========================================================================
# ТАЙМЕР БЕЗДЕЙСТВИЯ (1 час без активности — завершить игру)
# ===========================================================================

AFK_TIMEOUT = 3600  # 1 час в секундах
AFK_CHECK_INTERVAL = 600  # проверять каждые 10 минут

async def afk_game_timer(room_id: str):
    """Завершает игру если никто не активен больше 1 часа."""
    while True:
        await asyncio.sleep(AFK_CHECK_INTERVAL)
        room = rooms.get(room_id)
        if not room or not room.active:
            return
        elapsed = time.time() - room.last_activity
        if elapsed >= AFK_TIMEOUT:
            msg = "⏰ <b>Игра завершена из-за бездействия игроков (1 час без активности).</b>"
            if room.room_type == "group":
                try:
                    await bot.send_message(room.group_chat_id, msg)
                except Exception:
                    pass
                group_rooms.pop(room.group_chat_id, None)
            else:
                await notify_all_in_room(room, msg)
            room.active = False
            return

# ===========================================================================
# КНОПКА СДАТЬСЯ
# ===========================================================================

@dp.callback_query(F.data.startswith("surrender_"))
async def cb_surrender(call: CallbackQuery):
    """Игрок сдаётся — выходит из активной игры."""
    room_id = call.data[10:]
    room    = rooms.get(room_id)
    uid     = call.from_user.id

    if not room or not room.active:
        await call.answer("Игра уже завершена.", show_alert=True)
        return
    if uid not in room.player_ids:
        await call.answer("Ты не в этой игре.", show_alert=True)
        return

    uname = room.player_names[uid]
    room.player_ids.remove(uid)
    room.player_names.pop(uid, None)

    surrender_msg = f"🏳️ <b>{uname}</b> сдался и покинул игру."

    # Если остался 1 игрок — он победитель, иначе игра продолжается
    if len(room.player_ids) == 1:
        winner_uid  = room.player_ids[0]
        winner_name = room.player_names[winner_uid]
        win_msg = (
            f"{surrender_msg}\n\n"
            f"🏆 <b>{winner_name}</b> — единственный оставшийся игрок и побеждает!"
        )
        if room.room_type == "group":
            try:
                await bot.send_message(room.group_chat_id, win_msg)
            except Exception:
                pass
            group_rooms.pop(room.group_chat_id, None)
        else:
            await notify_all_in_room(room, win_msg)
        # Начислить очки победителю
        sc     = room.scores.get(winner_uid, 0)
        result = add_score_and_xp(winner_uid, sc, 0)
        if result.get("leveled_up"):
            lvl     = result["new_level"]
            rname   = result["new_rank_name"]
            try:
                await bot.send_message(winner_uid, f"🎉 <b>НОВЫЙ УРОВЕНЬ {lvl}!</b> {rname}")
            except Exception:
                pass
        room.active = False
        await call.answer("Ты сдался. Игра завершена.", show_alert=True)
        return
    elif len(room.player_ids) == 0:
        # Все сдались
        if room.room_type == "group":
            try:
                await bot.send_message(room.group_chat_id, f"{surrender_msg}\n\nВсе игроки покинули игру.")
            except Exception:
                pass
            group_rooms.pop(room.group_chat_id, None)
        room.active = False
        await call.answer("Ты сдался.", show_alert=True)
        return

    # Несколько игроков остались — игра продолжается
    # Если сдался текущий игрок — передать ход
    if room.current_player_id == uid:
        # current_player_idx мог стать невалидным — исправим
        room.current_player_idx = room.current_player_idx % len(room.player_ids)
    else:
        # Корректируем индекс если нужно
        try:
            idx = room.player_ids.index(room.current_player_id)
            room.current_player_idx = idx
        except ValueError:
            room.current_player_idx = 0

    cont_msg = f"{surrender_msg}\n\n👥 Игра продолжается! Осталось игроков: {len(room.player_ids)}"
    if room.room_type == "group":
        try:
            await bot.send_message(room.group_chat_id, cont_msg)
        except Exception:
            pass
    else:
        await notify_all_in_room(room, cont_msg)

    await call.answer("Ты сдался. Игра продолжается без тебя.", show_alert=True)

    if room.current_player_id == uid:
        await asyncio.sleep(1)
        await send_turn_message(room)



async def start_multi_game(room: GameRoom):
    room.start_game()
    players_list = "\n".join([f"{i+1}. {room.player_names[p]}" for i, p in enumerate(room.player_ids)])
    msg = (
        f"🎉 <b>Игра началась!</b>\n\n"
        f"👥 Игроки:\n{players_list}\n\n"
        f"🎯 Раундов: {room.total_rounds}  |  {DIFFICULTY_SETTINGS[room.difficulty]['label']}\n"
        f"📚 Категория: {room.current_category}\n\n"
        f"Начинаем!"
    )
    if room.room_type == "group":
        try:
            await bot.send_message(room.group_chat_id, msg)
        except Exception:
            pass
        try:
            await bot.send_message(
                room.group_chat_id,
                f"ℹ️ Буквы называйте <b>текстом</b> в чат группы (одна русская буква).\n"
                f"💡 Бесплатная подсказка: /hint_{room.room_id} в чате группы.",
            )
        except Exception:
            pass
        # Обновить кнопки в ЛС у всех — убрать кнопку "Начать досрочно"
        for uid in room.player_ids:
            try:
                await bot.send_message(
                    uid,
                    f"▶️ <b>Игра началась!</b> Следи за ходом в группе.\n"
                    f"💡 Напиши /hint_{room.room_id} в группе если хочешь использовать подсказку.",
                )
            except Exception:
                pass
    else:
        await notify_all_in_room(room, msg)

    await asyncio.sleep(2)
    await send_turn_message(room)
    # Запускаем таймер бездействия
    asyncio.create_task(afk_game_timer(room.room_id))
# ===========================================================================

@dp.message(F.text & ~F.via_bot)
async def msg_letter_input(message: Message, state: FSMContext):
    fsm_state = await state.get_state()
    if fsm_state in (
        SinglePlay.playing, SinglePlay.choosing_difficulty, SinglePlay.choosing_category,
        CreateRoom.waiting_players, CreateRoom.waiting_rounds,
        CreateRoom.waiting_difficulty, CreateRoom.waiting_category,
        CreateGroupRoom.waiting_players, CreateGroupRoom.waiting_rounds,
        CreateGroupRoom.waiting_difficulty, CreateGroupRoom.waiting_category,
        JoinRoom.waiting_room_id, ProfileState.choosing_title,
    ):
        return

    uid  = message.from_user.id
    text = message.text.strip().upper()

    if message.chat.type in ("group", "supergroup"):
        room_id = group_rooms.get(message.chat.id)
        room    = rooms.get(room_id) if room_id else None
        # В группе реагируем ТОЛЬКО на одиночные русские буквы (или полное слово для угадывания)
        # Обычные сообщения пользователей игнорируем полностью
        if not room or not room.active:
            return
        if room.current_player_id != uid:
            return
        # Проверяем что текст — одна буква алфавита или слово (без цифр, смешанного текста и т.д.)
        if len(text) == 1:
            if text not in ALPHABET:
                return  # Не русская буква — игнорируем
        elif text.isalpha() and all(c in ALPHABET for c in text):
            pass  # Слово целиком — допускаем (угадывание слова)
        else:
            return  # Числа, эмодзи, латиница, смешанный текст — игнорируем
    else:
        room = _find_room_by_player(uid)

    if not room or not room.active:
        return

    if room.current_player_id != uid:
        return

    cd = room.check_cooldown(uid)
    if cd > 0:
        return

    if len(text) > 1 and text.isalpha() and all(c in ALPHABET for c in text):
        room.last_activity = time.time()
        restart_turn_timer(room)
        await _handle_multi_word_guess(message, room, uid, text)
        return

    if len(text) != 1 or not text.isalpha() or text not in ALPHABET:
        return

    if room.room_type == "private" and room.spin_points is None and not room.prize_active:
        await message.reply("🎡 Сначала крутни барабан!")
        return

    if text in room.guessed_letters:
        await message.reply("Эта буква уже называлась!")
        return

    room.apply_cooldown(uid)
    room.last_activity = time.time()  # обновляем время активности
    restart_turn_timer(room)
    await _handle_multi_letter(message, room, uid, text)

async def _handle_multi_letter(message: Message, room: GameRoom, uid: int, letter: str):
    count = room.guess_letter(letter)
    uname = room.player_names[uid]

    if count > 0:
        points_per = room.spin_points if room.spin_points else 100
        earned     = points_per * count

        if room.prize_active:
            room.round_scores[uid] = (room.round_scores.get(uid, 0) + earned) * 2
            room.prize_active = False
            prize_text = " 🎉 ПРИЗ — очки удвоены!"
        else:
            room.round_scores[uid] = room.round_scores.get(uid, 0) + earned
            prize_text = ""

        if room.room_type == "group":
            await message.reply(
                f"✅ <b>«{letter}»</b> — {count} раз(а)! <b>+{earned} очков</b>{prize_text}",
            )
        else:
            await notify_all_in_room(
                room,
                f"✅ <b>{uname}</b>: буква «{letter}» — {count} раз(а)! +{earned}{prize_text}",
            )

        room.spin_points  = None
        room.prize_active = False

        if room.is_round_complete():
            if getattr(room, "jackpot_active", False):
                room.round_scores[uid] = room.round_scores.get(uid, 0) + 500
                room.jackpot_active = False
                await notify_all_in_room(room, f"🎰 <b>ДЖЕКПОТ!</b> {uname} завершил слово и получает +500 очков раунда!")
            await asyncio.sleep(1)
            await finish_round(room)
        else:
            status = build_round_status(room)
            if room.room_type == "group":
                await bot.send_message(
                    room.group_chat_id,
                    f"{status}\n\n👉 Ходит {mention(uid, uname)} — назови ещё букву или слово целиком!",
                )
            else:
                await bot.send_message(
                    uid,
                    f"{status}\n\n🎡 Назови ещё букву или крути барабан!",
                    reply_markup=kb_spin(room, uid),
                )
    else:
        if room.room_type == "group":
            await message.reply(f"❌ Буквы <b>«{letter}»</b> нет!")
        else:
            await notify_all_in_room(room, f"❌ <b>{uname}</b>: буквы «{letter}» нет, ход переходит!")

        room.spin_points  = None
        room.prize_active = False
        room.next_player()
        await asyncio.sleep(1)
        await send_turn_message(room)

async def _handle_multi_word_guess(message: Message, room: GameRoom, uid: int, guess: str):
    uname = room.player_names[uid]
    room.apply_cooldown(uid)
    if guess == room.current_word:
        room.round_scores[uid] = room.round_scores.get(uid, 0) + 200
        room.scores[uid]      += room.round_scores[uid]
        if room.room_type == "group":
            await message.reply(
                f"🎊 <b>{uname}</b> угадал слово: <b>{room.current_word}</b>!\n"
                f"+{room.round_scores[uid]} очков (с бонусом 200)!",
            )
        else:
            await notify_all_in_room(
                room,
                f"🎊 <b>{uname}</b> угадал слово: <b>{room.current_word}</b>!\n"
                f"+{room.round_scores[uid]} очков!"
            )
        if getattr(room, "jackpot_active", False):
            room.round_scores[uid] = room.round_scores.get(uid, 0) + 500
            room.jackpot_active = False
            await notify_all_in_room(room, f"🎰 <b>ДЖЕКПОТ!</b> {uname} завершил слово и получает +500 очков раунда!")
        await asyncio.sleep(2)
        await finish_round(room)
    else:
        room.spin_points = None
        room.next_player()
        next_name = room.player_names[room.current_player_id]
        if room.room_type == "group":
            await message.reply(f"❌ Неверно! Ход к <b>{next_name}</b>.")
        else:
            await notify_all_in_room(room, f"❌ <b>{uname}</b> назвал неверное слово! Ход к <b>{next_name}</b>.")
        await asyncio.sleep(1)
        await send_turn_message(room)

# ===========================================================================
# БАРАБАН (ЛС-мультиплеер)
# ===========================================================================

@dp.callback_query(F.data == "spin_wheel")
async def cb_spin_wheel(call: CallbackQuery):
    uid  = call.from_user.id
    room = _find_room_by_player(uid)
    if not room or room.room_type != "private":
        await call.answer("Игра не найдена!", show_alert=True)
        return
    if room.current_player_id != uid:
        await call.answer("Сейчас не твой ход!", show_alert=True)
        return
    await call.answer()
    room.last_activity = time.time()
    restart_turn_timer(room)

    sector              = spin_wheel()
    room.current_sector = sector

    if sector == "БАНКРОТ":
        old = room.round_scores[uid]
        room.round_scores[uid] = 0
        await call.message.edit_text(
            f"💀 <b>БАНКРОТ!</b> {room.player_names[uid]} теряет {old} очков раунда!\nХод переходит.",
        )
        room.next_player()
        await asyncio.sleep(2)
        await send_turn_message(room)

    elif sector == "ПРОПУСК":
        u = get_user(uid)
        if u and u["skip_skips"] > 0:
            use_skip_skip(uid)
            await call.message.edit_text(
                f"⏩ ПРОПУСК — но <b>🛡 Защита</b> сработала! Ход продолжается.",
                reply_markup=kb_spin(room, uid),
            )
        else:
            await call.message.edit_text(
                f"⏩ <b>ПРОПУСК ХОДА!</b> {room.player_names[uid]} пропускает ход.",
            )
            room.next_player()
            await asyncio.sleep(2)
            await send_turn_message(room)

    elif sector == "ПРИЗ":
        room.prize_active = True
        status = build_round_status(room)
        await notify_all_in_room(room, f"⭐ <b>ПРИЗ!</b> {room.player_names[uid]} активировал ПРИЗ!\nЕсли угадает букву — очки удвоятся!")
        await call.message.edit_text(
            f"⭐ <b>ПРИЗ!</b> Угадай букву — очки раунда удвоятся!\n\n{status}\n\nНапиши букву в чат:",
        )

    elif sector == "БОНУС":
        room.round_scores[uid] = room.round_scores.get(uid, 0) + 200
        status = build_round_status(room)
        await notify_all_in_room(room, f"🎁 <b>БОНУС!</b> {room.player_names[uid]} получает +200 очков раунда!")
        await call.message.edit_text(
            f"🎁 <b>БОНУС!</b> +200 очков раунда.\n\n{status}\n\nМожешь крутить ещё!",
            reply_markup=kb_spin(room, uid),
        )

    elif sector == "МИНУС":
        room.round_scores[uid] = max(0, room.round_scores.get(uid, 0) - 200)
        status = build_round_status(room)
        await notify_all_in_room(room, f"🧨 <b>МИНУС!</b> {room.player_names[uid]} теряет 200 очков раунда.")
        await call.message.edit_text(
            f"🧨 <b>МИНУС!</b> -200 очков раунда.\n\n{status}\n\nМожешь крутить ещё!",
            reply_markup=kb_spin(room, uid),
        )

    elif sector == "ПОДСКАЗКА":
        add_free_hints(uid, 1)
        u2 = get_user(uid)
        status = build_round_status(room)
        await notify_all_in_room(room, f"💡 <b>ПОДСКАЗКА!</b> {room.player_names[uid]} получает +1 бесплатную подсказку.")
        await call.message.edit_text(
            f"💡 <b>ПОДСКАЗКА!</b> +1 бесплатная подсказка (теперь: <b>{u2['free_hints'] if u2 else 0}</b>).\n\n{status}\n\nМожешь крутить ещё!",
            reply_markup=kb_spin(room, uid),
        )

    elif sector == "ЩИТ":
        add_skip_skips(uid, 1)
        u2 = get_user(uid)
        status = build_round_status(room)
        await notify_all_in_room(room, f"🛡 <b>ЩИТ!</b> {room.player_names[uid]} получает +1 защиту от ПРОПУСКА.")
        await call.message.edit_text(
            f"🛡 <b>ЩИТ!</b> +1 защита от ПРОПУСКА (теперь: <b>{u2['skip_skips'] if u2 else 0}</b>).\n\n{status}\n\nМожешь крутить ещё!",
            reply_markup=kb_spin(room, uid),
        )

    elif sector == "ДЖЕКПОТ":
        room.jackpot_active = True
        room.spin_points = 300
        status = build_round_status(room)
        await notify_all_in_room(room, f"🎰 <b>ДЖЕКПОТ!</b> {room.player_names[uid]} активировал джекпот (+500 при завершении слова)!\nСтавка: 300 очков за букву.")
        await call.message.edit_text(
            f"🎰 <b>ДЖЕКПОТ!</b> +500 очков при завершении слова!\n\n{status}\n\nНапиши букву в чат:",
        )

    else:
        points = int(sector)
        room.spin_points = points
        status = build_round_status(room)
        await call.message.edit_text(
            f"🎡 <b>Барабан: {points} очков!</b>\n\n{status}\n\nНапиши букву в чат:",
        )

@dp.callback_query(F.data == "use_free_hint_multi")
async def cb_use_free_hint_multi(call: CallbackQuery):
    uid  = call.from_user.id
    room = _find_room_by_player(uid)
    if not room:
        await call.answer("Игра не найдена!", show_alert=True)
        return
    if room.current_player_id != uid:
        await call.answer("Сейчас не твой ход!", show_alert=True)
        return
    u = get_user(uid)
    if not u or u["free_hints"] <= 0:
        await call.answer("Бесплатных подсказок нет!", show_alert=True)
        return
    room.last_activity = time.time()
    restart_turn_timer(room)
    hidden = [c for c in set(room.current_word) if c.isalpha() and c not in room.guessed_letters]
    if not hidden:
        await call.answer("Все буквы открыты!", show_alert=True)
        return
    letter = random.choice(hidden)
    room.guessed_letters.add(letter)
    use_free_hint(uid)
    await call.answer(f"💡 Открыта буква «{letter}»!")
    uname = room.player_names[uid]
    await notify_all_in_room(room, f"💡 <b>{uname}</b> использовал подсказку: буква «{letter}»!")
    if room.is_round_complete():
        if getattr(room, "jackpot_active", False):
            room.round_scores[uid] = room.round_scores.get(uid, 0) + 500
            room.jackpot_active = False
            await notify_all_in_room(room, f"🎰 <b>ДЖЕКПОТ!</b> {uname} завершил слово и получает +500 очков раунда!")
        await finish_round(room)
    else:
        status = build_round_status(room)
        await call.message.edit_text(
            f"{status}\n\nНазови ещё букву или крути барабан!",
            reply_markup=kb_spin(room, uid),
        )

@dp.callback_query(F.data == "guess_word_multi")
async def cb_guess_word_multi(call: CallbackQuery):
    uid  = call.from_user.id
    room = _find_room_by_player(uid)
    if not room:
        await call.answer("Игра не найдена!", show_alert=True)
        return
    if room.current_player_id != uid:
        await call.answer("Не твой ход!", show_alert=True)
        return
    await call.answer()
    room.last_activity = time.time()
    restart_turn_timer(room)
    await call.message.answer("🔤 Напиши слово целиком в чат:")

# ===========================================================================
# ЗАВЕРШЕНИЕ РАУНДА / ИГРЫ
# ===========================================================================

async def finish_round(room: GameRoom):
    for uid in room.player_ids:
        room.scores[uid] = room.scores.get(uid, 0) + room.round_scores.get(uid, 0)

    winner_uid  = max(room.round_scores, key=lambda x: room.round_scores.get(x, 0))
    winner_name = room.player_names[winner_uid]
    w_score     = room.round_scores[winner_uid]

    scores_text = "\n".join([
        f"{'🥇' if i == 0 else '  '} {room.player_names[uid]}: {room.scores[uid]} очков"
        for i, uid in enumerate(sorted(room.player_ids, key=lambda x: room.scores[x], reverse=True))
    ])

    msg = (
        f"🏁 <b>Раунд {room.current_round} завершён!</b>\n\n"
        f"🔤 Слово: <b>{room.current_word}</b>\n"
        f"🏆 Лучший в раунде: <b>{winner_name}</b> (+{w_score} очков)\n\n"
        f"💰 <b>Счёт:</b>\n{scores_text}"
    )

    if room.room_type == "group":
        try:
            await bot.send_message(room.group_chat_id, msg)
        except Exception:
            pass
    else:
        await notify_all_in_room(room, msg)

    await asyncio.sleep(3)

    if room.has_next_round():
        room.next_round()
        next_msg = f"🎯 <b>Раунд {room.current_round}/{room.total_rounds}</b>\n📚 {room.current_category}  |  Приготовились!"
        if room.room_type == "group":
            try:
                await bot.send_message(room.group_chat_id, next_msg)
            except Exception:
                pass
        else:
            await notify_all_in_room(room, next_msg)
        await asyncio.sleep(2)
        await send_turn_message(room)
    else:
        await finish_game(room)

async def finish_game(room: GameRoom):
    sorted_players = sorted(room.player_ids, key=lambda x: room.scores.get(x, 0), reverse=True)
    champion_uid   = sorted_players[0]
    champion_name  = room.player_names[champion_uid]
    medals         = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 20)]
    results        = [
        f"{medals[i]} {room.player_names[uid]}: <b>{room.scores.get(uid, 0)}</b> очков"
        for i, uid in enumerate(sorted_players)
    ]
    final_msg = (
        f"🎊 <b>ИГРА ОКОНЧЕНА!</b> 🎊\n\n"
        f"🏆 <b>ПОБЕДИТЕЛЬ: {champion_name}!</b>\n\n"
        f"📊 Итог:\n" + "\n".join(results) +
        "\n\nСпасибо за игру! /menu"
    )

    if room.room_type == "group":
        try:
            await bot.send_message(room.group_chat_id, final_msg, reply_markup=kb_rematch(room.room_id))
        except Exception:
            pass
        group_rooms.pop(room.group_chat_id, None)
    else:
        await notify_all_in_room(room, final_msg, reply_markup=kb_rematch(room.room_id))

    for uid in room.player_ids:
        sc     = room.scores.get(uid, 0)
        result = add_score_and_xp(uid, sc, 0)
        if result.get("leveled_up"):
            lvl     = result["new_level"]
            rname   = result["new_rank_name"]
            rewards = result.get("rewards", {})
            lv_txt  = f"🎉 <b>НОВЫЙ УРОВЕНЬ {lvl}!</b> {rname}\n"
            if rewards.get("hints"):
                lv_txt += f"💡 +{rewards['hints']} подсказок\n"
            if rewards.get("skip_skips"):
                lv_txt += f"🛡 +{rewards['skip_skips']} защит\n"
            if rewards.get("titles"):
                lv_txt += f"🏷 Новый титул: {', '.join(rewards['titles'])}\n"
            try:
                await bot.send_message(uid, lv_txt)
            except Exception:
                pass

    _cancel_turn_timer(room.room_id)
    room.active = False
    # Комната остаётся в памяти — не удаляем, чтобы игроки могли видеть результаты

def _reset_room_for_rematch(room: GameRoom):
    _cancel_turn_timer(room.room_id)
    room.active = False
    room.current_round = 0
    room.current_player_idx = 0
    room.turn_counter = 0
    room.turn_timer_token = 0

    room.current_category = room.base_category
    room.guessed_letters = set()
    room.used_words.clear()

    room.spin_points = None
    room.prize_active = False
    room.jackpot_active = False

    for uid in room.player_ids:
        room.scores[uid] = 0
        room.round_scores[uid] = 0

@dp.callback_query(F.data.startswith("rematch_"))
async def cb_rematch(call: CallbackQuery):
    room_id = call.data.split("_", 1)[1]
    room = rooms.get(room_id)
    if not room:
        await call.answer("Комната не найдена.", show_alert=True)
        return
    if room.active:
        await call.answer("Игра уже идёт.", show_alert=True)
        return
    if call.from_user.id != room.host_id:
        await call.answer("Рематч может запускать только хост.", show_alert=True)
        return

    _reset_room_for_rematch(room)

    if room.room_type == "group":
        group_rooms[room.group_chat_id] = room.room_id

    await call.answer("Запускаю рематч!")
    await start_multi_game(room)

# ===========================================================================
# ЗАПУСК
# ===========================================================================

async def main():
    init_db()
    logger.info("Запуск Поле Чудес бота...")

    # Устанавливаем команды бота
    await bot.set_my_commands([
        BotCommand(command="start",  description="Главное меню"),
        BotCommand(command="menu",   description="Главное меню"),
        BotCommand(command="single", description="🎮 Одиночная игра"),
        BotCommand(command="multi",  description="👥 Мультиплеер в группе"),
    ])

    await dp.start_polling(
        bot,
        skip_updates=True,
        allowed_updates=[
            "message",
            "callback_query",
            "my_chat_member",
            "chat_member",
        ]
    )

if __name__ == "__main__":
    asyncio.run(main())
