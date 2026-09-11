from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ConfigError(f"Invalid line {number} in {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            raise ConfigError(f"Missing key on line {number} in {path}")
        values[key] = value
    return values


def parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def parse_time(value: str, name: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must use HH:MM") from exc


@dataclass(frozen=True)
class Notifications:
    giveaway_found: bool
    entered: bool
    nothing_to_enter: bool
    insufficient_points: bool
    login_expired: bool
    started: bool
    recovered: bool
    errors: bool


@dataclass(frozen=True)
class Config:
    sg_cookie: str
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_user_id: int
    timezone: ZoneInfo
    poll_base_minutes: float
    poll_jitter_minutes: float
    quiet_start: time
    quiet_end: time
    max_pages: int
    http_timeout_seconds: float
    state_path: Path
    cookie_jar_path: Path
    notifications: Notifications
    user_agent: str

    @classmethod
    def from_env(cls, env_path: str | Path = ".env", environ: dict[str, str] | None = None) -> Config:
        source = load_env_file(Path(env_path))
        source.update(dict(os.environ if environ is None else environ))
        return cls.from_source(source)

    @classmethod
    def all_from_env(
        cls, env_path: str | Path = ".env", environ: dict[str, str] | None = None
    ) -> tuple[Config, ...]:
        source = load_env_file(Path(env_path))
        source.update(dict(os.environ if environ is None else environ))
        raw_users = source.get("USERS", "").strip()
        if not raw_users:
            return (cls.from_source(source),)
        records = [record.strip() for record in raw_users.split(";") if record.strip()]
        if not records:
            raise ConfigError("USERS does not contain any users")
        configs: list[Config] = []
        chat_ids: set[int] = set()
        user_ids: set[int] = set()
        for slot, record in enumerate(records, 1):
            fields = [field.strip() for field in record.split(",")]
            if len(fields) != 3:
                raise ConfigError(f"USERS entry {slot} must contain PHPSESSID,CHAT_ID,USER_ID")
            session_id, chat_id, user_id = fields
            if session_id.startswith("PHPSESSID="):
                session_id = session_id.split("=", 1)[1]
            if not session_id or any(character in session_id for character in ";, \t\r\n"):
                raise ConfigError(f"USERS entry {slot} has an invalid PHPSESSID")
            try:
                parsed_chat_id = int(chat_id)
                parsed_user_id = int(user_id)
            except ValueError as exc:
                raise ConfigError(f"USERS entry {slot} has an invalid CHAT_ID or USER_ID") from exc
            if parsed_chat_id <= 0 or parsed_user_id <= 0:
                raise ConfigError(f"USERS entry {slot} requires positive CHAT_ID and USER_ID values")
            if parsed_chat_id in chat_ids or parsed_user_id in user_ids:
                raise ConfigError(f"USERS entry {slot} duplicates a Telegram CHAT_ID or USER_ID")
            chat_ids.add(parsed_chat_id)
            user_ids.add(parsed_user_id)
            user_source = dict(source)
            user_source.update(
                {
                    "SG_COOKIE": f"PHPSESSID={session_id}",
                    "TELEGRAM_CHAT_ID": chat_id,
                    "TELEGRAM_USER_ID": user_id,
                    "STATE_PATH": f"data/users/{parsed_user_id}/state.sqlite3",
                    "COOKIE_JAR_PATH": f"data/users/{parsed_user_id}/steamgifts.cookies",
                }
            )
            configs.append(cls.from_source(user_source))
        return tuple(configs)

    @classmethod
    def from_source(cls, source: dict[str, str]) -> Config:
        def required(name: str) -> str:
            value = source.get(name, "").strip()
            if not value:
                raise ConfigError(f"Missing required setting: {name}")
            return value

        def number(name: str, default: str, minimum: float = 0) -> float:
            raw = source.get(name, default)
            try:
                value = float(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be a number") from exc
            if value < minimum:
                raise ConfigError(f"{name} must be at least {minimum}")
            return value

        def integer(name: str, default: str, minimum: int = 1) -> int:
            raw = source.get(name, default)
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer") from exc
            if value < minimum:
                raise ConfigError(f"{name} must be at least {minimum}")
            return value

        def enabled(name: str, default: str) -> bool:
            return parse_bool(source.get(name, default), name)

        try:
            timezone = ZoneInfo(source.get("TIMEZONE", "Asia/Tehran"))
        except ZoneInfoNotFoundError as exc:
            raise ConfigError("TIMEZONE is not recognized") from exc

        chat_id = integer("TELEGRAM_CHAT_ID", required("TELEGRAM_CHAT_ID"))
        user_id = integer("TELEGRAM_USER_ID", required("TELEGRAM_USER_ID"))
        if chat_id <= 0:
            raise ConfigError("TELEGRAM_CHAT_ID must identify a private chat")

        return cls(
            sg_cookie=required("SG_COOKIE"),
            telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=chat_id,
            telegram_user_id=user_id,
            timezone=timezone,
            poll_base_minutes=number("POLL_BASE_MINUTES", "180", 1),
            poll_jitter_minutes=number("POLL_JITTER_MINUTES", "60", 0),
            quiet_start=parse_time(source.get("QUIET_START", "00:00"), "QUIET_START"),
            quiet_end=parse_time(source.get("QUIET_END", "08:00"), "QUIET_END"),
            max_pages=integer("MAX_PAGES", "10"),
            http_timeout_seconds=number("HTTP_TIMEOUT_SECONDS", "30", 1),
            state_path=Path(source.get("STATE_PATH", "data/state.sqlite3")),
            cookie_jar_path=Path(source.get("COOKIE_JAR_PATH", "data/steamgifts.cookies")),
            notifications=Notifications(
                giveaway_found=enabled("NOTIFY_GIVEAWAY_FOUND", "true"),
                entered=enabled("NOTIFY_ENTERED", "true"),
                nothing_to_enter=enabled("NOTIFY_NOTHING_TO_ENTER", "false"),
                insufficient_points=enabled("NOTIFY_INSUFFICIENT_POINTS", "true"),
                login_expired=enabled("NOTIFY_LOGIN_EXPIRED", "true"),
                started=enabled("NOTIFY_STARTED", "true"),
                recovered=enabled("NOTIFY_RECOVERED", "true"),
                errors=enabled("NOTIFY_ERRORS", "true"),
            ),
            user_agent=source.get("USER_AGENT", "SteamGiftsWishlistAssistant/0.1"),
        )

    def redacted_summary(self) -> str:
        return "\n".join(
            [
                f"Telegram chat: {self.telegram_chat_id}",
                f"Telegram user: {self.telegram_user_id}",
                f"Timezone: {self.timezone.key}",
                f"Polling: {self.poll_base_minutes:g} ± {self.poll_jitter_minutes:g} minutes",
                f"Quiet hours: {self.quiet_start:%H:%M}–{self.quiet_end:%H:%M}",
                f"Maximum pages: {self.max_pages}",
                f"State: {self.state_path}",
                f"Cookie jar: {self.cookie_jar_path}",
            ]
        )
