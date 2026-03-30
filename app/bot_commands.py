"""
Управление фидами и мониторингом через Telegram-бота.
Команды: /feedlist, /help, /esc, /settings
Polling-based: получает обновления через getUpdates.
"""
import asyncio
import logging
import httpx
import os
import urllib.parse
from typing import TYPE_CHECKING

from telethon.errors import SessionPasswordNeededError

from app.database import (
    get_feed, get_all_feeds, create_feed, delete_feed, update_feed,
    add_channel, remove_channel, add_keyword, remove_keyword,
    get_user_settings, save_user_settings,
)

if TYPE_CHECKING:
    from app.session_manager import SessionManager

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# Состояние диалога: chat_id → {step, ctx}
_states: dict[int, dict] = {}

_CANCEL_HINT = "\n\n<i>/esc — отмена</i>"

_HELP_TEXT = (
    "📖 <b>Как пользоваться ботом</b>\n\n"
    "<b>Подготовка (один раз):</b>\n\n"
    "1️⃣ Получите Telegram API credentials\n"
    "Зайдите на <a href=\"https://my.telegram.org\">my.telegram.org</a> → API development tools\n"
    "Создайте приложение, скопируйте <b>App api_id</b> и <b>App api_hash</b>.\n\n"
    "2️⃣ Авторизуйте userbot через QR-код\n"
    "/settings → введите api_id и api_hash → 📷 Войти через QR-код\n"
    "Бот пришлёт QR — откройте Telegram → Настройки → Устройства → Подключить устройство → отсканируйте.\n"
    "Никакого номера телефона, никаких кодов.\n\n"
    "3️⃣ Служебный канал (добавляется автоматически)\n"
    "После авторизации бот добавит вас в закрытый служебный канал — это нужно для работы счётчика непрочитанных сообщений. "
    "Можете заархивировать его и никогда не открывать, но, пожалуйста, не выходите из него — иначе пересылка перестанет работать.\n\n"
    "<b>Настройка фидов:</b>\n\n"
    "1️⃣ Создайте фид\n"
    "/feedlist → ➕ Добавить фид → введите название.\n\n"
    "2️⃣ Создайте канал назначения\n"
    "Приватный Telegram-канал, куда будут пересылаться новости.\n"
    "Добавьте этого бота как администратора с правом публикации сообщений.\n\n"
    "3️⃣ Укажите канал назначения в фиде\n"
    "Управление фидом → 📺 Канал назначения → перешлите любое сообщение из этого канала.\n\n"
    "4️⃣ Добавьте источники\n"
    "📡 Управление каналами → ➕ Добавить → перешлите сообщение из канала-источника.\n\n"
    "5️⃣ Настройте фильтры (необязательно)\n"
    "🔍 Управление фильтрами → ➕ Добавить → введите слово-стоп.\n"
    "Несколько слов через <code>+</code>: <code>скидка+купить</code>\n\n"
    "<b>Команды:</b>\n"
    "/feedlist — управление фидами\n"
    "/settings — настройки userbot\n"
    "/help — эта справка\n"
    "/esc — отмена текущего ввода"
)


def _get_state(chat_id: int) -> dict:
    return _states.get(chat_id, {"step": "idle", "ctx": {}})


def _set_state(chat_id: int, step: str, ctx: dict | None = None):
    _states[chat_id] = {"step": step, "ctx": ctx or {}}


def _clear_state(chat_id: int):
    _states.pop(chat_id, None)


# ─── Bot API helpers ──────────────────────────────────────────────────────────

