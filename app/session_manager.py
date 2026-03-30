"""
Управляет экземплярами TelegramBot — по одному на каждого авторизованного пользователя.
"""
import asyncio
import logging
from typing import Optional

from app.database import get_all_user_settings
from app.bot import TelegramBot

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self):
        self._bots: dict[int, TelegramBot] = {}

    def get(self, user_id: int) -> Optional[TelegramBot]:
        """Возвращает бота пользователя или None."""
        return self._bots.get(user_id)

    @property
    def is_monitoring(self) -> bool:
        return any(b.is_monitoring for b in self._bots.values())

    async def get_or_create(self, user_id: int, api_id: int, api_hash: str) -> TelegramBot:
        """Возвращает существующего бота или создаёт нового с указанными credentials."""
        existing = self._bots.get(user_id)
        if existing and existing.api_id == api_id and existing.api_hash == api_hash:
            return existing
        if existing:
            await existing.stop_monitoring()
            await existing.disconnect()
        bot = TelegramBot(user_id=user_id, api_id=api_id, api_hash=api_hash)
        self._bots[user_id] = bot
        return bot

    async def start_all(self):
        """При старте приложения запускает мониторинг для всех пользователей с сессиями."""
        users = await get_all_user_settings()
        if not users:
            logger.info("Нет сохранённых сессий пользователей")
            return
        await asyncio.gather(*[self._start_safely(u) for u in users])

    async def _start_safely(self, user: dict):
        user_id = user["user_id"]
        bot = TelegramBot(user_id=user_id, api_id=user["api_id"], api_hash=user["api_hash"])
        self._bots[user_id] = bot
        try:
            await bot.start_monitoring()
        except Exception as e:
            logger.warning(f"Пользователь {user_id}: мониторинг не запущен — {e}")

    async def stop_all(self):
        """Останавливает всех ботов."""
        for bot in self._bots.values():
            await bot.stop_monitoring()
            await bot.disconnect()


# Глобальный экземпляр
session_manager = SessionManager()
