"""SQLite persistence for dev tasks and their step-by-step events."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ACTIVE = ("queued", "running")
STATUSES = (
    "queued",
    "running",
    "awaiting_approval",
    "done",
    "failed",
    "cancelled",
)


@dataclass
class Task:
    id: str
    kind: str  # codigo | revisao | pesquisa
    project: str
    prompt: str
    title: str = ""
    status: str = "queued"
    branch: str = ""
    base: str = ""
    worktree: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    steps: int = 0
    current_step: str = ""
    summary: str = ""
    diff_stat: str = ""
    files: List[str] = field(default_factory=list)
    pr_url: str = ""
    approval_id: str = ""
    error: str = ""
    followup: str = ""
    report: str = ""  # path of the saved Markdown report (review/research/...)
    routine: str = ""  # name of the routine that created the task, if any
    silent: bool = False  # nothing worth notifying (e.g. a test check that passed)
    created_at: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_COLUMNS = [f for f in Task.__dataclass_fields__]


class TaskStore:
    """Thread-safe store for :class:`Task` rows and ``task_events``."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, kind TEXT, project TEXT, prompt TEXT,
                title TEXT, status TEXT, branch TEXT, base TEXT, worktree TEXT,
                session_id TEXT, cost_usd REAL, steps INTEGER, current_step TEXT,
                summary TEXT, diff_stat TEXT, files TEXT, pr_url TEXT,
                approval_id TEXT, error TEXT, followup TEXT,
                created_at REAL, started_at REAL, ended_at REAL
            );
            CREATE TABLE IF NOT EXISTS routine_state (
                name TEXT PRIMARY KEY, last_fire REAL
            );
            CREATE TABLE IF NOT EXISTS task_events (
                task_id TEXT, seq INTEGER, ts REAL, kind TEXT, payload TEXT,
                PRIMARY KEY (task_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
            """
        )
        # Columns added after the first release of the table.
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        for column, decl in (
            ("report", "TEXT"),
            ("routine", "TEXT"),
            ("silent", "INTEGER"),
        ):
            if column not in have:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {decl}")
        self._conn.commit()

    # -- tasks -------------------------------------------------------------

    def create(
        self,
        *,
        kind: str,
        project: str,
        prompt: str,
        title: str = "",
        routine: str = "",
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:8],
            kind=kind,
            project=project,
            prompt=prompt,
            title=title or prompt[:80],
            routine=routine,
            created_at=time.time(),
        )
        row = task.to_dict()
        row["files"] = json.dumps(row["files"])
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tasks ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [row[c] for c in _COLUMNS],
            )
            self._conn.commit()
        return task

    def _row_to_task(self, row: tuple) -> Task:
        data = dict(zip(_COLUMNS, row))
        data["files"] = json.loads(data.get("files") or "[]")
        for key in ("cost_usd", "created_at", "started_at", "ended_at"):
            data[key] = data.get(key) or 0.0
        data["steps"] = data.get("steps") or 0
        data["silent"] = bool(data.get("silent"))
        for key in _COLUMNS:
            if data.get(key) is None:
                data[key] = ""
        return Task(**data)

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list(self, limit: int = 50) -> List[Task]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM tasks "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def update(self, task_id: str, **fields: Any) -> Optional[Task]:
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown task fields: {unknown}")
        if "files" in fields:
            fields["files"] = json.dumps(fields["files"])
        if fields:
            with self._lock:
                self._conn.execute(
                    f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in fields)} "
                    "WHERE id = ?",
                    [*fields.values(), task_id],
                )
                self._conn.commit()
        return self.get(task_id)

    def cost_since(self, since: float) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM tasks WHERE created_at >= ?",
                (since,),
            ).fetchone()
        return float(row[0] or 0.0)

    # -- routines ----------------------------------------------------------

    def last_fire(self, name: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_fire FROM routine_state WHERE name = ?", (name,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set_last_fire(self, name: str, ts: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO routine_state VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET last_fire = excluded.last_fire",
                (name, ts),
            )
            self._conn.commit()

    # -- events ------------------------------------------------------------

    def add_event(self, task_id: str, kind: str, payload: Dict[str, Any]) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            seq = int(row[0]) + 1
            self._conn.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?, ?)",
                (task_id, seq, time.time(), kind, json.dumps(payload, default=str)),
            )
            self._conn.commit()
        return seq

    def events(self, task_id: str, after: int = 0, limit: int = 500) -> List[Dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, kind, payload FROM task_events "
                "WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (task_id, after, limit),
            ).fetchall()
        return [
            {"seq": s, "ts": ts, "kind": k, "payload": json.loads(p or "{}")}
            for s, ts, k, p in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
