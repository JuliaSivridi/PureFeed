"""
Точка входа FastAPI приложения.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.database import init_db
from app.api import router
from app.session_manager import session_manager
from app.bot_commands import run_polling

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Запуск TG Feed Filter...")
    await init_db()
    logger.info("База данных инициализирована")

    await session_manager.start_all()

    # Polling команд бота — работает для всех пользователей
    asyncio.create_task(run_polling(session_manager))

    yield

    logger.info("Завершение работы...")
    await session_manager.stop_all()


app = FastAPI(title="TG Feed Filter", version="4.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(router)
