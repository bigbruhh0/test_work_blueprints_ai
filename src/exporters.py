from __future__ import annotations

import io
import json
from dataclasses import asdict
from typing import Any

import pandas as pd

from .models import ProjectResult


def _flatten(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ", ".join(map(str, value))
    if isinstance(value, tuple):
        return ", ".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def rows_from(items: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        raw = asdict(item)
        rows.append({key: _flatten(value) for key, value in raw.items()})
    return rows


def to_json_bytes(project: ProjectResult) -> bytes:
    return json.dumps(project.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")


def to_excel_bytes(project: ProjectResult) -> bytes:
    output = io.BytesIO()
    result = project.result
    sheets = {
        "Линии": rows_from(getattr(result, "lines", [])),
        "Точки": rows_from(getattr(result, "points", [])),
        "Участки": rows_from(getattr(result, "segments", [])),
        "Элементы": rows_from(getattr(result, "elements", [])),
        "Вершины": rows_from(getattr(result, "vertices", [])),
        "AI запросы": rows_from(getattr(result, "provider_traces", [])),
        "Неопределенности": rows_from(getattr(result, "uncertainties", [])),
        "Кандидаты": rows_from(getattr(result, "candidates", [])),
        "Классификация": rows_from(getattr(result, "candidate_classifications", [])),
        "Разметка": rows_from(getattr(result, "annotations", [])),
        "Граф_узлы": rows_from(getattr(result, "graph_nodes", [])),
        "Граф_ребра": rows_from(getattr(result, "graph_edges", [])),
        "Привязки": rows_from(getattr(result, "dimension_bindings", [])),
        "Нерешенные": rows_from(getattr(result, "unresolved_edges", [])),
    }

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, rows in sheets.items():
            frame = pd.DataFrame(rows)
            if frame.empty:
                frame = pd.DataFrame([{"status": "Нет данных"}])
            frame.to_excel(writer, sheet_name=name, index=False)

        pd.DataFrame(
            [
                {
                    "source_name": project.source_name,
                    "created_at": project.created_at,
                    "pages_count": project.pages_count,
                    "line_groups_count": project.line_groups_count,
                    "model_mode": project.model_mode,
                }
            ]
        ).to_excel(writer, sheet_name="Сводка", index=False)

    output.seek(0)
    return output.getvalue()


def dataframe_for(items: list[Any]) -> pd.DataFrame:
    frame = pd.DataFrame(rows_from(items))
    for column in frame.columns:
        if frame[column].dtype == "object":
            frame[column] = frame[column].map(lambda value: "" if value is None else str(_flatten(value)))
    return frame