async def _api(method: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(f"{_API}/{method}", json=kwargs)
    return resp.json()


async def _send(chat_id: int, text: str, markup=None, parse_mode: str = "HTML") -> dict:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                     "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    return await _api("sendMessage", **payload)


async def _delete(chat_id: int, message_id: int):
    try:
        await _api("deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _show(chat_id: int, msg_id: int | None, text: str, markup=None, parse_mode: str = "HTML"):
    """Удаляет старое сообщение и отправляет новое (всегда внизу чата)."""
    if msg_id:
        await _delete(chat_id, msg_id)
    await _send(chat_id, text, markup, parse_mode)


async def _answer(callback_id: str, text: str = ""):
    await _api("answerCallbackQuery", callback_query_id=callback_id, text=text)


# ─── Screens ──────────────────────────────────────────────────────────────────

async def show_feed_list(chat_id: int, msg_id: int | None, sm: "SessionManager"):
    bot = sm.get(chat_id)
    monitoring = bot.is_monitoring if bot else False
    status_line = "🟢 Сервис работает" if monitoring else "🔴 Сервис остановлен"

    feeds = await get_all_feeds(user_id=chat_id)
    seen: set[int] = set()
    unique: list[dict] = []
    for f in feeds:
        if f["id"] not in seen:
            seen.add(f["id"])
            unique.append(f)

    total_channels = sum(len(f["channels"]) for f in unique)
    text = f"{status_line}\n\n📋 <b>Фиды</b> — {total_channels}" + ("" if unique else "\n\nНет фидов. Создайте первый!")
    buttons = [
        [{"text": f"{'✅' if f['enabled'] else '❌'} {f['name']} — {len(f['channels'])}", "callback_data": f"feed:{f['id']}"}]
        for f in unique
    ]
    buttons.append([{"text": "➕ Добавить фид", "callback_data": "feed:new"}])
    if monitoring:
        buttons.append([{"text": "⏹ Стоп сервис", "callback_data": "monitor:stop"}])
    else:
        buttons.append([{"text": "▶️ Старт сервис", "callback_data": "monitor:start"}])
    buttons.append([{"text": "⚙️ Настройки", "callback_data": "settings"}])

    await _show(chat_id, msg_id, text, {"inline_keyboard": buttons})


async def show_settings(chat_id: int, msg_id: int | None, sm: "SessionManager"):
    settings = await get_user_settings(chat_id)
    bot = sm.get(chat_id)
    auth_status = "✅ авторизован" if (bot and await bot.is_authenticated()) else "❌ не авторизован"

    api_id = settings["api_id"] if settings and settings.get("api_id") else "не задан"
    api_hash = settings["api_hash"] if settings and settings.get("api_hash") else "не задан"
    if settings and settings.get("api_hash"):
        api_hash_display = settings["api_hash"][:6] + "****"
    else:
        api_hash_display = "не задан"

    text = (
        f"⚙️ <b>Настройки userbot</b>\n\n"
        f"api_id: <code>{api_id}</code>\n"
        f"api_hash: <code>{api_hash_display}</code>\n"
        f"Статус: {auth_status}"
    )
    markup = {"inline_keyboard": [
        [{"text": "🔑 Ввести api_id",        "callback_data": "set_api_id"}],
        [{"text": "🔑 Ввести api_hash",       "callback_data": "set_api_hash"}],
        [{"text": "📷 Войти через QR-код",    "callback_data": "auth_qr"}],
        [{"text": "◀️ Назад",                 "callback_data": "back:feeds"}],
    ]}
    await _show(chat_id, msg_id, text, markup)


async def show_feed(chat_id: int, feed_id: int, msg_id: int | None):
    feed = await get_feed(feed_id, user_id=chat_id)
    if not feed:
        await _send(chat_id, "❌ Фид не найден.")
        return
    enabled = feed["enabled"]
    dest = feed["destination_channel"] or "не задан"
    text = (
        f"📌 <b>{feed['name']}</b>\n\n"
        f"📺 Назначение: <code>{dest}</code>\n"
        f"📡 Каналов: {len(feed['channels'])}\n"
        f"🔍 Фильтров: {len(feed['keywords'])}\n"
        f"Статус: {'✅ активен' if enabled else '❌ выключен'}"
    )
    markup = {"inline_keyboard": [
        [{"text": "📺 Канал назначения",     "callback_data": f"setdest:{feed_id}"}],
        [{"text": "📡 Управление каналами",  "callback_data": f"channels:{feed_id}"}],
        [{"text": "🔍 Управление фильтрами", "callback_data": f"filters:{feed_id}"}],
        [
            {"text": "⏸ Стоп" if enabled else "▶️ Старт", "callback_data": f"toggle:{feed_id}"},
            {"text": "✏️ Название",                         "callback_data": f"rename:{feed_id}"},
            {"text": "🗑 Удалить",                          "callback_data": f"delete:{feed_id}"},
        ],
        [{"text": "◀️ Назад", "callback_data": "back:feeds"}],
    ]}
    await _show(chat_id, msg_id, text, markup)


async def show_channels(chat_id: int, feed_id: int, msg_id: int | None):
    feed = await get_feed(feed_id, user_id=chat_id)
    if not feed:
        return
    channels = feed["channels"]
    ch_list = "\n".join(f"• {c}" for c in channels) if channels else "Нет каналов"
    text = f"📡 <b>{feed['name']} — Каналы</b>\n\n{ch_list}"
    markup = {"inline_keyboard": [
        [
            {"text": "➕ Добавить", "callback_data": f"ch_add:{feed_id}"},
            {"text": "➖ Удалить",  "callback_data": f"ch_del_list:{feed_id}"},
        ],
        [{"text": "◀️ Назад", "callback_data": f"feed:{feed_id}"}],
    ]}
    await _show(chat_id, msg_id, text, markup)


async def show_channel_delete_list(chat_id: int, feed_id: int, msg_id: int | None):
    feed = await get_feed(feed_id, user_id=chat_id)
    if not feed:
        return
    channels = feed["channels"]
    if not channels:
        await _send(chat_id, "Нет каналов для удаления.")
        return
    text = f"📡 <b>{feed['name']}</b>\n\nВыберите канал для удаления:"
    buttons = [[{"text": c, "callback_data": f"ch_del:{feed_id}:{c}"}] for c in channels]
    buttons.append([{"text": "◀️ Назад", "callback_data": f"channels:{feed_id}"}])
    await _show(chat_id, msg_id, text, {"inline_keyboard": buttons})


async def show_filters(chat_id: int, feed_id: int, msg_id: int | None):
    feed = await get_feed(feed_id, user_id=chat_id)
    if not feed:
        return
    keywords = feed["keywords"]
    kw_list = "\n".join(f"• <code>{k}</code>" for k in keywords) if keywords else "Нет фильтров"
    text = f"🔍 <b>{feed['name']} — Фильтры</b>\n\n{kw_list}"
    markup = {"inline_keyboard": [
        [
            {"text": "➕ Добавить", "callback_data": f"kw_add:{feed_id}"},
            {"text": "➖ Удалить",  "callback_data": f"kw_del_list:{feed_id}"},
        ],
        [{"text": "◀️ Назад", "callback_data": f"feed:{feed_id}"}],
    ]}
    await _show(chat_id, msg_id, text, markup)


async def show_filter_delete_list(chat_id: int, feed_id: int, msg_id: int | None):
    feed = await get_feed(feed_id, user_id=chat_id)
    if not feed:
        return
    keywords = feed["keywords"]
    if not keywords:
        await _send(chat_id, "Нет фильтров для удаления.")
        return
    text = f"🔍 <b>{feed['name']}</b>\n\nВыберите фильтр для удаления:"
    buttons = [
        [{"text": k, "callback_data": f"kw_del:{feed_id}:{k[:50]}"}]
        for k in keywords
    ]
    buttons.append([{"text": "◀️ Назад", "callback_data": f"filters:{feed_id}"}])
    await _show(chat_id, msg_id, text, {"inline_keyboard": buttons})


async def show_delete_confirm(chat_id: int, feed_id: int, msg_id: int | None):
    feed = await get_feed(feed_id, user_id=chat_id)
    if not feed:
        return
    text = f"🗑 Удалить фид <b>{feed['name']}</b> со всеми каналами и фильтрами?"
    markup = {"inline_keyboard": [[
        {"text": "✅ Да, удалить", "callback_data": f"delete_ok:{feed_id}"},
        {"text": "❌ Нет",         "callback_data": f"feed:{feed_id}"},
    ]]}
    await _show(chat_id, msg_id, text, markup)


# ─── Cancel helper ────────────────────────────────────────────────────────────

async def _cancel(chat_id: int, step: str, ctx: dict, sm: "SessionManager"):
    _clear_state(chat_id)
    await _send(chat_id, "✅ Отменено")
    feed_id = ctx.get("feed_id")
    msg_id = ctx.get("msg_id")
    if step == "auth_qr_wait":
        task = ctx.get("task")
        if task:
            task.cancel()
        await show_settings(chat_id, msg_id, sm)
    elif step == "add_channel":
        await show_channels(chat_id, feed_id, msg_id)
    elif step == "add_keyword":
        await show_filters(chat_id, feed_id, msg_id)
    elif step in ("rename_feed", "set_dest"):
        await show_feed(chat_id, feed_id, msg_id)
    elif step in ("set_api_id", "set_api_hash", "auth_phone", "auth_code", "auth_2fa", "auth_2fa_qr"):
        await show_settings(chat_id, msg_id, sm)
    else:
        await show_feed_list(chat_id, msg_id, sm)


# ─── QR auth background waiter ────────────────────────────────────────────────

async def _qr_auth_waiter(chat_id: int, bot, sm: "SessionManager"):
    """Фоновая задача: ждёт сканирования QR и обрабатывает результат."""
    try:
        success = await bot.wait_for_qr_scan(timeout=120)
        state = _get_state(chat_id)
        # Если пользователь уже нажал /esc — не продолжаем
        if state["step"] != "auth_qr_wait":
            return
        if success:
            _clear_state(chat_id)
            await _send(chat_id, "✅ Авторизация успешна! Запускаю мониторинг...")
            try:
                await bot.start_monitoring()
            except Exception as e:
                await _send(chat_id, f"⚠️ Мониторинг не запустился: {e}")
            await show_feed_list(chat_id, None, sm)
        else:
            _clear_state(chat_id)
            await _send(chat_id, "❌ QR-код истёк. Попробуйте снова — /settings → Войти через QR.")
    except SessionPasswordNeededError:
        _set_state(chat_id, "auth_2fa_qr", {})
        await _send(chat_id, "🔐 Введите пароль двухфакторной аутентификации:" + _CANCEL_HINT)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _clear_state(chat_id)
        await _send(chat_id, f"❌ Ошибка QR-авторизации: {e}")


# ─── Update handlers ──────────────────────────────────────────────────────────

async def handle_message(chat_id: int, message: dict, sm: "SessionManager"):
    text = (message.get("text") or "").strip()
    forward_chat = message.get("forward_from_chat")

    if text.lower() == "/esc":
        state = _get_state(chat_id)
        await _cancel(chat_id, state["step"], state["ctx"], sm)
        return

    if text.startswith("/"):
        _clear_state(chat_id)
        if text in ("/feedlist", "/start"):
            await show_feed_list(chat_id, None, sm)
        elif text == "/help":
            await _send(chat_id, _HELP_TEXT)
        elif text == "/settings":
            await show_settings(chat_id, None, sm)
        return

    state = _get_state(chat_id)
    step = state["step"]
    ctx = state["ctx"]

    if step == "idle":
        return

    # ── Настройки userbot ─────────────────────────────────────────────────────
    if step == "set_api_id":
        if not text.isdigit():
            await _send(chat_id, "❌ api_id — только цифры. Попробуйте ещё раз." + _CANCEL_HINT)
            return
        await save_user_settings(chat_id, api_id=int(text))
        _clear_state(chat_id)
        await _send(chat_id, f"✅ api_id сохранён: <code>{text}</code>", parse_mode="HTML")
        await show_settings(chat_id, ctx.get("msg_id"), sm)

    elif step == "set_api_hash":
        if len(text) < 10:
            await _send(chat_id, "❌ api_hash слишком короткий. Попробуйте ещё раз." + _CANCEL_HINT)
            return
        await save_user_settings(chat_id, api_hash=text)
        _clear_state(chat_id)
        await _send(chat_id, "✅ api_hash сохранён.")
        await show_settings(chat_id, ctx.get("msg_id"), sm)

    elif step == "auth_phone":
        settings = await get_user_settings(chat_id)
        if not settings or not settings.get("api_id") or not settings.get("api_hash"):
            _clear_state(chat_id)
            await _send(chat_id, "❌ Сначала введите api_id и api_hash в настройках.")
            return
        bot = await sm.get_or_create(chat_id, settings["api_id"], settings["api_hash"])
        try:
            phone_code_hash = await bot.start_auth(text)
            _set_state(chat_id, "auth_code", {
                "phone": text,
                "phone_code_hash": phone_code_hash,
                "msg_id": ctx.get("msg_id"),
            })
            await _send(chat_id, "📩 Код отправлен в Telegram. Введите код:" + _CANCEL_HINT)
        except Exception as e:
            _clear_state(chat_id)
            await _send(chat_id, f"❌ Ошибка: {e}")

    elif step == "auth_code":
        settings = await get_user_settings(chat_id)
        bot = sm.get(chat_id)
        if not bot or not settings:
            _clear_state(chat_id)
            await _send(chat_id, "❌ Сессия потеряна. Начните заново через /settings.")
            return
        try:
            await bot.confirm_auth(
                phone=ctx["phone"],
                code=text,
                phone_code_hash=ctx["phone_code_hash"],
            )
            _clear_state(chat_id)
            await _send(chat_id, "✅ Авторизация успешна! Запускаю мониторинг...")
            try:
                await bot.start_monitoring()
            except Exception as e:
                await _send(chat_id, f"⚠️ Мониторинг не запустился: {e}")
            await show_feed_list(chat_id, None, sm)
        except ValueError as e:
            err = str(e)
            if "двухфакторной" in err:
                _set_state(chat_id, "auth_2fa", ctx)
                await _send(chat_id, "🔐 Введите пароль двухфакторной аутентификации:" + _CANCEL_HINT)
            else:
                _clear_state(chat_id)
                await _send(chat_id, f"❌ {e}")
        except Exception as e:
            _clear_state(chat_id)
            await _send(chat_id, f"❌ Ошибка авторизации: {e}")

    elif step == "auth_2fa":
        bot = sm.get(chat_id)
        if not bot:
            _clear_state(chat_id)
            return
        try:
            await bot.confirm_auth(
                phone=ctx["phone"],
                code=ctx.get("code", ""),
                phone_code_hash=ctx["phone_code_hash"],
                password=text,
            )
            _clear_state(chat_id)
            await _send(chat_id, "✅ Авторизация успешна!")
            await bot.start_monitoring()
            await show_feed_list(chat_id, None, sm)
        except Exception as e:
            _clear_state(chat_id)
            await _send(chat_id, f"❌ Ошибка: {e}")

    elif step == "auth_2fa_qr":
        bot = sm.get(chat_id)
        if not bot:
            _clear_state(chat_id)
            return
        try:
            await bot.confirm_2fa(text)
            _clear_state(chat_id)
            await _send(chat_id, "✅ Авторизация успешна! Запускаю мониторинг...")
            try:
                await bot.start_monitoring()
            except Exception as e:
                await _send(chat_id, f"⚠️ Мониторинг не запустился: {e}")
            await show_feed_list(chat_id, None, sm)
        except Exception as e:
            _clear_state(chat_id)
            await _send(chat_id, f"❌ Неверный пароль: {e}")

    # ── Управление фидами ─────────────────────────────────────────────────────
    elif step == "new_feed_name":
        feed = await create_feed(text, destination_channel="", user_id=chat_id)
        _clear_state(chat_id)
        bot = sm.get(chat_id)
        if bot:
            asyncio.create_task(bot.reload_feeds())
        await _send(chat_id, f"✅ Фид <b>{feed['name']}</b> создан! Теперь настройте канал назначения.", parse_mode="HTML")
        await show_feed(chat_id, feed["id"], None)

    elif step == "rename_feed":
        feed_id = ctx["feed_id"]
        await update_feed(feed_id, name=text)
        _clear_state(chat_id)
        await show_feed(chat_id, feed_id, ctx.get("msg_id"))

    elif step == "set_dest":
        feed_id = ctx["feed_id"]
        if not forward_chat:
            await _send(chat_id, "❌ Перешлите любое сообщение из канала назначения." + _CANCEL_HINT)
            return
        channel_id = str(forward_chat["id"])
        await update_feed(feed_id, destination_channel=channel_id)
        _clear_state(chat_id)
        bot = sm.get(chat_id)
        if bot:
            asyncio.create_task(bot.reload_feeds())
        title = forward_chat.get("title", channel_id)
        await _send(chat_id, f"✅ Канал назначения: <b>{title}</b> (<code>{channel_id}</code>)", parse_mode="HTML")
        await show_feed(chat_id, feed_id, ctx.get("msg_id"))

    elif step == "add_channel":
        feed_id = ctx["feed_id"]
        if not forward_chat:
            await _send(chat_id, "❌ Перешлите любое сообщение из нужного канала." + _CANCEL_HINT)
            return
        username = forward_chat.get("username")
        channel_key = f"@{username}" if username else str(forward_chat["id"])
        ok = await add_channel(feed_id, channel_key)
        _clear_state(chat_id)
        if ok:
            title = forward_chat.get("title", channel_key)
            await _send(chat_id, f"✅ Добавлен: <b>{title}</b> ({channel_key})", parse_mode="HTML")
            bot = sm.get(chat_id)
            if bot:
                asyncio.create_task(bot.reload_feeds())
        else:
            await _send(chat_id, "⚠️ Канал уже добавлен.")
        await show_channels(chat_id, feed_id, ctx.get("msg_id"))

    elif step == "add_keyword":
        feed_id = ctx["feed_id"]
        ok = await add_keyword(feed_id, text)
        _clear_state(chat_id)
        if ok:
            await _send(chat_id, f"✅ Фильтр добавлен: <code>{text}</code>", parse_mode="HTML")
        else:
            await _send(chat_id, "⚠️ Такой фильтр уже есть.")
        await show_filters(chat_id, feed_id, ctx.get("msg_id"))


async def handle_callback(callback: dict, sm: "SessionManager"):
    cq_id = callback["id"]
    chat_id = callback["from"]["id"]
    data = callback.get("data", "")
    msg_id = callback.get("message", {}).get("message_id")

    await _answer(cq_id)

    if data == "back:feeds":
        _clear_state(chat_id)
        await show_feed_list(chat_id, msg_id, sm)

    elif data == "settings":
        await show_settings(chat_id, msg_id, sm)

    elif data == "set_api_id":
        _set_state(chat_id, "set_api_id", {"msg_id": msg_id})
        await _show(chat_id, msg_id, "🔑 Введите api_id (только цифры).\n\nПолучить: my.telegram.org → API development tools" + _CANCEL_HINT)

    elif data == "set_api_hash":
        _set_state(chat_id, "set_api_hash", {"msg_id": msg_id})
        await _show(chat_id, msg_id, "🔑 Введите api_hash.\n\nПолучить: my.telegram.org → API development tools" + _CANCEL_HINT)

    elif data == "auth_start":
        settings = await get_user_settings(chat_id)
        if not settings or not settings.get("api_id") or not settings.get("api_hash"):
            await _send(chat_id, "❌ Сначала введите api_id и api_hash.")
            return
        _set_state(chat_id, "auth_phone", {"msg_id": msg_id})
        await _show(chat_id, msg_id, "📱 Введите номер телефона с кодом страны, например +49XXXXXXXXX:" + _CANCEL_HINT)

    elif data == "auth_qr":
        settings = await get_user_settings(chat_id)
        if not settings or not settings.get("api_id") or not settings.get("api_hash"):
            await _send(chat_id, "❌ Сначала введите api_id и api_hash.")
            return
        bot = await sm.get_or_create(chat_id, settings["api_id"], settings["api_hash"])
        try:
            url = await bot.start_qr_auth()
            qr_image = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={urllib.parse.quote(url, safe='')}"
            async with httpx.AsyncClient(timeout=10.0) as http:
                await http.post(
                    f"{_API}/sendPhoto",
                    json={
                        "chat_id": chat_id,
                        "photo": qr_image,
                        "caption": (
                            "📷 <b>Сканируйте QR-код для входа</b>\n\n"
                            "Откройте Telegram → Настройки → Устройства → Подключить устройство\n\n"
                            "Действует 2 минуты. /esc — отмена."
                        ),
                        "parse_mode": "HTML",
                    },
                )
            task = asyncio.create_task(_qr_auth_waiter(chat_id, bot, sm))
            _set_state(chat_id, "auth_qr_wait", {"task": task})
        except RuntimeError as e:
            await _send(chat_id, f"ℹ️ {e}")
            await show_settings(chat_id, msg_id, sm)
        except Exception as e:
            await _send(chat_id, f"❌ Ошибка запуска QR: {e}")

    elif data == "feed:new":
        _set_state(chat_id, "new_feed_name")
        await _show(chat_id, msg_id, "✏️ Введите название нового фида:" + _CANCEL_HINT)

    elif data.startswith("feed:"):
        feed_id = int(data.split(":")[1])
        _clear_state(chat_id)
        await show_feed(chat_id, feed_id, msg_id)

    elif data.startswith("rename:"):
        feed_id = int(data.split(":")[1])
        _set_state(chat_id, "rename_feed", {"feed_id": feed_id, "msg_id": msg_id})
        await _send(chat_id, "✏️ Введите новое название фида:" + _CANCEL_HINT)

    elif data.startswith("setdest:"):
        feed_id = int(data.split(":")[1])
        _set_state(chat_id, "set_dest", {"feed_id": feed_id, "msg_id": msg_id})
        await _send(chat_id, "📺 Перешлите любое сообщение из канала назначения:" + _CANCEL_HINT)

    elif data.startswith("toggle:"):
        feed_id = int(data.split(":")[1])
        feed = await get_feed(feed_id, user_id=chat_id)
        if feed:
            await update_feed(feed_id, enabled=not feed["enabled"])
            bot = sm.get(chat_id)
            if bot:
                asyncio.create_task(bot.reload_feeds())
        await show_feed(chat_id, feed_id, msg_id)

    elif data.startswith("delete:"):
        feed_id = int(data.split(":")[1])
        await show_delete_confirm(chat_id, feed_id, msg_id)

    elif data.startswith("delete_ok:"):
        feed_id = int(data.split(":")[1])
        feed = await get_feed(feed_id, user_id=chat_id)
        name = feed["name"] if feed else str(feed_id)
        await delete_feed(feed_id)
        bot = sm.get(chat_id)
        if bot:
            asyncio.create_task(bot.reload_feeds())
        if msg_id:
            await _delete(chat_id, msg_id)
        await _send(chat_id, f"🗑 Фид <b>{name}</b> удалён.", parse_mode="HTML")
        await show_feed_list(chat_id, None, sm)

    elif data.startswith("channels:"):
        feed_id = int(data.split(":")[1])
        await show_channels(chat_id, feed_id, msg_id)

    elif data.startswith("ch_add:"):
        feed_id = int(data.split(":")[1])
        _set_state(chat_id, "add_channel", {"feed_id": feed_id, "msg_id": msg_id})
        await _send(chat_id, "📡 Перешлите любое сообщение из канала который хотите добавить:" + _CANCEL_HINT)

    elif data.startswith("ch_del_list:"):
        feed_id = int(data.split(":")[1])
        await show_channel_delete_list(chat_id, feed_id, msg_id)

    elif data.startswith("ch_del:"):
        parts = data.split(":", 2)
        feed_id, channel = int(parts[1]), parts[2]
        await remove_channel(feed_id, channel)
        bot = sm.get(chat_id)
        if bot:
            asyncio.create_task(bot.reload_feeds())
        await show_channels(chat_id, feed_id, msg_id)

    elif data.startswith("filters:"):
        feed_id = int(data.split(":")[1])
        await show_filters(chat_id, feed_id, msg_id)

    elif data.startswith("kw_add:"):
        feed_id = int(data.split(":")[1])
        _set_state(chat_id, "add_keyword", {"feed_id": feed_id, "msg_id": msg_id})
        await _send(
            chat_id,
            "🔍 Введите ключевое слово или правило.\n\n"
            "Несколько слов через <code>+</code> = все должны присутствовать:\n"
            "• <code>промокод</code>\n"
            "• <code>скидка+купить</code>" + _CANCEL_HINT,
            parse_mode="HTML",
        )

    elif data.startswith("kw_del_list:"):
        feed_id = int(data.split(":")[1])
        await show_filter_delete_list(chat_id, feed_id, msg_id)

    elif data.startswith("kw_del:"):
        parts = data.split(":", 2)
        feed_id, keyword = int(parts[1]), parts[2]
        await remove_keyword(feed_id, keyword)
        await show_filters(chat_id, feed_id, msg_id)

    elif data == "monitor:start":
        bot = sm.get(chat_id)
        if bot:
            asyncio.create_task(bot.start_monitoring())
        await show_feed_list(chat_id, msg_id, sm)

    elif data == "monitor:stop":
        bot = sm.get(chat_id)
        if bot:
            await bot.stop_monitoring()
        await show_feed_list(chat_id, msg_id, sm)


# ─── Polling loop ─────────────────────────────────────────────────────────────

async def run_polling(sm: "SessionManager"):
    """Long-poll Bot API updates. Any Telegram user can interact."""
    logger.info("Команды бота: polling запущен")
    offset = 0

    while True:
        try:
            async with httpx.AsyncClient(timeout=35.0) as http:
                resp = await http.post(
                    f"{_API}/getUpdates",
                    json={"offset": offset, "timeout": 30, "allowed_updates": ["message", "callback_query"]},
                )
            data = resp.json()

            if not data.get("ok"):
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        await handle_callback(update["callback_query"], sm)
                    elif "message" in update:
                        msg = update["message"]
                        from_id = msg.get("from", {}).get("id")
                        if from_id:
                            await handle_message(from_id, msg, sm)
                except Exception as e:
                    logger.error(f"Ошибка обработки update {update.get('update_id')}: {e}")

        except asyncio.CancelledError:
            logger.info("Bot commands polling остановлен")
            return
        except Exception as e:
            logger.error(f"Polling ошибка: {e}")
            await asyncio.sleep(5)
