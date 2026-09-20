from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / ".cache" / "runs.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT,
                updated_at TEXT,
                status TEXT,
                source_name TEXT,
                line_ids TEXT,
                stop_stage TEXT,
                data TEXT
            )
            """
        )
        for column in ("kind", "kind_label", "model"):
            try:
                connection.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_run(run: dict[str, Any]) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO runs (run_id, created_at, updated_at, status, source_name, line_ids, stop_stage, data, kind, kind_label, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                updated_at = excluded.updated_at,
                status = excluded.status,
                source_name = excluded.source_name,
                line_ids = excluded.line_ids,
                stop_stage = excluded.stop_stage,
                data = excluded.data,
                kind = excluded.kind,
                kind_label = excluded.kind_label,
                model = excluded.model
            """,
            (
                run.get("run_id", ""),
                run.get("created_at") or _now(),
                _now(),
                run.get("status", ""),
                run.get("source_name", ""),
                json.dumps(run.get("line_ids", []), ensure_ascii=False),
                run.get("stop_stage", ""),
                json.dumps(run, ensure_ascii=False),
                run.get("kind", ""),
                run.get("kind_label", ""),
                run.get("model", ""),
            ),
        )


def load_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        return None
    return json.loads(row["data"])


def list_runs() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT run_id, created_at, updated_at, status, source_name, line_ids, stop_stage, kind, kind_label, model, data FROM runs ORDER BY created_at DESC"
        ).fetchall()
    output = []
    for row in rows:
        try:
            line_ids = json.loads(row["line_ids"])
        except (TypeError, ValueError):
            line_ids = []
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        errors = []
        for line in data.get("lines", []):
            if line.get("error"):
                errors.append(line["error"])
            for page in line.get("page_results", []):
                if page.get("error"):
                    errors.append(f"стр. {page.get('page_number')}: {page['error']}")
        output.append(
            {
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "status": "error" if errors else row["status"],
                "source_name": row["source_name"],
                "line_ids": line_ids,
                "stop_stage": row["stop_stage"],
                "kind": row["kind"],
                "kind_label": row["kind_label"],
                "model": row["model"],
                "error_count": len(errors),
                "errors": errors[:3],
            }
        )
    return output
