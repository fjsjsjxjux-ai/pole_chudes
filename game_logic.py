"""
game_logic.py - Логика игры Поле Чудес
"""

import random
import uuid
import time
from typing import Optional

from words import WORDS_BY_CATEGORY, ALL_CATEGORIES

ALPHABET = list("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")

WHEEL_SECTORS = [
    # Очки (весом больше, чем событий)
    "50", "100", "150", "200", "250", "300", "350", "400", "450", "500", "600", "700",
    "100", "150", "200", "250", "300", "400", "500",

    # События
    "ПРИЗ", "ПРИЗ",
    "БОНУС", "БОНУС",
    "ПОДСКАЗКА",
    "ЩИТ",
    "ДЖЕКПОТ",
    "МИНУС",
    "ПРОПУСК", "ПРОПУСК",
    "БАНКРОТ", "БАНКРОТ",
]

DIFFICULTY_SETTINGS = {
    "easy":   {"min_len": 3,  "max_len": 5,  "lives": 7, "label": "🟢 Лёгкий"},
    "medium": {"min_len": 6,  "max_len": 8,  "lives": 5, "label": "🟡 Средний"},
    "hard":   {"min_len": 9,  "max_len": 99, "lives": 4, "label": "🔴 Сложный"},
}

def spin_wheel() -> str:
    return random.choice(WHEEL_SECTORS)

def format_word_display(word: str, guessed: set) -> str:
    result = []
    for char in word:
        if char == " ":
            result.append("  ")
        elif char == "-":
            result.append("-")
        elif char.upper() in guessed:
            result.append(char.upper())
        else:
            result.append("_")
    return " ".join(result)

# ---------------------------------------------------------------------------
# Одиночная игра
# ---------------------------------------------------------------------------
class SinglePlayerGame:
    TOTAL_WORDS = 5

    def __init__(self, user_id: int, difficulty: str, category: str):
        self.user_id    = user_id
        self.difficulty = difficulty
        self.category   = category
        cfg             = DIFFICULTY_SETTINGS[difficulty]
        self.min_len    = cfg["min_len"]
        self.max_len    = cfg["max_len"]
        self.max_lives  = cfg["lives"]
        self.total_words = self.TOTAL_WORDS

        self.score          = 0
        self.words_guessed  = 0
        self.word_index     = 0
        self.word_list: list[dict] = []

        self.word            = ""
        self.hint            = ""
        self.guessed_letters: set = set()
        self.lives           = self.max_lives
        self.word_guessed    = False

    def load_words(self) -> bool:
        pool = WORDS_BY_CATEGORY.get(self.category, [])
        filtered = [
            w for w in pool
            if self.min_len <= len(w["word"].replace(" ", "").replace("-", "")) <= self.max_len
        ]
        if not filtered:
            # Попробуем без фильтра по длине
            filtered = pool[:]
        if not filtered:
            return False

        # De-duplicate by word so words don't repeat within a single game
        unique: dict[str, dict] = {}
        for entry in filtered:
            key = entry.get("word", "").strip().upper()
            if key and key not in unique:
                unique[key] = entry
        filtered = list(unique.values())
        if not filtered:
            return False

        random.shuffle(filtered)
        self.word_list  = filtered[:self.total_words]
        self.total_words = len(self.word_list)
        self._load_current_word()
        return True

    def _load_current_word(self):
        entry                = self.word_list[self.word_index]
        self.word            = entry["word"].upper()
        self.hint            = entry["hint"]
        self.guessed_letters = set()
        self.lives           = self.max_lives
        self.word_guessed    = False
        self.word_index     += 1

    def guess_letter(self, letter: str) -> int:
        letter = letter.upper()
        self.guessed_letters.add(letter)
        count = self.word.count(letter)
        if count == 0:
            self.lives -= 1
        else:
            self.score += count * 10
        return count

    def use_hint(self, free: bool = False) -> Optional[str]:
        hidden = [c for c in set(self.word) if c.isalpha() and c not in self.guessed_letters]
        if not hidden:
            return None
        letter = random.choice(hidden)
        if not free:
            self.score = max(0, self.score - 50)
        self.guessed_letters.add(letter)
        return letter

    def is_word_complete(self) -> bool:
        return all(c in self.guessed_letters or c in (" ", "-") for c in self.word)

    def next_word(self) -> bool:
        if self.word_index >= len(self.word_list):
            return False
        self._load_current_word()
        return True


