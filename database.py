"""SQLite persistence for the Group Guard Telegram bot."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str = "data/bot.sqlite3") -> None:
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    warn_limit INTEGER NOT NULL DEFAULT 3,
                    warn_action TEXT NOT NULL DEFAULT 'mute',
                    warn_action_2 TEXT NOT NULL DEFAULT 'tban',
                    flood_count INTEGER NOT NULL DEFAULT 5,
                    flood_seconds INTEGER NOT NULL DEFAULT 10,
                    flood_action TEXT NOT NULL DEFAULT 'mute',
                    flood_mute_minutes INTEGER NOT NULL DEFAULT 10,
                    spam_action TEXT NOT NULL DEFAULT 'warn',
                    captcha_enabled INTEGER NOT NULL DEFAULT 1,
                    captcha_minutes INTEGER NOT NULL DEFAULT 5,
                    lockdown INTEGER NOT NULL DEFAULT 0,
                    welcome TEXT NOT NULL DEFAULT '',
                    rules TEXT NOT NULL DEFAULT 'يرجى الالتزام بقوانين المجموعة واحترام الجميع.',
                    log_chat_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    is_activated INTEGER NOT NULL DEFAULT 0,
                    activated_by INTEGER,
                    activated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS roles (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    duration TEXT,
                    reason TEXT,
                    admin_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS words (
                    chat_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, word)
                );
                CREATE TABLE IF NOT EXISTS temporary_restrictions (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    until_ts INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id, kind)
                );
                CREATE TABLE IF NOT EXISTS challenges (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    answer TEXT NOT NULL,
                    expires_ts INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS trusted (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS user_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_history_lookup
                    ON user_history(chat_id, user_id, id DESC);
                CREATE TABLE IF NOT EXISTS auto_replies (
                    chat_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, keyword)
                );
                """
            )

    def is_activated(self, chat_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT is_activated FROM groups WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return bool(row and row["is_activated"])

    def activate_group(self, chat_id: int, user_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO groups(chat_id, is_activated, activated_by, activated_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    is_activated=1, activated_by=excluded.activated_by,
                    activated_at=excluded.activated_at
                """,
                (chat_id, user_id, utc_now()),
            )
            self.settings(chat_id)

    def deactivate_group(self, chat_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE groups SET is_activated=0 WHERE chat_id = ?", (chat_id,)
            )

    def settings(self, chat_id: int) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                now = utc_now()
                self.conn.execute(
                    "INSERT INTO settings (chat_id, created_at) VALUES (?, ?)",
                    (chat_id, now),
                )
                self.conn.commit()
                row = self.conn.execute(
                    "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
                ).fetchone()
            return dict(row)

    def update_settings(self, chat_id: int, **values: Any) -> None:
        allowed = {
            "warn_limit",
            "warn_action",
            "warn_action_2",
            "flood_count",
            "flood_seconds",
            "flood_action",
            "flood_mute_minutes",
            "spam_action",
            "captcha_enabled",
            "captcha_minutes",
            "lockdown",
            "welcome",
            "rules",
            "log_chat_id",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        if not values:
            return
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE settings SET {assignments} WHERE chat_id = ?",
                (*values.values(), chat_id),
            )

    def role(self, chat_id: int, user_id: int) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT role FROM roles WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return row["role"] if row else None

    def set_role(self, chat_id: int, user_id: int, role: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO roles(chat_id, user_id, role, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET role=excluded.role
                """,
                (chat_id, user_id, role, utc_now()),
            )

    def remove_role(self, chat_id: int, user_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM roles WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )

    def all_roles(self, chat_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM roles WHERE chat_id = ? ORDER BY role, user_id",
                    (chat_id,),
                )
            )

    def add_warning(
        self, chat_id: int, user_id: int, reason: str, admin_id: int
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO warnings(chat_id, user_id, reason, admin_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, reason, admin_id, utc_now()),
            )
            return int(cur.lastrowid)

    def active_warning_count(self, chat_id: int, user_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count FROM warnings
                WHERE chat_id = ? AND user_id = ? AND active = 1
                """,
                (chat_id, user_id),
            ).fetchone()
            return int(row["count"])

    def warnings(self, chat_id: int, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM warnings
                    WHERE chat_id = ? AND user_id = ? AND active = 1
                    ORDER BY id DESC LIMIT ?
                    """,
                    (chat_id, user_id, limit),
                )
            )

    def remove_last_warning(self, chat_id: int, user_id: int) -> bool:
        with self._lock, self.conn:
            row = self.conn.execute(
                """
                SELECT id FROM warnings
                WHERE chat_id = ? AND user_id = ? AND active = 1
                ORDER BY id DESC LIMIT 1
                """,
                (chat_id, user_id),
            ).fetchone()
            if not row:
                return False
            self.conn.execute("UPDATE warnings SET active=0 WHERE id=?", (row["id"],))
            return True

    def add_action(
        self,
        chat_id: int,
        user_id: int | None,
        action: str,
        duration: str | None,
        reason: str | None,
        admin_id: int | None,
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO actions(chat_id, user_id, action, duration, reason, admin_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, action, duration, reason, admin_id, utc_now()),
            )

    def actions(
        self, chat_id: int, user_id: int | None = None, limit: int = 15
    ) -> list[sqlite3.Row]:
        with self._lock:
            if user_id is None:
                return list(
                    self.conn.execute(
                        """
                        SELECT * FROM actions WHERE chat_id = ?
                        ORDER BY id DESC LIMIT ?
                        """,
                        (chat_id, limit),
                    )
                )
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM actions WHERE chat_id = ? AND user_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (chat_id, user_id, limit),
                )
            )

    def add_word(self, chat_id: int, word: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO words(chat_id, word, created_at) VALUES (?, ?, ?)",
                (chat_id, word.casefold(), utc_now()),
            )

    def remove_word(self, chat_id: int, word: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM words WHERE chat_id = ? AND word = ?",
                (chat_id, word.casefold()),
            )

    def word_list(self, chat_id: int) -> list[str]:
        with self._lock:
            return [
                row["word"]
                for row in self.conn.execute(
                    "SELECT word FROM words WHERE chat_id = ? ORDER BY word", (chat_id,)
                )
            ]

    def add_temp_restriction(
        self, chat_id: int, user_id: int, kind: str, until_ts: int
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO temporary_restrictions(chat_id, user_id, kind, until_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id, kind) DO UPDATE SET until_ts=excluded.until_ts
                """,
                (chat_id, user_id, kind, until_ts),
            )

    def due_restrictions(self, now_ts: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM temporary_restrictions WHERE until_ts <= ?",
                    (now_ts,),
                )
            )

    def remove_temp_restriction(self, chat_id: int, user_id: int, kind: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                DELETE FROM temporary_restrictions
                WHERE chat_id = ? AND user_id = ? AND kind = ?
                """,
                (chat_id, user_id, kind),
            )

    def temp_restrictions(self, chat_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM temporary_restrictions
                    WHERE chat_id = ? ORDER BY until_ts
                    """,
                    (chat_id,),
                )
            )

    def save_challenge(
        self, chat_id: int, user_id: int, message_id: int, answer: str, expires_ts: int
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO challenges(chat_id, user_id, message_id, answer, expires_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    message_id=excluded.message_id, answer=excluded.answer,
                    expires_ts=excluded.expires_ts
                """,
                (chat_id, user_id, message_id, answer, expires_ts),
            )

    def challenge(self, chat_id: int, user_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM challenges WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()

    def remove_challenge(self, chat_id: int, user_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM challenges WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )

    def trusted(self, chat_id: int, user_id: int) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM trusted WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone() is not None

    def set_trusted(self, chat_id: int, user_id: int, enabled: bool) -> None:
        with self._lock, self.conn:
            if enabled:
                self.conn.execute(
                    "INSERT OR IGNORE INTO trusted(chat_id, user_id, created_at) VALUES (?, ?, ?)",
                    (chat_id, user_id, utc_now()),
                )
            else:
                self.conn.execute(
                    "DELETE FROM trusted WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                )

    def remember_user(self, chat_id: int, user_id: int, username: str | None,
                      first_name: str, last_name: str | None) -> None:
        username = username or None
        first_name = first_name or ""
        last_name = last_name or ""
        with self._lock, self.conn:
            previous = self.conn.execute(
                """
                SELECT username, first_name, last_name FROM user_history
                WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1
                """,
                (chat_id, user_id),
            ).fetchone()
            current = (username, first_name, last_name)
            if previous and (
                previous["username"], previous["first_name"], previous["last_name"]
            ) == current:
                self.conn.execute(
                    "UPDATE user_history SET updated_at=? WHERE chat_id=? AND user_id=? AND id=(SELECT MAX(id) FROM user_history WHERE chat_id=? AND user_id=?)",
                    (utc_now(), chat_id, user_id, chat_id, user_id),
                )
                return
            stamp = utc_now()
            self.conn.execute(
                """
                INSERT INTO user_history(chat_id, user_id, username, first_name,
                    last_name, first_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, username, first_name, last_name, stamp, stamp),
            )

    def previous_user(self, chat_id: int, user_id: int,
                      username: str | None, first_name: str,
                      last_name: str | None) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                """
                SELECT * FROM user_history
                WHERE chat_id = ? AND user_id = ?
                  AND NOT (COALESCE(username, '') = COALESCE(?, '')
                           AND first_name = ? AND last_name = ?)
                ORDER BY id DESC LIMIT 1
                """,
                (chat_id, user_id, username, first_name or "", last_name or ""),
            ).fetchone()

    def first_seen(self, chat_id: int, user_id: int) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT first_seen FROM user_history WHERE chat_id=? AND user_id=? ORDER BY id LIMIT 1",
                (chat_id, user_id),
            ).fetchone()
            return row["first_seen"] if row else None

    def save_auto_reply(self, chat_id: int, keyword: str, content_type: str,
                        content: str, added_by: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO auto_replies(chat_id, keyword, content_type, content, added_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, keyword) DO UPDATE SET
                    content_type=excluded.content_type, content=excluded.content,
                    added_by=excluded.added_by, created_at=excluded.created_at
                """,
                (chat_id, keyword.casefold(), content_type, content, added_by, utc_now()),
            )

    def remove_auto_reply(self, chat_id: int, keyword: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM auto_replies WHERE chat_id=? AND keyword=?",
                (chat_id, keyword.casefold()),
            )

    def auto_replies(self, chat_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM auto_replies WHERE chat_id=? ORDER BY keyword", (chat_id,)
            ))

    def auto_reply(self, chat_id: int, keyword: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM auto_replies WHERE chat_id=? AND keyword=?",
                (chat_id, keyword.casefold()),
            ).fetchone()

    def matching_auto_reply(self, chat_id: int, text: str) -> sqlite3.Row | None:
        folded = text.casefold()
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM auto_replies WHERE chat_id=? ORDER BY LENGTH(keyword) DESC",
                (chat_id,),
            ).fetchall()
            return next((row for row in rows if row["keyword"] in folded), None)

    def close(self) -> None:
        with self._lock:
            self.conn.close()