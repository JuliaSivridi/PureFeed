# PureFeed — Technical Specification

**Version:** 1.0 (April 2026)  
**Repository:** github.com/JuliaSivridi/PureFeed  
**Bot:** @pure_feed_bot  
**Stack:** Python 3.11 · FastAPI · Telethon · SQLite · Docker

---

## 1. Overview

PureFeed is a self-hosted, multi-user Telegram feed filter. Each user authenticates their own Telegram account as a **userbot** (via Telethon + MTProto), configures independent **feeds** that map source channels to a destination channel with keyword-based ad filtering, and receives clean forwarded content — complete with working unread counters in destination channels.

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **No web UI** | All management via Telegram bot inline keyboards — zero external port exposure needed |
| **Per-user Telethon clients** | Each user monitors only their own subscribed channels; no cross-user data leakage |
| **Relay Buffer channel** | Bot token as the forwarder generates unread counters; direct userbot forwards do not |
| **QR-code auth only** | Telegram blocks server-IP phone+code login; QR auth happens device-side |
| **SQLite (not Postgres)** | Single-file, zero-config, sufficient for thousands of users given async I/O via aiosqlite |
| **In-memory conversation state** | Bot command dialogs are ephemeral; DB stores only persistent data |
| **FastAPI as shell** | Provides health-check endpoint and clean asyncio lifespan hooks; no REST API otherwise |

---

## 2. Tech Stack

| Layer | Library | Version | Notes |
|-------|---------|---------|-------|
| Language | Python | 3.11 | asyncio throughout; type hints |
| Web framework | FastAPI | 0.111.0 | Single health endpoint; lifespan for startup/shutdown |
| ASGI server | Uvicorn | 0.29.0 | `[standard]` extras for faster I/O |
| MTProto client | Telethon | 1.36.0 | One `TelegramClient` per user; SQLite session files |
| Bot API client | httpx | 0.27.0 | Direct HTTPS calls to api.telegram.org; no Telegram SDK |
| Database | aiosqlite | 0.20.0 | Async SQLite wrapper; single `feeds.db` file |
| QR generation | qrcode | 7.4.2 | Generates PNG for QR-code login |
| Image processing | Pillow | 10.3.0 | Required by qrcode for PNG output |
| Config | python-dotenv | 1.0.1 | Loads `.env` into environment |
| Container | Docker + Compose | — | `python:3.11-slim` base; no multi-stage build |

---

## 3. Architecture

### Pattern

**Event-driven async service.** No MVC/MVVM layers. Three independent async loops share the process:

1. **Telethon event loops** — one per authenticated user; receive MTProto push events
2. **Bot API polling loop** — single `getUpdates` long-poll; handles all user interactions
3. **Uvicorn** — ASGI server for the health-check endpoint

### Data Flow (ASCII)

```
Telegram Channel (source)
        │ MTProto push event
        ▼
TelegramClient (Telethon, per-user)
        │ NewMessage event
        ▼
_process_message()
        │ match chat.id → id_to_feeds
        │ mark source as read
        ▼
    [media group?]──yes──▶ buffer 1.0s ──▶ _forward_media_group()
        │no                                      │
        ▼                                        │
_process_single()                                │
        │ ad_filter.is_ad()                      │
        │ skip if ad                             │
        ▼                                        ▼
_forward_message() ◀──────────────────────────────
        │
        ├─── relay path (preferred) ─────────────────────────────────────────┐
        │    Telethon forward_messages(relay_channel, msg, silent=True)       │
        │    → Bot API forwardMessage(from=relay, to=destination)             │
        │    → Telethon delete_messages(relay_channel, [relay_id])            │
        │    Result: unread counter ✅                                         │
        │                                                                    │
        └─── direct path (fallback if relay fails) ──────────────────────────┘
             Telethon forward_messages(destination, msg)
             Result: no unread counter ❌
```

### Write Path (user action)

