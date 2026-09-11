from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .steamgifts import Giveaway


@dataclass(frozen=True)
class PendingMessage:
    id: int
    category: str
    body: str
    giveaway_code: str | None
    attempts: int


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS giveaways (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                points INTEGER,
                first_seen TEXT NOT NULL,
                entered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                body TEXT NOT NULL,
                giveaway_code TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def discover(self, giveaways: tuple[Giveaway, ...]) -> list[Giveaway]:
        now = datetime.now(UTC).isoformat()
        new: list[Giveaway] = []
        with self.connection:
            for giveaway in giveaways:
                exists = self.connection.execute(
                    "SELECT 1 FROM giveaways WHERE code = ?", (giveaway.code,)
                ).fetchone()
                if exists is None:
                    self.connection.execute(
                        "INSERT INTO giveaways(code, title, url, points, first_seen, entered_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (giveaway.code, giveaway.title, giveaway.url, giveaway.points, now, now if giveaway.entered else None),
                    )
                    new.append(giveaway)
                else:
                    self.connection.execute(
                        "UPDATE giveaways SET title = ?, url = ?, points = ?, entered_at = CASE WHEN ? THEN COALESCE(entered_at, ?) ELSE entered_at END WHERE code = ?",
                        (giveaway.title, giveaway.url, giveaway.points, giveaway.entered, now, giveaway.code),
                    )
        return new

    def giveaway(self, code: str) -> Giveaway | None:
        row = self.connection.execute(
            "SELECT code, title, url, points, entered_at FROM giveaways WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return None
        return Giveaway(row["code"], row["title"], row["url"], row["points"], row["entered_at"] is not None)

    def mark_entered(self, code: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE giveaways SET entered_at = ? WHERE code = ?",
                (datetime.now(UTC).isoformat(), code),
            )

    def enqueue(self, category: str, body: str, giveaway_code: str | None = None) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO outbox(category, body, giveaway_code, created_at) VALUES (?, ?, ?, ?)",
                (category, body, giveaway_code, datetime.now(UTC).isoformat()),
            )
        return int(cursor.lastrowid)

    def pending(self, limit: int = 100) -> list[PendingMessage]:
        rows = self.connection.execute(
            "SELECT id, category, body, giveaway_code, attempts FROM outbox ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [PendingMessage(row["id"], row["category"], row["body"], row["giveaway_code"], row["attempts"]) for row in rows]

    def delivered(self, message_id: int) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM outbox WHERE id = ?", (message_id,))

    def attempted(self, message_id: int) -> None:
        with self.connection:
            self.connection.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (message_id,))

    def record_delivery_failure(self, body: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO delivery_failures(body, created_at) VALUES (?, ?)",
                (body, datetime.now(UTC).isoformat()),
            )

    def release_delivery_failures(self) -> None:
        rows = self.connection.execute("SELECT id, body FROM delivery_failures ORDER BY id").fetchall()
        if not rows:
            return
        with self.connection:
            for row in rows:
                self.connection.execute(
                    "INSERT INTO outbox(category, body, created_at) VALUES ('errors', ?, ?)",
                    (row["body"], datetime.now(UTC).isoformat()),
                )
            self.connection.execute("DELETE FROM delivery_failures")

    def get(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
