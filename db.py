"""
db.py - Хранение профилей пользователей, рангов, инвентаря (SQLite)
"""

import sqlite3
import json
import os
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "game_data.db")

# ---------------------------------------------------------------------------
# Система рангов
# ---------------------------------------------------------------------------
RANKS = [
    {"level": 1,  "name": "🌱 Новичок",        "xp_needed": 0},
    {"level": 2,  "name": "📚 Ученик",          "xp_needed": 100},
    {"level": 3,  "name": "✏️ Грамотей",        "xp_needed": 250},
    {"level": 4,  "name": "🔤 Знаток букв",     "xp_needed": 500},
    {"level": 5,  "name": "💬 Словоблуд",       "xp_needed": 900},
    {"level": 6,  "name": "📖 Книгочей",        "xp_needed": 1400},
    {"level": 7,  "name": "🧠 Эрудит",          "xp_needed": 2000},
    {"level": 8,  "name": "🏆 Чемпион слов",    "xp_needed": 3000},
    {"level": 9,  "name": "⭐ Мастер",          "xp_needed": 4500},
    {"level": 10, "name": "👑 Гроссмейстер",    "xp_needed": 7000},
    {"level": 11, "name": "🔥 Легенда",         "xp_needed": 10000},
    {"level": 12, "name": "💎 Бессмертный",     "xp_needed": 15000},
]

LEVEL_UP_REWARDS = {
    2:  {"hints": 2, "title": "📚 Первые шаги"},
    3:  {"hints": 2, "title": "✏️ Грамотей"},
    4:  {"hints": 3, "skip_skips": 1, "title": "🔤 Знаток"},
    5:  {"hints": 3, "word_replaces": 1, "title": "💬 Словоблуд"},
    6:  {"hints": 4, "skip_skips": 1, "title": "📖 Книгочей"},
    7:  {"hints": 5, "skip_skips": 2, "title": "🧠 Эрудит"},
    8:  {"hints": 5, "skip_skips": 2, "word_replaces": 1, "title": "🏆 Чемпион"},
    9:  {"hints": 7, "skip_skips": 3, "title": "⭐ Мастер"},
    10: {"hints": 10, "skip_skips": 3, "title": "👑 Гроссмейстер"},
    11: {"hints": 15, "skip_skips": 5, "word_replaces": 2, "title": "🔥 Легенда"},
    12: {"hints": 20, "skip_skips": 7, "title": "💎 Бессмертный"},
}

def get_rank_for_xp(xp: int) -> dict:
    current = RANKS[0]
    for rank in RANKS:
        if xp >= rank["xp_needed"]:
            current = rank
        else:
            break
    return current

def get_next_rank(xp: int) -> Optional[dict]:
    for rank in RANKS:
        if rank["xp_needed"] > xp:
            return rank
    return None

def xp_for_score(score: int) -> int:
    return score // 10

