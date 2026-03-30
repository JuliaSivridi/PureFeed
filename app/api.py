"""
Минимальный FastAPI роутер: только статус сервиса.
Управление фидами — через Telegram-бота (/feedlist).
"""
import logging
from fastapi import APIRouter

from app.session_manager import session_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/status")
async def get_status():
    """Статус сервиса (для health check)."""
    active_users = sum(1 for bot in session_manager._bots.values() if bot.is_monitoring)
    return {
        "status": "ok",
        "active_users": active_users,
    }
