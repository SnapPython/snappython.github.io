from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


VALID_CATEGORIES = {"research", "course", "life", "health"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def clean_category(value: Any) -> str:
    category = str(value or "life")
    return category if category in VALID_CATEGORIES else "life"


def clean_time(value: Any, fallback: str) -> str:
    candidate = str(value or "")
    if len(candidate) == 5 and candidate[2] == ":":
        try:
            hour, minute = (int(part) for part in candidate.split(":"))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return candidate
        except ValueError:
            pass
    return fallback


def add_minutes(value: str, minutes: int) -> str:
    hour, minute = (int(part) for part in value.split(":"))
    total = min(1439, hour * 60 + minute + minutes)
    return f"{total // 60:02d}:{total % 60:02d}"


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;

                CREATE TABLE IF NOT EXISTS revisions (
                    user TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS priorities (
                    user TEXT NOT NULL,
                    day TEXT NOT NULL,
                    slot INTEGER NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user, day, slot)
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    user TEXT NOT NULL,
                    id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    category TEXT NOT NULL,
                    text TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user, id)
                );
                CREATE INDEX IF NOT EXISTS tasks_day_idx
                    ON tasks (user, day);

                CREATE TABLE IF NOT EXISTS blocks (
                    user TEXT NOT NULL,
                    id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'cockpit',
                    google_event_id TEXT,
                    google_etag TEXT,
                    google_updated_at TEXT,
                    sync_state TEXT NOT NULL DEFAULT 'pending_upsert',
                    deleted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user, id)
                );
                CREATE INDEX IF NOT EXISTS blocks_day_idx
                    ON blocks (user, day);
                CREATE UNIQUE INDEX IF NOT EXISTS blocks_google_event_idx
                    ON blocks (user, google_event_id)
                    WHERE google_event_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS notes (
                    user TEXT NOT NULL,
                    day TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user, day)
                );

                CREATE TABLE IF NOT EXISTS weeks (
                    user TEXT NOT NULL,
                    week TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT '',
                    milestone TEXT NOT NULL DEFAULT '',
                    progress INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user, week)
                );

                CREATE TABLE IF NOT EXISTS google_tokens (
                    user TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    calendar_id TEXT NOT NULL DEFAULT 'primary',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    user TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS active_days (
                    user TEXT NOT NULL,
                    day TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (user, day)
                );
                """
            )
            connection.commit()

    def _bump(self, connection: sqlite3.Connection, user: str) -> int:
        timestamp = utc_now()
        connection.execute(
            """
            INSERT INTO revisions (user, revision, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(user) DO UPDATE SET
                revision = revisions.revision + 1,
                updated_at = excluded.updated_at
            """,
            (user, timestamp),
        )
        row = connection.execute(
            "SELECT revision FROM revisions WHERE user = ?", (user,)
        ).fetchone()
        return int(row["revision"])

    def revision(self, user: str) -> int:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT revision FROM revisions WHERE user = ?", (user,)
            ).fetchone()
            return int(row["revision"]) if row else 0

    def has_data(self, user: str) -> bool:
        with self._lock, self.connect() as connection:
            for table in ("priorities", "tasks", "blocks", "notes", "weeks"):
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE user = ? LIMIT 1", (user,)
                ).fetchone()
                if row:
                    return True
        return False

    def touch_active_day(self, user: str, day: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO active_days (user, day, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user, day) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (user, day, utc_now()),
            )
            connection.commit()

    def active_days(self, user: str, limit: int = 7) -> list[str]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT day FROM active_days
                WHERE user = ?
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (user, limit),
            ).fetchall()
            return [str(row["day"]) for row in rows]

    def get_day(self, user: str, day: str, week: str) -> dict[str, Any]:
        with self._lock, self.connect() as connection:
            priority_rows = connection.execute(
                """
                SELECT slot, text, done, updated_at
                FROM priorities
                WHERE user = ? AND day = ?
                ORDER BY slot
                """,
                (user, day),
            ).fetchall()
            priority_by_slot = {int(row["slot"]): row for row in priority_rows}
            priorities = []
            for slot in range(3):
                row = priority_by_slot.get(slot)
                priorities.append(
                    {
                        "text": str(row["text"]) if row else "",
                        "done": bool(row["done"]) if row else False,
                        "updatedAt": str(row["updated_at"]) if row else None,
                    }
                )

            task_rows = connection.execute(
                """
                SELECT id, category, text, done, updated_at
                FROM tasks
                WHERE user = ? AND day = ?
                ORDER BY updated_at, id
                """,
                (user, day),
            ).fetchall()
            tasks = [
                {
                    "id": str(row["id"]),
                    "category": str(row["category"]),
                    "text": str(row["text"]),
                    "done": bool(row["done"]),
                    "updatedAt": str(row["updated_at"]),
                }
                for row in task_rows
            ]

            block_rows = connection.execute(
                """
                SELECT id, start_time, end_time, category, title, done,
                       source, google_event_id, sync_state, updated_at
                FROM blocks
                WHERE user = ? AND day = ? AND deleted = 0
                ORDER BY start_time, end_time, id
                """,
                (user, day),
            ).fetchall()
            blocks = [self._block_dict(row) for row in block_rows]

            note_row = connection.execute(
                """
                SELECT text, updated_at FROM notes
                WHERE user = ? AND day = ?
                """,
                (user, day),
            ).fetchone()
            note = {
                "text": str(note_row["text"]) if note_row else "",
                "updatedAt": str(note_row["updated_at"]) if note_row else None,
            }

            week_row = connection.execute(
                """
                SELECT goal, milestone, progress, updated_at
                FROM weeks
                WHERE user = ? AND week = ?
                """,
                (user, week),
            ).fetchone()
            week_data = {
                "key": week,
                "goal": str(week_row["goal"]) if week_row else "",
                "milestone": str(week_row["milestone"]) if week_row else "",
                "progress": int(week_row["progress"]) if week_row else 0,
                "updatedAt": str(week_row["updated_at"]) if week_row else None,
            }
            revision_row = connection.execute(
                "SELECT revision FROM revisions WHERE user = ?", (user,)
            ).fetchone()

        return {
            "date": day,
            "revision": int(revision_row["revision"]) if revision_row else 0,
            "priorities": priorities,
            "tasks": tasks,
            "blocks": blocks,
            "notes": note,
            "week": week_data,
        }

    @staticmethod
    def _block_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "start": str(row["start_time"]),
            "end": str(row["end_time"]),
            "category": str(row["category"]),
            "text": str(row["title"]),
            "done": bool(row["done"]),
            "source": str(row["source"]),
            "googleEventId": row["google_event_id"],
            "syncState": str(row["sync_state"]),
            "updatedAt": str(row["updated_at"]),
        }

    def update_priority(
        self, user: str, day: str, slot: int, text: str, done: bool
    ) -> int:
        timestamp = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO priorities
                    (user, day, slot, text, done, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user, day, slot) DO UPDATE SET
                    text = excluded.text,
                    done = excluded.done,
                    updated_at = excluded.updated_at
                """,
                (user, day, slot, text[:240], int(done), timestamp),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision

    def update_notes(self, user: str, day: str, text: str) -> int:
        timestamp = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO notes (user, day, text, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user, day) DO UPDATE SET
                    text = excluded.text,
                    updated_at = excluded.updated_at
                """,
                (user, day, text[:10000], timestamp),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision

    def update_week(
        self,
        user: str,
        week: str,
        goal: str,
        milestone: str,
        progress: int,
    ) -> int:
        timestamp = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO weeks
                    (user, week, goal, milestone, progress, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user, week) DO UPDATE SET
                    goal = excluded.goal,
                    milestone = excluded.milestone,
                    progress = excluded.progress,
                    updated_at = excluded.updated_at
                """,
                (
                    user,
                    week,
                    goal[:800],
                    milestone[:240],
                    max(0, min(100, int(progress))),
                    timestamp,
                ),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision

    def upsert_task(
        self,
        user: str,
        task_id: str,
        day: str,
        category: str,
        text: str,
        done: bool,
    ) -> tuple[dict[str, Any], int]:
        timestamp = utc_now()
        task_id = (task_id or str(uuid.uuid4()))[:120]
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (user, id, day, category, text, done, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user, id) DO UPDATE SET
                    day = excluded.day,
                    category = excluded.category,
                    text = excluded.text,
                    done = excluded.done,
                    updated_at = excluded.updated_at
                """,
                (
                    user,
                    task_id,
                    day,
                    clean_category(category),
                    text[:240],
                    int(done),
                    timestamp,
                ),
            )
            revision = self._bump(connection, user)
            connection.commit()
        return (
            {
                "id": task_id,
                "category": clean_category(category),
                "text": text[:240],
                "done": bool(done),
                "updatedAt": timestamp,
            },
            revision,
        )

    def delete_task(self, user: str, task_id: str) -> int:
        with self._lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM tasks WHERE user = ? AND id = ?",
                (user, task_id),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision

    def upsert_block(
        self,
        user: str,
        block_id: str,
        day: str,
        start: str,
        end: str,
        category: str,
        title: str,
        done: bool,
    ) -> tuple[dict[str, Any], int]:
        timestamp = utc_now()
        block_id = (block_id or str(uuid.uuid4()))[:120]
        with self._lock, self.connect() as connection:
            current = connection.execute(
                """
                SELECT google_event_id, source
                FROM blocks
                WHERE user = ? AND id = ?
                """,
                (user, block_id),
            ).fetchone()
            source = str(current["source"]) if current else "cockpit"
            google_event_id = current["google_event_id"] if current else None
            connection.execute(
                """
                INSERT INTO blocks (
                    user, id, day, start_time, end_time, category, title,
                    done, source, google_event_id, sync_state, deleted,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_upsert', 0, ?)
                ON CONFLICT(user, id) DO UPDATE SET
                    day = excluded.day,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    category = excluded.category,
                    title = excluded.title,
                    done = excluded.done,
                    sync_state = 'pending_upsert',
                    deleted = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    user,
                    block_id,
                    day,
                    clean_time(start, "09:00"),
                    clean_time(end, "10:00"),
                    clean_category(category),
                    title[:240],
                    int(done),
                    source,
                    google_event_id,
                    timestamp,
                ),
            )
            revision = self._bump(connection, user)
            row = connection.execute(
                """
                SELECT id, start_time, end_time, category, title, done,
                       source, google_event_id, sync_state, updated_at
                FROM blocks WHERE user = ? AND id = ?
                """,
                (user, block_id),
            ).fetchone()
            connection.commit()
            return self._block_dict(row), revision

    def get_block(self, user: str, block_id: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, day, start_time, end_time, category, title, done,
                       source, google_event_id, google_etag, sync_state,
                       deleted, updated_at
                FROM blocks
                WHERE user = ? AND id = ?
                """,
                (user, block_id),
            ).fetchone()
            return dict(row) if row else None

    def delete_block(self, user: str, block_id: str) -> tuple[bool, int]:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT google_event_id FROM blocks
                WHERE user = ? AND id = ?
                """,
                (user, block_id),
            ).fetchone()
            if row and row["google_event_id"]:
                connection.execute(
                    """
                    UPDATE blocks SET
                        deleted = 1,
                        sync_state = 'pending_delete',
                        updated_at = ?
                    WHERE user = ? AND id = ?
                    """,
                    (utc_now(), user, block_id),
                )
                pending_google_delete = True
            else:
                connection.execute(
                    "DELETE FROM blocks WHERE user = ? AND id = ?",
                    (user, block_id),
                )
                pending_google_delete = False
            revision = self._bump(connection, user)
            connection.commit()
            return pending_google_delete, revision

    def pending_blocks(
        self, user: str, day: str | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, day, start_time, end_time, category, title, done,
                   source, google_event_id, google_etag, sync_state,
                   deleted, updated_at
            FROM blocks
            WHERE user = ? AND sync_state IN ('pending_upsert', 'pending_delete')
        """
        params: list[Any] = [user]
        if day:
            query += " AND day = ?"
            params.append(day)
        query += " ORDER BY updated_at"
        with self._lock, self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(query, tuple(params)).fetchall()
            ]

    def mark_block_synced(
        self,
        user: str,
        block_id: str,
        google_event_id: str,
        google_etag: str | None,
        google_updated_at: str | None,
    ) -> int:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE blocks SET
                    google_event_id = ?,
                    google_etag = ?,
                    google_updated_at = ?,
                    sync_state = 'synced',
                    deleted = 0
                WHERE user = ? AND id = ?
                """,
                (
                    google_event_id,
                    google_etag,
                    google_updated_at,
                    user,
                    block_id,
                ),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision

    def purge_block(self, user: str, block_id: str) -> int:
        with self._lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM blocks WHERE user = ? AND id = ?",
                (user, block_id),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision

    def mark_block_sync_error(self, user: str, block_id: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE blocks SET sync_state = 'error'
                WHERE user = ? AND id = ?
                """,
                (user, block_id),
            )
            connection.commit()

    def retry_sync_errors(self, user: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE blocks SET sync_state = 'pending_upsert'
                WHERE user = ? AND sync_state = 'error' AND deleted = 0
                """,
                (user,),
            )
            connection.execute(
                """
                UPDATE blocks SET sync_state = 'pending_delete'
                WHERE user = ? AND sync_state = 'error' AND deleted = 1
                """,
                (user,),
            )
            connection.commit()

    def upsert_google_event(
        self,
        user: str,
        block_id: str,
        day: str,
        start: str,
        end: str,
        category: str,
        title: str,
        done: bool,
        google_event_id: str,
        google_etag: str | None,
        google_updated_at: str | None,
        source: str,
    ) -> tuple[bool, int]:
        timestamp = utc_now()
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id, day, start_time, end_time, category, title, done,
                       source, google_event_id, google_etag, sync_state,
                       deleted
                FROM blocks
                WHERE user = ? AND google_event_id = ?
                """,
                (user, google_event_id),
            ).fetchone()
            if existing and existing["sync_state"] in {
                "pending_upsert",
                "pending_delete",
            }:
                revision_row = connection.execute(
                    "SELECT revision FROM revisions WHERE user = ?", (user,)
                ).fetchone()
                return False, int(revision_row["revision"]) if revision_row else 0

            actual_id = str(existing["id"]) if existing else block_id
            comparable = (
                day,
                start,
                end,
                clean_category(category),
                title[:240],
                int(done),
                source,
                google_etag,
                0,
            )
            if existing:
                current = (
                    str(existing["day"]),
                    str(existing["start_time"]),
                    str(existing["end_time"]),
                    str(existing["category"]),
                    str(existing["title"]),
                    int(existing["done"]),
                    str(existing["source"]),
                    existing["google_etag"],
                    int(existing["deleted"]),
                )
                if current == comparable:
                    revision_row = connection.execute(
                        "SELECT revision FROM revisions WHERE user = ?", (user,)
                    ).fetchone()
                    return (
                        False,
                        int(revision_row["revision"]) if revision_row else 0,
                    )

            connection.execute(
                """
                INSERT INTO blocks (
                    user, id, day, start_time, end_time, category, title,
                    done, source, google_event_id, google_etag,
                    google_updated_at, sync_state, deleted, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced', 0, ?)
                ON CONFLICT(user, id) DO UPDATE SET
                    day = excluded.day,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    category = excluded.category,
                    title = excluded.title,
                    done = excluded.done,
                    source = excluded.source,
                    google_event_id = excluded.google_event_id,
                    google_etag = excluded.google_etag,
                    google_updated_at = excluded.google_updated_at,
                    sync_state = 'synced',
                    deleted = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    user,
                    actual_id,
                    day,
                    start,
                    end,
                    clean_category(category),
                    title[:240],
                    int(done),
                    source,
                    google_event_id,
                    google_etag,
                    google_updated_at,
                    timestamp,
                ),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return True, revision

    def remove_missing_google_events(
        self, user: str, day: str, present_event_ids: set[str]
    ) -> tuple[bool, int]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, google_event_id FROM blocks
                WHERE user = ? AND day = ? AND google_event_id IS NOT NULL
                      AND sync_state = 'synced'
                """,
                (user, day),
            ).fetchall()
            missing_ids = [
                str(row["id"])
                for row in rows
                if str(row["google_event_id"]) not in present_event_ids
            ]
            if not missing_ids:
                revision_row = connection.execute(
                    "SELECT revision FROM revisions WHERE user = ?", (user,)
                ).fetchone()
                return False, int(revision_row["revision"]) if revision_row else 0
            connection.executemany(
                "DELETE FROM blocks WHERE user = ? AND id = ?",
                [(user, item_id) for item_id in missing_ids],
            )
            revision = self._bump(connection, user)
            connection.commit()
            return True, revision

    def import_local_state(self, user: str, raw_state: dict[str, Any]) -> int:
        imported = 0
        timestamp = utc_now()
        days = raw_state.get("days", {})
        weeks = raw_state.get("weeks", {})
        if not isinstance(days, dict):
            days = {}
        if not isinstance(weeks, dict):
            weeks = {}

        with self._lock, self.connect() as connection:
            for day, payload in days.items():
                if not isinstance(day, str) or not isinstance(payload, dict):
                    continue
                priorities = payload.get("priorities", [])
                if isinstance(priorities, list):
                    for slot, item in enumerate(priorities[:3]):
                        if not isinstance(item, dict):
                            continue
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO priorities
                                (user, day, slot, text, done, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                user,
                                day,
                                slot,
                                str(item.get("text", ""))[:240],
                                int(bool(item.get("done"))),
                                timestamp,
                            ),
                        )
                        imported += cursor.rowcount

                tasks = payload.get("tasks", [])
                if isinstance(tasks, list):
                    for item in tasks[:500]:
                        if not isinstance(item, dict) or not item.get("text"):
                            continue
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO tasks
                                (user, id, day, category, text, done, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                user,
                                str(item.get("id") or uuid.uuid4())[:120],
                                day,
                                clean_category(item.get("category")),
                                str(item.get("text"))[:240],
                                int(bool(item.get("done"))),
                                timestamp,
                            ),
                        )
                        imported += cursor.rowcount

                blocks = payload.get("blocks", [])
                if isinstance(blocks, list):
                    for item in blocks[:500]:
                        if not isinstance(item, dict) or not item.get("text"):
                            continue
                        start = clean_time(
                            item.get("start") or item.get("time"), "09:00"
                        )
                        end = clean_time(item.get("end"), add_minutes(start, 60))
                        cursor = connection.execute(
                            """
                            INSERT OR IGNORE INTO blocks (
                                user, id, day, start_time, end_time, category,
                                title, done, source, sync_state, deleted,
                                updated_at
                            )
                            VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, 'cockpit',
                                'pending_upsert', 0, ?
                            )
                            """,
                            (
                                user,
                                str(item.get("id") or uuid.uuid4())[:120],
                                day,
                                start,
                                end,
                                clean_category(item.get("category")),
                                str(item.get("text"))[:240],
                                int(bool(item.get("done"))),
                                timestamp,
                            ),
                        )
                        imported += cursor.rowcount

                note_text = str(payload.get("notes", ""))[:10000]
                if note_text:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO notes
                            (user, day, text, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (user, day, note_text, timestamp),
                    )
                    imported += cursor.rowcount

            for week, payload in weeks.items():
                if not isinstance(week, str) or not isinstance(payload, dict):
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO weeks (
                        user, week, goal, milestone, progress, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user,
                        week,
                        str(payload.get("goal", ""))[:800],
                        str(payload.get("milestone", ""))[:240],
                        max(0, min(100, int(payload.get("progress", 0) or 0))),
                        timestamp,
                    ),
                )
                imported += cursor.rowcount

            revision = self._bump(connection, user) if imported else self.revision(user)
            connection.commit()
            return revision

    def replace_local_state(self, user: str, raw_state: dict[str, Any]) -> int:
        with self._lock, self.connect() as connection:
            for table in (
                "priorities",
                "tasks",
                "blocks",
                "notes",
                "weeks",
                "active_days",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE user = ?", (user,)
                )
            revision = self._bump(connection, user)
            connection.commit()

        imported_revision = self.import_local_state(user, raw_state)
        return max(revision, imported_revision)

    def store_oauth_state(self, state: str, user: str, expires_at: int) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at < ?",
                (int(datetime.now(timezone.utc).timestamp()),),
            )
            connection.execute(
                """
                INSERT INTO oauth_states (state, user, expires_at)
                VALUES (?, ?, ?)
                """,
                (state, user, expires_at),
            )
            connection.commit()

    def consume_oauth_state(self, state: str) -> str | None:
        now = int(datetime.now(timezone.utc).timestamp())
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT user FROM oauth_states
                WHERE state = ? AND expires_at >= ?
                """,
                (state, now),
            ).fetchone()
            connection.execute(
                "DELETE FROM oauth_states WHERE state = ?", (state,)
            )
            connection.commit()
            return str(row["user"]) if row else None

    def set_google_token(
        self,
        user: str,
        access_token: str,
        refresh_token: str,
        expires_at: int,
        scope: str,
    ) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO google_tokens (
                    user, access_token, refresh_token, expires_at,
                    scope, calendar_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'primary', ?)
                ON CONFLICT(user) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = CASE
                        WHEN excluded.refresh_token = ''
                        THEN google_tokens.refresh_token
                        ELSE excluded.refresh_token
                    END,
                    expires_at = excluded.expires_at,
                    scope = excluded.scope,
                    updated_at = excluded.updated_at
                """,
                (
                    user,
                    access_token,
                    refresh_token,
                    expires_at,
                    scope,
                    utc_now(),
                ),
            )
            connection.commit()

    def get_google_token(self, user: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM google_tokens WHERE user = ?", (user,)
            ).fetchone()
            return dict(row) if row else None

    def google_users(self) -> list[str]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT user FROM google_tokens ORDER BY user"
            ).fetchall()
            return [str(row["user"]) for row in rows]

    def delete_google_token(self, user: str) -> int:
        with self._lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM google_tokens WHERE user = ?", (user,)
            )
            connection.execute(
                "DELETE FROM blocks WHERE user = ? AND source = 'google'",
                (user,),
            )
            connection.execute(
                """
                UPDATE blocks SET
                    google_event_id = NULL,
                    google_etag = NULL,
                    google_updated_at = NULL,
                    sync_state = 'pending_upsert'
                WHERE user = ? AND source = 'cockpit'
                """,
                (user,),
            )
            revision = self._bump(connection, user)
            connection.commit()
            return revision
