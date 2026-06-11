# PureFeed

[![Telegram Bot](https://img.shields.io/badge/@pure__feed__bot-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/pure_feed_bot)

![Python](https://img.shields.io/badge/Python_3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)

Telegram-бот для агрегации каналов с фильтрацией рекламы. Подключается к Telegram-аккаунту пользователя через userbot (Telethon + MTProto), мониторит подписанные каналы, фильтрует сообщения по ключевым словам и пересылает чистый контент в приватные каналы назначения. Управление — через Telegram-бота.

Мультипользовательский: каждый пользователь авторизует свой аккаунт и управляет своими фидами независимо.

**Фиды:**
- 📡 **Обычный фид** — собирает каналы-источники и пересылает сообщения в канал назначения
- 🔇 **Тихое чтение** — встроенный фид без пересылки: сообщения из добавленных каналов автоматически помечаются прочитанными, счётчик непрочитанных гасится

**Фильтрация** (для обычных фидов):
- ⬛ **Чёрный список** — сообщения с этими словами/фразами блокируются
- ⬜ **Белый список** — исключения из чёрного списка (если пост заблокирован, но содержит слово из белого — всё равно пропускается)
- Правила через `+`: `скидка+купить` — оба слова должны присутствовать

---

## Требования

- Сервер или VPS с **Docker** и **Docker Compose**
- Telegram-бот (создать у [@BotFather](https://t.me/BotFather))
- Приватный канал **Relay Buffer** (технический канал для счётчика непрочитанных)

---

## Установка

### 1. Клонируйте репозиторий

```bash
git clone git@github.com:JuliaSivridi/PureFeed.git
cd PureFeed
```

### 2. Создайте конфигурацию

```bash
cp .env.example .env
nano .env
```

```env
TELEGRAM_BOT_TOKEN=        # токен от @BotFather
RELAY_CHANNEL_ID=-100...   # ID вашего Relay Buffer канала
RELAY_INVITE_LINK=https://t.me/+...  # ссылка-приглашение в Relay Buffer
```

**Relay Buffer** — приватный технический канал, через который проходят пересылки (нужен для счётчика непрочитанных в каналах назначения). Создайте его, добавьте бота как администратора с правом **«Добавлять администраторов»** и скопируйте invite-ссылку.

### 3. Создайте папку для данных

```bash
mkdir -p data
```

### 4. Запустите

```bash
docker compose up -d --build
docker compose logs -f
```

---

## Использование

Найдите своего бота в Telegram и отправьте `/help` — там есть полная пошаговая инструкция.

### Коротко

1. `/settings` → введите `api_id` и `api_hash` с [my.telegram.org](https://my.telegram.org) → войдите через QR-код
2. `/feedlist` → создайте фид → укажите канал назначения → добавьте каналы-источники
3. Для тихого чтения: `/feedlist` → **🔇 Тихое чтение** → добавьте каналы, счётчики которых хотите гасить
4. Запустите мониторинг кнопкой **▶️ Старт сервис**

### Команды бота

| Команда | Действие |
|---------|----------|
| `/feedlist` | 📋 Управление фидами и запуск мониторинга |
| `/settings` | ⚙️ Авторизация userbot |
| `/help` | 📖 Подробная справка |
| `/esc` | ✖️ Отмена текущего ввода |

---

## Управление сервисом

```bash
docker compose logs -f        # логи
docker compose restart        # перезапуск
docker compose down           # остановка
docker compose up -d --build  # пересборка после изменений кода
```

---

## Заметки

- **Данные** хранятся в `./data/` — не удаляйте эту папку при обновлениях
- **Сессии** пользователей хранятся как `telegram_{user_id}.session` в `./data/`
- **Relay Buffer** — пользователи добавляются в него автоматически при авторизации; канал виден в списке, но взаимодействие не требуется
- **Безопасность** — не передавайте `.env` третьим лицам

---

## Документация

- **Техническая спецификация:** [`docs/tech-spec.md`](docs/tech-spec.md)

