from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]


def _load_prompt_template() -> str:
    from .prompts import load_prompt

    return load_prompt("analyze")


def _load_prompt_revision() -> dict[str, object]:
    from .prompts import load_prompt_revision

    return load_prompt_revision("analyze")


def build_fragments(
    vertices: list[dict[str, Any]],
    numbers: list[dict[str, Any]],
) -> tuple[str, str]:
    vertex_lines = ["# id\trole\tcolor\tx\ty\tdegree"]
    for vertex in vertices:
        vertex_lines.append(
            f"{vertex['id']}\t{vertex['role']}\t{vertex.get('color', '')}\t"
            f"{vertex['x']:.2f}\t{vertex['y']:.2f}\t{vertex['degree']}"
        )
    numbers_rows = []
    for number in numbers:
        center = number.get("center") or [
            (number["bbox"][0] + number["bbox"][2]) / 2,
            (number["bbox"][1] + number["bbox"][3]) / 2,
        ]
        numbers_rows.append(f"{number['text']}\t{center[0]:.1f}\t{center[1]:.1f}")
    return "\n".join(vertex_lines), "\n".join(numbers_rows)


def build_coordinates_fragment(coordinates: list[dict[str, Any]]) -> str:
    rows = []
    for coord in coordinates:
        vb = coord.get("value_bbox") or coord.get("bbox")
        if vb and len(vb) == 4:
            cx = (vb[0] + vb[2]) / 2
            cy = (vb[1] + vb[3]) / 2
            rows.append(f"{coord['label']}\t{coord['value']}\t{cx:.1f}\t{cy:.1f}")
        else:
            rows.append(f"{coord['label']}\t{coord['value']}")
    return "\n".join(rows)


def parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except ValueError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char == "{":
                try:
                    parsed, _end = decoder.raw_decode(text[index:])
                    return parsed
                except ValueError:
                    continue
        raise


def normalize_vertex_coordinates(
    answer: dict[str, Any],
    vertices: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize AI coordinate rows and require an existing vertex id."""
    known = {str(vertex.get("id")): vertex for vertex in vertices if vertex.get("id")}
    raw_points = answer.get("points")
    if not isinstance(raw_points, list):
        answer["points"] = []
        answer["coordinate_warnings"] = ["Ответ не содержит массива points"]
        return answer

    points: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw in enumerate(raw_points, start=1):
        if not isinstance(raw, dict):
            warnings.append(f"points[{index}] пропущен: ожидался объект")
            continue
        vertex_id = str(raw.get("vertex_id") or raw.get("id") or raw.get("node_id") or "").strip()
        if vertex_id not in known:
            warnings.append(f"points[{index}] пропущен: неизвестная вершина {vertex_id or '—'}")
            continue
        points.append(
            {
                "id": vertex_id,
                "vertex_id": vertex_id,
                "x": raw.get("x"),
                "y": raw.get("y"),
                "z": raw.get("z"),
                "source_coordinate_labels": raw.get("source_coordinate_labels") or raw.get("sources") or [],
                "confidence": raw.get("confidence"),
                "reason": raw.get("reason", ""),
            }
        )
    answer["points"] = points
    if warnings:
        answer["coordinate_warnings"] = warnings
    else:
        answer.pop("coordinate_warnings", None)
    return answer


def call_distance_ai(
    api_key: str,
    model: str,
    vertices: list[dict[str, Any]],
    numbers: list[dict[str, Any]],
    coordinates: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    event_callback=None,
) -> dict[str, Any]:
    trace = call_distance_ai_trace(
        api_key=api_key,
        model=model,
        vertices=vertices,
        numbers=numbers,
        coordinates=coordinates,
        base_url=base_url,
        event_callback=event_callback,
    )
    return trace["answer"]


def call_distance_ai_trace(
    api_key: str,
    model: str,
    vertices: list[dict[str, Any]],
    numbers: list[dict[str, Any]],
    coordinates: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    event_callback=None,
) -> dict[str, Any]:
    prompt_revision = _load_prompt_revision()
    template = str(prompt_revision["text"])
    vertices_block, numbers_block = build_fragments(vertices, numbers)
    coordinates_block = build_coordinates_fragment(coordinates or [])
    prompt = (
        template.replace("{VERTICES_BLOCK}", vertices_block)
        .replace("{NUMBERS_BLOCK}", numbers_block)
        .replace("{COORDINATES_BLOCK}", coordinates_block)
    )

    base = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Ты — анализатор изометрических чертежей. Возвращай только JSON без Markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }

    last_error = ""
    content = ""
    status_code = 0
    elapsed = 0.0
    for attempt in range(1, 3):
        started = time.perf_counter()
        if event_callback:
            event_callback("markai.request.start", {
                "model": model,
                "vertices": len(vertices),
                "numbers": len(numbers),
                "payload_chars": len(prompt),
                "attempt": attempt,
                "prompt_version": prompt_revision["version"],
                "prompt_sha256": prompt_revision["sha256"],
            })
        try:
            response = requests.post(
                base,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=240,
            )
            elapsed = round(time.perf_counter() - started, 2)
            status_code = response.status_code
            if event_callback:
                event_callback("markai.response", {
                    "status_code": response.status_code,
                    "elapsed_seconds": elapsed,
                    "response_kb": round(len(response.content) / 1024, 1),
                    "attempt": attempt,
                })
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            content = (content or "").strip()
            if content:
                answer = normalize_vertex_coordinates(parse_json(content), vertices)
                return {
                    "answer": answer,
                    "prompt": prompt,
                    "payload": payload,
                    "response_raw": content,
                    "status_code": status_code,
                    "elapsed_seconds": elapsed,
                    "model": model,
                    "attempts": attempt,
                    "prompt_name": prompt_revision["name"],
                    "prompt_version": prompt_revision["version"],
                    "prompt_source": prompt_revision["source"],
                    "prompt_sha256": prompt_revision["sha256"],
                }
            last_error = "пустой ответ от провайдера"
        except Exception as error:  # noqa: BLE001
            last_error = str(error)
        if attempt == 1:
            time.sleep(1.0)

    raise RuntimeError(f"Провайдер не вернул валидный ответ: {last_error}")


def numbers_rows_block(numbers_csv: str) -> str:
    return numbers_csv


__all__ = ["call_distance_ai", "call_distance_ai_trace", "build_fragments", "parse_json"]