# ---------------------------------------------------------------------------
# Инициализация БД
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            xp          INTEGER DEFAULT 0,
            level       INTEGER DEFAULT 1,
            total_score INTEGER DEFAULT 0,
            games_played INTEGER DEFAULT 0,
            words_guessed INTEGER DEFAULT 0,
            -- Инвентарь
            free_hints    INTEGER DEFAULT 1,
            skip_skips    INTEGER DEFAULT 0,
            word_replaces INTEGER DEFAULT 0,
            titles        TEXT DEFAULT '[]',
            active_title  TEXT DEFAULT ''
        )
    """)
    # Миграция: добавить столбец word_replaces если его нет
    try:
        conn.execute("ALTER TABLE users ADD COLUMN word_replaces INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # Уже существует
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
def get_user(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    cols = ["user_id","username","xp","level","total_score","games_played",
            "words_guessed","free_hints","skip_skips","word_replaces","titles","active_title"]
    d = dict(zip(cols, row))
    d["titles"] = json.loads(d["titles"])
    return d

def ensure_user(user_id: int, username: str) -> dict:
    u = get_user(user_id)
    if u:
        # Обновим имя
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
        conn.close()
        u["username"] = username
        return u
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO users (user_id, username) VALUES (?, ?)",
        (user_id, username)
    )
    conn.commit()
    conn.close()
    return get_user(user_id)

def add_score_and_xp(user_id: int, score: int, words: int = 0) -> dict:
    """Добавить очки, начислить XP. Возвращает dict с инфо о level_up."""
    u = get_user(user_id)
    if not u:
        return {}

    gained_xp    = xp_for_score(score)
    old_level    = u["level"]
    new_xp       = u["xp"] + gained_xp
    new_rank     = get_rank_for_xp(new_xp)
    new_level    = new_rank["level"]

    # Собираем награды за все пропущенные уровни
    rewards      = {}
    leveled_up   = new_level > old_level
    for lvl in range(old_level + 1, new_level + 1):
        r = LEVEL_UP_REWARDS.get(lvl, {})
        rewards["hints"]         = rewards.get("hints", 0)         + r.get("hints", 0)
        rewards["skip_skips"]    = rewards.get("skip_skips", 0)    + r.get("skip_skips", 0)
        rewards["word_replaces"] = rewards.get("word_replaces", 0) + r.get("word_replaces", 0)
        if "title" in r:
            rewards.setdefault("titles", []).append(r["title"])

    new_hints         = u["free_hints"]      + rewards.get("hints", 0)
    new_skip_skips    = u["skip_skips"]      + rewards.get("skip_skips", 0)
    new_word_replaces = u["word_replaces"]   + rewards.get("word_replaces", 0)
    new_titles        = u["titles"] + rewards.get("titles", [])

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE users SET
            xp=?, level=?, total_score=total_score+?,
            games_played=games_played+1,
            words_guessed=words_guessed+?,
            free_hints=?, skip_skips=?, word_replaces=?,
            titles=?
        WHERE user_id=?
    """, (new_xp, new_level, score, words, new_hints, new_skip_skips,
          new_word_replaces, json.dumps(new_titles, ensure_ascii=False), user_id))
    conn.commit()
    conn.close()

    return {
        "gained_xp": gained_xp,
        "leveled_up": leveled_up,
        "old_level": old_level,
        "new_level": new_level,
        "new_rank_name": new_rank["name"],
        "rewards": rewards,
    }

def use_free_hint(user_id: int) -> bool:
    u = get_user(user_id)
    if not u or u["free_hints"] <= 0:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET free_hints=free_hints-1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True

def add_free_hints(user_id: int, amount: int = 1) -> bool:
    if amount <= 0:
        return False
    if not get_user(user_id):
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET free_hints=free_hints+? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()
    return True

def use_skip_skip(user_id: int) -> bool:
    u = get_user(user_id)
    if not u or u["skip_skips"] <= 0:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET skip_skips=skip_skips-1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True

def add_skip_skips(user_id: int, amount: int = 1) -> bool:
    if amount <= 0:
        return False
    if not get_user(user_id):
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET skip_skips=skip_skips+? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()
    return True

def use_word_replace(user_id: int) -> bool:
    u = get_user(user_id)
    if not u or u.get("word_replaces", 0) <= 0:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET word_replaces=word_replaces-1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True

def add_word_replaces(user_id: int, amount: int = 1) -> bool:
    if amount <= 0:
        return False
    if not get_user(user_id):
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET word_replaces=word_replaces+? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()
    return True

def set_active_title(user_id: int, title: str) -> bool:
    u = get_user(user_id)
    if not u or title not in u["titles"]:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET active_title=? WHERE user_id=?", (title, user_id))
    conn.commit()
    conn.close()
    return True

def get_leaderboard(limit: int = 10) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username, xp, level, total_score
        FROM users ORDER BY xp DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "username": r[1], "xp": r[2],
             "level": r[3], "total_score": r[4]} for r in rows]

def get_users_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    n = c.fetchone()[0]
    conn.close()
    return int(n or 0)

def get_leaderboard_xp(limit: int = 10, offset: int = 0) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username, xp, level, total_score
        FROM users
        ORDER BY xp DESC, total_score DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "username": r[1], "xp": r[2],
             "level": r[3], "total_score": r[4]} for r in rows]

def get_leaderboard_score(limit: int = 10, offset: int = 0) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username, xp, level, total_score
        FROM users
        ORDER BY total_score DESC, xp DESC
        LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "username": r[1], "xp": r[2],
             "level": r[3], "total_score": r[4]} for r in rows]

def get_user_position_xp(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT xp FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return 0
    xp = row[0]
    c.execute("SELECT COUNT(*)+1 FROM users WHERE xp > ?", (xp,))
    pos = c.fetchone()[0]
    conn.close()
    return int(pos or 0)

def get_user_position_score(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_score FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return 0
    sc = row[0]
    c.execute("SELECT COUNT(*)+1 FROM users WHERE total_score > ?", (sc,))
    pos = c.fetchone()[0]
    conn.close()
    return int(pos or 0)
