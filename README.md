# SteamGifts Wishlist Assistant

Checks authenticated SteamGifts wishlist giveaways and sends them privately to Telegram. Pressing a giveaway's Telegram button submits that specific entry.

Python 3.11 or newer is required. It uses only the standard library.

## Setup

```bash
python3 -m venv .venv
cp .env.example .env
chmod 600 .env
```

Fill in `.env`, then discover your private Telegram IDs:

```bash
.venv/bin/python main.py --discover-telegram-ids
```

Check the configuration:

```bash
.venv/bin/python main.py --check-config
```

Run one check:

```bash
.venv/bin/python main.py --once
```

Run continuously:

```bash
.venv/bin/python main.py
```

## Docker

Build and start it in the background:

```bash
docker compose up -d --build
```

Follow its logs:

```bash
docker compose logs -f
```

Stop it:

```bash
docker compose down
```

The `.env` file is passed to the container at runtime, and application data is kept in the `sgwatcher-data` Docker volume.

Keep `.env`, `data/state.sqlite3`, and `data/steamgifts.cookies` private.
