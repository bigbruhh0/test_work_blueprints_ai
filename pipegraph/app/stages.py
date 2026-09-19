from __future__ import annotations

import time
from typing import Any

from .pre import drawing_numbers, read_document, vertex_dicts
from .route import extract_local_vertices
from .runtime import LineRunState


def stage_vertices(line: LineRunState, pdf_path: str, context: dict[str, Any]) -> None:
    pages, groups = read_document(pdf_path)
    group = next((group for group in groups if group.line_id == line.line_id), None)
    if group is None:
        raise RuntimeError(f"Группа {line.line_id} не найдена")
    page_number = group.pages[0].page_number
    line.pages = [page.page_number for page in group.pages]
    line.stage = "vertices"
    line.status = "running"
    line.events.append({"time": time.time(), "stage": "vertices", "message": f"стр. {page_number}: поиск вершин"})
    vertices = extract_local_vertices(str(pdf_path), page_number, group.line_id, None)
    line.vertices = vertex_dicts(vertices)
    line.events.append({"time": time.time(), "stage": "vertices", "message": f"вершин найдено: {len(line.vertices)}"})


def stage_numbers(line: LineRunState, pdf_path: str, context: dict[str, Any]) -> None:
    pages, groups = read_document(pdf_path)
    group = next((group for group in groups if group.line_id == line.line_id), None)
    if group is None:
        raise RuntimeError(f"Группа линии не найдена при извлечении чисел")
    line.stage = "numbers"
    numbers = drawing_numbers(pdf_path, group)
    line.numbers = numbers
    line.events.append({"time": time.time(), "stage": "numbers", "message": f"чисел в графической зоне: {len(numbers)}"})


def stage_ai(line: LineRunState, pdf_path: str, context: dict[str, Any]) -> None:
    line.stage = "ai"
    line.events.append(
        {
            "time": time.time(),
            "stage": "ai",
            "message": f"DeepSeek: вершин {len(line.vertices)}, чисел {len(line.numbers)}",
        }
    )
    page_number = line.pages[0] if line.pages else 1
    payload = context["deepseek"].call_deepseek(
        api_key=context["api_key"],
        model=context["model"],
        pdf_path=str(pdf_path),
        page_number=page_number,
        vertices=line.vertices,
        numbers=line.numbers,
    )
    line.result = {
        "page": page_number,
        "vertices": line.vertices,
        "numbers": line.numbers,
        "answer": payload,
    }
    line.stage = "done"
    line.status = "complete"
    line.events.append({"time": time.time(), "stage": "done", "message": "Расстояния получены"})


STAGES = (stage_vertices, stage_numbers, stage_ai)


def _process_line(line, context: dict[str, Any]) -> None:
    pdf_path = context["pdf_path"]
    try:
        for stage in STAGES:
            stage(line, pdf_path, context)
        line.status = "complete"
    except Exception as error:  # noqa: BLE001
        line.status = "error"
        line.error = str(error)
        line.events.append({"time": time.time(), "stage": "error", "message": str(error)})


__all__ = ["LineRunState", "RunState", "STAGES", "_process_line"]
