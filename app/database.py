"""
Модуль для работы с базой данных SQLite через aiosqlite.
Хранит конфигурацию фидов, каналов и ключевых слов.
"""

import aiosqlite
from datetime import datetime
from typing import Optional

# Путь к файлу базы данных
DB_PATH = "/app/data/feeds.db"


async def init_db():
    """Инициализация базы данных: создание таблиц если не существуют."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица фидов (наборов настроек фильтрации)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                destination_channel TEXT NOT NULL,
                use_ai_filter INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                user_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        # Миграция: добавляем user_id если колонки ещё нет
        try:
            await db.execute("ALTER TABLE feeds ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        except Exception:
            pass  # Колонка уже существует

        # Таблица исходных каналов для каждого фида
        await db.execute("""
            CREATE TABLE IF NOT EXISTS source_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                channel_username TEXT NOT NULL,
                FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
            )
        """)

        # Таблица ключевых слов для фильтрации
        await db.execute("""
            CREATE TABLE IF NOT EXISTS filter_keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
            )
        """)

        # Таблица для хранения последнего обработанного сообщения по каждому каналу
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_last_seen (
                channel_username TEXT PRIMARY KEY,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)

        # Таблица настроек пользователей (Telegram API credentials)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                api_id INTEGER,
                api_hash TEXT
            )
        """)

        await db.commit()


def _feed_row_to_dict(row) -> dict:
    """Преобразует строку из БД в словарь фида."""
    return {
        "id": row[0],
        "name": row[1],
        "destination_channel": row[2],
        "use_ai_filter": bool(row[3]),
        "enabled": bool(row[4]),
        "user_id": row[5],
        "created_at": row[6],
        "channels": [],
        "keywords": [],
    }


