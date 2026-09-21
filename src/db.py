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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                run_id TEXT NOT NULL,
                line_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                candidate_id TEXT NOT NULL,
                rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
                prompt_name TEXT,
                prompt_version TEXT,
                prompt_sha256 TEXT,
                context TEXT NOT NULL,
                UNIQUE(run_id, line_id, page_number, candidate_id)
            )
            """
        )


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
        feedback_rows = connection.execute(
            """
            SELECT run_id,
                   SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) AS negative,
                   COUNT(*) AS total
            FROM analysis_feedback
            GROUP BY run_id
            """
        ).fetchall()
    feedback_by_run = {row["run_id"]: dict(row) for row in feedback_rows}
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
        manual_adjustment_count = 0
        feedback = feedback_by_run.get(row["run_id"], {})
        for line in data.get("lines", []):
            if line.get("error"):
                errors.append(line["error"])
            for page in line.get("page_results", []):
                if page.get("error"):
                    errors.append(f"стр. {page.get('page_number')}: {page['error']}")
                manual_adjustment_count += len(((page.get("analysis") or {}).get("manual_adjustments") or {}))
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
                "manual_adjustment_count": manual_adjustment_count,
                "feedback": {
                    "positive": int(feedback.get("positive") or 0),
                    "negative": int(feedback.get("negative") or 0),
                    "total": int(feedback.get("total") or 0),
                },
            }
        )
    return output


def save_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
    created_at = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO analysis_feedback
                (created_at, run_id, line_id, page_number, candidate_id, rating,
                 prompt_name, prompt_version, prompt_sha256, context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, line_id, page_number, candidate_id) DO UPDATE SET
                created_at = excluded.created_at,
                rating = excluded.rating,
                prompt_name = excluded.prompt_name,
                prompt_version = excluded.prompt_version,
                prompt_sha256 = excluded.prompt_sha256,
                context = excluded.context
            """,
            (
                created_at,
                feedback["run_id"],
                feedback["line_id"],
                int(feedback["page_number"]),
                feedback["candidate_id"],
                feedback["rating"],
                feedback.get("prompt_name"),
                feedback.get("prompt_version"),
                feedback.get("prompt_sha256"),
                json.dumps(feedback.get("context", {}), ensure_ascii=False),
            ),
        )
        row = connection.execute(
            """
            SELECT id, created_at, run_id, line_id, page_number, candidate_id,
                   rating, prompt_name, prompt_version, prompt_sha256, context
            FROM analysis_feedback
            WHERE run_id = ? AND line_id = ? AND page_number = ? AND candidate_id = ?
            """,
            (feedback["run_id"], feedback["line_id"], int(feedback["page_number"]), feedback["candidate_id"]),
        ).fetchone()
    result = dict(row)
    result["context"] = json.loads(result["context"] or "{}")
    return result


def feedback_summary() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT prompt_name, prompt_version, prompt_sha256,
                   SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) AS negative,
                   COUNT(*) AS total
            FROM analysis_feedback
            GROUP BY prompt_name, prompt_version, prompt_sha256
            ORDER BY prompt_name, prompt_version
            """
        ).fetchall()
    return [dict(row) for row in rows]


def feedback_rows(prompt_name: str | None = None) -> list[dict[str, Any]]:
    with _connect() as connection:
        if prompt_name:
            rows = connection.execute(
                "SELECT * FROM analysis_feedback WHERE prompt_name = ? ORDER BY created_at DESC",
                (prompt_name,),
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM analysis_feedback ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["context"] = json.loads(item.get("context") or "{}")
        result.append(item)
    return result
