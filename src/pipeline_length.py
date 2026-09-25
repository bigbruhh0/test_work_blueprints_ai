from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
import fitz

from . import prompts


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 180
DEFAULT_CODEX_TIMEOUT_SECONDS = 300
DEFAULT_PIPELINE_LENGTH_MAX_TOKENS = 32000
MAX_PIPELINE_LENGTH_MAX_TOKENS = 32000
MAX_PROVIDER_TIMEOUT_SECONDS = 600
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024


def _bounded_timeout(env_name: str, default: int) -> int:
    try:
        value = int(os.getenv(env_name, str(default)))
    except ValueError:
        value = default
    return max(1, min(value, MAX_PROVIDER_TIMEOUT_SECONDS))


def _pipeline_length_max_tokens() -> int:
    try:
        value = int(os.getenv("PIPELINE_LENGTH_MAX_TOKENS", str(DEFAULT_PIPELINE_LENGTH_MAX_TOKENS)))
    except ValueError:
        value = DEFAULT_PIPELINE_LENGTH_MAX_TOKENS
    return max(1024, min(value, MAX_PIPELINE_LENGTH_MAX_TOKENS))


def _base_vertices(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = mapping.get("base_vertices") or mapping.get("vertices") or []
    return [dict(row) for row in rows if str(row.get("id", "")).startswith("V")]


def _base_edges(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = mapping.get("base_edges") or mapping.get("edges") or []
    return [
        dict(row)
        for row in rows
        if row.get("id") and not str(row.get("id")).startswith("F-")
    ]


def _compact_point(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    return [round(float(value[0]), 2), round(float(value[1]), 2)]


def _dimension_geometry(dimension: dict[str, Any]) -> dict[str, Any]:
    dimension_stroke = dimension.get("dimension_stroke") or {}
    leader_stroke = dimension.get("leader_stroke") or {}
    extension_strokes = dimension.get("extension_strokes") or []
    resolution = dimension.get("leader_resolution") or {}
    return {
        "dimension_stroke": {
            "start": dimension_stroke.get("start"),
            "end": dimension_stroke.get("end"),
            "length_px": dimension_stroke.get("length_px"),
            "merged_indices": dimension_stroke.get("merged_indices"),
        } if dimension_stroke else None,
        "leader_stroke": {
            "start": leader_stroke.get("start"),
            "end": leader_stroke.get("end"),
            "length_px": leader_stroke.get("length_px"),
        } if leader_stroke else None,
        "leader_resolution": {
            "target_projection": resolution.get("target_projection"),
            "target_gap_px": resolution.get("target_gap_px"),
            "suspect": resolution.get("suspect"),
            "suspect_reasons": resolution.get("suspect_reasons"),
        } if resolution else None,
        "extension_strokes": [
            {
                "start": stroke.get("start"),
                "end": stroke.get("end"),
                "pipe_edge_id": stroke.get("pipe_edge_id"),
                "target_endpoint": stroke.get("target_endpoint"),
                "target_gap_px": stroke.get("target_gap_px"),
                "parallel_score": stroke.get("parallel_score"),
            }
            for stroke in extension_strokes[:3]
            if isinstance(stroke, dict)
        ],
        "attachment_kind": dimension.get("attachment_kind"),
        "attachment_source": dimension.get("attachment_source"),
        "final_ray_origin": dimension.get("final_ray_origin"),
        "final_ray_origin_kind": dimension.get("final_ray_origin_kind"),
        "final_contact_point": dimension.get("final_contact_point"),
    }


def _dimension_row(dimension: dict[str, Any], page_number: int) -> dict[str, Any]:
    candidate_id = dimension.get("id")
    candidate_key = f"{page_number}:{candidate_id}"
    final_edge_id = dimension.get("final_interval_id")
    source_edge_id = dimension.get("edge_id")
    return {
        "id": candidate_id,
        "candidate_key": candidate_key,
        "page": page_number,
        "value_mm": float(dimension.get("value", 0.0)),
        "text": dimension.get("text", ""),
        "bbox": dimension.get("bbox"),
        "label_center": dimension.get("label_center"),
        "edge_id": final_edge_id,
        "final_edge_id": final_edge_id,
        "source_edge_id": source_edge_id,
        "final_from_vertex": dimension.get("final_from_vertex"),
        "final_to_vertex": dimension.get("final_to_vertex"),
        "final_interval_status": dimension.get("final_interval_status"),
        "final_interval_reason": dimension.get("final_interval_reason"),
        "edge_candidates": ([{"edge_id": final_edge_id, "source": "final_dimension_mapping"}] if final_edge_id else []),
        "status": dimension.get("status"),
        "hints": dimension.get("hints", []),
        "geometry": _dimension_geometry(dimension),
        "local_decision": {
            "decision": dimension.get("local_filter_decision"),
            "reason": dimension.get("local_filter_reason"),
            "conflict_with": dimension.get("local_filter_conflict_with"),
        },
    }


def _coordinate_number(value: Any, label: Any = None) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if str(label or "").strip().upper() == "Z-":
        return -abs(number)
    return number


def _coordinate_axis(label: Any) -> str | None:
    normalized = str(label or "").strip().upper()
    if normalized == "X":
        return "x"
    if normalized == "Y":
        return "y"
    if normalized in {"Z", "Z+", "Z-"}:
        return "z"
    return None


def _coordinate_lead_payload(row: dict[str, Any], page_number: int) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "coordinate_key": f"{page_number}:{row.get('id')}" if row.get("id") else None,
        "page": page_number,
        "label": row.get("label"),
        "value": row.get("value"),
        "bbox": row.get("bbox"),
        "label_bbox": row.get("label_bbox"),
        "value_bbox": row.get("value_bbox"),
        "coordinate_block_refs": row.get("coordinate_block_refs") or [],
        "lead_search_bbox": row.get("lead_search_bbox"),
        "lead_search_center": row.get("lead_search_center"),
        "lead_search_source": row.get("lead_search_source"),
        "arrow_found": bool(row.get("arrow_found")),
        "arrow_has_arrowhead": bool(row.get("arrow_has_arrowhead")),
        "arrow_start": row.get("arrow_start"),
        "arrow_end": row.get("arrow_end"),
        "arrow_segments": row.get("arrow_segments") or [],
        "matched_vertex_id": row.get("matched_vertex_id"),
        "matched_vertex_point": row.get("matched_vertex_point"),
        "matched_vertex_gap_px": row.get("matched_vertex_gap_px"),
    }


def _coordinate_lead_rows(mapping: dict[str, Any], page_number: int) -> list[dict[str, Any]]:
    return [_coordinate_lead_payload(row, page_number) for row in mapping.get("coordinate_leads") or []]


def _vertex_coordinate_lookup(mapping: dict[str, Any], page_number: int) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in mapping.get("coordinate_leads") or []:
        vertex_id = row.get("matched_vertex_id")
        if not vertex_id:
            continue
        vertex_key = str(vertex_id)
        entry = lookup.setdefault(
            vertex_key,
            {
                "vertex_id": vertex_key,
                "vertex_key": f"{page_number}:{vertex_key}",
                "page": page_number,
                "x": None,
                "y": None,
                "z": None,
                "confidence": 0.0,
                "method": "coordinate_lead",
                "reason": "координатный блок привязан lead-стрелкой к вершине",
                "source_coordinate_labels": [],
                "coordinate_refs": [],
                "coordinate_leads": [],
                "coordinate_conflicts": [],
            },
        )
        axis = _coordinate_axis(row.get("label"))
        value = _coordinate_number(row.get("value"), row.get("label"))
        if axis and value is not None:
            current_value = entry.get(axis)
            if current_value is None:
                entry[axis] = value
            elif abs(float(current_value) - value) > 1e-6:
                entry["coordinate_conflicts"].append(
                    {
                        "axis": axis,
                        "kept_value": current_value,
                        "ignored_value": value,
                        "source": row.get("id"),
                        "reason": "ось уже заполнена более ранним координатным блоком",
                    }
                )
        label = row.get("label")
        raw_value = row.get("value")
        if label and raw_value is not None:
            source_label = f"{label}={raw_value}"
            if source_label not in entry["source_coordinate_labels"]:
                entry["source_coordinate_labels"].append(source_label)
        for ref in row.get("coordinate_block_refs") or []:
            if ref not in entry["coordinate_refs"]:
                entry["coordinate_refs"].append(ref)
        entry["coordinate_leads"].append(_coordinate_lead_payload(row, page_number))

    for entry in lookup.values():
        filled = sum(1 for axis in ("x", "y", "z") if entry.get(axis) is not None)
        entry["confidence"] = 0.95 if filled == 3 else (0.7 if filled else 0.0)
        if filled < 3:
            entry["reason"] = "координатный lead найден, но набор X/Y/Z неполный"
        if entry.get("coordinate_conflicts"):
            entry["reason"] += "; есть конфликтующие повторные значения"
    return lookup


def _local_vertex_coordinates(
    vertex: dict[str, Any],
    page_number: int,
    coordinate_lookup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    vertex_id = str(vertex.get("id") or "")
    point = _compact_point(vertex.get("point"))
    matched = (coordinate_lookup or {}).get(vertex_id)
    if matched:
        return {
            **matched,
            "sheet_x": point[0] if point else vertex.get("x"),
            "sheet_y": point[1] if point else vertex.get("y"),
        }
    return {
        "vertex_id": vertex_id,
        "vertex_key": f"{page_number}:{vertex_id}",
        "page": page_number,
        "sheet_x": point[0] if point else vertex.get("x"),
        "sheet_y": point[1] if point else vertex.get("y"),
        "x": None,
        "y": None,
        "z": None,
        "confidence": 0.0,
        "method": "не определено",
        "reason": "нет надежной локальной X/Y/Z привязки",
        "source_coordinate_labels": [],
        "coordinate_refs": [],
        "coordinate_leads": [],
        "coordinate_conflicts": [],
    }


def _final_vertex_rows(mapping: dict[str, Any], page_number: int) -> list[dict[str, Any]]:
    coordinate_lookup = _vertex_coordinate_lookup(mapping, page_number)
    rows = []
    for vertex in mapping.get("final_vertices") or []:
        vertex_id = vertex.get("id")
        if not vertex_id:
            continue
        point = _compact_point(vertex.get("point"))
        rows.append(
            {
                "id": vertex_id,
                "vertex_key": f"{page_number}:{vertex_id}",
                "page": page_number,
                "point": point,
                "sheet_x": point[0] if point else None,
                "sheet_y": point[1] if point else None,
                "vertex_source": vertex.get("vertex_source"),
                "source": vertex.get("source"),
                "edge_id": vertex.get("edge_id") or vertex.get("pipe_edge_id"),
                "dimension_ids": vertex.get("dimension_ids") or [],
                "replaces_vertex_ids": vertex.get("replaces_vertex_ids") or vertex.get("replaced_vertex_ids") or [],
                "handwheel_id": vertex.get("handwheel_id"),
                "glyph_id": vertex.get("glyph_id"),
                "coordinate_refs": coordinate_lookup.get(str(vertex_id), {}).get("coordinate_refs", []),
                "local_coordinates": _local_vertex_coordinates(vertex, page_number, coordinate_lookup),
            }
        )
    return rows


def _final_edge_rows(mapping: dict[str, Any], page_number: int) -> list[dict[str, Any]]:
    groups = {str(row.get("edge_id")): row for row in mapping.get("final_edge_candidate_groups") or [] if row.get("edge_id")}
    dimensions_by_id = {
        str(row.get("id")): row
        for row in mapping.get("dimensions") or []
        if row.get("id")
    }
    rows = []
    for edge in mapping.get("final_contour") or []:
        edge_id = edge.get("id")
        if not edge_id:
            continue
        group = groups.get(str(edge_id)) or {}
        rows.append(
            {
                "id": edge_id,
                "edge_key": f"{page_number}:{edge_id}",
                "page": page_number,
                "from_vertex": edge.get("from_vertex"),
                "to_vertex": edge.get("to_vertex"),
                "path_points": edge.get("path_points") or [edge.get("start"), edge.get("end")],
                "pixel_length": edge.get("pixel_length"),
                "is_handwheel_segment": bool(edge.get("is_handwheel_segment")),
                "element_type": edge.get("element_type") or ("valve" if edge.get("is_handwheel_segment") else "pipe"),
                "handwheel_ids": edge.get("handwheel_ids") or [],
                "bridge_source": edge.get("bridge_source"),
                "candidates": [
                    {
                        **candidate,
                        "candidate_key": f"{page_number}:{candidate.get('candidate_id')}",
                        "local_decision": candidate.get("local_decision")
                        or (dimensions_by_id.get(str(candidate.get("candidate_id"))) or {}).get("local_filter_decision"),
                        "local_reason": candidate.get("local_reason")
                        or (dimensions_by_id.get(str(candidate.get("candidate_id"))) or {}).get("local_filter_reason"),
                    }
                    for candidate in group.get("candidates") or []
                ],
            }
        )
    return rows


def _coordinate_reconstruction_payload(
    final_vertices: list[dict[str, Any]],
    final_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    known_vertices: list[dict[str, Any]] = []
    partial_vertices: list[dict[str, Any]] = []
    unknown_vertices: list[dict[str, Any]] = []
    for vertex in final_vertices:
        coordinates = vertex.get("local_coordinates") or {}
        row = {
            "vertex_id": vertex.get("id"),
            "vertex_key": vertex.get("vertex_key"),
            "sheet_point": vertex.get("point"),
            "role": vertex.get("role"),
            "handwheel_id": vertex.get("handwheel_id"),
            "x": coordinates.get("x"),
            "y": coordinates.get("y"),
            "z": coordinates.get("z"),
            "confidence": coordinates.get("confidence"),
            "method": coordinates.get("method"),
            "source_coordinate_labels": coordinates.get("source_coordinate_labels") or [],
            "coordinate_refs": coordinates.get("coordinate_refs") or vertex.get("coordinate_refs") or [],
        }
        filled = sum(1 for axis in ("x", "y", "z") if row.get(axis) is not None)
        if filled == 3:
            known_vertices.append(row)
        elif filled:
            partial_vertices.append(row)
        else:
            unknown_vertices.append(row)

    included_edges: list[dict[str, Any]] = []
    for edge in final_edges:
        candidates = [
            candidate
            for candidate in edge.get("candidates") or []
            if candidate.get("local_decision") == "include"
        ]
        for candidate in candidates:
            included_edges.append(
                {
                    "edge_id": edge.get("id"),
                    "from_vertex": edge.get("from_vertex"),
                    "to_vertex": edge.get("to_vertex"),
                    "value_mm": candidate.get("value_mm") or candidate.get("value") or candidate.get("length_mm"),
                }
            )
    return {
        "task": (
            "Восстанови координаты всех unknown/partial VE/HG по known_vertices, "
            "included_edges, изображению и видимым направлениям трубы."
        ),
        "known_vertices": known_vertices,
        "partial_vertices": partial_vertices,
        "unknown_vertices": unknown_vertices,
        "included_edges": included_edges,
        "rules": [
            "Верни vertex_coordinates для каждой final_vertices.",
            "Локально известные X/Y/Z не меняй.",
            "Для связанных unknown вершин вычисляй координаты через included_edges и направление на изображении.",
            "Если есть несколько вариантов направления, выбери наиболее вероятный и снизь confidence.",
        ],
    }


def _unresolved_dimension_rows(dimensions: list[dict[str, Any]], page_number: int) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": row.get("id"),
            "candidate_key": f"{page_number}:{row.get('id')}",
            "value_mm": row.get("value"),
            "text": row.get("text"),
            "local_decision": row.get("local_filter_decision"),
            "status": row.get("status"),
            "final_interval_status": row.get("final_interval_status") or "unresolved",
            "final_interval_reason": row.get("final_interval_reason") or row.get("local_filter_reason"),
            "bbox": row.get("bbox"),
            "label_center": row.get("label_center"),
        }
        for row in dimensions
        if row.get("final_interval_status") != "resolved" or not row.get("final_interval_id")
    ]


def _coordinate_rows(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, row in enumerate(mapping.get("coordinates") or [], start=1):
        rows.append(
            {
                "id": row.get("id") or f"C{index:03d}",
                "label": row.get("label"),
                "value": row.get("value"),
                "label_bbox": row.get("label_bbox"),
                "value_bbox": row.get("value_bbox"),
            }
        )
    return rows


_CROSS_SHEET_REF_PATTERN = re.compile(
    # "СМ ... ЛИСТ 2" / "ЛИСТ № 2"; "Лист 200X300X6" (материал) отсекается тем,
    # что за номером листа не может идти цифра или знак размера "X".
    r"ЛИСТ\s*[:№]?\s*(?:№\s*)?(\d+)(?![\dXx×])",
    flags=re.IGNORECASE | re.UNICODE,
)


def _cross_sheet_text_refs(
    dimensions: list[dict[str, Any]],
    page_number: int,
    connections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def push(
        source_type: str,
        candidate_id: Any,
        text: Any,
        bbox: Any,
        label_center: Any,
        target_sheet: Any = None,
    ) -> None:
        value = str(text or "")
        match = _CROSS_SHEET_REF_PATTERN.search(value)
        if not match:
            return
        rows.append(
            {
                "page": page_number,
                "target_page": int(target_sheet) if str(target_sheet or "").isdigit() else int(match.group(1)),
                "source_text": value,
                "source_type": source_type,
                "candidate_id": candidate_id,
                "candidate_key": f"{page_number}:{candidate_id}" if candidate_id else None,
                "bbox": bbox,
                "label_center": label_center,
                "reason": "явная ссылка со словом ЛИСТ",
            }
        )

    for dimension in dimensions:
        push("dimension", dimension.get("id"), dimension.get("text"), dimension.get("bbox"), dimension.get("label_center"))
    for connection in connections or []:
        push(
            "connection",
            connection.get("id"),
            connection.get("label"),
            connection.get("bbox"),
            connection.get("center"),
            connection.get("target_sheet"),
        )
    return rows


def build_pipeline_length_page(mapping: dict[str, Any], page_number: int) -> dict[str, Any]:
    raw_dimensions = list(mapping.get("dimensions") or [])
    dimensions = [_dimension_row(row, page_number) for row in raw_dimensions]
    final_vertices = _final_vertex_rows(mapping, page_number)
    final_edges = _final_edge_rows(mapping, page_number)
    annotations = mapping.get("handwheel_annotations") or {}
    handwheels = [
        {
            "id": row.get("id"),
            "label": row.get("label"),
            "bbox": row.get("bbox"),
            "edge_id": row.get("edge_id"),
            "arrow_found": row.get("arrow_found"),
            "arrow_end": row.get("arrow_end"),
            "glyph_id": row.get("glyph_id"),
        }
        for row in annotations.get("handwheels") or []
    ]
    handwheel_vertices = {}
    for vertex in final_vertices:
        handwheel_id = vertex.get("handwheel_id")
        if handwheel_id:
            handwheel_vertices.setdefault(str(handwheel_id), []).append(vertex.get("id"))
    for row in handwheels:
        if row.get("id"):
            row["endpoint_vertices"] = handwheel_vertices.get(str(row["id"]), [])
    glyphs = [
        {
            "id": row.get("id"),
            "center": row.get("center"),
            "matched_source": row.get("matched_source"),
            "symbol_points": row.get("symbol_points"),
        }
        for row in annotations.get("glyphs") or []
    ]
    return {
        "page": page_number,
        "vertices": final_vertices,
        "edges": final_edges,
        "final_vertices": final_vertices,
        "final_edges": final_edges,
        "local_vertex_coordinates": [row["local_coordinates"] for row in final_vertices],
        "coordinate_reconstruction": _coordinate_reconstruction_payload(final_vertices, final_edges),
        "debug_source_vertices": _base_vertices(mapping),
        "debug_source_edges": _base_edges(mapping),
        "coordinates": _coordinate_rows(mapping),
        "coordinate_leads": _coordinate_lead_rows(mapping, page_number),
        "dimensions": dimensions,
        "unresolved_dimensions": _unresolved_dimension_rows(raw_dimensions, page_number),
        "local_decisions": [
            dict(row)
            for row in mapping.get("local_decisions") or [
                {
                    "candidate_id": row.get("id"),
                    "candidate_key": f"{page_number}:{row.get('id')}",
                    "value_mm": row.get("value"),
                    "decision": row.get("local_filter_decision"),
                    "reason": row.get("local_filter_reason"),
                    "conflict_with": row.get("local_filter_conflict_with"),
                    "status": row.get("status"),
                }
                for row in mapping.get("dimensions") or []
            ]
        ],
        "connections": list(mapping.get("connections") or []),
        "discarded_numbers": [],
        "cross_sheet_text_refs": _cross_sheet_text_refs(
            raw_dimensions,
            page_number,
            mapping.get("connections") or [],
        ),
        "handwheels": handwheels,
        "handwheel_glyphs": glyphs,
    }


def build_pipeline_length_payload(
    line_id: str,
    source_name: str,
    page_mappings: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    pages = [build_pipeline_length_page(mapping, page) for page, mapping in sorted(page_mappings, key=lambda item: item[0])]
    return {
        "analysis_type": "pipeline_length",
        "line_id": line_id,
        "source_name": source_name,
        "pages": pages,
        "cross_page_context": {
            "line_id": line_id,
            "page_order": [page["page"] for page in pages],
            "available_connections": [connection for page in pages for connection in page["connections"]],
        },
    }


def build_pipeline_length_page_payload(payload: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    """Return a provider-sized payload for one page with line context kept compact."""
    pages = payload.get("pages") or []
    provider_page = _compact_pipeline_length_provider_page(page)
    return {
        "analysis_type": payload.get("analysis_type", "pipeline_length"),
        "line_id": payload.get("line_id"),
        "source_name": payload.get("source_name"),
        "pages": [provider_page],
        "cross_page_context": {
            "line_id": payload.get("line_id"),
            "page_order": [item.get("page") for item in pages],
            "current_page": page.get("page"),
            "available_connections": [connection for item in pages for connection in item.get("connections") or []],
        },
    }


def calculate_local_length_summary(payload: dict[str, Any]) -> dict[str, Any]:
    included: list[str] = []
    excluded: list[str] = []
    ambiguous: list[str] = []
    candidate_estimates: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    clean = dirty = uncertain = 0.0
    for page in payload.get("pages") or []:
        page_clean = page_dirty = page_uncertain = 0.0
        for row in page.get("dimensions") or []:
            candidate_id = row.get("id")
            value = float(row.get("value_mm") or 0.0)
            decision = (row.get("local_decision") or {}).get("decision")
            candidate_estimates.append({
                "candidate_id": candidate_id,
                "candidate_key": row.get("candidate_key") or f"{page.get('page')}:{candidate_id}",
                "page": page.get("page"),
                "value_mm": value,
                "local_decision": decision,
                "edge_id": row.get("edge_id"),
                "final_edge_id": row.get("final_edge_id") or row.get("edge_id"),
                "final_from_vertex": row.get("final_from_vertex"),
                "final_to_vertex": row.get("final_to_vertex"),
                "final_interval_status": row.get("final_interval_status"),
                "final_interval_reason": row.get("final_interval_reason"),
                "reason": (row.get("local_decision") or {}).get("reason"),
            })
            if decision == "include":
                included.append(candidate_id)
                clean += value
                dirty += value
                page_clean += value
                page_dirty += value
            elif decision == "exclude":
                excluded.append(candidate_id)
            else:
                ambiguous.append(candidate_id)
                dirty += value
                uncertain += value
                page_dirty += value
                page_uncertain += value
        page_summaries.append({
            "page": page.get("page"),
            "clean_length_mm": round(page_clean, 2),
            "dirty_length_mm": round(page_dirty, 2),
            "ambiguous_length_mm": round(page_uncertain, 2),
        })
    return {
        "clean_length_mm": round(clean, 2),
        "dirty_length_mm": round(dirty, 2),
        "ambiguous_length_mm": round(uncertain, 2),
        "included_candidate_ids": included,
        "excluded_candidate_ids": excluded,
        "ambiguous_candidate_ids": ambiguous,
        "candidate_estimates": candidate_estimates,
        "page_summaries": page_summaries,
        "main": {"clean_length_mm": round(clean, 2), "dirty_length_mm": round(dirty, 2), "ambiguous_length_mm": round(uncertain, 2)},
        "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
    }


def build_pipeline_length_text(payload: dict[str, Any]) -> str:
    lines = [
        f"ANALYSIS: {payload.get('analysis_type')}",
        f"LINE: {payload.get('line_id')}",
        f"SOURCE: {payload.get('source_name')}",
    ]
    for page in payload.get("pages") or []:
        lines.append(f"PAGE {page.get('page')}:")
        lines.append("FINAL_VERTICES: " + ", ".join(str(vertex.get("id")) for vertex in page.get("final_vertices") or page.get("vertices") or []))
        for edge in page.get("final_edges") or page.get("edges") or []:
            candidates = edge.get("candidates") or []
            candidate_ids = ",".join(
                str(candidate.get("candidate_key") or candidate.get("candidate_id"))
                if isinstance(candidate, dict) else str(candidate)
                for candidate in candidates
            )
            candidate_ids = candidate_ids or ",".join(str(item) for item in edge.get("candidate_keys") or [])
            lines.append(f"FINAL_EDGE {edge.get('edge_key') or edge.get('edge_id') or edge.get('id')} {edge.get('from_vertex')} -> {edge.get('to_vertex')} type={edge.get('element_type')} handwheel={edge.get('is_handwheel_segment')} candidates={candidate_ids}")
        for dimension in page.get("dimensions") or []:
            local = dimension.get("local_decision") or {}
            decision = local.get("decision") if isinstance(local, dict) else local
            reason = local.get("reason") if isinstance(local, dict) else dimension.get("local_reason")
            lines.append(f"CANDIDATE {dimension.get('candidate_key')} local_id={dimension.get('id')} value_mm={dimension.get('value_mm')} final_edge={dimension.get('final_edge_id') or dimension.get('edge_id')} final_status={dimension.get('final_interval_status')} local={decision} reason={reason}")
        for dimension in page.get("unresolved_dimensions") or []:
            lines.append(f"UNRESOLVED {dimension.get('candidate_key')} value_mm={dimension.get('value_mm')} reason={dimension.get('final_interval_reason')}")
        for coordinate in page.get("coordinates") or []:
            lines.append(f"COORDINATE {coordinate.get('id')} {coordinate.get('label')}={coordinate.get('value')} label_bbox={coordinate.get('label_bbox')} value_bbox={coordinate.get('value_bbox')}")
        for link in page.get("cross_sheet_text_refs") or []:
            lines.append(f"CROSS_SHEET_REF page={link.get('page')} target={link.get('target_page')} text={link.get('source_text')} bbox={link.get('bbox')}")
        for handwheel in page.get("handwheels") or []:
            lines.append(f"HANDWHEEL {handwheel.get('id')} label={handwheel.get('label')} endpoints={handwheel.get('endpoint_vertices')} edge={handwheel.get('edge_id')} arrow_end={handwheel.get('arrow_end')}")
        for glyph in page.get("handwheel_glyphs") or []:
            lines.append(f"HANDWHEEL_GLYPH {glyph.get('id')} center={glyph.get('center')} source={glyph.get('matched_source')}")
    return "\n".join(lines) + "\n"


def _parse_json(text: str) -> dict[str, Any]:
    original = text or ""
    text = original.strip()
    if not text:
        raise ValueError("Провайдер вернул пустой ответ вместо JSON")
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            preview = text[:300].replace("\n", " ")
            raise ValueError(f"Провайдер вернул не JSON. Начало ответа: {preview}") from None
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            preview = text[:300].replace("\n", " ")
            raise ValueError(f"Провайдер вернул поврежденный JSON: {error.msg}. Начало ответа: {preview}") from None
    if not isinstance(result, dict):
        raise ValueError("pipeline_length provider response must be an object")
    return result


def _provider_failure_details(raw: dict[str, Any], choice: dict[str, Any]) -> str:
    usage = raw.get("usage") or {}
    message = choice.get("message") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    parts = [
        f"finish_reason={choice.get('finish_reason') or 'unknown'}",
        f"prompt_tokens={usage.get('prompt_tokens', 'unknown')}",
        f"completion_tokens={usage.get('completion_tokens', 'unknown')}",
        f"reasoning_tokens={completion_details.get('reasoning_tokens', 'unknown')}",
        f"content_chars={len(message.get('content') or '')}",
        f"reasoning_chars={len(message.get('reasoning_content') or '')}",
    ]
    return ", ".join(parts)


def _render_pipeline_page_image(
    pdf_path: Path | str | None,
    page_number: int | str | None,
    *,
    scale: float = 1.5,
) -> tuple[str, bytes] | None:
    if pdf_path is None or page_number is None:
        return None
    source = Path(pdf_path)
    if not source.exists():
        return None
    try:
        page_index = max(0, int(page_number) - 1)
    except (TypeError, ValueError):
        return None
    with fitz.open(str(source)) as document:
        if document.page_count <= 0:
            return None
        page_index = max(0, min(page_index, document.page_count - 1))
        pixmap = document[page_index].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image_bytes = pixmap.tobytes("png")
    image_data = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{image_data}", image_bytes


def run_pipeline_length_provider(
    payload: dict[str, Any],
    api_key: str,
    model: str,
    *,
    provider: str = "deepseek",
    pdf_path: Path | str | None = None,
    page_number: int | str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    revision = prompts.load_prompt_revision("pipeline_length")
    prompt = revision["text"]
    provider_prompt = (
        "Верни только компактный JSON. Не пиши ход рассуждений, пояснения вне JSON или markdown. "
        "reason делай коротким: до 12 слов.\n\n"
        + prompt
    )
    request_text = json.dumps(payload, ensure_ascii=False)
    rendered_image = _render_pipeline_page_image(pdf_path, page_number)
    user_content: Any = request_text
    if rendered_image is not None:
        image_url, _image_bytes = rendered_image
        user_content = [
            {
                "type": "text",
                "text": (
                    "PAYLOAD_JSON ниже. Используй приложенное изображение листа вместе с "
                    "coordinate_reconstruction, included_edges и final_edges, чтобы восстановить "
                    "координаты всех VE/HG. Не придумывай уверенные координаты без основания.\n\n"
                    + request_text
                ),
            },
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    provider = (provider or "deepseek").strip().lower()
    if provider == "deepseek":
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY не задан в .env")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        request_max_tokens = _pipeline_length_max_tokens()
        retry_max_tokens = max(request_max_tokens * 2, DEFAULT_PIPELINE_LENGTH_MAX_TOKENS)
        token_attempts = [request_max_tokens]
        if retry_max_tokens > request_max_tokens:
            token_attempts.append(min(retry_max_tokens, MAX_PIPELINE_LENGTH_MAX_TOKENS))
        last_error = None
        raw: dict[str, Any] = {}
        answer: dict[str, Any] | None = None
        for attempt_index, max_tokens in enumerate(token_attempts, start=1):
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "low",
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": provider_prompt},
                        {"role": "user", "content": user_content},
                    ],
                },
                timeout=_bounded_timeout("PIPELINE_LENGTH_DEEPSEEK_TIMEOUT", DEFAULT_DEEPSEEK_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            if len(response.content) > MAX_PROVIDER_RESPONSE_BYTES:
                raise RuntimeError("Ответ провайдера превысил лимит 4 МБ")
            raw = response.json()
            choice = (raw.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "")
            try:
                answer = _parse_json(content)
                break
            except ValueError as error:
                details = _provider_failure_details(raw, choice)
                last_error = RuntimeError(f"{error}. {details}, attempt={attempt_index}, max_tokens={max_tokens}")
                if choice.get("finish_reason") != "length" or attempt_index == len(token_attempts):
                    raise last_error from None
        if answer is None:
            raise last_error or RuntimeError("Провайдер не вернул JSON")
        trace = {
            "response_raw": raw,
            "provider": provider,
            "request_max_tokens": token_attempts[0],
            "attempts": attempt_index,
            "used_max_tokens": max_tokens,
            "image_attached": rendered_image is not None,
        }
    elif provider == "codex_cli":
        with tempfile.TemporaryDirectory(prefix="codex-pipeline-length-") as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = Path(temp_dir) / "pipeline_length_response.schema.json"
            output_path = Path(temp_dir) / "last_message.json"
            schema_path.write_text(
                json.dumps(_response_schema(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            command = [
                "codex", "exec", "--ephemeral", "--ignore-rules", "--skip-git-repo-check",
                "--sandbox", "read-only", "--ask-for-approval", "never", "--cd", str(Path.cwd()),
                "--output-schema", str(schema_path), "--output-last-message", str(output_path), "-",
            ]
            if rendered_image is not None:
                image_path = temp_path / "page.png"
                image_path.write_bytes(rendered_image[1])
                command.extend(["--image", str(image_path)])
            response = subprocess.run(
                command,
                input=(
                    "SYSTEM_PROMPT:\n"
                    + provider_prompt
                    + "\n\nИспользуй приложенное изображение листа вместе с coordinate_reconstruction, "
                    "included_edges и final_edges, чтобы восстановить координаты всех VE/HG.\n\n"
                    "PAYLOAD_JSON:\n"
                    + request_text
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_bounded_timeout("PIPELINE_LENGTH_CODEX_TIMEOUT", DEFAULT_CODEX_TIMEOUT_SECONDS),
            )
            output_text = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else response.stdout
            if len(output_text.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES:
                raise RuntimeError("Ответ провайдера превысил лимит 4 МБ")
            if response.returncode != 0:
                raise RuntimeError(f"codex CLI завершился с ошибкой {response.returncode}: {(response.stderr or output_text)[:1200]}")
            answer = _parse_json(output_text)
            trace = {
                "response_raw": {"stdout": response.stdout, "stderr": response.stderr, "returncode": response.returncode},
                "provider": provider,
                "image_attached": rendered_image is not None,
            }
    else:
        raise RuntimeError(f"Неизвестный провайдер анализа: {provider}")
    usage = (trace.get("response_raw") or {}).get("usage") or {}
    return {
        "answer": answer,
        "payload": payload,
        "prompt": prompt,
        "prompt_name": revision["name"],
        "prompt_version": revision["version"],
        "prompt_sha256": revision["sha256"],
        "prompt_source": revision["source"],
        "model": model,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        **trace,
    }


def _prefix_page_id(page_number: Any, value: Any) -> Any:
    if value is None:
        return value
    text = str(value)
    if ":" in text or not text:
        return text
    return f"{page_number}:{text}"


def _normalize_page_answer(answer: dict[str, Any], page_number: Any) -> dict[str, Any]:
    normalized = dict(answer or {})
    normalized.setdefault("schema_version", 1)
    assessments = []
    for item in normalized.get("candidate_assessments") or []:
        row = dict(item)
        if isinstance(row.get("local_decision"), dict):
            row["local_decision"] = row["local_decision"].get("decision")
        candidate_id = row.get("candidate_id") or row.get("candidate_key")
        row["candidate_id"] = _prefix_page_id(page_number, candidate_id)
        row["candidate_key"] = _prefix_page_id(page_number, row.get("candidate_key") or candidate_id)
        if row.get("target_final_edge_id"):
            row["target_final_edge_id"] = _prefix_page_id(page_number, row.get("target_final_edge_id"))
        assessments.append(row)
    normalized["candidate_assessments"] = assessments

    unresolved_reviews = []
    for item in normalized.get("unresolved_reviews") or []:
        row = dict(item)
        candidate_id = row.get("candidate_id") or row.get("candidate_key")
        row["candidate_id"] = _prefix_page_id(page_number, candidate_id)
        row["candidate_key"] = _prefix_page_id(page_number, row.get("candidate_key") or candidate_id)
        unresolved_reviews.append(row)
    normalized["unresolved_reviews"] = unresolved_reviews

    edge_routes = []
    for item in normalized.get("edge_routes") or []:
        row = dict(item)
        row["edge_id"] = _prefix_page_id(page_number, row.get("edge_id"))
        edge_routes.append(row)
    normalized["edge_routes"] = edge_routes

    branches = []
    for item in normalized.get("branch_routes") or normalized.get("branches") or []:
        row = dict(item)
        row["edge_ids"] = [_prefix_page_id(page_number, edge_id) for edge_id in row.get("edge_ids") or []]
        row["candidate_ids"] = [_prefix_page_id(page_number, candidate_id) for candidate_id in row.get("candidate_ids") or []]
        branches.append(row)
    normalized["branch_routes"] = branches

    segments = []
    for item in normalized.get("route_segments") or []:
        row = dict(item)
        row["candidate_ids"] = [_prefix_page_id(page_number, candidate_id) for candidate_id in row.get("candidate_ids") or []]
        row.setdefault("from_page", page_number)
        row.setdefault("to_page", page_number)
        segments.append(row)
    normalized["route_segments"] = segments

    cross_sheet_links = []
    for item in normalized.get("cross_sheet_links") or []:
        row = dict(item)
        row.setdefault("from_page", page_number)
        row.setdefault("page", page_number)
        cross_sheet_links.append(row)
    normalized["cross_sheet_links"] = cross_sheet_links

    intermediate_distances = []
    for item in normalized.get("intermediate_distances") or []:
        row = dict(item)
        row.setdefault("from_page", page_number)
        row.setdefault("page", page_number)
        if row.get("candidate_id"):
            row["candidate_id"] = _prefix_page_id(page_number, row.get("candidate_id"))
        intermediate_distances.append(row)
    normalized["intermediate_distances"] = intermediate_distances

    vertex_coordinates = []
    for item in normalized.get("vertex_coordinates") or normalized.get("points") or []:
        row = dict(item)
        vertex_id = row.get("vertex_id") or row.get("id")
        if vertex_id:
            row["vertex_id"] = _prefix_page_id(page_number, vertex_id)
            row["vertex_key"] = _prefix_page_id(page_number, row.get("vertex_key") or vertex_id)
            row.setdefault("local_vertex_id", vertex_id)
        row.setdefault("page", page_number)
        vertex_coordinates.append(row)
    normalized["vertex_coordinates"] = vertex_coordinates
    return normalized


def merge_pipeline_length_provider_traces(
    payload: dict[str, Any],
    page_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    combined_answer: dict[str, Any] = {
        "schema_version": 2,
        "candidate_assessments": [],
        "edge_routes": [],
        "branch_routes": [],
        "route_segments": [],
        "unresolved_reviews": [],
        "cross_sheet_links": [],
        "intermediate_distances": [],
        "vertex_coordinates": [],
        "errors": [],
        "lengths": {
            "main": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
            "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
            "total_clean_length_mm": 0.0,
            "total_dirty_length_mm": 0.0,
            "total_ambiguous_length_mm": 0.0,
        },
    }
    response_raw = {"page_responses": []}
    errors = []
    elapsed = 0.0
    prompt_tokens = completion_tokens = total_tokens = 0
    has_prompt_tokens = has_completion_tokens = has_total_tokens = False
    first_trace = page_traces[0] if page_traces else {}
    for trace in page_traces:
        page_payload = trace.get("payload") or {}
        page_number = ((page_payload.get("pages") or [{}])[0] or {}).get("page")
        answer = _normalize_page_answer(trace.get("answer") or {}, page_number)
        for key in ("candidate_assessments", "edge_routes", "branch_routes", "route_segments", "unresolved_reviews", "cross_sheet_links", "intermediate_distances", "vertex_coordinates"):
            combined_answer[key].extend(answer.get(key) or [])
        response_raw["page_responses"].append({
            "page": page_number,
            "provider": trace.get("provider"),
            "model": trace.get("model"),
            "image_attached": trace.get("image_attached"),
            "elapsed_seconds": trace.get("elapsed_seconds"),
            "prompt_tokens": trace.get("prompt_tokens"),
            "completion_tokens": trace.get("completion_tokens"),
            "total_tokens": trace.get("total_tokens"),
            "response_raw": trace.get("response_raw"),
            "error": trace.get("error"),
        })
        if trace.get("error"):
            errors.append({"page": page_number, "error": trace["error"]})
        elapsed += float(trace.get("elapsed_seconds") or 0.0)
        if trace.get("prompt_tokens") is not None:
            prompt_tokens += int(trace.get("prompt_tokens") or 0)
            has_prompt_tokens = True
        if trace.get("completion_tokens") is not None:
            completion_tokens += int(trace.get("completion_tokens") or 0)
            has_completion_tokens = True
        if trace.get("total_tokens") is not None:
            total_tokens += int(trace.get("total_tokens") or 0)
            has_total_tokens = True
    combined_answer["errors"] = errors
    return {
        "answer": combined_answer,
        "payload": payload,
        "page_traces": [
            {
                "page": ((trace.get("payload") or {}).get("pages") or [{}])[0].get("page"),
                "image_attached": trace.get("image_attached"),
                "elapsed_seconds": trace.get("elapsed_seconds"),
                "prompt_tokens": trace.get("prompt_tokens"),
                "completion_tokens": trace.get("completion_tokens"),
                "total_tokens": trace.get("total_tokens"),
            }
            for trace in page_traces
        ],
        "prompt": first_trace.get("prompt", ""),
        "prompt_name": first_trace.get("prompt_name", "pipeline_length"),
        "prompt_version": first_trace.get("prompt_version"),
        "prompt_sha256": first_trace.get("prompt_sha256"),
        "prompt_source": first_trace.get("prompt_source"),
        "model": first_trace.get("model"),
        "provider": first_trace.get("provider"),
        "image_attached": any(bool(trace.get("image_attached")) for trace in page_traces),
        "elapsed_seconds": round(elapsed, 3),
        "prompt_tokens": prompt_tokens if has_prompt_tokens else None,
        "completion_tokens": completion_tokens if has_completion_tokens else None,
        "total_tokens": total_tokens if has_total_tokens else None,
        "response_raw": response_raw,
        "errors": errors,
        "error": "; ".join(f"стр. {item['page']}: {item['error']}" for item in errors) if errors else None,
    }


def _response_schema() -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "pipeline_length_response.schema.json").read_text(encoding="utf-8"))


def build_pipeline_length_result(payload: dict[str, Any], provider_answer: dict[str, Any]) -> dict[str, Any]:
    provider_result = dict(provider_answer or {})
    provider_result["calculated_lengths"] = calculate_provider_length_summary(payload, provider_result)
    return {
        "analysis_type": "pipeline_length",
        "local_result": calculate_local_length_summary(payload),
        "provider_result": provider_result,
        "manual_confirmation_required": True,
        "applied_provider_changes": False,
    }


def _compact_pipeline_length_provider_page(page: dict[str, Any]) -> dict[str, Any]:
    """Keep provider context useful without sending duplicated debug geometry."""
    vertex_rows = []
    for vertex in page.get("final_vertices") or page.get("vertices") or []:
        local = vertex.get("local_coordinates") or {}
        vertex_rows.append(
            {
                "id": vertex.get("id"),
                "vertex_key": vertex.get("vertex_key"),
                "point": vertex.get("point"),
                "role": vertex.get("role"),
                "handwheel_id": vertex.get("handwheel_id"),
                "coordinate_refs": vertex.get("coordinate_refs") or [],
                "local_coordinates": {
                    key: local.get(key)
                    for key in (
                        "vertex_id",
                        "vertex_key",
                        "page",
                        "x",
                        "y",
                        "z",
                        "confidence",
                        "method",
                        "source_coordinate_labels",
                        "coordinate_refs",
                    )
                    if local.get(key) is not None
                },
            }
        )

    edge_rows = []
    for edge in page.get("final_edges") or page.get("edges") or []:
        edge_id = edge.get("edge_key") or edge.get("edge_id") or edge.get("id")
        candidate_keys = [
            candidate.get("candidate_key") or candidate.get("candidate_id")
            for candidate in edge.get("candidates") or []
            if candidate.get("candidate_key") or candidate.get("candidate_id")
        ]
        edge_rows.append(
            {
                key: edge.get(key)
                for key in (
                    "from_vertex",
                    "to_vertex",
                    "element_type",
                    "is_handwheel_segment",
                    "bridge_source",
                )
                if edge.get(key) is not None
            }
            | {"edge_id": edge_id, "candidate_keys": candidate_keys}
        )

    dimensions = []
    for row in page.get("dimensions") or []:
        geometry = row.get("geometry") or {}
        compact_geometry = {
            "leader_resolution": geometry.get("leader_resolution"),
            "attachment_kind": geometry.get("attachment_kind"),
            "attachment_source": geometry.get("attachment_source"),
            "final_ray_origin": geometry.get("final_ray_origin"),
            "final_ray_origin_kind": geometry.get("final_ray_origin_kind"),
            "final_contact_point": geometry.get("final_contact_point"),
            "extension_strokes": [
                {
                    key: stroke.get(key)
                    for key in ("start", "end", "pipe_edge_id", "target_endpoint", "parallel_score")
                    if stroke.get(key) is not None
                }
                for stroke in (geometry.get("extension_strokes") or [])[:2]
            ],
        }
        local = row.get("local_decision") or {}
        decision = local.get("decision") if isinstance(local, dict) else local
        unresolved = row.get("final_interval_status") != "resolved" or not row.get("final_edge_id")
        final_edge_id = row.get("final_edge_id")
        if final_edge_id and ":" not in str(final_edge_id):
            final_edge_id = f"{page.get('page')}:{final_edge_id}"
        dimensions.append({
            "candidate_key": row.get("candidate_key"),
            "value_mm": row.get("value_mm"),
            "text": row.get("text"),
            "bbox": row.get("bbox"),
            "label_center": row.get("label_center"),
            "final_edge_id": final_edge_id,
            "final_interval_status": row.get("final_interval_status") or "unresolved",
            "is_unresolved": unresolved,
            "unresolved_reason": row.get("final_interval_reason") if unresolved else None,
            "local_decision": decision,
            "local_reason": local.get("reason") if isinstance(local, dict) else None,
        })

    coordinate_leads = []
    for row in page.get("coordinate_leads") or []:
        coordinate_leads.append(
            {
                key: row.get(key)
                for key in (
                    "id",
                    "coordinate_key",
                    "label",
                    "value",
                    "coordinate_block_refs",
                    "arrow_found",
                    "arrow_has_arrowhead",
                    "arrow_start",
                    "arrow_end",
                    "matched_vertex_id",
                    "matched_vertex_point",
                    "matched_vertex_gap_px",
                )
                if row.get(key) is not None
            }
        )

    connections = [
        {
            key: row.get(key)
            for key in ("id", "label", "center", "bbox", "target_sheet", "source_text")
            if row.get(key) is not None
        }
        for row in page.get("connections") or []
        if isinstance(row, dict)
    ]
    reconstruction = dict(page.get("coordinate_reconstruction") or {})
    reconstruction["included_edges"] = []
    for edge in page.get("final_edges") or page.get("edges") or []:
        edge_id = edge.get("edge_key") or edge.get("edge_id") or edge.get("id")
        for candidate in edge.get("candidates") or []:
            if candidate.get("local_decision") != "include":
                continue
            reconstruction["included_edges"].append({
                "edge_id": edge_id if ":" in str(edge_id or "") else f"{page.get('page')}:{edge_id}",
                "from_vertex": edge.get("from_vertex"),
                "to_vertex": edge.get("to_vertex"),
                "value_mm": candidate.get("value_mm") or candidate.get("value") or candidate.get("length_mm"),
            })
    return {
        "page": page.get("page"),
        "final_edges": edge_rows,
        "coordinate_reconstruction": reconstruction,
        "coordinates": [
            {
                "coordinate_id": row.get("id"),
                "label": row.get("label"),
                "value": row.get("value"),
                "bbox": row.get("label_bbox") or row.get("value_bbox"),
                "attached_vertex_id": row.get("attached_vertex_id"),
            }
            for row in page.get("coordinates") or []
        ],
        "dimensions": dimensions,
    }


def build_pipeline_length_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the exact compact payload sent to the provider for all pages."""
    pages = [
        build_pipeline_length_page_payload(payload, page)["pages"][0]
        for page in payload.get("pages") or []
    ]
    return {
        "schema_version": 2,
        "analysis_type": payload.get("analysis_type", "pipeline_length"),
        "line_id": payload.get("line_id"),
        "source_name": payload.get("source_name"),
        "pages": pages,
        "cross_page_context": {
            "line_id": payload.get("line_id"),
            "page_order": [page.get("page") for page in pages],
            "available_connections": [],
        },
    }
def calculate_provider_length_summary(
    payload: dict[str, Any],
    provider: dict[str, Any],
    manual_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate provider-side totals after local/provider candidate decisions."""
    assessments = {
        str(item.get("candidate_id")): item
        for item in provider.get("candidate_assessments") or []
        if item.get("candidate_id")
    }
    routes = {
        str(item.get("edge_id")): item.get("route_type")
        for item in provider.get("edge_routes") or []
        if item.get("edge_id")
    }
    branch_specs = provider.get("branch_routes") or provider.get("branches") or []
    branch_by_candidate: dict[str, str] = {}
    branch_by_edge: dict[str, str] = {}
    branches: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(branch_specs, start=1):
        branch_id = str(item.get("branch_id") or item.get("id") or f"branch-{index:02d}")
        branches[branch_id] = {
            "branch_id": branch_id,
            "name": item.get("name") or branch_id,
            "junction_vertex_id": item.get("junction_vertex_id"),
            "endpoint_vertex_id": item.get("endpoint_vertex_id"),
            "edge_ids": list(item.get("edge_ids") or []),
            "candidate_ids": list(item.get("candidate_ids") or []),
            "clean_length_mm": 0.0,
            "dirty_length_mm": 0.0,
            "ambiguous_length_mm": 0.0,
            "counted_candidate_ids": [],
        }
        for candidate_id in branches[branch_id]["candidate_ids"]:
            branch_by_candidate[str(candidate_id)] = branch_id
        for edge_id in branches[branch_id]["edge_ids"]:
            branch_by_edge[str(edge_id)] = branch_id
    manual_confirmation = manual_confirmation or {}
    accepted_by_user = {str(item) for item in manual_confirmation.get("candidate_ids") or []}
    kept_local_by_user = {str(item) for item in manual_confirmation.get("keep_local_candidate_ids") or []}
    accepted_intermediate_indices = {
        int(item)
        for item in manual_confirmation.get("intermediate_distance_indices") or []
        if isinstance(item, int) or str(item).isdigit()
    }
    seen_candidates: set[str] = set()
    totals = {
        "main": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0, "counted_candidate_ids": [], "counted_intermediate_distances": []},
        "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0, "counted_candidate_ids": [], "counted_intermediate_distances": []},
    }
    for page in payload.get("pages") or []:
        page_number = page.get("page")
        for row in page.get("dimensions") or []:
            key = str(row.get("candidate_key") or f"{page_number}:{row.get('id')}")
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            review = assessments.get(key) or assessments.get(str(row.get("id")))
            decision = (row.get("local_decision") or {}).get("decision")
            if key in kept_local_by_user or str(row.get("id")) in kept_local_by_user:
                decision = (row.get("local_decision") or {}).get("decision")
            elif key in accepted_by_user or str(row.get("id")) in accepted_by_user:
                decision = (review or {}).get("proposed_decision") or decision
            elif review and review.get("proposed_decision"):
                decision = review["proposed_decision"]
            elif review and review.get("assessment") == "accepted":
                decision = review.get("local_decision") or decision
            elif review and review.get("assessment") in {"disputed", "insufficient"}:
                decision = "ambiguous"
            edge_key = str(row.get("edge_id") or "")
            route_type = routes.get(f"{page_number}:{edge_key}", routes.get(edge_key, "main"))
            branch_id = branch_by_candidate.get(key) or branch_by_candidate.get(str(row.get("id")))
            branch_id = branch_id or branch_by_edge.get(f"{page_number}:{edge_key}") or branch_by_edge.get(edge_key)
            bucket = branches[branch_id] if branch_id else totals["branch" if route_type == "branch" else "main"]
            value = float(row.get("value_mm") or 0.0)
            if decision == "include":
                bucket["clean_length_mm"] += value
                bucket["dirty_length_mm"] += value
                bucket["counted_candidate_ids"].append(key)
            elif decision not in {"exclude", None}:
                bucket["dirty_length_mm"] += value
                bucket["ambiguous_length_mm"] += value
    for branch in branches.values():
        for key in ("clean_length_mm", "dirty_length_mm", "ambiguous_length_mm"):
            totals["branch"][key] += branch[key]
        totals["branch"]["counted_candidate_ids"].extend(branch["counted_candidate_ids"])
    for index, item in enumerate(provider.get("intermediate_distances") or []):
        if index not in accepted_intermediate_indices:
            continue
        value = float(item.get("value_mm") or item.get("length_mm") or 0.0)
        if value <= 0:
            continue
        route_type = str(item.get("route_type") or item.get("route") or "main").lower()
        branch_id = str(item.get("branch_id") or "")
        bucket = branches.get(branch_id) if branch_id else None
        branch_bucket = bool(bucket)
        if not bucket:
            bucket = totals["branch" if route_type == "branch" else "main"]
        bucket["clean_length_mm"] += value
        bucket["dirty_length_mm"] += value
        bucket.setdefault("counted_intermediate_distances", []).append(index)
        if branch_bucket:
            totals["branch"]["clean_length_mm"] += value
            totals["branch"]["dirty_length_mm"] += value
            totals["branch"].setdefault("counted_intermediate_distances", []).append(index)
    for bucket in totals.values():
        for key in ("clean_length_mm", "dirty_length_mm", "ambiguous_length_mm"):
            bucket[key] = round(bucket[key], 2)
    for branch in branches.values():
        for key in ("clean_length_mm", "dirty_length_mm", "ambiguous_length_mm"):
            branch[key] = round(branch[key], 2)
    return {
        **totals,
        "branches": list(branches.values()),
        "total_clean_length_mm": round(sum(item["clean_length_mm"] for item in totals.values()), 2),
        "total_dirty_length_mm": round(sum(item["dirty_length_mm"] for item in totals.values()), 2),
        "total_ambiguous_length_mm": round(sum(item["ambiguous_length_mm"] for item in totals.values()), 2),
    }
