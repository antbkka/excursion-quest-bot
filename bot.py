"""
Telegram-бот для интерактивной экскурсии с кодовыми словами и рассылкой акций.

Стек: Python 3.11+, aiogram 3.x, SQLite, python-dotenv.

Структура файла (слои):
- Settings (dataclass) — загрузка конфигурации из .env
- Database (SQLite) — все операции с БД
- AdminStates (aiogram FSM) — состояния диалогов с админом
- Handlers (aiogram) — обработчики команд и сообщений
- main() — точка входа

Возможности админки (через /admin → инлайн-кнопки):
— список точек, добавление / редактирование / удаление,
— рассылка сообщений подписчикам, добавление акций,
— розыгрыш победителя, статистика, краткая помощь.
"""

import asyncio
import logging
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# Настройка логирования
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("excursion_bot")


# ─────────────────────────────────────────────────────────────
# Settings — загрузка конфигурации из .env
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    """Конфигурация бота, загружаемая из переменных окружения."""

    bot_token: str
    admin_chat_ids: tuple[int, ...]  # список админов (один или несколько)
    excursion_file_path: Path
    database_path: Path
    daily_broadcast_hour: int

    @classmethod
    def load(cls) -> "Settings":
        """Загружает настройки из .env. Бросает RuntimeError при критических ошибках."""
        load_dotenv()

        token = os.getenv("BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "BOT_TOKEN не найден. Укажите токен в .env (см. .env.example)."
            )

        # ───── Парсим ADMIN_CHAT_ID: один или несколько через запятую ─────
        admin_raw = os.getenv("ADMIN_CHAT_ID")
        if not admin_raw:
            raise RuntimeError(
                "ADMIN_CHAT_ID не найден. Укажите один или несколько "
                "Telegram ID через запятую, например: ADMIN_CHAT_ID=123,456"
            )

        admin_ids: list[int] = []
        for piece in admin_raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if not piece.isdigit():
                raise RuntimeError(
                    f"ADMIN_CHAT_ID содержит некорректное значение: {piece!r}. "
                    "Ожидаются целые числа через запятую."
                )
            admin_ids.append(int(piece))

        if not admin_ids:
            raise RuntimeError(
                "ADMIN_CHAT_ID задан, но не содержит ни одного валидного ID."
            )

        file_path = Path(os.getenv("EXCURSION_FILE_PATH", "./files/excursion.pdf"))
        db_path = Path(os.getenv("DATABASE_PATH", "./data/bot.db"))

        # Создаём директории при необходимости
        file_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        hour_raw = os.getenv("DAILY_BROADCAST_HOUR", "10")
        try:
            hour = int(hour_raw)
            if not 0 <= hour <= 23:
                raise ValueError
        except ValueError:
            raise RuntimeError(
                "DAILY_BROADCAST_HOUR должен быть целым числом от 0 до 23."
            )

        return cls(
            bot_token=token,
            admin_chat_ids=tuple(admin_ids),
            excursion_file_path=file_path,
            database_path=db_path,
            daily_broadcast_hour=hour,
        )


