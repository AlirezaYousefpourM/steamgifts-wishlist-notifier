from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    pass


@dataclass(frozen=True)
class EntryCallback:
    query_id: str
    user_id: int
    chat_id: int | None
    code: str


class TelegramClient:
    def __init__(self, token: str, chat_id: int, user_id: int, timeout: float):
        self.token = token
        self.chat_id = chat_id
        self.user_id = user_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        request = Request(
            self.base_url + method,
            data=urlencode({key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in payload.items()}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TelegramError(f"Telegram returned HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(f"Could not reach Telegram: {reason}") from exc
        if not result.get("ok"):
            raise TelegramError(f"Telegram rejected the request: {result.get('description', 'unknown error')}")
        return result.get("result")

    def send(self, text: str, giveaway_code: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if giveaway_code:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Enter this giveaway", "callback_data": f"enter:{giveaway_code}"}]]
            }
        self._call("sendMessage", payload)

    def updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        result = self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query"]},
        )
        return result if isinstance(result, list) else []

    def identities(self) -> list[tuple[int, int, str]]:
        result = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        identities: list[tuple[int, int, str]] = []
        for update in result if isinstance(result, list) else []:
            message = update.get("message") or {}
            sender = message.get("from") or {}
            chat = message.get("chat") or {}
            try:
                identities.append((int(sender["id"]), int(chat["id"]), str(chat.get("type", "unknown"))))
            except (KeyError, TypeError, ValueError):
                continue
        return identities

    def callback(self, update: dict[str, Any]) -> EntryCallback | None:
        query = update.get("callback_query")
        if not isinstance(query, dict):
            return None
        data = query.get("data", "")
        if not isinstance(data, str) or not data.startswith("enter:"):
            return None
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        sender = query.get("from") or {}
        try:
            code = data.split(":", 1)[1]
            if not code:
                return None
            return EntryCallback(
                query_id=str(query["id"]),
                user_id=int(sender["id"]),
                chat_id=int(chat["id"]) if "id" in chat else None,
                code=code,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def authorized(self, callback: EntryCallback) -> bool:
        return callback.user_id == self.user_id and callback.chat_id == self.chat_id

    def answer(self, query_id: str, text: str, alert: bool = False) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})
