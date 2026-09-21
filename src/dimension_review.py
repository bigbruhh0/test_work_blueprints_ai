from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests
import base64

import fitz


def build_map_text(mapping: dict[str, Any]) -> str:
    lines = [
        f"PDF: {mapping.get('pdf')}",
        f"PAGE: {mapping.get('page')}",
        "VERTICES:",
    ]
    for vertex in mapping.get("vertices", []):
        lines.append(
            f"{vertex['id']} role={vertex['role']} x={vertex['x']} y={vertex['y']} confidence={vertex.get('confidence', 1.0)}"
        )
    lines.append("UNCERTAIN_VERTICES:")
    for vertex in mapping.get("uncertain_vertices", []):
        lines.append(
            f"{vertex['id']} x={vertex['x']} y={vertex['y']} confidence={vertex.get('confidence', 0.0)} source={vertex.get('source', '')}"
        )
    lines.append("EDGES:")
    for edge in mapping.get("edges", []):
        lines.append(
            f"{edge['id']} {edge.get('from_vertex')} -> {edge.get('to_vertex')} status={edge.get('status')} pixel_length={edge.get('pixel_length')}"
        )
    lines.append("CANDIDATE_GROUPS:")
    for group in mapping.get("edge_candidate_groups", []):
        lines.append(
            f"{group['from_vertex']} -> {group['to_vertex']} edges={','.join(group['edge_ids'])} candidates={','.join(group['candidate_ids'])} values={group['candidate_values_mm']} status={group['status']}"
        )
    lines.append("DIMENSIONS:")
    for dimension in mapping.get("dimensions", []):
        possible = ",".join(item["edge_id"] for item in dimension.get("edge_candidates", []))
        existing = dimension.get("existing_mapping", {})
        lines.append(
            f"{dimension['id']} value_mm={dimension['value_mm']} label_center={dimension['label_center']} selected_edge={dimension.get('selected_edge_id')} possible_edges={possible} status={dimension.get('status')} attachment_kind={existing.get('attachment_kind')} dimension_stroke={existing.get('dimension_stroke')} leader_stroke={existing.get('leader_stroke')} extension_strokes={existing.get('extension_strokes')}"
        )
    lines.append("DISCARDED_NUMBERS:")
    lines.extend(f"{item['text']} reason={item['reason']}" for item in mapping.get("discarded_numbers", []))
    return "\n".join(lines) + "\n"


def build_review_payload(mapping: dict[str, Any], map_text: str) -> dict[str, Any]:
    return {
        "page": mapping.get("page"),
        "vertices": mapping.get("vertices", []),
        "uncertain_vertices": mapping.get("uncertain_vertices", []),
        "edges": mapping.get("edges", []),
        "dimensions": mapping.get("dimensions", []),
        "edge_candidate_groups": mapping.get("edge_candidate_groups", []),
        "map_text": map_text,
    }