async def get_all_feeds(user_id: int | None = None) -> list[dict]:
    """
    Возвращает фиды с их каналами и ключевыми словами.
    Если user_id задан — только фиды этого пользователя.
    Без user_id — все фиды (для мониторинга).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id is not None:
            cursor = await db.execute(
                "SELECT id, name, destination_channel, use_ai_filter, enabled, user_id, created_at FROM feeds WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT id, name, destination_channel, use_ai_filter, enabled, user_id, created_at FROM feeds ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        feeds = [_feed_row_to_dict(row) for row in rows]

        if not feeds:
            return []

        feed_map = {f["id"]: f for f in feeds}
        feed_ids = list(feed_map.keys())
        placeholders = ",".join("?" * len(feed_ids))

        # Получаем каналы для всех фидов одним запросом
        cursor = await db.execute(
            f"SELECT feed_id, channel_username FROM source_channels WHERE feed_id IN ({placeholders})",
            feed_ids,
        )
        for row in await cursor.fetchall():
            feed_map[row[0]]["channels"].append(row[1])

        # Получаем ключевые слова для всех фидов одним запросом
        cursor = await db.execute(
            f"SELECT feed_id, keyword FROM filter_keywords WHERE feed_id IN ({placeholders})",
            feed_ids,
        )
        for row in await cursor.fetchall():
            feed_map[row[0]]["keywords"].append(row[1])

        return feeds


async def get_feed(feed_id: int, user_id: int | None = None) -> Optional[dict]:
    """Возвращает один фид по ID. Если задан user_id — проверяет владельца."""
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id is not None:
            cursor = await db.execute(
                "SELECT id, name, destination_channel, use_ai_filter, enabled, user_id, created_at FROM feeds WHERE id = ? AND user_id = ?",
                (feed_id, user_id),
            )
        else:
            cursor = await db.execute(
                "SELECT id, name, destination_channel, use_ai_filter, enabled, user_id, created_at FROM feeds WHERE id = ?",
                (feed_id,),
            )
        row = await cursor.fetchone()
        if not row:
            return None

        feed = _feed_row_to_dict(row)

        # Каналы фида
        cursor = await db.execute(
            "SELECT channel_username FROM source_channels WHERE feed_id = ?",
            (feed_id,),
        )
        feed["channels"] = [r[0] for r in await cursor.fetchall()]

        # Ключевые слова фида
        cursor = await db.execute(
            "SELECT keyword FROM filter_keywords WHERE feed_id = ?",
            (feed_id,),
        )
        feed["keywords"] = [r[0] for r in await cursor.fetchall()]

        return feed


async def create_feed(name: str, destination_channel: str, user_id: int = 0, use_ai_filter: bool = False) -> dict:
    """Создаёт новый фид и возвращает его."""
    created_at = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO feeds (name, destination_channel, use_ai_filter, enabled, user_id, created_at) VALUES (?, ?, ?, 1, ?, ?)",
            (name, normalize_channel(destination_channel), int(use_ai_filter), user_id, created_at),
        )
        await db.commit()
        feed_id = cursor.lastrowid

    return await get_feed(feed_id)


async def update_feed(feed_id: int, **kwargs) -> Optional[dict]:
    """
    Обновляет поля фида. Принимает именованные аргументы:
    name, destination_channel, use_ai_filter, enabled.
    """
    allowed_fields = {"name", "destination_channel", "use_ai_filter", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}

    # Нормализуем канал назначения если он обновляется
    if "destination_channel" in updates:
        updates["destination_channel"] = normalize_channel(updates["destination_channel"])

    if not updates:
        return await get_feed(feed_id)

    # Преобразуем булевые значения в int для SQLite
    if "use_ai_filter" in updates:
        updates["use_ai_filter"] = int(updates["use_ai_filter"])
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [feed_id]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE feeds SET {set_clause} WHERE id = ?", values)
        await db.commit()

    return await get_feed(feed_id)


async def delete_feed(feed_id: int) -> bool:
    """Удаляет фид и все связанные данные (ON DELETE CASCADE)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_feed_channels(feed_id: int) -> list[str]:
    """Возвращает список каналов фида."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT channel_username FROM source_channels WHERE feed_id = ?",
            (feed_id,),
        )
        return [row[0] for row in await cursor.fetchall()]


def normalize_channel(channel: str) -> str:
    """
    Приводит адрес канала к формату, понятному Telethon.
    Поддерживает:
      https://t.me/lovevjazanie    →  @lovevjazanie  (публичный канал)
      t.me/lovevjazanie            →  @lovevjazanie
      lovevjazanie                 →  @lovevjazanie
      @lovevjazanie                →  @lovevjazanie
      https://t.me/c/1812695632/  →  -1001812695632  (приватный канал по ID)
      https://t.me/c/1812695632/5 →  -1001812695632  (ссылка на пост — берём только ID канала)
      -1001812695632               →  -1001812695632  (уже в правильном формате)
    """
    ch = channel.strip()

    # Ссылка-приглашение (t.me/+HASH или t.me/joinchat/HASH) — сохраняем как есть,
    # Telethon умеет резолвить их напрямую через API
    if "t.me/+" in ch or "joinchat" in ch:
        return ch

    # Приватный канал: t.me/c/XXXXXXXXX/...
    if "t.me/c/" in ch:
        # Извлекаем числовой ID между /c/ и следующим /
        parts = ch.split("t.me/c/")[-1].strip("/").split("/")
        channel_id = parts[0]
        if channel_id.isdigit():
            return f"-100{channel_id}"
        return ch

    # Публичный канал по ссылке
    if "t.me/" in ch:
        ch = ch.split("t.me/")[-1].strip("/")

    # Уже числовой ID (со знаком минус или без, возможно с @ спереди — старые данные)
    if ch.lstrip("@").lstrip("-").isdigit():
        return ch.lstrip("@")  # убираем @ если он случайно приклеился

    # Добавляем @ если нет
    if ch and not ch.startswith("@"):
        ch = "@" + ch

    return ch


async def add_channel(feed_id: int, channel_username: str) -> bool:
    """
    Добавляет канал к фиду. Возвращает False если канал уже добавлен.
    Нормализует адрес: https://t.me/name → @name
    """
    # Нормализация имени канала
    username = normalize_channel(channel_username)

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, нет ли уже такого канала
        cursor = await db.execute(
            "SELECT id FROM source_channels WHERE feed_id = ? AND channel_username = ?",
            (feed_id, username),
        )
        if await cursor.fetchone():
            return False

        await db.execute(
            "INSERT INTO source_channels (feed_id, channel_username) VALUES (?, ?)",
            (feed_id, username),
        )
        await db.commit()
        return True


async def remove_channel(feed_id: int, channel_username: str) -> bool:
    """Удаляет канал из фида."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM source_channels WHERE feed_id = ? AND channel_username = ?",
            (feed_id, channel_username),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_feed_keywords(feed_id: int) -> list[str]:
    """Возвращает список ключевых слов фида."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT keyword FROM filter_keywords WHERE feed_id = ?",
            (feed_id,),
        )
        return [row[0] for row in await cursor.fetchall()]


async def add_keyword(feed_id: int, keyword: str) -> bool:
    """Добавляет ключевое слово к фиду. Возвращает False если уже существует."""
    kw = keyword.strip().lower()
    if not kw:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM filter_keywords WHERE feed_id = ? AND keyword = ?",
            (feed_id, kw),
        )
        if await cursor.fetchone():
            return False

        await db.execute(
            "INSERT INTO filter_keywords (feed_id, keyword) VALUES (?, ?)",
            (feed_id, kw),
        )
        await db.commit()
        return True


async def get_last_seen_id(channel: str) -> int:
    """Возвращает ID последнего обработанного сообщения для канала (0 если первый запуск)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT last_message_id FROM channel_last_seen WHERE channel_username = ?",
            (channel,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def update_last_seen_id(channel: str, message_id: int):
    """Обновляет (или создаёт) запись о последнем обработанном сообщении канала."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO channel_last_seen (channel_username, last_message_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                updated_at      = excluded.updated_at
            """,
            (channel, message_id, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def remove_keyword(feed_id: int, keyword: str) -> bool:
    """Удаляет ключевое слово из фида."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM filter_keywords WHERE feed_id = ? AND keyword = ?",
            (feed_id, keyword.strip().lower()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_settings(user_id: int) -> Optional[dict]:
    """Возвращает настройки пользователя (api_id, api_hash) или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, api_id, api_hash FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {"user_id": row[0], "api_id": row[1], "api_hash": row[2]}


async def save_user_settings(user_id: int, **kwargs) -> dict:
    """Сохраняет или обновляет api_id и/или api_hash пользователя."""
    allowed = {"api_id", "api_hash"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return await get_user_settings(user_id) or {"user_id": user_id}

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM user_settings WHERE user_id = ?", (user_id,)
        )
        if await cursor.fetchone():
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            values = list(fields.values()) + [user_id]
            await db.execute(f"UPDATE user_settings SET {set_clause} WHERE user_id = ?", values)
        else:
            cols = ["user_id"] + list(fields.keys())
            vals = [user_id] + list(fields.values())
            await db.execute(
                f"INSERT INTO user_settings ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))})",
                vals,
            )
        await db.commit()

    return await get_user_settings(user_id)


async def get_all_user_settings() -> list[dict]:
    """Возвращает всех пользователей с заполненными api_id и api_hash."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, api_id, api_hash FROM user_settings "
            "WHERE api_id IS NOT NULL AND api_hash IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [{"user_id": r[0], "api_id": r[1], "api_hash": r[2]} for r in rows]