# ─────────────────────────────────────────────────────────────
# Database — слой работы с SQLite
# ─────────────────────────────────────────────────────────────
class Database:
    """
    Класс-обёртка над SQLite.
    Все запросы используют параметризацию — защита от SQL-инъекций.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # Включаем поддержку внешних ключей
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_schema(self) -> None:
        """Создаёт таблицы, если их ещё нет."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id        INTEGER PRIMARY KEY,
                    username       TEXT,
                    first_name     TEXT,
                    passed_at      TEXT,
                    is_subscribed  INTEGER NOT NULL DEFAULT 1,
                    is_blocked     INTEGER NOT NULL DEFAULT 0,
                    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS points (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL,
                    code_word   TEXT NOT NULL UNIQUE,
                    order_num   INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS user_progress (
                    user_id     INTEGER NOT NULL,
                    point_id    INTEGER NOT NULL,
                    passed_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, point_id),
                    FOREIGN KEY (user_id)  REFERENCES users(user_id)  ON DELETE CASCADE,
                    FOREIGN KEY (point_id) REFERENCES points(id)     ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS promos (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    text        TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    sent_at     TEXT
                );
                """
            )
        logger.info("Схема БД инициализирована: %s", self.path)

    # ───── users ─────
    def upsert_user(self, user_id: int, username: Optional[str],
                    first_name: Optional[str]) -> None:
        """Создаёт запись о пользователе или обновляет username/first_name."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username   = excluded.username,
                    first_name = excluded.first_name,
                    is_blocked = 0
                """,
                (user_id, username, first_name),
            )

    def mark_user_passed(self, user_id: int) -> None:
        """Помечает, что пользователь прошёл всю экскурсию (passed_at)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET passed_at = datetime('now') "
                "WHERE user_id = ? AND passed_at IS NULL",
                (user_id,),
            )

    def mark_user_blocked(self, user_id: int) -> None:
        """Помечает пользователя как заблокировавшего бота."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET is_blocked = 1 WHERE user_id = ?",
                (user_id,),
            )

    def get_subscribed_users(self) -> list[sqlite3.Row]:
        """Возвращает пользователей, подписанных на рассылку и не заблокировавших бота."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT user_id, username, first_name FROM users "
                "WHERE is_subscribed = 1 AND is_blocked = 0"
            ).fetchall()

    def get_completed_users(self) -> list[sqlite3.Row]:
        """Возвращает пользователей, прошедших экскурсию (passed_at IS NOT NULL)."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT user_id, username, first_name, passed_at FROM users "
                "WHERE passed_at IS NOT NULL ORDER BY passed_at"
            ).fetchall()

    # ───── points ─────
    def add_point(self, name: str, code_word: str, order_num: int) -> int:
        """Добавляет точку. Возвращает id новой точки."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO points (name, code_word, order_num) VALUES (?, ?, ?)",
                (name, code_word, order_num),
            )
            return cur.lastrowid

    def delete_point(self, point_id: int) -> bool:
        """Удаляет точку по id. Возвращает True, если точка существовала."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM points WHERE id = ?", (point_id,))
            return cur.rowcount > 0

    def get_point_by_code(self, code_word: str) -> Optional[sqlite3.Row]:
        """Ищет точку по кодовому слову (без учёта регистра)."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM points WHERE LOWER(code_word) = LOWER(?)",
                (code_word.strip(),),
            ).fetchone()

    def get_point_by_id(self, point_id: int) -> Optional[sqlite3.Row]:
        """Ищет точку по id."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM points WHERE id = ?", (point_id,)
            ).fetchone()

    def get_all_points(self) -> list[sqlite3.Row]:
        """Возвращает все точки, отсортированные по order_num."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT id, name, code_word, order_num FROM points "
                "ORDER BY order_num, id"
            ).fetchall()

    def count_points(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]

    def update_point_code(self, point_id: int, new_code_word: str) -> bool:
        """
        Обновляет кодовое слово точки.
        Возвращает True, если точка существовала и обновление прошло успешно.
        Бросает sqlite3.IntegrityError при дубликате кодового слова.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE points SET code_word = ? WHERE id = ?",
                (new_code_word.strip(), point_id),
            )
            return cur.rowcount > 0

    # ───── progress ─────
    def has_passed_point(self, user_id: int, point_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_progress WHERE user_id = ? AND point_id = ?",
                (user_id, point_id),
            ).fetchone()
            return row is not None

    def add_progress(self, user_id: int, point_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_progress (user_id, point_id) "
                "VALUES (?, ?)",
                (user_id, point_id),
            )

    def get_user_progress_count(self, user_id: int) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM user_progress WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]

    def has_completed_all(self, user_id: int) -> bool:
        total = self.count_points()
        if total == 0:
            return False
        passed = self.get_user_progress_count(user_id)
        return passed >= total

    def get_user_passed_points(self, user_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT p.id, p.name, p.code_word, up.passed_at
                FROM user_progress up
                JOIN points p ON p.id = up.point_id
                WHERE up.user_id = ?
                ORDER BY p.order_num, p.id
                """,
                (user_id,),
            ).fetchall()

    # ───── promos ─────
    def add_promo(self, text: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO promos (text) VALUES (?)", (text,)
            )
            return cur.lastrowid

    def get_unsent_promos(self) -> list[sqlite3.Row]:
        """Возвращает акции, которые ещё не были разосланы автоматически."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT id, text FROM promos WHERE sent_at IS NULL ORDER BY id"
            ).fetchall()

    def mark_promos_sent(self, promo_ids: list[int]) -> None:
        if not promo_ids:
            return
        placeholders = ",".join("?" * len(promo_ids))
        with self._connect() as conn:
            conn.execute(
                f"UPDATE promos SET sent_at = datetime('now') WHERE id IN ({placeholders})",
                promo_ids,
            )

    # ───── stats ─────
    def get_stats(self) -> dict:
        """Возвращает статистику для /stats."""
        with self._connect() as conn:
            total_users = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM users WHERE passed_at IS NOT NULL"
            ).fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_blocked = 1"
            ).fetchone()[0]

            # Сколько пользователей прошло каждую точку
            per_point = conn.execute(
                """
                SELECT p.id, p.name, COUNT(up.user_id) AS cnt
                FROM points p
                LEFT JOIN user_progress up ON up.point_id = p.id
                GROUP BY p.id, p.name
                ORDER BY p.order_num, p.id
                """
            ).fetchall()

        return {
            "total_users": total_users,
            "completed": completed,
            "blocked": blocked,
            "per_point": per_point,
        }


# ─────────────────────────────────────────────────────────────
# FSM — состояния админ-диалогов
# ─────────────────────────────────────────────────────────────
class AdminStates(StatesGroup):
    """Состояния машины состояний для сценариев админки."""

    # Добавление точки
    add_point_name = State()
    add_point_code = State()
    add_point_order = State()

    # Редактирование точки
    edit_point_select = State()
    edit_point_new_code = State()

    # Удаление точки
    del_point_select = State()
    del_point_confirm = State()

    # Рассылка
    broadcast_text = State()

    # Акция (промо)
    promo_text = State()

    # Розыгрыш
    draw_count = State()
    draw_prizes = State()


class QuestStates(StatesGroup):
    """Состояния машины состояний гостевого квеста."""

    # Гость нажал «📍 Локация N пройдена» — ждём от него кодовое слово
    waiting_code = State()


# ─────────────────────────────────────────────────────────────
# Handlers — слой обработчиков aiogram
# ─────────────────────────────────────────────────────────────
def is_admin(user_id: int, admin_ids: tuple[int, ...]) -> bool:
    """Проверка, является ли пользователь одним из администраторов."""
    return user_id in admin_ids


def build_bot(settings: Settings, db: Database) -> tuple[Bot, Dispatcher]:
    """Создаёт и настраивает Bot и Dispatcher с зарегистрированными хэндлерами."""
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()
    dp.include_router(router)

    # ───────── Вспомогательные клавиатуры ─────────

    def admin_menu_keyboard() -> InlineKeyboardMarkup:
        """Главное меню админки (8 кнопок в 4 ряда)."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📍 Список точек", callback_data="admin:list"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="➕ Добавить точку", callback_data="admin:add"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Редактировать точку",
                        callback_data="admin:edit",
                    ),
                    InlineKeyboardButton(
                        text="🗑 Удалить точку", callback_data="admin:del"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📢 Рассылка", callback_data="admin:broadcast"
                    ),
                    InlineKeyboardButton(
                        text="🎁 Добавить акцию", callback_data="admin:promo"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="🎲 Розыгрыш", callback_data="admin:draw"
                    ),
                    InlineKeyboardButton(
                        text="📊 Статистика", callback_data="admin:stats"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="❓ Помощь", callback_data="admin:help"
                    ),
                ],
            ]
        )

    def points_picker_keyboard(
        callback_prefix: str, back_to: str = "admin:menu"
    ) -> InlineKeyboardMarkup:
        """Клавиатура со списком точек для выбора. callback_prefix, например 'admin:edit:pick'."""
        points = db.get_all_points()
        rows: list[list[InlineKeyboardButton]] = []
        for p in points:
            label = f"#{p['id']} «{html.quote(p['name'])}»"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"{callback_prefix}:{p['id']}",
                    )
                ]
            )
        rows.append(
            [InlineKeyboardButton(text="↩️ В админ-меню", callback_data=back_to)]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def confirm_keyboard(
        yes_cb: str, no_cb: str = "admin:menu"
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Да, удалить", callback_data=yes_cb
                    ),
                    InlineKeyboardButton(
                        text="↩️ Отмена", callback_data=no_cb
                    ),
                ]
            ]
        )

    # ───────── Хелперы квеста ─────────

    def _next_unpassed_point(user_id: int) -> Optional[sqlite3.Row]:
        """Возвращает первую непройденную пользователем точку (по order_num)."""
        passed_ids: set[int] = {
            row["id"] for row in db.get_user_passed_points(user_id)
        }
        for p in db.get_all_points():
            if p["id"] not in passed_ids:
                return p
        return None

    def _all_points_keyboard(user_id: int) -> InlineKeyboardMarkup:
        """
        Возвращает «табло» — по одной кнопке на каждую НЕпройденную локацию.
        Пройденные локации не отображаются.
        callback_data: "quest:pick:<point_id>".
        """
        passed_ids: set[int] = {
            row["id"] for row in db.get_user_passed_points(user_id)
        }
        rows: list[list[InlineKeyboardButton]] = []
        for p in db.get_all_points():
            if p["id"] in passed_ids:
                continue
            label = f"📍 {html.quote(p['name'])}"
            rows.append(
                [InlineKeyboardButton(
                    text=label,
                    callback_data=f"quest:pick:{p['id']}",
                )]
            )
        if not rows:
            # На всякий случай — если все точки вдруг пройдены.
            rows.append(
                [InlineKeyboardButton(
                    text="🏆 КВЕСТ ПРОЙДЕН",
                    callback_data="quest:finished",
                )]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _quest_finished_keyboard() -> InlineKeyboardMarkup:
        """Финальная клавиатура: «🏆 КВЕСТ ПРОЙДЕН»."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🏆 КВЕСТ ПРОЙДЕН",
                    callback_data="quest:finished",
                )]
            ]
        )

    def _quest_board_text(user_id: int) -> str:
        """
        Текст для табло: прогресс X/Y и список оставшихся локаций.
        """
        passed = db.get_user_progress_count(user_id)
        total = db.count_points()
        if total == 0:
            return "ℹ️ Квест ещё не настроен: администратор не добавил точки маршрута."

        passed_ids: set[int] = {
            row["id"] for row in db.get_user_passed_points(user_id)
        }
        remaining = [p for p in db.get_all_points() if p["id"] not in passed_ids]

        lines = [
            f"🗺 <b>Табло локаций</b>",
            f"📊 Прогресс: <b>{passed}/{total}</b>",
            "",
        ]
        if not remaining:
            lines.append("Все локации пройдены! Нажмите кнопку ниже. 👇")
        else:
            lines.append("Выберите локацию, у которой нашли кодовое слово:")
            lines.append("")
            for p in remaining:
                lines.append(
                    f"• 📍 <b>{html.quote(p['name'])}</b> "
                    f"<i>(порядок {p['order_num']})</i>"
                )
        return "\n".join(lines)

    # Алиас для совместимости со старыми хендлерами.
    def _quest_progress_keyboard(user_id: int) -> InlineKeyboardMarkup:
        return _all_points_keyboard(user_id)

    # ───────── /start ─────────
    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not message.from_user:
            return
        user = message.from_user
        db.upsert_user(user.id, user.username, user.first_name)
        logger.info(
            "Пользователь %s (%s) запустил бота",
            user.id,
            html.quote(user.full_name),
        )

        text = (
            "👋 <b>Добро пожаловать на интерактивную экскурсию!</b>\n\n"
            "Ниже — файл с описанием маршрута. "
            "На каждой точке спрятано кодовое слово.\n\n"
            "Когда найдёте кодовое слово — нажмите кнопку ниже "
            "(«📍 Локация N пройдена»). Бот попросит ввести его.\n\n"
            "Когда все точки пройдены — появится кнопка «🏆 КВЕСТ ПРОЙДЕН» 🎁"
        )

        if settings.excursion_file_path.exists():
            try:
                doc = FSInputFile(settings.excursion_file_path)
                await message.answer_document(
                    document=doc,
                    caption=text,
                )
            except Exception as exc:
                logger.exception("Не удалось отправить файл экскурсии: %s", exc)
                await message.answer(
                    text + "\n\n⚠️ Не удалось прикрепить файл экскурсии."
                )
        else:
            logger.warning(
                "Файл экскурсии не найден по пути %s",
                settings.excursion_file_path,
            )
            await message.answer(
                text
                + "\n\n⚠️ Файл экскурсии временно недоступен, "
                  "обратитесь к администратору."
            )

        # Предлагаем начать/продолжить квест.
        total = db.count_points()
        if total == 0:
            await message.answer(
                "ℹ️ Квест ещё не настроен: администратор не добавил точки маршрута."
            )
        else:
            await message.answer(
                "🗺 Нажмите кнопку ниже, чтобы открыть табло локаций:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="▶️ Начать квест",
                            callback_data="quest:start",
                        )]
                    ]
                ),
            )

        # Если это админ — сразу покажем подсказку про /admin
        if is_admin(user.id, settings.admin_chat_ids):
            await message.answer(
                "🛠 Вы администратор. Введите <code>/admin</code> для управления ботом."
            )

    # ───────── /admin — главное меню админки ─────────
    @router.message(Command("admin"))
    async def cmd_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return
        await message.answer(
            "🛠 <b>Панель администратора</b>\n\n"
            "Выберите действие:",
            reply_markup=admin_menu_keyboard(),
        )

    # ───────── Callback: открыть главное меню ─────────
    @router.callback_query(F.data == "admin:menu")
    async def cb_admin_menu(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.clear()
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return
        if callback.message:
            try:
                await callback.message.edit_text(
                    "🛠 <b>Панель администратора</b>\n\nВыберите действие:",
                    reply_markup=admin_menu_keyboard(),
                )
            except Exception:
                await callback.message.answer(
                    "🛠 <b>Панель администратора</b>\n\nВыберите действие:",
                    reply_markup=admin_menu_keyboard(),
                )
        await callback.answer()

    # ───────── Список точек ─────────
    @router.callback_query(F.data == "admin:list")
    async def cb_admin_list(callback: CallbackQuery) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        points = db.get_all_points()
        if not points:
            text = "Точек пока нет."
        else:
            lines = ["<b>📍 Список точек:</b>\n"]
            for p in points:
                lines.append(
                    f"#{p['id']} (порядок {p['order_num']}) — "
                    f"«{html.quote(p['name'])}» — "
                    f"<code>{html.quote(p['code_word'])}</code>"
                )
            text = "\n".join(lines)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В админ-меню", callback_data="admin:menu")],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    # ───────── Добавление точки: вход в сценарий ─────────
    @router.callback_query(F.data == "admin:add")
    async def cb_admin_add_start(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        await state.set_state(AdminStates.add_point_name)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin:menu")],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(
                    "➕ <b>Добавление точки</b>\n\n"
                    "Шаг 1 из 3.\n"
                    "Введите <b>название</b> точки (как будет выглядеть в списке):",
                    reply_markup=kb,
                )
            except Exception:
                await callback.message.answer(
                    "➕ <b>Добавление точки</b>\n\n"
                    "Шаг 1 из 3.\n"
                    "Введите <b>название</b> точки:",
                    reply_markup=kb,
                )
        await callback.answer()

    @router.message(StateFilter(AdminStates.add_point_name))
    async def st_add_name(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer("Название не может быть пустым. Попробуйте ещё раз:")
            return
        name = message.text.strip()
        if not name:
            await message.answer("Название не может быть пустым. Попробуйте ещё раз:")
            return
        if len(name) > 200:
            await message.answer("Слишком длинное название (макс. 200 символов). Попробуйте короче:")
            return

        await state.update_data(add_name=name)
        await state.set_state(AdminStates.add_point_code)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin:menu")],
            ]
        )
        await message.answer(
            f"Название: <b>«{html.quote(name)}»</b>\n\n"
            "Шаг 2 из 3.\n"
            "Введите <b>кодовое слово</b> (то, что пользователи будут отправлять боту):",
            reply_markup=kb,
        )

    @router.message(StateFilter(AdminStates.add_point_code))
    async def st_add_code(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer("Кодовое слово не может быть пустым. Попробуйте ещё раз:")
            return
        code = message.text.strip()
        if not code:
            await message.answer("Кодовое слово не может быть пустым. Попробуйте ещё раз:")
            return
        if len(code) > 100:
            await message.answer("Слишком длинное кодовое слово (макс. 100 символов). Попробуйте короче:")
            return

        # Уже есть точка с таким словом?
        if db.get_point_by_code(code) is not None:
            await message.answer(
                "❌ Точка с таким кодовым словом уже существует. "
                "Введите другое кодовое слово:"
            )
            return

        await state.update_data(add_code=code)
        await state.set_state(AdminStates.add_point_order)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏭ Пропустить", callback_data="admin:add:skip_order"
                    )
                ],
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin:menu")],
            ]
        )
        await message.answer(
            f"Кодовое слово: <code>{html.quote(code)}</code>\n\n"
            "Шаг 3 из 3.\n"
            "Введите <b>порядковый номер</b> точки в маршруте (целое число). "
            "Если нажмёте «⏭ Пропустить» — номер будет присвоен автоматически "
            "(в конец списка).",
            reply_markup=kb,
        )

    @router.callback_query(
        F.data == "admin:add:skip_order",
        StateFilter(AdminStates.add_point_order),
    )
    async def cb_add_skip_order(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        data = await state.get_data()
        name = data.get("add_name", "")
        code = data.get("add_code", "")
        if not name or not code:
            await state.clear()
            await callback.answer("Данные устарели. Начните заново.", show_alert=True)
            return
        order_num = db.count_points() + 1
        await _finish_add_point(callback.message, state, name, code, order_num)
        await callback.answer()

    @router.message(StateFilter(AdminStates.add_point_order))
    async def st_add_order(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer(
                "Введите целое число или нажмите «⏭ Пропустить»:"
            )
            return
        raw = message.text.strip()
        try:
            order_num = int(raw)
        except ValueError:
            await message.answer("Это не целое число. Попробуйте ещё раз:")
            return

        data = await state.get_data()
        name = data.get("add_name", "")
        code = data.get("add_code", "")
        await _finish_add_point(message, state, name, code, order_num)

    async def _finish_add_point(
        target: Optional[Message], state: FSMContext,
        name: str, code: str, order_num: int,
    ) -> None:
        try:
            new_id = db.add_point(name, code, order_num)
        except sqlite3.IntegrityError:
            await state.clear()
            if target:
                await target.answer(
                    "❌ Точка с таким кодовым словом уже существует. "
                    "Попробуйте ещё раз через /admin.",
                    reply_markup=admin_menu_keyboard(),
                )
            return
        except Exception as exc:
            logger.exception("Ошибка при добавлении точки: %s", exc)
            await state.clear()
            if target:
                await target.answer(
                    "⚠️ Произошла ошибка при сохранении. "
                    "Попробуйте ещё раз через /admin.",
                    reply_markup=admin_menu_keyboard(),
                )
            return

        await state.clear()
        logger.info(
            "Админ добавил точку #%s «%s» (%s)",
            new_id, html.quote(name), html.quote(code),
        )
        if target:
            await target.answer(
                f"✅ Точка добавлена (#{new_id}): «{html.quote(name)}» — "
                f"код: <code>{html.quote(code)}</code> — порядок: {order_num}",
                reply_markup=admin_menu_keyboard(),
            )

    # ───────── Редактирование точки ─────────
    @router.callback_query(F.data == "admin:edit")
    async def cb_admin_edit_start(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        points = db.get_all_points()
        if not points:
            if callback.message:
                await callback.message.edit_text(
                    "Точек пока нет. Сначала добавьте точку.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="↩️ В админ-меню",
                                callback_data="admin:menu",
                            )],
                        ]
                    ),
                )
            await callback.answer()
            return

        await state.set_state(AdminStates.edit_point_select)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "✏️ <b>Редактирование точки</b>\n\n"
                    "Выберите точку, у которой хотите изменить кодовое слово:",
                    reply_markup=points_picker_keyboard("admin:edit:pick"),
                )
            except Exception:
                await callback.message.answer(
                    "✏️ <b>Редактирование точки</b>\n\n"
                    "Выберите точку, у которой хотите изменить кодовое слово:",
                    reply_markup=points_picker_keyboard("admin:edit:pick"),
                )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("admin:edit:pick:"),
        StateFilter(AdminStates.edit_point_select),
    )
    async def cb_admin_edit_pick(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        raw_id = callback.data.split(":")[-1]  # type: ignore[union-attr]
        try:
            point_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный ID.", show_alert=True)
            return

        point = db.get_point_by_id(point_id)
        if point is None:
            await state.clear()
            if callback.message:
                await callback.message.edit_text(
                    "❌ Точка не найдена.",
                    reply_markup=admin_menu_keyboard(),
                )
            await callback.answer()
            return

        await state.update_data(edit_id=point_id)
        await state.set_state(AdminStates.edit_point_new_code)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin:menu")],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(
                    f"Текущая точка: #{point['id']} «{html.quote(point['name'])}»\n"
                    f"Текущее кодовое слово: <code>{html.quote(point['code_word'])}</code>\n\n"
                    "Введите <b>новое кодовое слово</b>:",
                    reply_markup=kb,
                )
            except Exception:
                await callback.message.answer(
                    f"Текущая точка: #{point['id']} «{html.quote(point['name'])}»\n"
                    f"Текущее кодовое слово: <code>{html.quote(point['code_word'])}</code>\n\n"
                    "Введите <b>новое кодовое слово</b>:",
                    reply_markup=kb,
                )
        await callback.answer()

    @router.message(StateFilter(AdminStates.edit_point_new_code))
    async def st_edit_new_code(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer("Кодовое слово не может быть пустым. Попробуйте ещё раз:")
            return
        new_code = message.text.strip()
        if not new_code:
            await message.answer("Кодовое слово не может быть пустым. Попробуйте ещё раз:")
            return
        if len(new_code) > 100:
            await message.answer("Слишком длинное кодовое слово. Попробуйте короче:")
            return

        data = await state.get_data()
        point_id = data.get("edit_id")
        if point_id is None:
            await state.clear()
            await message.answer(
                "Данные устарели. Начните заново через /admin.",
                reply_markup=admin_menu_keyboard(),
            )
            return

        # Проверим, не занято ли слово другой точкой
        existing = db.get_point_by_code(new_code)
        if existing is not None and existing["id"] != point_id:
            await message.answer(
                "❌ Это кодовое слово уже используется другой точкой. "
                "Введите другое:"
            )
            return

        try:
            ok = db.update_point_code(point_id, new_code)
        except sqlite3.IntegrityError:
            await message.answer(
                "❌ Это кодовое слово уже используется другой точкой. "
                "Введите другое:"
            )
            return
        except Exception as exc:
            logger.exception("Ошибка при обновлении точки: %s", exc)
            await state.clear()
            await message.answer(
                "⚠️ Не удалось сохранить. Попробуйте ещё раз через /admin.",
                reply_markup=admin_menu_keyboard(),
            )
            return

        await state.clear()
        if ok:
            point = db.get_point_by_id(point_id)
            name = point["name"] if point else "—"
            logger.info(
                "Админ обновил код точки #%s на %s",
                point_id, html.quote(new_code),
            )
            await message.answer(
                f"✅ Точка #{point_id} «{html.quote(name)}»: "
                f"новое кодовое слово <code>{html.quote(new_code)}</code>",
                reply_markup=admin_menu_keyboard(),
            )
        else:
            await message.answer(
                "❌ Точка не найдена (возможно, была удалена).",
                reply_markup=admin_menu_keyboard(),
            )

    # ───────── Удаление точки ─────────
    @router.callback_query(F.data == "admin:del")
    async def cb_admin_del_start(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        points = db.get_all_points()
        if not points:
            if callback.message:
                await callback.message.edit_text(
                    "Удалять нечего — точек пока нет.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="↩️ В админ-меню",
                                callback_data="admin:menu",
                            )],
                        ]
                    ),
                )
            await callback.answer()
            return

        await state.set_state(AdminStates.del_point_select)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "🗑 <b>Удаление точки</b>\n\n"
                    "Выберите точку, которую хотите удалить:",
                    reply_markup=points_picker_keyboard("admin:del:pick"),
                )
            except Exception:
                await callback.message.answer(
                    "🗑 <b>Удаление точки</b>\n\n"
                    "Выберите точку, которую хотите удалить:",
                    reply_markup=points_picker_keyboard("admin:del:pick"),
                )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("admin:del:pick:"),
        StateFilter(AdminStates.del_point_select),
    )
    async def cb_admin_del_pick(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        raw_id = callback.data.split(":")[-1]  # type: ignore[union-attr]
        try:
            point_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный ID.", show_alert=True)
            return

        point = db.get_point_by_id(point_id)
        if point is None:
            await state.clear()
            if callback.message:
                await callback.message.edit_text(
                    "❌ Точка не найдена.",
                    reply_markup=admin_menu_keyboard(),
                )
            await callback.answer()
            return

        await state.update_data(del_id=point_id)
        await state.set_state(AdminStates.del_point_confirm)
        kb = confirm_keyboard(f"admin:del:confirm:{point_id}")
        if callback.message:
            try:
                await callback.message.edit_text(
                    f"Точно удалить?\n\n"
                    f"#{point['id']} «{html.quote(point['name'])}»\n"
                    f"код: <code>{html.quote(point['code_word'])}</code>",
                    reply_markup=kb,
                )
            except Exception:
                await callback.message.answer(
                    f"Точно удалить?\n\n"
                    f"#{point['id']} «{html.quote(point['name'])}»\n"
                    f"код: <code>{html.quote(point['code_word'])}</code>",
                    reply_markup=kb,
                )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("admin:del:confirm:"),
        StateFilter(AdminStates.del_point_confirm),
    )
    async def cb_admin_del_confirm(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        raw_id = callback.data.split(":")[-1]  # type: ignore[union-attr]
        try:
            point_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный ID.", show_alert=True)
            return

        if db.delete_point(point_id):
            logger.info("Админ удалил точку #%s", point_id)
            if callback.message:
                await callback.message.edit_text(
                    f"🗑 Точка #{point_id} удалена.",
                    reply_markup=admin_menu_keyboard(),
                )
        else:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Точка #{point_id} не найдена (возможно, уже удалена).",
                    reply_markup=admin_menu_keyboard(),
                )
        await state.clear()
        await callback.answer()

    # ───────── Рассылка ─────────
    @router.callback_query(F.data == "admin:broadcast")
    async def cb_admin_broadcast_start(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        await state.set_state(AdminStates.broadcast_text)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin:menu")],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(
                    "📢 <b>Рассылка подписчикам</b>\n\n"
                    "Введите текст, который нужно разослать всем подписчикам. "
                    "Поддерживается HTML-разметка.",
                    reply_markup=kb,
                )
            except Exception:
                await callback.message.answer(
                    "📢 <b>Рассылка подписчикам</b>\n\n"
                    "Введите текст для рассылки (поддерживается HTML):",
                    reply_markup=kb,
                )
        await callback.answer()

    @router.message(StateFilter(AdminStates.broadcast_text))
    async def st_broadcast_text(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer("Текст не может быть пустым. Введите текст рассылки:")
            return
        text = message.text.strip()
        if not text:
            await message.answer("Текст не может быть пустым. Введите текст рассылки:")
            return

        users = db.get_subscribed_users()
        await state.clear()
        if not users:
            await message.answer(
                "Нет подписчиков для рассылки.",
                reply_markup=admin_menu_keyboard(),
            )
            return

        await message.answer(
            f"⏳ Начинаю рассылку {len(users)} подписчикам…",
            reply_markup=admin_menu_keyboard(),
        )

        sent, blocked = await _do_broadcast(bot, db, text, users)
        await message.answer(
            f"📤 Рассылка завершена.\n"
            f"Отправлено: <b>{sent}</b>\n"
            f"Заблокировали бота: <b>{blocked}</b>",
            reply_markup=admin_menu_keyboard(),
        )

    # ───────── Добавление акции (промо) ─────────
    @router.callback_query(F.data == "admin:promo")
    async def cb_admin_promo_start(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        await state.set_state(AdminStates.promo_text)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin:menu")],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(
                    "🎁 <b>Добавление акции</b>\n\n"
                    "Введите текст акции. "
                    "Она будет автоматически разослана подписчикам "
                    "в <b>%02d:00</b> (ежедневная авторассылка)."
                    % settings.daily_broadcast_hour,
                    reply_markup=kb,
                )
            except Exception:
                await callback.message.answer(
                    "🎁 <b>Добавление акции</b>\n\n"
                    "Введите текст акции:",
                    reply_markup=kb,
                )
        await callback.answer()

    @router.message(StateFilter(AdminStates.promo_text))
    async def st_promo_text(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer("Текст акции не может быть пустым. Попробуйте ещё раз:")
            return
        text = message.text.strip()
        if not text:
            await message.answer("Текст акции не может быть пустым. Попробуйте ещё раз:")
            return

        promo_id = db.add_promo(text)
        await state.clear()
        logger.info("Админ добавил акцию #%s", promo_id)
        await message.answer(
            f"✅ Акция #{promo_id} добавлена и будет разослана "
            f"в ближайшую авторассылку ({settings.daily_broadcast_hour:02d}:00).",
            reply_markup=admin_menu_keyboard(),
        )

    # ───────── Розыгрыш: пошаговый сценарий ─────────
    @router.callback_query(F.data == "admin:draw")
    async def cb_admin_draw(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        completed = db.get_completed_users()
        if not completed:
            if callback.message:
                await callback.message.edit_text(
                    "❌ Розыгрыш невозможен: "
                    "пока нет никого, кто прошёл всю экскурсию.",
                    reply_markup=admin_menu_keyboard(),
                )
            await state.clear()
            await callback.answer()
            return

        await state.clear()
        await state.set_state(AdminStates.draw_count)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="↩️ Отмена", callback_data="admin:draw:cancel"
                )],
            ]
        )
        text = (
            "🎲 <b>Розыгрыш</b>\n\n"
            f"Всего прошли экскурсию: <b>{len(completed)}</b> чел.\n\n"
            "Введите <b>количество победителей</b> (целое число от 1 до 10):"
        )
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    @router.callback_query(
        F.data == "admin:draw:cancel",
        StateFilter(
            AdminStates.draw_count,
            AdminStates.draw_prizes,
        ),
    )
    async def cb_draw_cancel(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_text(
                    "❌ Розыгрыш отменён.",
                    reply_markup=admin_menu_keyboard(),
                )
            except Exception:
                await callback.message.answer(
                    "❌ Розыгрыш отменён.",
                    reply_markup=admin_menu_keyboard(),
                )
        await callback.answer()

    @router.message(StateFilter(AdminStates.draw_count))
    async def st_draw_count(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer(
                "Введите целое число от 1 до 10:"
            )
            return
        raw = message.text.strip()
        try:
            count = int(raw)
        except ValueError:
            await message.answer(
                "Это не целое число. Попробуйте ещё раз (от 1 до 10):"
            )
            return
        if not 1 <= count <= 10:
            await message.answer(
                "Число должно быть от 1 до 10. Попробуйте ещё раз:"
            )
            return

        completed = db.get_completed_users()
        if not completed:
            await state.clear()
            await message.answer(
                "❌ Розыгрыш невозможен: пока нет участников, "
                "прошедших экскурсию.",
                reply_markup=admin_menu_keyboard(),
            )
            return

        # Если участников меньше, чем запрошено — выбираем всех.
        actual = min(count, len(completed))
        winners = random.sample(completed, actual)

        # Сохраняем победителей в FSM-стейт для следующего шага.
        winners_data = [
            {
                "user_id": w["user_id"],
                "username": w["username"],
                "first_name": w["first_name"] or "—",
            }
            for w in winners
        ]
        await state.update_data(
            draw_winners=winners_data,
            draw_requested=count,
        )
        await state.set_state(AdminStates.draw_prizes)

        # Подготовим список для предпросмотра.
        winner_lines = []
        for i, w in enumerate(winners_data, start=1):
            if w["username"]:
                ref = f"@{html.quote(w['username'])}"
            else:
                ref = html.quote(w["first_name"])
            winner_lines.append(f"{i}. {ref}")

        warning = ""
        if actual < count:
            warning = (
                f"\n\n⚠️ Вы просили <b>{count}</b>, но участников всего "
                f"<b>{len(completed)}</b>. Будет выбрано <b>{actual}</b>."
            )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="↩️ Отмена", callback_data="admin:draw:cancel"
                )],
            ]
        )
        await message.answer(
            f"🎲 <b>Случайные победители:</b>\n\n"
            + "\n".join(winner_lines)
            + warning
            + "\n\n🎁 Теперь введите <b>призы</b> — по одному в строке, "
            f"всего <b>{actual}</b> шт., в том же порядке.\n\n"
            "Пример:\n<code>Худи \"CHOKUDA\"\nФутболка \"CHOKUDA\"\nКепка \"CHOKUDA\"</code>",
            reply_markup=kb,
        )

    @router.message(StateFilter(AdminStates.draw_prizes))
    async def st_draw_prizes(message: Message, state: FSMContext) -> None:
        if not message.text:
            await message.answer(
                "Отправьте призы текстом, по одному в строке:"
            )
            return

        data = await state.get_data()
        winners: list[dict] = data.get("draw_winners") or []
        requested: int = int(data.get("draw_requested") or len(winners))
        if not winners:
            await state.clear()
            await message.answer(
                "Данные устарели. Начните розыгрыш заново через /admin.",
                reply_markup=admin_menu_keyboard(),
            )
            return

        # Разбиваем ввод на строки, убираем пустые.
        raw_lines = [ln for ln in message.text.splitlines() if ln.strip()]
        actual = len(winners)

        if len(raw_lines) < actual:
            await message.answer(
                f"Нужно ввести <b>{actual}</b> призов (по одному в строке), "
                f"а вы прислали <b>{len(raw_lines)}</b>.\n"
                f"Попробуйте ещё раз:"
            )
            return

        prizes = [ln.strip() for ln in raw_lines[:actual]]
        # Если админ прислал лишние строки — обрежем, остальные проигнорируем.
        if len(raw_lines) > actual:
            logger.info(
                "Админ прислал %d строк-призов, нужно %d — лишние проигнорированы",
                len(raw_lines), actual,
            )

        # Формируем итоговый пост.
        out_lines = ["🏆 <b>Победители розыгрыша:</b>", ""]
        for prize, w in zip(prizes, winners):
            if w["username"]:
                ref = f"@{html.quote(w['username'])}"
            else:
                ref = html.quote(w["first_name"])
            out_lines.append(f"{html.quote(prize)} — {ref}")

        final_text = "\n".join(out_lines)

        # Логируем розыгрыш.
        logger.info(
            "Розыгрыш: %d победителей (запрошено %d), участников всего %d",
            actual, requested, len(winners),
        )
        for w in winners:
            logger.info(
                "  - победитель user_id=%s username=%s",
                w["user_id"], w["username"],
            )

        await state.clear()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🎲 Новый розыгрыш", callback_data="admin:draw"
                )],
                [InlineKeyboardButton(
                    text="↩️ В админ-меню", callback_data="admin:menu"
                )],
            ]
        )
        await message.answer(
            "✅ <b>Готово!</b> Ниже — итоговый пост, "
            "его можно скопировать и отправить в канал:\n\n"
            + final_text,
            reply_markup=kb,
        )

    # ───────── Статистика (через callback) ─────────
    @router.callback_query(F.data == "admin:stats")
    async def cb_admin_stats(callback: CallbackQuery) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        s = db.get_stats()
        lines = [
            "📊 <b>Статистика</b>\n",
            f"👤 Всего пользователей: <b>{s['total_users']}</b>",
            f"✅ Прошли экскурсию: <b>{s['completed']}</b>",
            f"🚫 Заблокировали бота: <b>{s['blocked']}</b>",
            "",
            "<b>Прохождение по точкам:</b>",
        ]
        if not s["per_point"]:
            lines.append("— точек пока нет —")
        else:
            for p in s["per_point"]:
                lines.append(
                    f"• #{p['id']} «{html.quote(p['name'])}»: "
                    f"<b>{p['cnt']}</b> чел."
                )
        text = "\n".join(lines)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="↩️ В админ-меню", callback_data="admin:menu"
                )],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    # ───────── Помощь (через callback) ─────────
    @router.callback_query(F.data == "admin:help")
    async def cb_admin_help(callback: CallbackQuery) -> None:
        if not callback.from_user or not is_admin(
            callback.from_user.id, settings.admin_chat_ids
        ):
            await callback.answer("⛔ Нет доступа.", show_alert=True)
            return

        text = (
            "❓ <b>Помощь по админке</b>\n\n"
            "• <b>📍 Список точек</b> — посмотреть все точки маршрута.\n"
            "• <b>➕ Добавить точку</b> — три шага: название → слово → номер.\n"
            "• <b>✏️ Редактировать</b> — выбрать точку и сменить кодовое слово.\n"
            "• <b>🗑 Удалить</b> — выбрать точку и подтвердить удаление.\n"
            "• <b>📢 Рассылка</b> — ввести текст, бот разошлёт всем подписчикам.\n"
            "• <b>🎁 Акция</b> — добавить текст акции в очередь "
            "(разошлётся автоматически).\n"
            "• <b>🎲 Розыгрыш</b> — случайный победитель среди прошедших.\n"
            "• <b>📊 Статистика</b> — пользователи и прохождения.\n\n"
            "Также доступны текстовые команды:\n"
            "<code>/add_point Название | слово | номер</code>\n"
            "<code>/del_point ID</code>\n"
            "<code>/list_points</code>\n"
            "<code>/add_promo Текст</code>\n"
            "<code>/broadcast Текст</code>\n"
            "<code>/draw</code>\n"
            "<code>/stats</code>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="↩️ В админ-меню", callback_data="admin:menu"
                )],
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    # ───────── Команды-альтернативы (для опытных) ─────────
    @router.message(Command("add_point"))
    async def cmd_add_point(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        args = message.text or ""
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or "|" not in parts[1]:
            await message.answer(
                "Формат:\n"
                "<code>/add_point Название | кодовое_слово | номер</code>\n\n"
                "Пример:\n"
                "<code>/add_point Главный зал | secret42 | 1</code>\n\n"
                "💡 Совет: удобнее добавлять точки через /admin → ➕ Добавить точку."
            )
            return

        payload = [p.strip() for p in parts[1].split("|")]
        if len(payload) < 2:
            await message.answer(
                "Не хватает параметров. Формат: "
                "<code>/add_point Название | кодовое_слово | номер</code>"
            )
            return

        name = payload[0]
        code_word = payload[1]
        try:
            order_num = int(payload[2]) if len(payload) >= 3 else db.count_points() + 1
        except ValueError:
            await message.answer("Порядковый номер должен быть целым числом.")
            return

        if not name or not code_word:
            await message.answer("Название и кодовое слово не могут быть пустыми.")
            return

        try:
            new_id = db.add_point(name, code_word, order_num)
        except sqlite3.IntegrityError:
            await message.answer("❌ Точка с таким кодовым словом уже существует.")
            return

        await message.answer(
            f"✅ Точка добавлена (#{new_id}): «{html.quote(name)}» — "
            f"код: <code>{html.quote(code_word)}</code>"
        )

    @router.message(Command("del_point"))
    async def cmd_del_point(message: Message) -> None:
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        args = (message.text or "").split()
        if len(args) < 2 or not args[1].isdigit():
            await message.answer(
                "Формат: <code>/del_point ID</code>\n\n"
                "💡 Удобнее удалять точки через /admin → 🗑 Удалить точку."
            )
            return

        point_id = int(args[1])
        if db.delete_point(point_id):
            await message.answer(f"🗑 Точка #{point_id} удалена.")
        else:
            await message.answer(f"❌ Точка #{point_id} не найдена.")

    @router.message(Command("list_points"))
    async def cmd_list_points(message: Message) -> None:
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        points = db.get_all_points()
        if not points:
            await message.answer("Точек пока нет.")
            return

        lines = ["<b>📍 Список точек:</b>\n"]
        for p in points:
            lines.append(
                f"#{p['id']} (порядок {p['order_num']}) — "
                f"«{html.quote(p['name'])}» — "
                f"<code>{html.quote(p['code_word'])}</code>"
            )
        await message.answer("\n".join(lines))

    @router.message(Command("add_promo"))
    async def cmd_add_promo(message: Message) -> None:
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer(
                "Формат: <code>/add_promo Текст акции</code>\n\n"
                "💡 Удобнее через /admin → 🎁 Добавить акцию."
            )
            return

        promo_id = db.add_promo(parts[1].strip())
        await message.answer(f"✅ Акция #{promo_id} добавлена и будет разослана.")

    @router.message(Command("broadcast"))
    async def cmd_broadcast(message: Message) -> None:
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer(
                "Формат: <code>/broadcast Текст рассылки</code>\n\n"
                "💡 Удобнее через /admin → 📢 Рассылка."
            )
            return

        text = parts[1].strip()
        users = db.get_subscribed_users()
        if not users:
            await message.answer("Нет подписчиков для рассылки.")
            return

        sent, blocked = await _do_broadcast(bot, db, text, users)
        await message.answer(
            f"📤 Рассылка завершена.\n"
            f"Отправлено: {sent}\n"
            f"Заблокировали бота: {blocked}"
        )

    @router.message(Command("draw"))
    async def cmd_draw(message: Message) -> None:
        """Случайным образом выбирает победителя среди прошедших экскурсию."""
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        completed = db.get_completed_users()
        if not completed:
            await message.answer(
                "❌ Победитель не может быть выбран: "
                "пока нет никого, кто прошёл всю экскурсию.\n\n"
                "💡 Удобнее через /admin → 🎲 Розыгрыш."
            )
            return

        winner = random.choice(completed)
        user_id = winner["user_id"]
        username = winner["username"]
        first_name = winner["first_name"] or "—"

        if username:
            user_ref = f"@{html.quote(username)}"
        else:
            user_ref = html.quote(first_name)

        logger.info(
            "Розыгрыш /draw: выбран пользователь %s среди %d участников",
            user_id, len(completed),
        )

        await message.answer(
            "🏆 <b>Победитель розыгрыша</b>\n\n"
            f"👤 Имя: <b>{html.quote(first_name)}</b>\n"
            f"🔗 Username: {user_ref}\n"
            f"🆔 User ID: <code>{user_id}</code>\n\n"
            f"Всего участников: {len(completed)}"
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        if not message.from_user or not is_admin(
            message.from_user.id, settings.admin_chat_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.")
            return

        s = db.get_stats()
        lines = [
            "📊 <b>Статистика</b>\n",
            f"👤 Всего пользователей: {s['total_users']}",
            f"✅ Прошли экскурсию: {s['completed']}",
            f"🚫 Заблокировали бота: {s['blocked']}",
            "",
            "<b>Прохождение по точкам:</b>",
        ]
        if not s["per_point"]:
            lines.append("— точек пока нет —")
        else:
            for p in s["per_point"]:
                lines.append(
                    f"• #{p['id']} «{html.quote(p['name'])}»: "
                    f"{p['cnt']} чел."
                )
        await message.answer("\n".join(lines))

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        is_user_admin = bool(
            message.from_user
            and is_admin(message.from_user.id, settings.admin_chat_ids)
        )
        text = (
            "ℹ️ <b>Помощь</b>\n\n"
            "Введите <code>/start</code> и нажмите кнопку «▶️ Начать квест».\n\n"
            "Откроется табло со всеми локациями. "
            "Когда найдёте кодовое слово — нажмите кнопку с этой локацией, "
            "бот попросит ввести слово. После прохождения локация исчезает из табло.\n\n"
            "Локации можно проходить в любом порядке.\n\n"
            "Когда все локации пройдены — появится кнопка «🏆 КВЕСТ ПРОЙДЕН»."
        )
        if is_user_admin:
            text += (
                "\n\n🛠 <b>Вы администратор.</b>\n"
                "Введите <code>/admin</code> для управления ботом через удобное "
                "инлайн-меню (с кнопками).\n\n"
                "Также доступны текстовые команды:\n"
                "<code>/add_point Название | кодовое_слово | номер</code>\n"
                "<code>/del_point ID</code>\n"
                "<code>/list_points</code>\n"
                "<code>/add_promo Текст акции</code>\n"
                "<code>/broadcast Текст</code>\n"
                "<code>/draw</code> — случайный победитель\n"
                "<code>/stats</code>"
            )
        await message.answer(text)

    # ───────── Квест: кнопка «▶️ Начать квест» → табло локаций ─────────
    @router.callback_query(F.data == "quest:start")
    async def cb_quest_start(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user:
            return
        user_id = callback.from_user.id

        # Если идёт ввод кода — не сбрасываем, пусть доведёт до конца.
        cur = await state.get_state()
        if cur == QuestStates.waiting_code.state:
            await callback.answer(
                "Сначала введите кодовое слово для выбранной локации.",
                show_alert=True,
            )
            return

        total = db.count_points()
        if total == 0:
            await callback.answer(
                "Квест ещё не настроен.", show_alert=True
            )
            return

        if db.has_completed_all(user_id):
            await callback.answer()
            if callback.message:
                try:
                    await callback.message.edit_text(
                        "🎉 Вы уже прошли весь маршрут!\n"
                        "Нажмите кнопку ниже, чтобы подтвердить:",
                        reply_markup=_quest_finished_keyboard(),
                    )
                except Exception:
                    await callback.message.answer(
                        "🎉 Вы уже прошли весь маршрут!",
                        reply_markup=_quest_finished_keyboard(),
                    )
            return

        await state.clear()
        text = _quest_board_text(user_id)
        kb = _all_points_keyboard(user_id)
        if callback.message:
            try:
                await callback.message.edit_text(text, reply_markup=kb)
            except Exception:
                await callback.message.answer(text, reply_markup=kb)
        await callback.answer()

    # ───────── Квест: гость выбрал конкретную локацию из табло ─────────
    @router.callback_query(F.data.startswith("quest:pick:"))
    async def cb_quest_pick(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        if not callback.from_user:
            return
        user_id = callback.from_user.id

        raw_id = callback.data.split(":")[-1]  # type: ignore[union-attr]
        try:
            point_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный запрос.", show_alert=True)
            return

        point = db.get_point_by_id(point_id)
        if point is None:
            await callback.answer("Локация не найдена.", show_alert=True)
            return

        if db.has_passed_point(user_id, point_id):
            await callback.answer(
                "Эта локация уже пройдена.", show_alert=True
            )
            # Обновим табло — пройденная исчезнет.
            if callback.message and callback.message.text:
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=_all_points_keyboard(user_id)
                    )
                except Exception:
                    pass
            return

        cur = await state.get_state()
        if cur == QuestStates.waiting_code.state:
            await callback.answer(
                "Сначала введите кодовое слово для текущей локации.",
                show_alert=True,
            )
            return

        # Переводим гостя в FSM и просим кодовое слово.
        await state.set_state(QuestStates.waiting_code)
        await state.update_data(
            quest_point_id=point_id,
            quest_board_message_id=callback.message.message_id
            if callback.message else None,
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="quest:cancel")]
            ]
        )
        if callback.message:
            try:
                await callback.message.edit_text(
                    f"🔐 Вы выбрали локацию «{html.quote(point['name'])}».\n\n"
                    f"Введите <b>кодовое слово</b> для неё:",
                    reply_markup=kb,
                )
            except Exception:
                await callback.message.answer(
                    f"🔐 Введите <b>кодовое слово</b> для локации "
                    f"«{html.quote(point['name'])}»:",
                    reply_markup=kb,
                )
        await callback.answer()

    @router.callback_query(
        F.data == "quest:cancel",
        StateFilter(QuestStates.waiting_code),
    )
    async def cb_quest_cancel(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.clear()
        if callback.message:
            try:
                await callback.message.edit_text(
                    "Ввод кодового слова отменён.",
                    reply_markup=(
                        _quest_progress_keyboard(callback.from_user.id)
                        if callback.from_user else None
                    ),
                )
            except Exception:
                pass
        await callback.answer()

    @router.message(StateFilter(QuestStates.waiting_code))
    async def st_quest_waiting_code(
        message: Message, state: FSMContext
    ) -> None:
        if not message.from_user or not message.text:
            await message.answer(
                "Пожалуйста, отправьте кодовое слово текстом."
            )
            return
        user_id = message.from_user.id
        data = await state.get_data()
        point_id = data.get("quest_point_id")
        if point_id is None:
            await state.clear()
            await message.answer(
                "Состояние устарело. Начните заново.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="▶️ Начать квест",
                            callback_data="quest:start",
                        )]
                    ]
                ),
            )
            return

        point = db.get_point_by_id(int(point_id))
        if point is None:
            await state.clear()
            await message.answer(
                "❌ Локация больше не существует.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="▶️ Начать квест",
                            callback_data="quest:start",
                        )]
                    ]
                ),
            )
            return

        word_input = message.text.strip()
        if not word_input:
            await message.answer(
                "Кодовое слово не может быть пустым. Попробуйте ещё раз:"
            )
            return

        if db.has_passed_point(user_id, point["id"]):
            await state.clear()
            await message.answer(
                "ℹ️ Эта локация уже пройдена.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="▶️ Начать квест",
                            callback_data="quest:start",
                        )]
                    ]
                ),
            )
            return

        # Сравниваем без учёта регистра
        if word_input.lower() != point["code_word"].strip().lower():
            await message.answer("❌ Неверно. Попробуйте ещё раз.")
            # Состояние НЕ сбрасываем — пусть попробует снова.
            return

        # Засчитываем точку
        db.add_progress(user_id, point["id"])
        logger.info(
            "Пользователь %s прошёл локацию #%s '%s'",
            user_id,
            point["id"],
            html.quote(point["name"]),
        )
        await state.clear()

        passed = db.get_user_progress_count(user_id)
        total = db.count_points()

        if db.has_completed_all(user_id):
            # Все точки пройдены — показываем «КВЕСТ ПРОЙДЕН»
            await message.answer(
                f"✅ Локация «{html.quote(point['name'])}» пройдена!\n"
                f"📊 Прогресс: <b>{passed}/{total}</b>.\n\n"
                "🎉 <b>Поздравляем!</b> Вы прошли весь маршрут!\n"
                "Нажмите кнопку ниже, чтобы подтвердить и подписаться на акции 🎁",
                reply_markup=_quest_finished_keyboard(),
            )
        else:
            # Обновляем табло: пройденная локация исчезает.
            await message.answer(
                f"✅ Локация «{html.quote(point['name'])}» пройдена!\n"
                f"📊 Прогресс: <b>{passed}/{total}</b>.\n\n"
                "🗺 Выберите следующую локацию:",
                reply_markup=_all_points_keyboard(user_id),
            )

    # ───────── Квест: «🏆 КВЕСТ ПРОЙДЕН» ─────────
    @router.callback_query(F.data == "quest:finished")
    async def cb_quest_finished(
        callback: CallbackQuery, state: FSMContext
    ) -> None:
        await state.clear()
        if not callback.from_user:
            return
        user_id = callback.from_user.id

        if not db.has_completed_all(user_id):
            await callback.answer(
                "Вы ещё не прошли все точки маршрута.", show_alert=True
            )
            if callback.message:
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=_quest_progress_keyboard(user_id)
                    )
                except Exception:
                    pass
            return

        db.mark_user_passed(user_id)
        logger.info("Пользователь %s отметил прохождение экскурсии", user_id)
        await callback.answer("Спасибо! 🎉")
        if callback.message:
            await callback.message.answer(
                "Спасибо! Теперь вы будете получать наши акции и новости. 🎁"
            )

    # ───────── Совместимость со старой кнопкой «finished» ─────────
    @router.callback_query(F.data == "finished")
    async def on_finished_legacy(callback: CallbackQuery) -> None:
        # Перенаправляем на новый унифицированный хендлер
        if not callback.from_user:
            return
        user_id = callback.from_user.id
        if not db.has_completed_all(user_id):
            await callback.answer(
                "Вы ещё не прошли все точки экскурсии.", show_alert=True
            )
            if callback.message:
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=_quest_progress_keyboard(user_id)
                    )
                except Exception:
                    pass
            return
        db.mark_user_passed(user_id)
        await callback.answer("Спасибо! 🎉")
        if callback.message:
            await callback.message.answer(
                "Спасибо! Теперь вы будете получать наши акции и новости. 🎁"
            )

    # ───────── Любой текст вне FSM → подсказка с кнопкой (В САМОМ КОНЦЕ!) ─────────
    @router.message(F.text)
    async def handle_codeword(message: Message, state: FSMContext) -> None:
        """
        Если пользователь не в FSM и просто прислал текст — это не команда
        и не кодовое слово (код проверяется в стейте QuestStates.waiting_code).
        Реагируем мягкой подсказкой: «Нажмите кнопку «📍 Локация N пройдена»».
        """
        cur = await state.get_state()
        if cur is not None:
            return  # активный стейт перехватит сообщение

        if not message.from_user:
            return

        user = message.from_user
        db.upsert_user(user.id, user.username, user.first_name)

        total = db.count_points()
        if total == 0:
            await message.answer(
                "ℹ️ Квест ещё не настроен: администратор не добавил точки маршрута."
            )
            return

        if db.has_completed_all(user.id):
            await message.answer(
                "✅ Вы уже прошли весь маршрут!\n"
                "Нажмите кнопку «🏆 КВЕСТ ПРОЙДЕН», чтобы подтвердить.",
                reply_markup=_quest_finished_keyboard(),
            )
            return

        await message.answer(
            "💡 Кодовое слово нельзя ввести просто так — "
            "нажмите кнопку <b>«▶️ Начать квест»</b>, "
            "выберите локацию в табло и введите слово для неё.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="▶️ Начать квест",
                        callback_data="quest:start",
                    )]
                ]
            ),
        )

    return bot, dp


# ─────────────────────────────────────────────────────────────
# Рассылка (вынесена для переиспользования)
# ─────────────────────────────────────────────────────────────
async def _do_broadcast(
    bot: Bot,
    db: Database,
    text: str,
    users: list[sqlite3.Row],
) -> tuple[int, int]:
    """
    Рассылает text списку users.
    Возвращает кортеж (успешно отправлено, заблокировали бота).
    """
    sent = 0
    blocked = 0
    for row in users:
        user_id = row["user_id"]
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            db.mark_user_blocked(user_id)
            blocked += 1
            logger.warning("Пользователь %s заблокировал бота", user_id)
        except TelegramAPIError as exc:
            logger.error(
                "Ошибка Telegram API при отправке пользователю %s: %s",
                user_id,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "Неожиданная ошибка при отправке пользователю %s: %s",
                user_id,
                exc,
            )
        # Небольшая пауза, чтобы не упереться в лимиты Telegram
        await asyncio.sleep(0.05)
    return sent, blocked


# ─────────────────────────────────────────────────────────────
# Фоновая задача: ежедневная авторассылка в 10:00
# ─────────────────────────────────────────────────────────────
async def daily_broadcast_loop(bot: Bot, db: Database, hour: int) -> None:
    """Каждый день в hour:00 рассылает все неразосланные акции подписчикам."""
    logger.info("Запущена фоновая рассылка на %02d:00", hour)
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            logger.info(
                "Следующая авторассылка в %s (через %.0f сек.)",
                target.isoformat(timespec="seconds"),
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

            promos = db.get_unsent_promos()
            if not promos:
                logger.info("Нет новых акций для авторассылки.")
                continue

            users = db.get_subscribed_users()
            if not users:
                logger.info("Нет подписчиков для авторассылки.")
                db.mark_promos_sent([p["id"] for p in promos])
                continue

            text = "🎁 <b>Наши акции:</b>\n\n" + "\n\n".join(
                p["text"] for p in promos
            )
            sent, blocked = await _do_broadcast(bot, db, text, users)
            db.mark_promos_sent([p["id"] for p in promos])
            logger.info(
                "Авторассылка выполнена: акций=%d, отправлено=%d, заблокировали=%d",
                len(promos),
                sent,
                blocked,
            )
        except asyncio.CancelledError:
            logger.info("Фоновая рассылка остановлена.")
            raise
        except Exception as exc:
            logger.exception("Ошибка в цикле авторассылки: %s", exc)
            # Чтобы не зациклиться при постоянной ошибке — подождём минуту
            await asyncio.sleep(60)


# ─────────────────────────────────────────────────────────────
# main — точка входа
# ─────────────────────────────────────────────────────────────
async def main() -> None:
    try:
        settings = Settings.load()
    except RuntimeError as exc:
        logger.critical("Ошибка конфигурации: %s", exc)
        sys.exit(1)

    db = Database(settings.database_path)
    bot, dp = build_bot(settings, db)

    # Удаляем вебхук и сбрасываем накопившиеся апдейты (если бот перезапускается)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as exc:
        logger.warning("Не удалось сбросить вебхук: %s", exc)

    # Запускаем фоновую задачу авторассылки
    bg_task = asyncio.create_task(
        daily_broadcast_loop(bot, db, settings.daily_broadcast_hour),
        name="daily-broadcast",
    )

    logger.info(
        "Бот запущен. Админы: %s", ", ".join(str(x) for x in settings.admin_chat_ids)
    )
    try:
        await dp.start_polling(bot)
    finally:
        bg_task.cancel()
        try:
            await bg_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")