```
User presses button in bot
→ handle_callback() or handle_message()
→ DB write (aiosqlite)
→ bot.reload_feeds()
→ _setup_handler(resolve_ids=True)  ← rebuilds channel→feed mapping
→ _show(chat_id, ...)               ← delete old message, send new
```

### Read Path (monitoring)

```
On start_monitoring():
  Phase 1: _setup_handler(resolve_ids=False)  ← register handler immediately
  iter_dialogs() → populate Telethon entity cache (~1–2s)
  Phase 2: _setup_handler(resolve_ids=True)   ← rebuild with numeric IDs
  _init_bot_relay()
  _updates_keepalive() task (every 30s GetStateRequest)
```

### Error Handling Strategy

- **Relay failures** → log warning, fall back to direct userbot forward (message not lost)
- **FloodWaitError** → sleep `e.seconds`, then continue (not retry)
- **Per-feed exceptions** → log error, continue processing other feeds (not fatal)
- **Auth exceptions** → surface to user via bot message; bot continues for other users
- **Polling exceptions** → log, sleep 5s, retry (bot never crashes)
- **Keepalive exceptions** → silently swallowed; loop continues

---

## 4. Package / Folder Structure

```
D:\Projects\Telegram_subs\
├── app/
│   ├── __init__.py          # Package marker (empty)
│   ├── main.py              # FastAPI app; lifespan: init_db → start_all → run_polling
│   ├── bot.py               # TelegramBot class: Telethon client + relay + auth + monitoring
│   ├── session_manager.py   # SessionManager: dict[user_id → TelegramBot]; lifecycle
│   ├── bot_commands.py      # Bot API polling loop; all screens; conversation state machine
│   ├── database.py          # aiosqlite CRUD; normalize_channel(); migrations
│   ├── filter.py            # AdFilter.is_ad(): keyword matching
│   └── api.py               # GET /api/status
├── data/                    # Docker volume (./data:/app/data)
│   ├── feeds.db             # SQLite database (all user data)
│   └── telegram_*.session   # Telethon session file per user
├── docs/
│   ├── tech-spec.md         # This file
│   ├── tech-spec.html       # HTML version of this spec
│   └── tech-spec-example.css # Stylesheet for HTML spec
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 5. Data Model

### Feed

Represents one user's filtering configuration: a set of source channels → one destination channel, with optional keyword rules.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| id | int | autoincrement | Primary key |
| name | str | — | User-defined name, displayed in bot |
| destination_channel | str | — | Normalized: `@username` or `-100XXXXXXXXXX` |
| use_ai_filter | bool | False | Reserved; AI filtering removed, always False |
| enabled | bool | True | Paused feeds still stored but skipped in handler |
| user_id | int | 0 | Telegram user ID of owner; isolates feeds per user |
| created_at | str | utcnow() | ISO 8601 timestamp |
| channels | list[str] | [] | Virtual: loaded from `source_channels` table |
| keywords | list[str] | [] | Virtual: loaded from `filter_keywords` table |

### UserSettings

Stores Telegram API credentials per user, entered via `/settings`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| user_id | int | — | PK; Telegram user ID |
| api_id | int | NULL | From my.telegram.org |
| api_hash | str | NULL | From my.telegram.org |

### SourceChannel

Many-to-one with Feed. Stores each monitored source channel.

| Field | Type | Description |
|-------|------|-------------|
| id | int | PK autoincrement |
| feed_id | int | FK → feeds.id ON DELETE CASCADE |
| channel_username | str | Normalized key: `@username` or `-100XXXXXXXXXX` |

### FilterKeyword

Many-to-one with Feed. Stores each ad-detection rule.

| Field | Type | Description |
|-------|------|-------------|
| id | int | PK autoincrement |
| feed_id | int | FK → feeds.id ON DELETE CASCADE |
| keyword | str | Stored lowercase; single word or `word+word` AND-rule |

### ChannelLastSeen (legacy)

| Field | Type | Description |
|-------|------|-------------|
| channel_username | str | PK |
| last_message_id | int | Last processed message ID |
| updated_at | str | ISO 8601 |

---

## 6. Database / Storage Schema

**File:** `/app/data/feeds.db` (SQLite 3)

```sql
CREATE TABLE feeds (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    destination_channel TEXT NOT NULL,
    use_ai_filter       INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1,
    user_id             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE TABLE source_channels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id          INTEGER NOT NULL,
    channel_username TEXT NOT NULL,
    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
);