def _parse_answer(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


def review_map_with_provider(
    mapping: dict[str, Any],
    map_text: str,
    api_key: str,
    model: str,
    pdf_path: Path | None = None,
    page_number: int | None = None,
) -> dict[str, Any]:
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY не задан в .env")
    payload = build_review_payload(mapping, map_text)
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    user_content: Any = json.dumps(payload, ensure_ascii=False)
    image_source = Path(pdf_path) if pdf_path is not None else None
    if image_source is not None and page_number is not None and image_source.exists():
        with fitz.open(str(image_source)) as document:
            page_index = max(0, min(page_number - 1, document.page_count - 1))
            page = document[page_index]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            image_data = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
        user_content = [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
        ]
    from src import prompts

    review_prompt = prompts.load_prompt("dimension_review")
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": review_prompt},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=180,
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"]["content"]
    answer = _parse_answer(content)
    return {
        "answer": answer,
        "payload": payload,
        "response_raw": raw,
        "prompt": review_prompt,
        "model": model,
    }


def calculate_lengths(mapping: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    decision_rows = {
        item.get("candidate_id"): item
        for item in review.get("candidate_decisions", [])
        if item.get("candidate_id")
    }
    decisions = {key: item.get("decision") for key, item in decision_rows.items()}
    dimensions = {item["id"]: item for item in mapping.get("dimensions", [])}
    edge_types = {
        item.get("edge_id"): item.get("route_type", "main")
        for item in review.get("edge_decisions", [])
        if item.get("edge_id")
    }
    edge_types.update(
        {
            item.get("edge_id"): item.get("route_type", "main")
            for item in review.get("edge_classifications", [])
            if item.get("edge_id")
        }
    )

    def is_pipe_length_candidate(candidate_id: str) -> bool:
        row = decision_rows.get(candidate_id, {})
        kind = row.get("kind")
        return kind in {None, "pipe_length"}

    def is_prevalidated_invalid(candidate_id: str) -> bool:
        row = decision_rows.get(candidate_id, {})
        if not row:
            return False
        decision = row.get("decision")
        kind = row.get("kind")
        reason = (row.get("reason") or "").lower()
        if decision == "exclude" and kind in {"valve_dimension", "cross_sheet_dimension", "other"}:
            return True
        if decision == "exclude" and (
            "handwheel" in reason
            or "cross_sheet" in reason
            or "see sheet" in reason
            or "continuation" in reason
            or "invalid_overlap" in reason
            or "same axis" in reason
        ):
            return True
        return False

    covered_edges: set[str] = set()
    counted_candidate_ids: list[str] = []
    duplicate_candidate_ids: list[str] = []
    prevalidated_invalid_candidate_ids: list[str] = []
    include_rows = []
    for candidate_id, row in decision_rows.items():
        if is_prevalidated_invalid(candidate_id):
            prevalidated_invalid_candidate_ids.append(candidate_id)
        if row.get("decision") != "include" or candidate_id not in dimensions or not is_pipe_length_candidate(candidate_id):
            continue
        edge_ids = row.get("covered_edge_ids") or []
        if not edge_ids and row.get("edge_id"):
            edge_ids = [row["edge_id"]]
        include_rows.append((len(edge_ids), float(dimensions[candidate_id].get("value_mm", 0)), candidate_id, edge_ids))
    for _coverage_count, _value, candidate_id, edge_ids in sorted(include_rows, reverse=True):
        primary_edge_ids = []
        row = decision_rows.get(candidate_id, {})
        if row.get("edge_id"):
            primary_edge_ids = [row["edge_id"]]
        elif edge_ids:
            primary_edge_ids = [edge_ids[0]]

        if primary_edge_ids and covered_edges.intersection(primary_edge_ids):
            duplicate_candidate_ids.append(candidate_id)
            continue
        if primary_edge_ids:
            covered_edges.update(primary_edge_ids)
        elif edge_ids:
            covered_edges.update(edge_ids)
        counted_candidate_ids.append(candidate_id)
    clean = sum(
        float(dimensions[candidate_id].get("value_mm", 0))
        for candidate_id in counted_candidate_ids
    )
    ambiguous = sum(
        float(dimensions[candidate_id].get("value_mm", 0))
        for candidate_id, decision in decisions.items()
        if candidate_id in dimensions and decision == "ambiguous" and is_pipe_length_candidate(candidate_id)
    )
    dirty = sum(
        float(dimensions[candidate_id].get("value_mm", 0))
        for candidate_id, decision in decisions.items()
        if candidate_id in dimensions and decision in {"include", "ambiguous"} and is_pipe_length_candidate(candidate_id)
    )

    def route_type(candidate_id: str) -> str:
        row = decision_rows.get(candidate_id, {})
        if row.get("route_type") in {"main", "branch"}:
            return row["route_type"]
        edge_id = row.get("edge_id")
        return edge_types.get(edge_id, "main")

    route_lengths = {
        "main": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
        "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
    }
    for candidate_id, decision in decisions.items():
        if candidate_id not in dimensions or decision not in {"include", "ambiguous"} or not is_pipe_length_candidate(candidate_id):
            continue
        route = route_type(candidate_id)
        if route not in route_lengths:
            route = "main"
        value = float(dimensions[candidate_id].get("value_mm", 0))
        route_lengths[route]["dirty_length_mm"] += value
        if decision == "ambiguous":
            route_lengths[route]["ambiguous_length_mm"] += value
    for candidate_id in counted_candidate_ids:
        route = route_type(candidate_id)
        if route not in route_lengths:
            route = "main"
        route_lengths[route]["clean_length_mm"] += float(dimensions[candidate_id].get("value_mm", 0))
    return {
        "dirty_length_mm": round(dirty, 2),
        "clean_length_mm": round(clean, 2),
        "ambiguous_length_mm": round(ambiguous, 2),
        "included_candidate_ids": [key for key, value in decisions.items() if value == "include" and is_pipe_length_candidate(key)],
        "counted_candidate_ids": counted_candidate_ids,
        "duplicate_included_candidate_ids": duplicate_candidate_ids,
        "prevalidated_invalid_candidate_ids": sorted(set(prevalidated_invalid_candidate_ids)),
        "deterministically_invalid_candidate_ids": sorted(set(prevalidated_invalid_candidate_ids)),
        "excluded_candidate_ids": [key for key, value in decisions.items() if value == "exclude"],
        "ambiguous_candidate_ids": [key for key, value in decisions.items() if value == "ambiguous" and is_pipe_length_candidate(key)],
        "main": {key: round(value, 2) for key, value in route_lengths["main"].items()},
        "branch": {key: round(value, 2) for key, value in route_lengths["branch"].items()},
        "main_route": review.get("main_route", {}),
        "branch_routes": review.get("branch_routes", []),
        "cross_sheet_connections": review.get("cross_sheet_connections", []),
        "valve_dimensions": review.get("valve_dimensions", []),
        "cross_sheet_dimensions": review.get("cross_sheet_dimensions", []),
    }
