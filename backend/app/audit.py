from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    step_name TEXT,
                    step_id INTEGER,
                    event_type TEXT DEFAULT 'info',
                    actor TEXT DEFAULT 'system',
                    action TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    worker_output TEXT,
                    verdict TEXT,
                    metadata TEXT,
                    timestamp TEXT NOT NULL
                )
                """
            )
            # Check existing columns and add any missing ones
            cursor = connection.execute("PRAGMA table_info(audit_log)")
            columns = {row[1] for row in cursor.fetchall()}
            if "ticket_id" in columns and "task_id" not in columns:
                connection.execute("ALTER TABLE audit_log RENAME COLUMN ticket_id TO task_id")
            if "step_name" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN step_name TEXT")
            if "step_id" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN step_id INTEGER")
            if "event_type" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN event_type TEXT DEFAULT 'info'")
            if "actor" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT DEFAULT 'system'")
            if "action" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN action TEXT DEFAULT ''")
            if "detail" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN detail TEXT DEFAULT ''")
            if "metadata" not in columns:
                connection.execute("ALTER TABLE audit_log ADD COLUMN metadata TEXT")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def record_event(
        self,
        task_id: str,
        event_type: str,
        actor: str,
        action: str,
        detail: str,
        step_id: int | None = None,
        verdict: str | None = None,
        metadata: dict[str, Any] | None = None,
        worker_output: Any = None,
    ) -> None:
        step_name = f"step_{step_id}" if step_id is not None else action or event_type
        output_str = json.dumps(worker_output, default=str) if worker_output is not None else ""
        meta_str = json.dumps(metadata, default=str) if metadata is not None else "{}"
        
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_log (
                    task_id, step_name, step_id, event_type, actor, action, detail,
                    worker_output, verdict, metadata, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step_name,
                    step_id,
                    event_type,
                    actor,
                    action,
                    detail,
                    output_str,
                    verdict,
                    meta_str,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record(self, task_id: str, step_name: str, worker_output: Any, verdict: str | None, timestamp: str) -> None:
        """Backward compatibility for legacy record() callers."""
        self.record_event(
            task_id=task_id,
            event_type="step_record",
            actor="worker" if "step" in step_name else "manager",
            action=step_name,
            detail=verdict or "recorded",
            verdict=verdict,
            worker_output=worker_output,
        )

    def list_for_task(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, task_id, step_name, step_id, event_type, actor, action, detail,
                       worker_output, verdict, metadata, timestamp
                FROM audit_log WHERE task_id = ? ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        
        events = []
        for r in rows:
            w_out = None
            if r[8]:
                try:
                    w_out = json.loads(r[8])
                except Exception:
                    w_out = r[8]
            meta = None
            if r[10]:
                try:
                    meta = json.loads(r[10])
                except Exception:
                    meta = {}
            events.append({
                "id": r[0],
                "task_id": r[1],
                "step_name": r[2],
                "step_id": r[3],
                "event_type": r[4],
                "actor": r[5],
                "action": r[6],
                "detail": r[7],
                "worker_output": w_out,
                "verdict": r[9],
                "metadata": meta or {},
                "timestamp": r[11],
            })
        return events