# ---------------------------------------------------------------------------
# Мультиплеерная комната (работает в ЛС и в группах)
# ---------------------------------------------------------------------------
class GameRoom:
    """
    Комната может быть:
    - type='private': ЛС, управление через inline-кнопки
    - type='group': группа, буквы пишут текстом, управление через inline

    group_chat_id: ID чата группы (если type='group')
    group_message_id: ID последнего статусного сообщения в группе
    """

    def __init__(
        self,
        host_id: int,
        host_name: str,
        total_rounds: int,
        max_players: int,  # 0 = неограничено
        category: str,
        difficulty: str = "medium",
        room_type: str = "private",   # 'private' | 'group'
        group_chat_id: int = 0,
    ):
        self.room_id        = self._gen_room_id()
        self.host_id        = host_id
        self.total_rounds   = total_rounds
        self.max_players    = max_players   # 0 = unlimited
        self.base_category  = category
        self.difficulty     = difficulty
        self.room_type      = room_type
        self.group_chat_id  = group_chat_id
        self.group_message_id: Optional[int] = None  # для редактирования статуса

        # Игроки
        self.player_ids:   list[int]      = []
        self.player_names: dict[int, str] = {}
        self.scores:       dict[int, int] = {}
        self.round_scores: dict[int, int] = {}

        self.active              = False
        self.current_round       = 0
        self.current_player_idx  = 0
        self.turn_counter        = 0
        self.turn_timer_token    = 0
        self.last_activity       = 0.0  # время последней активности (для таймера AFK)

        # Текущий раунд
        self.current_word     = ""
        self.current_hint     = ""
        self.current_category = category
        self.guessed_letters: set = set()
        self.used_words: set[str] = set()

        # Состояние хода
        self.spin_points:  Optional[int] = None
        self.prize_active: bool          = False
        self.jackpot_active: bool        = False
        self.current_sector: str         = ""

        # Кулдаун для ввода букв текстом (для комнат и групп)
        self.letter_cooldowns: dict[int, float] = {}  # user_id -> timestamp
        self.COOLDOWN_SEC = 0.5

        # Таймер ожидания игроков в группе (5 мин)
        self.join_deadline: float = time.time() + 300

        self.add_player(host_id, host_name)

    @staticmethod
    def _gen_room_id() -> str:
        return uuid.uuid4().hex[:6].upper()

    def add_player(self, uid: int, name: str):
        self.player_ids.append(uid)
        self.player_names[uid] = name
        self.scores[uid]       = 0
        self.round_scores[uid] = 0

    def remove_player(self, uid: int):
        if uid in self.player_ids:
            self.player_ids.remove(uid)
            self.player_names.pop(uid, None)
            self.scores.pop(uid, None)
            self.round_scores.pop(uid, None)
            if self.current_player_idx >= len(self.player_ids):
                self.current_player_idx = 0

    @property
    def current_player_id(self) -> int:
        if not self.player_ids:
            return 0
        return self.player_ids[self.current_player_idx % len(self.player_ids)]

    def check_cooldown(self, uid: int) -> float:
        """Возвращает 0 если можно, иначе секунды до конца кулдауна."""
        last = self.letter_cooldowns.get(uid, 0)
        diff = time.time() - last
        if diff < self.COOLDOWN_SEC:
            return self.COOLDOWN_SEC - diff
        return 0

    def apply_cooldown(self, uid: int):
        self.letter_cooldowns[uid] = time.time()

    def next_player(self):
        self.current_player_idx = (self.current_player_idx + 1) % max(1, len(self.player_ids))
        self.spin_points  = None
        self.prize_active = False
        self.jackpot_active = False
        self.turn_counter += 1

    def start_game(self):
        self.active        = True
        self.current_round = 1
        self._load_round()

    def _get_word_pool(self) -> list[dict]:
        cfg = DIFFICULTY_SETTINGS[self.difficulty]
        pool = WORDS_BY_CATEGORY.get(self.current_category, [])
        if not pool:
            for cat_words in WORDS_BY_CATEGORY.values():
                pool.extend(cat_words)
        filtered = [
            w for w in pool
            if cfg["min_len"] <= len(w["word"].replace(" ", "").replace("-", "")) <= cfg["max_len"]
        ]
        return filtered if filtered else pool

    def _load_round(self):
        pool = self._get_word_pool()
        available = [w for w in pool if w.get("word", "").upper() not in self.used_words] if pool else []
        if not available and pool:
            # Если уникальные слова закончились — начинаем новый цикл
            self.used_words.clear()
            available = pool

        entry = random.choice(available) if available else {"word": "СЛОВО", "hint": "Загаданное слово"}
        word_upper = entry.get("word", "СЛОВО").upper()
        self.used_words.add(word_upper)
        self.current_word    = word_upper
        self.current_hint    = entry["hint"]
        self.guessed_letters = set()
        for uid in self.player_ids:
            self.round_scores[uid] = 0
        self.spin_points  = None
        self.prize_active = False
        self.jackpot_active = False
        self.turn_counter += 1

    def guess_letter(self, letter: str) -> int:
        letter = letter.upper()
        self.guessed_letters.add(letter)
        return self.current_word.count(letter)

    def is_round_complete(self) -> bool:
        return all(c in self.guessed_letters or c in (" ", "-") for c in self.current_word)

    def has_next_round(self) -> bool:
        return self.current_round < self.total_rounds

    def next_round(self):
        self.current_round     += 1
        self.current_player_idx = (self.current_round - 1) % max(1, len(self.player_ids))
        self._load_round()

    @property
    def is_full(self) -> bool:
        if self.max_players == 0:
            return False
        return len(self.player_ids) >= self.max_players
