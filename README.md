# SteamGifts Wishlist Assistant

Checks authenticated SteamGifts wishlist giveaways and sends them privately to Telegram. Pressing a giveaway's Telegram button submits that specific entry.

Python 3.11 or newer is required. It uses only the standard library.

## Configuration

```bash
cp .env.example .env
chmod 600 .env
```

One Telegram bot is shared by every user. Put users in the `USERS` variable as semicolon-separated records. Each record contains `PHPSESSID,TELEGRAM_CHAT_ID,TELEGRAM_USER_ID`:

```dotenv
TELEGRAM_BOT_TOKEN=1234567890:replace-me
USERS="first_session_id,111111111,111111111;second_session_id,222222222,222222222;"
```

The first field may be either the raw session ID or `PHPSESSID=the_session_id`. A trailing semicolon is allowed. Commas and semicolons cannot appear inside a field.

Each Telegram user must start a private conversation with the bot. To discover IDs after they send `/start`:

```bash
python main.py --discover-telegram-ids
```

Each user gets isolated state and cookies under `data/users/<telegram-user-id>/`. Reordering `USERS` does not swap their data. The same Telegram chat or user ID cannot appear twice.

Check the parsed configuration without displaying cookies or the bot token:

```bash
python main.py --check-config
```

Run one check:

```bash
python main.py --once
```

Run continuously:

```bash
python main.py
```

The old single-user `SG_COOKIE`, `TELEGRAM_CHAT_ID`, and `TELEGRAM_USER_ID` variables still work when `USERS` is absent.

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

Stop it without deleting its persistent data:

```bash
docker compose down
```

Keep `.env`, `data/`, and the `sgwatcher-data` Docker volume private.
