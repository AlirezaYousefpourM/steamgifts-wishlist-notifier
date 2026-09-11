from __future__ import annotations

import logging
import random
import re
import signal
from datetime import datetime, time, timedelta
from typing import Callable

from .config import Config
from .state import State
from .steamgifts import EntryError, Giveaway, LoginExpiredError, SteamGiftsClient
from .telegram import EntryCallback, TelegramClient, TelegramError


class Redactor:
    def __init__(self, secrets: list[str]):
        self.secrets = sorted((secret for secret in secrets if secret), key=len, reverse=True)

    def __call__(self, value: object) -> str:
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)(PHPSESSID|token|cookie|xsrf_token)=([^\s;&]+)", r"\1=[redacted]", text)
        return text[:3500]


def in_quiet_hours(value: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= value < end
    return value >= start or value < end


def next_quiet_end(now: datetime, start: time, end: time) -> datetime:
    candidate = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if start < end:
        if now.time() >= end:
            candidate += timedelta(days=1)
    elif now.time() >= start:
        candidate += timedelta(days=1)
    return candidate


class Application:
    def __init__(
        self,
        config: Config,
        state: State,
        steamgifts: SteamGiftsClient,
        telegram: TelegramClient,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ):
        self.config = config
        self.state = state
        self.steamgifts = steamgifts
        self.telegram = telegram
        self.random_uniform = random_uniform
        self.redact = Redactor([config.sg_cookie, config.telegram_bot_token])
        self.running = True
        self.logger = logging.getLogger("sgwatcher")

    def enabled(self, category: str) -> bool:
        return bool(getattr(self.config.notifications, category))

    def queue(self, category: str, body: str, giveaway_code: str | None = None) -> None:
        if self.enabled(category):
            self.state.enqueue(category, body, giveaway_code)

    def flush(self) -> None:
        delivered_any = False
        for message in self.state.pending():
            try:
                self.telegram.send(message.body, message.giveaway_code)
            except TelegramError as exc:
                self.state.attempted(message.id)
                failure = self.redact(exc)
                self.logger.error("Telegram delivery failed: %s", failure)
                if self.enabled("errors"):
                    self.state.record_delivery_failure(f"Error: Telegram delivery failed: {failure}")
                break
            self.state.delivered(message.id)
            delivered_any = True
        if delivered_any:
            self.state.release_delivery_failures()

    def error(self, exc: BaseException, context: str) -> None:
        detail = self.redact(exc)
        self.logger.error("%s: %s", context, detail)
        self.queue("errors", f"Error during {context}: {detail}")

    def giveaway_message(self, giveaway: Giveaway) -> str:
        price = f"{giveaway.points}P" if giveaway.points is not None else "points unknown"
        return f"Wishlist giveaway found\n{giveaway.title}\n{price}\n{giveaway.url}"

    def check_wishlist(self) -> tuple[Giveaway, ...]:
        try:
            all_giveaways = self.steamgifts.wishlist(self.config.max_pages)
            new = self.state.discover(all_giveaways)
            giveaways = tuple(giveaway for giveaway in all_giveaways if not giveaway.entered)
            for giveaway in (item for item in new if not item.entered):
                self.queue("giveaway_found", self.giveaway_message(giveaway), giveaway.code)
            if not giveaways:
                self.queue("nothing_to_enter", "No wishlist giveaways are currently available to enter.")
            if self.state.get("poll_failed") == "1":
                self.queue("recovered", "SteamGifts connectivity and parsing recovered.")
            self.state.set("poll_failed", "0")
            return giveaways
        except LoginExpiredError as exc:
            self.queue("login_expired", self.redact(exc))
            self.state.set("poll_failed", "1")
        except Exception as exc:
            self.error(exc, "SteamGifts wishlist check")
            self.state.set("poll_failed", "1")
        return ()

    def handle_entry(self, callback: EntryCallback) -> None:
        if not self.telegram.authorized(callback):
            self.safe_answer(callback.query_id, "Not authorized", alert=True)
            return
        giveaway = self.state.giveaway(callback.code)
        if giveaway is None:
            self.safe_answer(callback.query_id, "Unknown or expired giveaway", alert=True)
            return
        if giveaway.entered:
            self.safe_answer(callback.query_id, "Already entered", alert=True)
            return
        self.safe_answer(callback.query_id, "Submitting this entry…")
        try:
            result = self.steamgifts.enter(callback.code)
            self.state.mark_entered(callback.code)
            suffix = f"\nRemaining points: {result.points}" if result.points is not None else ""
            self.queue("entered", f"Entered giveaway\n{giveaway.title}{suffix}\n{giveaway.url}")
        except EntryError as exc:
            if exc.already_entered:
                self.state.mark_entered(callback.code)
                self.queue("entered", f"Entry already confirmed\n{giveaway.title}\n{giveaway.url}")
            else:
                category = "insufficient_points" if exc.insufficient_points else "errors"
                self.queue(category, f"Could not enter {giveaway.title}: {self.redact(exc)}\n{giveaway.url}")
        except LoginExpiredError as exc:
            self.queue("login_expired", self.redact(exc))
        except Exception as exc:
            self.error(exc, f"entry for {giveaway.title}")
        self.flush()

    def safe_answer(self, query_id: str, text: str, alert: bool = False) -> None:
        try:
            self.telegram.answer(query_id, text, alert=alert)
        except TelegramError as exc:
            self.error(exc, "Telegram callback response")

    def process_updates(self, timeout: int) -> None:
        offset = int(self.state.get("telegram_offset") or "0")
        try:
            updates = self.telegram.updates(offset, timeout)
        except TelegramError as exc:
            self.error(exc, "Telegram update polling")
            return
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.state.set("telegram_offset", str(update_id + 1))
            callback = self.telegram.callback(update)
            if callback is not None:
                try:
                    self.handle_entry(callback)
                except Exception as exc:
                    self.error(exc, "Telegram callback processing")

    def next_interval(self) -> timedelta:
        base = self.config.poll_base_minutes
        jitter = self.config.poll_jitter_minutes
        minutes = self.random_uniform(max(1, base - jitter), base + jitter)
        return timedelta(minutes=minutes)

    def stop(self, *_: object) -> None:
        self.running = False

    def run_once(self) -> None:
        self.check_wishlist()
        self.flush()

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.queue("started", "SteamGifts wishlist assistant started.")
        self.flush()
        next_poll = datetime.now(self.config.timezone)
        while self.running:
            now = datetime.now(self.config.timezone)
            if now >= next_poll:
                if in_quiet_hours(now.time(), self.config.quiet_start, self.config.quiet_end):
                    next_poll = next_quiet_end(now, self.config.quiet_start, self.config.quiet_end)
                else:
                    self.run_once()
                    next_poll = now + self.next_interval()
            seconds = max(1, min(25, int((next_poll - datetime.now(self.config.timezone)).total_seconds())))
            self.process_updates(seconds)
            self.flush()


class MultiApplication:
    def __init__(self, applications: list[Application]):
        if not applications:
            raise ValueError("At least one application is required")
        self.applications = applications
        first = applications[0]
        self.telegram = TelegramClient(first.config.telegram_bot_token, 0, 0, first.config.http_timeout_seconds)
        self.offset_state = first.state
        self.running = True

    def stop(self, *_: object) -> None:
        self.running = False

    def process_updates(self, timeout: int) -> None:
        offset = int(self.offset_state.get("telegram_offset") or "0")
        try:
            updates = self.telegram.updates(offset, timeout)
        except TelegramError as exc:
            for application in self.applications:
                application.error(exc, "Telegram update polling")
            return
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset_state.set("telegram_offset", str(update_id + 1))
            callback = self.telegram.callback(update)
            if callback is None:
                continue
            authorized = [item for item in self.applications if item.telegram.authorized(callback)]
            application = authorized[0] if len(authorized) == 1 else None
            if application is None:
                try:
                    self.telegram.answer(callback.query_id, "Unknown or expired user", alert=True)
                except TelegramError:
                    pass
                continue
            try:
                application.handle_entry(callback)
            except Exception as exc:
                application.error(exc, "Telegram callback processing")

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        for application in self.applications:
            application.queue("started", "SteamGifts wishlist assistant started.")
            application.flush()
        next_polls = {
            application.config.telegram_user_id: datetime.now(application.config.timezone)
            for application in self.applications
        }
        while self.running:
            for application in self.applications:
                config = application.config
                now = datetime.now(config.timezone)
                if now < next_polls[config.telegram_user_id]:
                    continue
                if in_quiet_hours(now.time(), config.quiet_start, config.quiet_end):
                    next_polls[config.telegram_user_id] = next_quiet_end(now, config.quiet_start, config.quiet_end)
                else:
                    application.run_once()
                    next_polls[config.telegram_user_id] = now + application.next_interval()
            seconds = max(
                1,
                min(
                    25,
                    int(
                        min(
                            (next_polls[item.config.telegram_user_id] - datetime.now(item.config.timezone)).total_seconds()
                            for item in self.applications
                        )
                    ),
                ),
            )
            self.process_updates(seconds)
            for application in self.applications:
                application.flush()