CREATE TABLE filter_keywords (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL,
    keyword TEXT NOT NULL,
    FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
);

CREATE TABLE channel_last_seen (
    channel_username TEXT PRIMARY KEY,
    last_message_id  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

CREATE TABLE user_settings (
    user_id  INTEGER PRIMARY KEY,
    api_id   INTEGER,
    api_hash TEXT
);
```

**Migration:** On startup, `init_db()` attempts `ALTER TABLE feeds ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0`. The `except` block silently ignores `OperationalError` if column exists.

**Bulk read pattern:** `get_all_feeds()` fetches feeds, then channels, then keywords in **three queries** (not N+1) using `WHERE feed_id IN (?,?,...)`.

### Channel Format Reference

| Input | Stored as | Example |
|-------|-----------|---------|
| `https://t.me/channelname` | `@channelname` | → `@news` |
| `t.me/channelname` | `@channelname` | → `@durov` |
| `channelname` | `@channelname` | → `@tech` |
| `@channelname` | `@channelname` | → `@channel` |
| `https://t.me/c/1812695632/5` | `-1001812695632` | private channel via post link |
| `https://t.me/c/1812695632/` | `-1001812695632` | private channel |
| `-1001812695632` | `-1001812695632` | already canonical |
| `https://t.me/+AbCdEfGhIjK` | unchanged | invite link |
| `https://t.me/joinchat/HASH` | unchanged | old invite link |

---

## 7. Authentication & First-Launch Setup

### QR Login Flow (recommended)

```
1. User: /settings in bot
2. Bot: shows settings screen (api_id, api_hash, auth status)
3. User: enters api_id via 🔑 button → saves to user_settings
4. User: enters api_hash via 🔑 button → saves to user_settings
5. User: presses 📷 Войти через QR-код
6. Bot: calls bot.start_qr_auth()
     → _ensure_client() lazy-inits TelegramClient
     → client.qr_login() → QRLogin object with .url (tg://login?token=...)
     → generate QR PNG via qrcode library
     → sendPhoto to user with caption + /esc hint
7. Bot: creates background task _qr_auth_waiter(chat_id, bot, sm)
8. Bot: sets state auth_qr_wait (stores task ref for cancellation)
9. User: opens Telegram → Settings → Devices → Scan QR code
10. Telegram: approves session server-side (user's device talks to Telegram)
11. _qr_auth_waiter: await qr.wait(timeout=120s)
    → SUCCESS: clear state, call bot.start_monitoring(), show feed list
    → TIMEOUT: notify user "QR-код истёк"
    → SessionPasswordNeededError: transition to auth_2fa_qr state
12. [If 2FA] User types password → bot.confirm_2fa(password) → start_monitoring()
```

### Phone Login Flow (alternative)

```
1–4: Same as above (api_id, api_hash entry)
5. User: presses 📱 Войти по телефону
6. Bot: prompts phone with +CC format hint
7. User: enters +49XXXXXXXXX
8. Bot: await bot.start_auth(phone) → client.send_code_request(phone)
        → stores phone_code_hash in state ctx
9. User: gets code in Telegram app (or SMS)
10. User: enters 5-digit code
11. Bot: await bot.confirm_auth(phone, code, hash)
    → SUCCESS: start_monitoring()
    → SessionPasswordNeededError: prompt 2FA
    → PhoneCodeInvalidError / PhoneCodeExpiredError: show error
12. [If 2FA] Same as QR flow step 12
```

**Session persistence:** After auth, Telethon writes `/app/data/telegram_{user_id}.session`. On container restart, `start_all()` reloads credentials from DB and reconnects using the existing session file — no re-auth required.

---

## 8. Monitoring Engine

### Two-Phase Handler Setup

Called in `start_monitoring()` and on `reload_feeds()`:

```
Phase 1 – resolve_ids=False (called immediately on start):
  - Read enabled feeds from DB (get_all_feeds(user_id=self.user_id))
  - Build channel_to_feeds: dict[str, list[feed]]  (@username → feeds)
  - Skip @username → ID resolution (entity cache empty)
  - Remove previous NewMessage handler if exists
  - Register new handler with: channel_to_feeds, id_to_feeds={}
  - Log: "Мониторинг N каналов"

  iter_dialogs() loop — populates Telethon entity cache
  Log: "Загружено диалогов: {count}"

Phase 2 – resolve_ids=True (called after dialogs loaded):
  - Same feed read + channel_to_feeds build
  - For each channel key:
      try: entity = await client.get_input_entity(key)
           id_to_feeds[entity.channel_id] = feeds_list
      except Exception: log warning, continue
  - Remove Phase 1 handler
  - Register handler with full id_to_feeds
  - Log: "Резолвлено каналов по ID: X из Y"
```

### Message Handler (`_process_message`)

```python
async def _process_message(event, channel_to_feeds, id_to_feeds):
    chat = event.chat
    chat_id = chat.id  # positive numeric ID from Telethon

    # Fast path: numeric ID match
    if chat_id in id_to_feeds:
        feeds_for_channel = id_to_feeds[chat_id]
        source_name = getattr(chat, "title", str(chat_id))
    else:
        # Fallback: string key match
        username = getattr(chat, "username", None)
        key = f"@{username}" if username else f"-100{chat_id}"
        if key not in channel_to_feeds and f"-100{chat_id}" not in channel_to_feeds:
            logger.debug("Не в фидах: ...")
            return
        feeds_for_channel = channel_to_feeds.get(key) or channel_to_feeds.get(...)

    # Mark source as read (best-effort)
    try: await client.send_read_acknowledge(chat, ...)
    except: pass

    # Route by message type
    if event.message.grouped_id:
        await _buffer_media_group(message, source_name, feeds_for_channel)
    else:
        await _process_single(message, source_name, feeds_for_channel)
```

### Keepalive Loop

```
Every 30 seconds (while _monitoring=True):
    await client(functions.updates.GetStateRequest())
    # Forces Telegram to flush any missed updates
    # Without this: 10–15 min delivery delay observed on channels
```

### Media Group Buffering

```
On each part of an album (message.grouped_id == gid):

  _media_groups[gid] = {
    messages: [msg1, msg2, ...],  # appended each time
    source_name: str,
    feeds: [feed_dict, ...],
  }

  Cancel _media_group_tasks[gid] if exists
  Create new asyncio.Task(_process_media_group_later(gid))

After sleep(1.0):
  group = _media_groups.pop(gid)
  messages = sorted(group.messages, key=lambda m: m.id)
  Extract text from first message that has text
  For each feed: filter → forward as album via relay
```

---

## 9. Relay & Unread Counters

### Why Relay

Telegram only marks messages as unread (and increments badge counter) when they arrive from a **bot token** (Bot API sender). Direct userbot forwards arrive without unread status. The Relay Buffer channel bridges this gap.

### Relay Channel Setup

| Requirement | Who | How |
|-------------|-----|-----|
| Bot token is admin | Service owner | Manually in channel settings, with "Add Admins" right |
| Bot invite link | Service owner | Set in `RELAY_INVITE_LINK` env var |
| Each userbot is admin | Automatic | `_init_bot_relay()` on `start_monitoring()` |

### Relay Init (`_init_bot_relay`)

```
1. Check BOT_TOKEN set; log warning + return if not
2. Check RELAY_CHANNEL_ID set; log warning + return if not
3. If RELAY_INVITE_LINK set:
     await client.join_channel(RELAY_INVITE_LINK)
     except: pass  # already member or other — ignore
4. entity = await _resolve_entity_telethon(RELAY_CHANNEL_ID)
5. relay_chat_id = -(1000000000000 + entity.id)  # canonical Bot API format
6. POST /promoteChatMember {
     chat_id: relay_chat_id,
     user_id: _userbot_id,
     can_post_messages: True, can_edit_messages: True, can_delete_messages: True
   }
   if "owner" in error_description → ignore (already has all rights)
7. Log: "Relay готов: канал {id} «{title}»"
```

### Single Message Forward

```
# Step 1: Userbot → Relay
relay_messages = await client.forward_messages(relay_entity, [msg], silent=True)
relay_id = relay_messages[0].id

# Step 2: Bot API → Destination
POST /forwardMessage {
  chat_id: destination_chat_id,
  from_chat_id: relay_chat_id,
  message_id: relay_id
}

# Step 3: Cleanup
await client.delete_messages(relay_entity, [relay_id])
```

### Album Forward

```
# Step 1: Userbot → Relay (all messages)
relay_messages = await client.forward_messages(relay_entity, messages, silent=True)
relay_ids = [m.id for m in relay_messages]

# Step 2: Bot API → Destination (all at once)
POST /forwardMessages {
  chat_id: destination_chat_id,
  from_chat_id: relay_chat_id,
  message_ids: relay_ids
}

# Step 3: Cleanup
await client.delete_messages(relay_entity, relay_ids)
```

### Fallback

Any exception in the relay path triggers direct userbot forward:
```python
await client.forward_messages(destination_entity, messages)
# No unread counter, but message delivered
```

---

## 10. Filtering Engine

**File:** `app/filter.py` — class `AdFilter`, singleton `ad_filter`

### Algorithm

```
is_ad(text, keywords, use_ai=False) → (bool, str):
  if not text or not text.strip():
    return (False, "")          # Empty/media-only messages always pass

  text_lower = text.lower()

  for rule in keywords:
    parts = [p.strip() for p in rule.split("+") if p.strip()]
    # "скидка+купить" → ["скидка", "купить"]
    # "реклама" → ["реклама"]

    if all(part in text_lower for part in parts):
      label = " + ".join(parts) if len(parts) > 1 else parts[0]
      return (True, f"Найдено правило: '{label}'")

  return (False, "")
```

### Rule Syntax

| Rule | Logic | Matches |
|------|-------|---------|
| `реклама` | single word | any message containing "реклама" (any case) |
| `скидка+купить` | AND | message must contain BOTH "скидка" AND "купить" |
| `промокод+10%` | AND | message must contain "промокод" AND "10%" |

### Limitations

- No wildcard / regex support
- No OR logic between rules (each rule is independent OR; within a rule it's AND)
- No word boundary check (substring match: "скидочный" matches rule "скидок")
- `use_ai` parameter accepted but ignored; AI filtering removed

---

## 11. Bot Commands UI & State Machine

### Screens

| Screen | Trigger | Shows | Buttons |
|--------|---------|-------|---------|
| Feed list | `/start`, `/feedlist`, `back:feeds` | All user feeds with channel count; monitoring status | Feed buttons, ➕ Add, ▶️/⏹ Start/Stop, ⚙️ Settings |
| Feed detail | `feed:{id}` | Name, destination, channel count, filter count, enabled status | 📺 Destination, 📡 Channels, 🔍 Filters, ✅/❌ Toggle, ✏️ Rename, 🗑️ Delete, ◀️ Back |
| Channels | `channels:{id}` | Source channel list | ➕ Add, ➖ Delete, ◀️ Back |
| Channel delete | `ch_del_list:{id}` | Channel list, each tappable | [channel buttons], ◀️ Back |
| Filters | `filters:{id}` | Keyword list | ➕ Add, ➖ Delete, ◀️ Back |
| Filter delete | `kw_del_list:{id}` | Keyword list, each tappable | [keyword buttons], ◀️ Back |
| Delete confirm | `delete:{id}` | "Delete feed X?" | ✅ Yes (`delete_ok:{id}`), ❌ No |
| Settings | `/settings`, `settings` | api_id, api_hash (masked \*\*\*), auth status | 🔑 Enter api_id, 🔑 Enter api_hash, 📷 QR login, 📱 Phone login, ◀️ Back |

### State Machine

```
_states: dict[int, {"step": str, "ctx": dict}]
```

| Step | Waiting for | Success action | Error action |
|------|-------------|----------------|--------------|
| `set_api_id` | Integer string | save_user_settings, show_settings | "Введите только цифры" |
| `set_api_hash` | String ≥ 10 chars | save_user_settings, show_settings | "api_hash слишком короткий" |
| `auth_phone` | String starting with `+` | start_auth(), → `auth_code` | show error |
| `auth_code` | Digits | confirm_auth(), start_monitoring | → `auth_2fa` on 2FA |
| `auth_2fa` | Password string | confirm_auth(password=...) | "Неверный пароль" |
| `auth_qr_wait` | (background task) | automatic on QR scan | timeout message |
| `auth_2fa_qr` | Password string | confirm_2fa(password) | "Неверный пароль" |
| `new_feed_name` | Non-empty string | create_feed(), show_feed | — |
| `rename_feed` | Non-empty string | update_feed(name=...) | — |
| `set_dest` | Forwarded message | update_feed(destination=...), reload | "Перешлите сообщение из канала" |
| `add_channel` | Forwarded message | add_channel(), reload | "Перешлите сообщение из канала" |
| `add_keyword` | Non-empty string | add_keyword() | — |

### Commands

| Command | Action |
|---------|--------|
| `/start`, `/feedlist` | Show feed list |
| `/settings` | Show settings screen |
| `/help` | Send static 8-step setup guide |
| `/esc` | Cancel active state; route back; cancel QR task if pending |

### Cancel Routing (`/esc`)

```
step in (set_api_id, set_api_hash, auth_phone, auth_code, auth_2fa, auth_2fa_qr):
  → show_settings()

step = auth_qr_wait:
  → task.cancel()
  → show_settings()

step in (new_feed_name,):
  → show_feed_list()

step in (rename_feed, set_dest, toggle, delete):
  → show_feed(feed_id from ctx)

step in (add_channel, ch_del_list):
  → show_channels(feed_id from ctx)

step in (add_keyword, kw_del_list):
  → show_filters(feed_id from ctx)
```

### Send-and-Delete Pattern

All screen updates use `_show(chat_id, msg_id, text, markup)`:

```
1. if msg_id: await bot_api.deleteMessage(chat_id, msg_id)
2. response = await bot_api.sendMessage(chat_id, text, reply_markup=markup, parse_mode="HTML")
3. return new msg_id  (stored for next _show call)
```

---

## 12. Key Algorithms

### Channel Normalization

```
normalize_channel(channel: str) -> str:
  ch = channel.strip()

  if "t.me/+" in ch or "joinchat" in ch:
    return ch  # invite link — keep as-is

  if "t.me/c/" in ch:
    # Private channel: https://t.me/c/1234567890/5
    parts = ch.split("t.me/c/")[-1].strip("/").split("/")
    channel_id = parts[0]
    if channel_id.isdigit():
      return f"-100{channel_id}"
    return ch

  if "t.me/" in ch:
    ch = ch.split("t.me/")[-1].strip("/")

  if ch.lstrip("@").lstrip("-").isdigit():
    return ch.lstrip("@")

  if ch and not ch.startswith("@"):
    ch = "@" + ch

  return ch
```

### get_all_feeds Bulk Load (no N+1)

```
1. SELECT all feeds [WHERE user_id=?]  → feed_map: dict[feed_id → feed_dict]
2. extract feed_ids list
3. SELECT source_channels WHERE feed_id IN (?, ?, ...)
   → append each row to feed_map[row.feed_id]["channels"]
4. SELECT filter_keywords WHERE feed_id IN (?, ?, ...)
   → append each row to feed_map[row.feed_id]["keywords"]
5. return list(feed_map.values())
Total: always 3 queries regardless of feed count
```

### Bot API Long-Poll Loop

```
offset = 0
while True:
  try:
    response = POST /getUpdates {offset: offset, timeout: 30, limit: 100}
    # http timeout: 35s (30s poll + 5s buffer)
    
    for update in response.result:
      offset = update.update_id + 1
      try:
        if update.message: await handle_message(chat_id, message, sm)
        if update.callback_query: await handle_callback(callback, sm)
      except Exception as e:
        logger.error(f"Ошибка обработки update {id}: {e}")
  except asyncio.CancelledError:
    logger.error("Bot commands polling остановлен")
    return
  except Exception as e:
    logger.warning(f"Polling ошибка: {e}")
    await asyncio.sleep(5)  # backoff
```

### Relay Numeric ID Calculation

```
# Telethon returns entity.id as positive (e.g., 1234567890)
# Bot API and Telegram expect full format: -100{entity.id}

if RELAY_CHANNEL_RAW.startswith('-100'):
  relay_chat_id = int(RELAY_CHANNEL_RAW)
else:
  full = await client.get_entity(entity)
  relay_chat_id = -(1000000000000 + full.id)
```

---

## 13. All Numeric Constants

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| Media group delay | 1.0 s | `bot.py` `_process_media_group_later` | Wait for all album parts from Telegram |
| Keepalive interval | 30 s | `bot.py` `_updates_keepalive` | GetStateRequest interval |
| QR auth timeout | 120 s | `bot.py` `wait_for_qr_scan` | Max wait for QR scan |
| HTTP timeout (general) | 10.0 s | `bot.py` `_api()`, auth helpers | Standard Bot API calls |
| HTTP timeout (relay forward) | 15.0 s | `bot.py` relay path | Slower relay forward operation |
| HTTP timeout (polling) | 35.0 s | `bot_commands.py` `run_polling` | 30s long-poll + 5s buffer |
| Polling timeout param | 30 s | `bot_commands.py` `getUpdates` | Telegram long-poll window |
| Polling error backoff | 5 s | `bot_commands.py` | Sleep after exception |
| Host port | 8001 | `docker-compose.yml` | External HTTP port |
| Container port | 8000 | `Dockerfile`, `docker-compose.yml` | Internal Uvicorn port |
| Health check interval | 30 s | `docker-compose.yml` | Docker health probe interval |
| Health check timeout | 10 s | `docker-compose.yml` | Health probe timeout |
| Health check retries | 3 | `docker-compose.yml` | Retries before unhealthy |
| Health check start period | 10 s | `docker-compose.yml` | Grace period on start |

---

## 14. Environment Variables

| Variable | Required | Default | Used in | Effect if missing |
|----------|----------|---------|---------|-------------------|
| `TELEGRAM_BOT_TOKEN` | Yes | `""` | `bot.py` line 37 | Relay disabled; no bot commands |
| `RELAY_CHANNEL_ID` | Recommended | `""` | `bot.py` line 43 | Relay disabled; no unread counters |
| `RELAY_INVITE_LINK` | Recommended | `""` | `bot.py` line 44 | Userbot won't auto-join relay channel |

---

## 15. Logging Reference

**Format:** `%(asctime)s [%(levelname)s] %(name)s: %(message)s`  
**Suppressed:** `httpx` (→ WARNING), `uvicorn` (→ WARNING), `uvicorn.access` (→ WARNING)

| Level | Sample message | Trigger |
|-------|----------------|---------|
| INFO | Запуск TG Feed Filter... | App startup |
| INFO | База данных инициализирована | DB init done |
| INFO | Нет сохранённых сессий пользователей | No users with credentials in DB |
| INFO | Мониторинг N каналов | Handler registered |
| INFO | Загрузка диалогов... | iter_dialogs start |
| INFO | Загружено диалогов: {N} | iter_dialogs complete |
| INFO | Резолвлено каналов по ID: X из Y | Phase 2 handler setup |
| INFO | Relay готов: канал {id} «{name}» | Relay init success |
| INFO | Мониторинг запущен (user={id}) | start_monitoring complete |
| INFO | Мониторинг остановлен | stop_monitoring called |
| INFO | [{feed}] Реклама из {src}: {reason} | Message filtered |
| INFO | [{feed}] Переслано из {src} | Message forwarded |
| INFO | [{feed}] Переслан альбом (N шт.) | Album forwarded |
| DEBUG | Не в фидах: chat.id=... | Message from non-monitored channel |
| WARNING | BOT_TOKEN не задан | Missing BOT_TOKEN env var |
| WARNING | RELAY_CHANNEL_ID не задан | Missing relay config |
| WARNING | FloodWait: ждём Xs | Telegram rate limit |
| WARNING | Relay: не удалось назначить права: ... | promoteChatMember failed |
| WARNING | Relay не удался: {e} | Exception in relay forward |
| WARNING | Polling ошибка: {e} | Exception in polling loop |
| ERROR | Критическая ошибка в обработчике: {e} | Exception in _process_message |
| ERROR | Ошибка фида '{name}': {e} | Exception processing single message |

---

## 16. Setup & Deployment (First-Time Developer Guide)

### Prerequisites

- Docker + Docker Compose on Linux VPS
- Telegram account for testing
- API credentials from [my.telegram.org](https://my.telegram.org) → API development tools

### Relay Buffer Channel Setup

1. Create a new private Telegram channel (e.g., "PureFeed Relay")
2. Add the bot as admin: Settings → Administrators → Add Admin
3. Give the bot **all admin rights** including "Add Admins"
4. Copy the channel ID (forward a message to @userinfobot or check channel link)
5. Create a permanent invite link: Administrators → Invite Links → Create Link (no expiry, no limit)

### Environment Setup

```bash
git clone git@github.com:JuliaSivridi/PureFeed.git
cd PureFeed
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=1234567890:AAbbCCddEEffGGhhIIjjKKllMMnnOOppQQ
RELAY_CHANNEL_ID=-1001234567890
RELAY_INVITE_LINK=https://t.me/+AbCdEfGhIjKlMnOp
```

### Start

```bash
mkdir -p data
chmod 777 data/           # Docker container runs as root; data dir must be writable
docker compose up -d
docker compose logs -f    # Verify: "База данных инициализирована", "polling запущен"
```

### First User Setup (in Telegram)

1. Find the bot → `/settings`
2. Enter `api_id` from my.telegram.org
3. Enter `api_hash` from my.telegram.org
4. Press **📷 Войти через QR-код**
5. Open Telegram → Settings → Devices → Scan QR
6. After auth: `/feedlist` → create feeds and add channels
7. Press **▶️ Старт сервис**

### Useful Commands

```bash
docker compose logs -f          # Stream logs
docker compose restart          # Restart without rebuild
docker compose up -d --build    # Rebuild after code changes
docker compose down             # Stop
docker compose exec app sqlite3 /app/data/feeds.db ".tables"  # Inspect DB
```
