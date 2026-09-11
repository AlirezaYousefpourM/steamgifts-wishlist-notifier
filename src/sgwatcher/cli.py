from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .app import Application
from .config import Config, ConfigError, load_env_file
from .state import State
from .steamgifts import SteamGiftsClient, SteamGiftsError
from .telegram import TelegramClient, TelegramError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="sg-watcher")
    result.add_argument("--env-file", default=".env", type=Path)
    result.add_argument("--once", action="store_true")
    result.add_argument("--check-config", action="store_true")
    result.add_argument("--discover-telegram-ids", action="store_true")
    result.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.discover_telegram_ids:
        try:
            values = load_env_file(args.env_file)
            values.update(os.environ)
            token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                raise ConfigError("Missing required setting: TELEGRAM_BOT_TOKEN")
            telegram = TelegramClient(token, 0, 0, 30)
            identities = telegram.identities()
        except (ConfigError, TelegramError) as exc:
            print(f"Telegram discovery error: {exc}", file=sys.stderr)
            return 2
        if not identities:
            print("No messages found. Send /start to the bot and run this command again.")
            return 1
        for user_id, chat_id, chat_type in identities:
            print(f"TELEGRAM_USER_ID={user_id} TELEGRAM_CHAT_ID={chat_id} CHAT_TYPE={chat_type}")
        return 0
    try:
        config = Config.from_env(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print(config.redacted_summary())
        return 0
    state = State(config.state_path)
    try:
        steamgifts = SteamGiftsClient(
            config.sg_cookie,
            config.cookie_jar_path,
            config.http_timeout_seconds,
            config.user_agent,
        )
        telegram = TelegramClient(
            config.telegram_bot_token,
            config.telegram_chat_id,
            config.telegram_user_id,
            config.http_timeout_seconds,
        )
        application = Application(config, state, steamgifts, telegram)
        application.run_once() if args.once else application.run()
    except SteamGiftsError as exc:
        logging.error("Startup failed: %s", exc)
        return 1
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
