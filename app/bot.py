"""
Гибридный мониторинг Telegram-каналов.

Схема:
  - Telethon (userbot) — слушает каналы через MTProto (push, все каналы).
  - Bot API — отправляет чистые сообщения в каналы-назначения.

Почему гибрид:
  - Telethon умеет читать любые каналы без членства бота.
  - Бот (не аккаунт пользователя) как отправитель → Telegram показывает счётчик непрочитанных.
  - Bot API forwardMessage даёт нативный «Переслано из [Канал]».
"""

import os
import asyncio
import logging
from typing import Optional

import httpx
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from telethon.tl.types import Message, PeerChannel

from app.database import get_all_feeds
from app.filter import ad_filter

logger = logging.getLogger(__name__)

BOT_SESSION_DIR = "/app/data"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Приватный канал для relay: юзербот форвардит сюда → бот пересылает из него в назначение.
# Оба (юзербот и бот) должны быть администраторами этого канала.
# Формат: -100XXXXXXXXXX
RELAY_CHANNEL_RAW = os.getenv("RELAY_CHANNEL_ID", "")
RELAY_INVITE_LINK = os.getenv("RELAY_INVITE_LINK", "")


class TelegramBot:
    """
    Один экземпляр на пользователя: Telethon мониторит каналы, Bot API пересылает.
    """

    def __init__(self, user_id: int, api_id: int, api_hash: str):
        self.user_id = user_id
        self.api_id = api_id
        self.api_hash = api_hash

        self.client: Optional[TelegramClient] = None
        self._monitoring = False
        self._monitored_channels: list[str] = []
        self._current_handler = None
        self._pending_phone: Optional[str] = None
        self._dialogs_loaded = False

        # Relay: юзербот → relay-канал → Bot API forward → канал назначения
        self._bot_entity = None          # relay-канал как Telethon InputPeer
        self._relay_chat_id: Optional[int] = None   # числовой ID relay-канала для Bot API

        # Кэш: channel_key (строка) → numeric Telegram ID (для надёжного матчинга)
        self._channel_id_cache: dict[str, int] = {}

        # ID авторизованного юзербота (для команд бота)
        self._userbot_id: Optional[int] = None

        # QR-авторизация
        self._qr_login = None

        # Буфер медиагрупп (альбомов): grouped_id → данные
        self._media_groups: dict[int, dict] = {}
        self._media_group_tasks: dict[int, asyncio.Task] = {}

    def _create_client(self) -> TelegramClient:
        return TelegramClient(
            f"{BOT_SESSION_DIR}/telegram_{self.user_id}",
            self.api_id,
            self.api_hash,
            device_model="Desktop",
            app_version="1.0",
            lang_code="ru",
        )

    async def _ensure_client(self):
        if self.client is None:
            self.client = self._create_client()
        if not self.client.is_connected():
            await self.client.connect()

    # ─── Авторизация ─────────────────────────────────────────────────────────

    async def start_auth(self, phone: str) -> str:
        await self._ensure_client()
        self._pending_phone = phone
        result = await self.client.send_code_request(phone)
        logger.info(f"Код авторизации отправлен на {phone}")
        return result.phone_code_hash

    async def confirm_auth(
        self,
        phone: str,
        code: str,
        phone_code_hash: str,
        password: Optional[str] = None,
    ) -> dict:
        await self._ensure_client()
        try:
            await self.client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                raise ValueError("Требуется пароль двухфакторной аутентификации")
            await self.client.sign_in(password=password)
        except PhoneCodeInvalidError:
            raise ValueError("Неверный код подтверждения")
        except PhoneCodeExpiredError:
            raise ValueError("Код подтверждения истёк. Запросите новый")
        except PasswordHashInvalidError:
            raise ValueError("Неверный пароль двухфакторной аутентификации")

        me = await self.client.get_me()
        logger.info(f"Авторизация успешна: {me.first_name} (@{me.username})")
        return self._user_to_dict(me)

    async def start_qr_auth(self) -> str:
        """Запускает QR-авторизацию. Возвращает tg:// URL для показа пользователю."""
        await self._ensure_client()
        if await self.client.is_user_authorized():
            raise RuntimeError("Аккаунт уже авторизован")
        self._qr_login = await self.client.qr_login()
        return self._qr_login.url

    async def wait_for_qr_scan(self, timeout: float = 120) -> bool:
        """Ждёт сканирования QR. True — успех, False — таймаут.
        Поднимает SessionPasswordNeededError если включена двухфакторная аутентификация."""
        if not self._qr_login:
            return False
        try:
            await asyncio.wait_for(self._qr_login.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except SessionPasswordNeededError:
            raise
        except Exception as e:
            logger.warning(f"QR ожидание завершилось ошибкой: {e}")
            return False

    async def confirm_2fa(self, password: str):
        """Завершает авторизацию паролем 2FA (после QR-сканирования)."""
        await self.client.sign_in(password=password)

    async def is_authenticated(self) -> bool:
        try:
            await self._ensure_client()
            return await self.client.is_user_authorized()
        except Exception:
            return False

    async def get_me(self) -> Optional[dict]:
        try:
            await self._ensure_client()
            if not await self.client.is_user_authorized():
                return None
            return self._user_to_dict(await self.client.get_me())
        except Exception as e:
            logger.error(f"Ошибка get_me: {e}")
            return None

    def _user_to_dict(self, user) -> dict:
        return {
            "id": user.id,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
            "phone": user.phone or "",
        }

    # ─── Мониторинг ──────────────────────────────────────────────────────────

    async def start_monitoring(self):
        if self._monitoring:
            logger.info("Мониторинг уже запущен")
            return

        await self._ensure_client()

        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram не авторизован")

        self._monitoring = True

        # Шаг 1: минимальный обработчик — без резолвинга ID (кэш ещё пуст).
        # Ловит сообщения, которые придут во время загрузки диалогов.
        await self._setup_handler(resolve_ids=False)

        # Шаг 2: загружаем диалоги — заполняем кэш сущностей Telethon.
        if not self._dialogs_loaded:
            logger.info("Загрузка диалогов...")
            count = 0
            async for _ in self.client.iter_dialogs():
                count += 1
            self._dialogs_loaded = True
            logger.info(f"Загружено диалогов: {count}")

        # Шаг 3: перестраиваем обработчик — теперь кэш заполнен,
        # можно резолвить @username → numeric ID для надёжного матчинга.
        await self._setup_handler(resolve_ids=True)

        # Получаем ID авторизованного пользователя (нужен до _init_bot_relay)
        try:
            me = await self.client.get_me()
            self._userbot_id = me.id
        except Exception as e:
            logger.warning(f"Не удалось получить ID юзербота: {e}")

        await self._init_bot_relay()

        # Фоновый цикл: каждые 30 с синхронизируем состояние обновлений.
        # Telegram не всегда шлёт push для каналов — без этого задержка может быть 10-15 минут.
        asyncio.create_task(self._updates_keepalive())

        logger.info(f"Мониторинг запущен (user={self.user_id})")

    async def stop_monitoring(self):
        if not self._monitoring:
            return
        if self._current_handler is not None:
            self.client.remove_event_handler(self._current_handler)
            self._current_handler = None
        self._monitoring = False
        self._monitored_channels = []
        logger.info("Мониторинг остановлен")

    async def reload_feeds(self):
        """Перечитывает фиды из БД и обновляет обработчик событий."""
        if not self._monitoring:
            return
        logger.info("Перезагрузка списка каналов...")
        await self._setup_handler()

    async def _updates_keepalive(self):
        """Каждые 30 с запрашивает GetState — заставляет Telethon подтянуть пропущенные обновления."""
        from telethon import functions
        while self._monitoring:
            await asyncio.sleep(30)
            if not self._monitoring:
                break
            try:
                await self.client(functions.updates.GetStateRequest())
            except Exception:
                pass

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring

    async def disconnect(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    # ─── Внутренние методы ───────────────────────────────────────────────────

    async def _setup_handler(self, resolve_ids: bool = True):
        if self._current_handler is not None:
            self.client.remove_event_handler(self._current_handler)
            self._current_handler = None

        feeds = await get_all_feeds(user_id=self.user_id)
        enabled_feeds = [f for f in feeds if f["enabled"]]

        # channel_key (строка из БД) → список фидов
        channel_to_feeds: dict[str, list[dict]] = {}
        for feed in enabled_feeds:
            for channel in feed["channels"]:
                if channel not in channel_to_feeds:
                    channel_to_feeds[channel] = []
                channel_to_feeds[channel].append(feed)

        self._monitored_channels = list(channel_to_feeds.keys())

        if not self._monitored_channels:
            logger.info("Нет каналов для мониторинга")
            return

        logger.info(f"Мониторинг {len(self._monitored_channels)} каналов")

        # numeric_id → список фидов (надёжный матчинг даже если username не пришёл в событии)
        id_to_feeds: dict[int, list[dict]] = {}
        for channel_key, feeds_list in channel_to_feeds.items():
            clean = channel_key.strip()
            # Числовые ключи — парсим сразу без сетевого запроса
            if clean.startswith('-100') and clean[4:].isdigit():
                id_to_feeds[int(clean[4:])] = feeds_list
                continue
            if clean.lstrip('-').isdigit():
                id_to_feeds[abs(int(clean))] = feeds_list
                continue
            # @username — резолвим через Telethon только если разрешено (кэш должен быть готов)
            if channel_key in self._channel_id_cache:
                id_to_feeds[self._channel_id_cache[channel_key]] = feeds_list
            elif resolve_ids:
                try:
                    entity = await self.client.get_input_entity(channel_key)
                    full = await self.client.get_entity(entity)
                    self._channel_id_cache[channel_key] = full.id
                    id_to_feeds[full.id] = feeds_list
                except Exception as e:
                    logger.warning(f"Не удалось резолвить {channel_key}: {e}")

        if resolve_ids:
            resolved = len(id_to_feeds)
            logger.info(f"Резолвлено каналов по ID: {resolved} из {len(self._monitored_channels)}")

        async def handle_new_message(event):
            await self._process_message(event, channel_to_feeds, id_to_feeds)

        # Глобальный обработчик — без фильтра chats (ненадёжен для большого списка).
        # Сопоставление с каналами происходит внутри _process_message.
        self._current_handler = handle_new_message
        self.client.add_event_handler(handle_new_message, events.NewMessage())

    async def _process_message(
        self,
        event,
        channel_to_feeds: dict[str, list[dict]],
        id_to_feeds: dict[int, list[dict]],
    ):
        try:
            message: Message = event.message
            chat = await event.get_chat()

            chat_username = None
            if hasattr(chat, 'username') and chat.username:
                chat_username = f"@{chat.username}"

            # Матчинг: сначала по numeric ID (надёжнее), потом по строке
            feeds_for_channel: list[dict] = []
            if chat.id in id_to_feeds:
                feeds_for_channel = id_to_feeds[chat.id]
            else:
                for channel_key, feeds_list in channel_to_feeds.items():
                    if chat_username and channel_key.lower() == chat_username.lower():
                        feeds_for_channel = feeds_list
                        break
                    if str(chat.id) in channel_key or f"-100{chat.id}" in channel_key:
                        feeds_for_channel = feeds_list
                        break

            if not feeds_for_channel:
                logger.debug(f"Не в фидах: chat.id={chat.id} username={chat_username} title={getattr(chat, 'title', '?')!r}")
                return

            logger.info(f"Сообщение из {chat_username or chat.id} (id={chat.id}) → {len(feeds_for_channel)} фид(ов)")

            # Помечаем прочитанным в исходном канале
            try:
                await self.client.send_read_acknowledge(chat, message)
            except Exception:
                pass

            source_name = chat_username or getattr(chat, 'title', str(chat.id))

            # Медиагруппа (альбом с несколькими фото) — буферизуем
            if message.grouped_id:
                await self._buffer_media_group(message, source_name, feeds_for_channel)
                return

            # Одиночное сообщение
            await self._process_single(message, source_name, feeds_for_channel)

        except Exception as e:
            logger.error(f"Критическая ошибка в обработчике: {e}")

    # ─── Медиагруппы (альбомы) ───────────────────────────────────────────────

    async def _buffer_media_group(
        self,
        message: Message,
        source_name: str,
        feeds_for_channel: list[dict],
    ):
        """Буферизует сообщения из альбома; через 1 с обрабатывает группу целиком."""
        gid = message.grouped_id

        if gid not in self._media_groups:
            self._media_groups[gid] = {
                "messages": [],
                "source_name": source_name,
                "feeds": feeds_for_channel,
            }

        self._media_groups[gid]["messages"].append(message)

        # Сбрасываем таймер, чтобы подождать все части альбома
        if gid in self._media_group_tasks:
            self._media_group_tasks[gid].cancel()

        self._media_group_tasks[gid] = asyncio.create_task(
            self._process_media_group_later(gid)
        )

    async def _process_media_group_later(self, grouped_id: int):
        """Ждёт 1 секунду (собираем все части) и обрабатывает альбом целиком."""
        try:
            await asyncio.sleep(1.0)

            group = self._media_groups.pop(grouped_id, None)
            self._media_group_tasks.pop(grouped_id, None)
            if not group:
                return

            messages: list[Message] = sorted(group["messages"], key=lambda m: m.id)
            source_name: str = group["source_name"]
            feeds_for_channel: list[dict] = group["feeds"]

            # Текст для фильтра — из любого сообщения группы
            text = ""
            for msg in messages:
                if msg.text:
                    text = msg.text
                    break

            for feed in feeds_for_channel:
                try:
                    is_ad, reason = await ad_filter.is_ad(
                        text=text,
                        keywords=feed.get("keywords", []),
                        use_ai=feed.get("use_ai_filter", False),
                    )
                    if is_ad:
                        logger.info(
                            f"[{feed['name']}] Реклама (альбом {len(messages)} шт.) "
                            f"из {source_name}: {reason[:80]}"
                        )
                        continue

                    await self._forward_media_group(messages, feed["destination_channel"])
                    logger.info(
                        f"[{feed['name']}] Переслан альбом ({len(messages)} шт.) из {source_name}"
                    )

                except FloodWaitError as e:
                    logger.warning(f"FloodWait при пересылке альбома: ждём {e.seconds}с")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logger.error(f"Ошибка фида '{feed['name']}' при пересылке альбома: {e}")

        except asyncio.CancelledError:
            pass

    async def _forward_media_group(self, messages: list[Message], destination: str):
        """Пересылает альбом через relay (бот как отправитель → счётчик непрочитанных)."""
        clean = destination.strip()
        to_chat: int | str = int(clean) if clean.lstrip('-').isdigit() else clean

        # ── Попытка 1: relay ──────────────────────────────────────────────────
        if self._bot_entity and self._relay_chat_id:
            try:
                fwd = await self.client.forward_messages(
                    self._bot_entity, messages, silent=True
                )
                relay_msgs = fwd if isinstance(fwd, list) else [fwd]
                if not relay_msgs:
                    raise RuntimeError("forward_messages вернул пустой список")
                relay_ids = [m.id for m in relay_msgs]

                async with httpx.AsyncClient(timeout=15.0) as http:
                    resp = await http.post(
                        f"{BOT_API_BASE}/forwardMessages",
                        json={
                            "chat_id": to_chat,
                            "from_chat_id": self._relay_chat_id,
                            "message_ids": relay_ids,
                        },
                    )
                data = resp.json()

                try:
                    await self.client.delete_messages(self._bot_entity, relay_ids)
                except Exception:
                    pass

                if data.get("ok"):
                    return  # ✅ счётчик непрочитанных

                logger.warning(
                    f"Relay album: бот не смог переслать: {data.get('description')} "
                    f"(to={to_chat}, relay_ids={relay_ids})"
                )
            except Exception as e:
                logger.warning(f"Relay album не удался: {e}")

        # ── Резерв: прямой форвард через юзербота ─────────────────────────────
        logger.info("Прямой форвард альбома через юзербот (без счётчика)")
        entity = await self._resolve_entity_telethon(destination)
        await self.client.forward_messages(entity, messages)

    # ─── Одиночные сообщения ─────────────────────────────────────────────────

    async def _process_single(
        self,
        message: Message,
        source_name: str,
        feeds_for_channel: list[dict],
    ):
        """Фильтрует и пересылает одиночное сообщение."""
        text = message.text or ""

        for feed in feeds_for_channel:
            try:
                is_ad, reason = await ad_filter.is_ad(
                    text=text,
                    keywords=feed.get("keywords", []),
                    use_ai=feed.get("use_ai_filter", False),
                )

                if is_ad:
                    logger.info(f"[{feed['name']}] Реклама из {source_name}: {reason[:100]}")
                    continue

                await self._forward_message(message, feed["destination_channel"])
                logger.info(f"[{feed['name']}] Переслано из {source_name}")

            except FloodWaitError as e:
                logger.warning(f"FloodWait: ждём {e.seconds}с")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Ошибка фида '{feed['name']}': {e}")

    # ─── Пересылка ───────────────────────────────────────────────────────────

    async def _init_bot_relay(self):
        """
        Инициализирует relay юзербот → relay-канал → Bot API forward → назначение.

        Требует RELAY_CHANNEL_ID в .env — ID приватного канала, где
        оба (юзербот и бот-токен) являются администраторами.
        """
        if not BOT_TOKEN:
            logger.warning("BOT_TOKEN не задан — relay через бота недоступен")
            return

        if not RELAY_CHANNEL_RAW:
            logger.warning(
                "RELAY_CHANNEL_ID не задан — relay недоступен, "
                "сообщения будут пересылаться напрямую через юзербота (без счётчика непрочитанных). "
                "Создайте приватный канал, добавьте бота и юзербота как администраторов, "
                "укажите его ID в RELAY_CHANNEL_ID."
            )
            return

        try:
            # Вступаем в relay-канал по invite-ссылке (если ещё не участник)
            if RELAY_INVITE_LINK:
                try:
                    await self.client.join_channel(RELAY_INVITE_LINK)
                    logger.info("Relay: вступили в канал")
                except Exception:
                    pass  # Уже участник — нормально

            # Резолвим relay-канал через Telethon
            self._bot_entity = await self._resolve_entity_telethon(RELAY_CHANNEL_RAW)
            full = await self.client.get_entity(self._bot_entity)

            clean = RELAY_CHANNEL_RAW.strip()
            self._relay_chat_id = int(clean) if clean.startswith('-100') else -(1000000000000 + full.id)

            # Назначаем юзербота администратором relay через Bot API
            if self._userbot_id:
                async with httpx.AsyncClient(timeout=10.0) as http:
                    resp = await http.post(
                        f"{BOT_API_BASE}/promoteChatMember",
                        json={
                            "chat_id": self._relay_chat_id,
                            "user_id": self._userbot_id,
                            "can_post_messages": True,
                            "can_edit_messages": True,
                            "can_delete_messages": True,
                        },
                    )
                    data = resp.json()
                    if data.get("ok"):
                        logger.info(f"Relay: права администратора назначены (user={self._userbot_id})")
                    else:
                        logger.warning(f"Relay: не удалось назначить права: {data.get('description')}")

            logger.info(f"Relay готов: канал {self._relay_chat_id} «{getattr(full, 'title', '?')}»")
        except Exception as e:
            self._bot_entity = None
            self._relay_chat_id = None
            logger.warning(f"Relay недоступен: {e}")

    async def _resolve_entity_telethon(self, destination: str):
        """Резолвит строку назначения в Telethon-сущность."""
        clean = destination.strip()
        if clean.startswith('-100') and clean[4:].isdigit():
            return await self.client.get_input_entity(PeerChannel(int(clean[4:])))
        elif clean.lstrip('-').isdigit():
            return await self.client.get_input_entity(int(clean))
        else:
            return await self.client.get_input_entity(clean)

    async def _forward_via_userbot(self, message: Message, destination: str):
        """Форвард через юзербота (без счётчика непрочитанных, но работает всегда)."""
        entity = await self._resolve_entity_telethon(destination)
        await self.client.forward_messages(entity, message)

    async def _forward_message(self, message: Message, destination: str):
        """
        Пересылает сообщение с приоритетом:

        1. Relay юзербот → relay-канал → Bot API forwardMessage → назначение:
           - Результат: нативный «Переслано из [Канал]» + счётчик непрочитанных ✅

        2. Прямой форвард через юзербот (резерв):
           - нативный «Переслано из [Канал]», но без счётчика непрочитанных
        """
        clean = destination.strip()
        to_chat: int | str = int(clean) if clean.lstrip('-').isdigit() else clean

        # ── Попытка 1: relay через канал ─────────────────────────────────────
        if self._bot_entity and self._relay_chat_id:
            try:
                # Шаг 1: юзербот форвардит в relay-канал (тихо)
                fwd = await self.client.forward_messages(
                    self._bot_entity, message, silent=True
                )
                relay_msgs = fwd if isinstance(fwd, list) else [fwd]
                if not relay_msgs:
                    raise RuntimeError("forward_messages вернул пустой список")
                relay_id = relay_msgs[0].id

                # Шаг 2: бот пересылает из relay-канала в канал назначения
                async with httpx.AsyncClient(timeout=15.0) as http:
                    resp = await http.post(
                        f"{BOT_API_BASE}/forwardMessage",
                        json={
                            "chat_id": to_chat,
                            "from_chat_id": self._relay_chat_id,
                            "message_id": relay_id,
                        },
                    )
                data = resp.json()

                # Шаг 3: удаляем из relay-канала в любом случае
                try:
                    await self.client.delete_messages(self._bot_entity, [relay_id])
                except Exception:
                    pass

                if data.get("ok"):
                    return  # ✅ нативный форвард + счётчик непрочитанных

                logger.warning(
                    f"Relay: бот не смог переслать из relay-канала: {data.get('description')} "
                    f"(to={to_chat}, relay_channel={self._relay_chat_id}, relay_msg={relay_id})"
                )

            except Exception as e:
                logger.warning(f"Relay не удался: {e}")

        # ── Попытка 2: прямой форвард через юзербот ──────────────────────────
        logger.info("Прямой форвард через юзербот (без счётчика непрочитанных)")
        await self._forward_via_userbot(message, destination)


