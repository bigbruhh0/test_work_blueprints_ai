from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import fitz

from . import mark_pipeline
from .models import Candidate
from .route_reconstruction import extract_axis_graph


MAX_GAP_PX = 45.0
CLEAN_PDF_SCALE = 2.0


def _distance_to_segment(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float, tuple[float, float]]:
    px, py = point
    x0, y0 = start
    x1, y1 = end
    dx = x1 - x0
    dy = y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-9:
        return math.hypot(px - x0, py - y0), 0.0, start
    position = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length_sq))
    projection = (x0 + position * dx, y0 + position * dy)
    return math.hypot(px - projection[0], py - projection[1]), position, projection


def _stroke_graph(pdf_path: str | Path, page_number: int) -> list[dict[str, Any]]:
    strokes = mark_pipeline.extract_axis_strokes(str(pdf_path), page_number)
    if not strokes:
        return []
    nodes, adjacency = mark_pipeline.build_node_graph(strokes)
    allowed = mark_pipeline.pipe_component_nodes(adjacency, nodes)
    output = []
    for index, stroke in enumerate(strokes, start=1):
        start_index = min(range(len(nodes)), key=lambda i: math.hypot(nodes[i][0] - stroke.x0, nodes[i][1] - stroke.y0))
        end_index = min(range(len(nodes)), key=lambda i: math.hypot(nodes[i][0] - stroke.x1, nodes[i][1] - stroke.y1))
        if start_index not in allowed or end_index not in allowed or start_index == end_index:
            continue
        output.append(
            {
                "id": f"E{len(output) + 1:03d}",
                "from_node_id": f"N{start_index + 1:03d}",
                "to_node_id": f"N{end_index + 1:03d}",
                "start": [round(stroke.x0, 2), round(stroke.y0, 2)],
                "end": [round(stroke.x1, 2), round(stroke.y1, 2)],
                "pixel_length": round(stroke.length, 2),
            }
        )
    return output


def _vertex_rows(pdf_path: str | Path, page_number: int) -> list[dict[str, Any]]:
    vertices = mark_pipeline.extract_vertices(str(pdf_path), page_number)
    return [
        {
            "id": f"V{index:02d}",
            "role": vertex["role"],
            "x": vertex["x"],
            "y": vertex["y"],
            "degree": vertex["degree"],
        }
        for index, vertex in enumerate(vertices, start=1)
    ]


def _dimension_hints_from_text(text: str) -> set[str]:
    text = (text or "").strip()
    if not text:
        return set()
    normalized = " ".join(text.split()).upper()
    hints: set[str] = set()
    if re.search(r"(?:СМ\.?\s*ЛИСТ|СМ\.?\s*SHEET|SEE\s+SHEET|CONTINUATION|ЛИСТ\s*\d+|SHEET\s*\d+)", text, flags=re.IGNORECASE):
        hints.add("cross_sheet_reference")
    if re.search(r"(?:ШТУРВАЛ|РУКОЯТКА|РУКОЯТЬ|РУЧКА|МАХОВИК|КОЛЕСО|ТРОС|РЫЧАГ|HANDWHEEL|WHEEL|HANDLE|CRANK|LEVER)", text, flags=re.IGNORECASE):
        hints.add("handwheel")
    return hints


def _same_directed_contour(first: dict[str, Any], second: dict[str, Any], edge_by_id: dict[str, dict[str, Any]]) -> bool:
    first_edge = edge_by_id.get(first.get("edge_id"))
    second_edge = edge_by_id.get(second.get("edge_id"))
    if not first_edge or not second_edge:
        return False
    if first_edge["id"] != second_edge["id"] and first_edge["to_node_id"] != second_edge["from_node_id"] and second_edge["to_node_id"] != first_edge["from_node_id"]:
        return False
    first_stroke = first.get("dimension_stroke")
    second_stroke = second.get("dimension_stroke")
    if not first_stroke or not second_stroke:
        return False
    if set(first_stroke.get("merged_indices", [])) & set(second_stroke.get("merged_indices", [])):
        return False
    first_dx = first_stroke["end"][0] - first_stroke["start"][0]
    first_dy = first_stroke["end"][1] - first_stroke["start"][1]
    second_dx = second_stroke["end"][0] - second_stroke["start"][0]
    second_dy = second_stroke["end"][1] - second_stroke["start"][1]
    first_length = math.hypot(first_dx, first_dy) or 1.0
    second_length = math.hypot(second_dx, second_dy) or 1.0
    direction_cosine = abs((first_dx * second_dx + first_dy * second_dy) / (first_length * second_length))
    if direction_cosine < 0.97:
        return False
    unit_x = first_dx / first_length
    unit_y = first_dy / first_length
    first_interval = sorted(
        (first_stroke["start"][0] * unit_x + first_stroke["start"][1] * unit_y,
         first_stroke["end"][0] * unit_x + first_stroke["end"][1] * unit_y)
    )
    second_interval = sorted(
        (second_stroke["start"][0] * unit_x + second_stroke["start"][1] * unit_y,
         second_stroke["end"][0] * unit_x + second_stroke["end"][1] * unit_y)
    )
    overlap_start = max(first_interval[0], second_interval[0])
    overlap_end = min(first_interval[1], second_interval[1])
    interval_overlap = overlap_end - overlap_start
    if interval_overlap <= 2.0:
        return False
    first_offset = first.get("offset_px")
    second_offset = second.get("offset_px")
    offset_delta = None if first_offset is None or second_offset is None else abs(first_offset - second_offset)
    if offset_delta is not None and offset_delta <= 22.0:
        # Same straight contour / same axis: sequential labels or repeated values on one
        # pipe segment are not true overlaps, even when projections intersect.
        return False
    shorter_length = min(first_length, second_length)
    # Require a substantial shared projection to mark a real overlap. A tiny overlap from
    # a nearby label or a shifted leader line should stay valid.
    if interval_overlap < max(10.0, 0.25 * shorter_length):
        return False
    first_mid = (first_interval[0] + first_interval[1]) / 2.0
    second_mid = (second_interval[0] + second_interval[1]) / 2.0
    center_distance = abs(first_mid - second_mid)
    if offset_delta is not None and center_distance <= 0.15 * shorter_length and offset_delta <= 32.0:
        return False
    # For genuine overlap, the labels must not just be parallel; their projections need to
    # overlap over a meaningful chunk and their perpendicular separation must indicate a
    # distinct measurement line, not a same-axis duplicate.
    return True


def _mark_overlapping_dimensions(dimensions: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    edge_by_id = {edge["id"]: edge for edge in edges}
    for index, first in enumerate(dimensions):
        if first.get("status") == "unresolved":
            continue
        for second in dimensions[index + 1:]:
            if second.get("status") == "unresolved" or not _same_directed_contour(first, second, edge_by_id):
                continue
            if first["value"] == second["value"]:
                continue
            smaller, larger = (first, second) if first["value"] < second["value"] else (second, first)
            smaller["status"] = "invalid_overlap"
            smaller["valid"] = False
            smaller["conflict_with"] = larger["id"]
            smaller["reason"] = "overlaps_larger_dimension_on_same_directed_contour"
            larger.setdefault("valid", True)


def _nearest_edge_for_stroke(stroke: dict[str, Any], edges: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float, tuple[float, float] | None]:
    points = [tuple(stroke["start"]), tuple(stroke["end"])]
    best_edge = None
    best_distance = float("inf")
    best_projection = None
    for edge in edges:
        for point in points:
            distance, _position, projection = _distance_to_segment(point, tuple(edge["start"]), tuple(edge["end"]))
            if distance < best_distance:
                best_distance = distance
                best_edge = edge
                best_projection = projection
    return best_edge, best_distance, best_projection


def _merge_dimension_stroke(stroke: Any, strokes: list[Any]) -> dict[str, Any]:
    direction_x = stroke.x1 - stroke.x0
    direction_y = stroke.y1 - stroke.y0
    direction_length = math.hypot(direction_x, direction_y) or 1.0
    unit_x = direction_x / direction_length
    unit_y = direction_y / direction_length
    points = [(stroke.x0, stroke.y0), (stroke.x1, stroke.y1)]
    used = {stroke.index}
    changed = True
    while changed:
        changed = False
        for candidate in strokes:
            if candidate.index in used or candidate.kind != "dimension":
                continue
            candidate_dx = candidate.x1 - candidate.x0
            candidate_dy = candidate.y1 - candidate.y0
            candidate_length = math.hypot(candidate_dx, candidate_dy) or 1.0
            cosine = abs((direction_x * candidate_dx + direction_y * candidate_dy) / (direction_length * candidate_length))
            if cosine < 0.97:
                continue
            candidate_points = [(candidate.x0, candidate.y0), (candidate.x1, candidate.y1)]
            if min(
                math.hypot(a[0] - b[0], a[1] - b[1])
                for a in points
                for b in candidate_points
            ) > 6.0:
                continue
            points.extend(candidate_points)
            used.add(candidate.index)
            changed = True
    projections = [point[0] * unit_x + point[1] * unit_y for point in points]
    start_projection = min(projections)
    end_projection = max(projections)
    origin_x = points[0][0]
    origin_y = points[0][1]
    start = [origin_x + (start_projection - (origin_x * unit_x + origin_y * unit_y)) * unit_x,
             origin_y + (start_projection - (origin_x * unit_x + origin_y * unit_y)) * unit_y]
    end = [origin_x + (end_projection - (origin_x * unit_x + origin_y * unit_y)) * unit_x,
           origin_y + (end_projection - (origin_x * unit_x + origin_y * unit_y)) * unit_y]
    return {
        "index": stroke.index,
        "start": [round(start[0], 2), round(start[1], 2)],
        "end": [round(end[0], 2), round(end[1], 2)],
        "length_px": round(math.hypot(end[0] - start[0], end[1] - start[1]), 2),
        "merged_indices": sorted(used),
    }


def _stroke_angle_matches_edge(stroke: Any, edge: dict[str, Any]) -> bool:
    stroke_dx = stroke.x1 - stroke.x0
    stroke_dy = stroke.y1 - stroke.y0
    edge_dx = edge["end"][0] - edge["start"][0]
    edge_dy = edge["end"][1] - edge["start"][1]
    stroke_length = math.hypot(stroke_dx, stroke_dy) or 1.0
    edge_length = math.hypot(edge_dx, edge_dy) or 1.0
    cosine = abs((stroke_dx * edge_dx + stroke_dy * edge_dy) / (stroke_length * edge_length))
    return cosine >= 0.97


def _resolve_leader_target(stroke: Any, edge: dict[str, Any], strokes: list[Any]) -> Any | None:
    candidates = []
    for candidate in strokes:
        if candidate.index == stroke.index or candidate.kind != "dimension":
            continue
        if not _stroke_angle_matches_edge(candidate, edge):
            continue
        endpoint_distance = min(
            _distance_to_segment((stroke.x0, stroke.y0), (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0],
            _distance_to_segment((stroke.x1, stroke.y1), (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0],
        )
        if endpoint_distance > 28.0:
            continue
        edge_distance = min(
            _distance_to_segment((edge["start"][0], edge["start"][1]), (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0],
            _distance_to_segment((edge["end"][0], edge["end"][1]), (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0],
        )
        candidates.append((endpoint_distance + edge_distance * 0.35, -candidate.length, candidate))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _add_pipe_anchor(dimension: dict[str, Any], edges: list[dict[str, Any]]) -> None:
    edge = next((item for item in edges if item["id"] == dimension.get("edge_id")), None)
    stroke = dimension.get("dimension_stroke")
    if edge is None or not stroke:
        return
    candidates = [
        _distance_to_segment(tuple(point), tuple(edge["start"]), tuple(edge["end"]))
        for point in (stroke["start"], stroke["end"])
    ]
    gap, position, projection = min(candidates, key=lambda item: item[0])
    has_leader = dimension.get("leader_stroke") is not None
    if not has_leader and gap > 20.0:
        return
    if has_leader and gap > 35.0:
        return
    dimension["pipe_anchor"] = {
        "edge_id": edge["id"],
        "position": round(position, 4),
        "point": [round(projection[0], 2), round(projection[1], 2)],
        "gap_px": round(gap, 2),
    }


def _find_extension_strokes(dimension: dict[str, Any], edge: dict[str, Any] | None, strokes: list[Any]) -> list[dict[str, Any]]:
    target = dimension.get("dimension_stroke")
    if not target or edge is None:
        return []
    target_points = [tuple(target["start"]), tuple(target["end"])]
    candidates = []
    target_indices = set(target.get("merged_indices", []))
    for stroke in strokes:
        if stroke.kind != "dimension" or stroke.index in target_indices:
            continue
        endpoints = [(stroke.x0, stroke.y0), (stroke.x1, stroke.y1)]
        target_gaps = [
            min(math.hypot(point[0] - target_point[0], point[1] - target_point[1]) for target_point in target_points)
            for point in endpoints
        ]
        pipe_gaps = [
            _distance_to_segment(point, tuple(edge["start"]), tuple(edge["end"]))[0]
            for point in endpoints
        ]
        assignments = [
            (target_gaps[0], pipe_gaps[1]),
            (target_gaps[1], pipe_gaps[0]),
        ]
        target_gap, pipe_gap = min(assignments, key=lambda item: item[0] + item[1])
        if target_gap <= 18.0 and pipe_gap <= 22.0 and stroke.length >= 8.0:
            candidates.append(
                {
                    "index": stroke.index,
                    "start": [round(stroke.x0, 2), round(stroke.y0, 2)],
                    "end": [round(stroke.x1, 2), round(stroke.y1, 2)],
                    "length_px": round(stroke.length, 2),
                    "target_gap_px": round(target_gap, 2),
                    "pipe_gap_px": round(pipe_gap, 2),
                }
            )
    candidates.sort(key=lambda item: item["target_gap_px"] + item["pipe_gap_px"])
    return candidates[:2]


def map_dimensions(pdf_path: str | Path, page_number: int) -> dict[str, Any]:
    pdf_path = str(pdf_path)
    with fitz.open(pdf_path) as document:
        page = document[page_number - 1]
        drawing_area, _format = mark_pipeline.get_drawing_area(page)
        dimensions, _rectangles, discarded = mark_pipeline.extract_dimension_numbers(page, drawing_area)

    edges = _stroke_graph(pdf_path, page_number)
    vertices = _vertex_rows(pdf_path, page_number)
    dimension_candidates = [
        Candidate(
            id=f"D{index:03d}",
            line_id="dimension-mapping",
            page=page_number,
            kind="dimension",
            text=text,
            zone="drawing",
            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
        )
        for index, (text, rect) in enumerate(dimensions, start=1)
    ]
    vector_graph = extract_axis_graph(pdf_path, page_number, dimension_candidates)
    attached_strokes = {
        candidate_id: vector_graph.strokes[stroke_index]
        for candidate_id, stroke_index in vector_graph.attached.items()
        if 0 <= stroke_index < len(vector_graph.strokes)
    }
    mapped = []
    for index, (text, rect) in enumerate(dimensions, start=1):
        center = ((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        candidates = []
        for edge in edges:
            distance, position, projection = _distance_to_segment(
                center,
                tuple(edge["start"]),
                tuple(edge["end"]),
            )
            candidates.append((distance, position, projection, edge))
        candidates.sort(key=lambda item: item[0])
        hints = _dimension_hints_from_text(text)
        if candidates and candidates[0][0] <= MAX_GAP_PX:
            distance, position, projection, edge = candidates[0]
            edge_dx = edge["end"][0] - edge["start"][0]
            edge_dy = edge["end"][1] - edge["start"][1]
            edge_length = math.hypot(edge_dx, edge_dy) or 1.0
            offset_px = (edge_dx * (center[1] - projection[1]) - edge_dy * (center[0] - projection[0])) / edge_length
            mapped.append(
                {
                    "id": f"D{index:03d}",
                    "value": float(text.replace(",", ".")),
                    "text": text,
                    "hints": sorted(hints),
                    "label_center": [round(center[0], 2), round(center[1], 2)],
                    "edge_id": edge["id"],
                    "position": round(position, 4),
                    "projection": [round(projection[0], 2), round(projection[1], 2)],
                    "gap_px": round(distance, 2),
                    "offset_px": round(offset_px, 2),
                    "status": "projected" if distance > 1.0 else "exact",
                }
            )
        else:
            mapped.append(
                {
                    "id": f"D{index:03d}",
                    "value": float(text.replace(",", ".")),
                    "text": text,
                    "hints": sorted(hints),
                    "label_center": [round(center[0], 2), round(center[1], 2)],
                    "edge_id": None,
                    "position": None,
                    "projection": None,
                    "gap_px": round(candidates[0][0], 2) if candidates else None,
                    "offset_px": None,
                    "status": "unresolved",
                    "reason": "no_pipe_edge_within_tolerance",
                }
            )
        if "cross_sheet_reference" in mapped[-1]["hints"]:
            mapped[-1]["status"] = "cross_sheet_reference"
            mapped[-1]["valid"] = False
        if "handwheel" in mapped[-1]["hints"]:
            mapped[-1]["status"] = "handwheel"
            mapped[-1]["valid"] = False
        mapped[-1]["dimension_stroke"] = None
        attached = attached_strokes.get(mapped[-1]["id"])
        if attached is not None:
            initial_stroke = _merge_dimension_stroke(attached, vector_graph.strokes)
            mapped[-1]["dimension_stroke"] = initial_stroke
            mapped[-1]["leader_stroke"] = None
            pipe_edge = next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None)
            if pipe_edge is not None and not _stroke_angle_matches_edge(attached, pipe_edge):
                target = _resolve_leader_target(attached, pipe_edge, vector_graph.strokes)
                if target is not None:
                    mapped[-1]["leader_stroke"] = initial_stroke
                    mapped[-1]["dimension_stroke"] = _merge_dimension_stroke(target, vector_graph.strokes)
                    mapped[-1]["leader_attached"] = True
                    mapped[-1]["attachment_kind"] = "leader_to_dimension_arrow"
            else:
                mapped[-1]["attachment_kind"] = "direct_dimension_arrow"
            _add_pipe_anchor(mapped[-1], edges)
            mapped[-1]["extension_strokes"] = _find_extension_strokes(
                mapped[-1],
                next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None),
                vector_graph.strokes,
            )
            mapped[-1]["leader_attached"] = True
            if mapped[-1]["edge_id"] is None:
                edge, edge_gap, edge_projection = _nearest_edge_for_stroke(mapped[-1]["dimension_stroke"], edges)
                if edge is not None:
                    mapped[-1]["edge_id"] = edge["id"]
                    mapped[-1]["edge_gap_px"] = round(edge_gap, 2)
                    mapped[-1]["projection"] = [round(edge_projection[0], 2), round(edge_projection[1], 2)]
            if mapped[-1]["status"] == "unresolved":
                mapped[-1]["status"] = "leader_attached"
                mapped[-1]["reason"] = "number_attached_to_dimension_arrow"
        else:
            mapped[-1]["leader_attached"] = False

    _mark_overlapping_dimensions(mapped, edges)
    return {
        "page_number": page_number,
        "vertices": vertices,
        "edges": edges,
        "dimensions": mapped,
        "discarded_numbers": [
            {"text": text, "reason": reason}
            for text, _rect, reason in discarded
        ],
        "tolerance_px": MAX_GAP_PX,
    }


def save_dimension_mapping_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=page.rect.width, height=page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)
        drawing_area, _format = mark_pipeline.get_drawing_area(page)
        marked.draw_rect(drawing_area, color=(0, 0.3, 1.0), width=1.2, dashes="[4 3] 0")

        for edge in mapping["edges"]:
            start = fitz.Point(*edge["start"])
            end = fitz.Point(*edge["end"])
            marked.draw_line(start, end, color=(0.2, 0.65, 0.95), width=1.2)
            midpoint = fitz.Point((start.x + end.x) / 2, (start.y + end.y) / 2)
            marked.insert_text(midpoint + (3, -3), edge["id"], fontsize=6, fontname="helv", color=(0.05, 0.35, 0.7))

        for vertex in mapping["vertices"]:
            color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex["role"], (0, 0, 0))
            center = fitz.Point(vertex["x"], vertex["y"])
            marked.draw_circle(center, 5, color=color, fill=color, width=1.1)
            marked.insert_text(center + (7, -7), vertex["id"], fontsize=8, fontname="helv", color=color)

        for dimension in mapping["dimensions"]:
            center = fitz.Point(*dimension["label_center"])
            color = (0.85, 0.1, 0.1) if dimension["status"] in {"unresolved", "invalid_overlap"} else (0.0, 0.55, 0.15)
            stroke = dimension.get("dimension_stroke")
            if stroke:
                marked.draw_line(
                    fitz.Point(*stroke["start"]),
                    fitz.Point(*stroke["end"]),
                    color=(0.95, 0.45, 0.05),
                    width=1.0,
                )
                marked.draw_line(
                    center,
                    fitz.Point(*stroke["start"]),
                    color=(0.95, 0.45, 0.05),
                    width=0.7,
                    dashes="[2 2] 0",
                )
            marked.draw_rect(fitz.Rect(center.x - 3, center.y - 7, center.x + 35, center.y + 5), color=color, width=1.0)
            label = f"{dimension['text']} -> {dimension['edge_id'] or '?'}"
            marked.insert_text(center + (2, 3), label, fontsize=7, fontname="helv", color=color)
            if dimension.get("projection"):
                projection = fitz.Point(*dimension["projection"])
                marked.draw_line(center, projection, color=color, width=0.7, dashes="[2 2] 0")
                marked.draw_circle(projection, 2.5, color=color, fill=color, width=0.8)

        output.save(str(output_pdf))
        output.close()


def save_preprocess_annotation_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Legacy local QA overlay kept for compatibility; richer than the minimal clean version."""
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=page.rect.width, height=page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)

        for vertex in mapping.get("vertices", []):
            color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex.get("role"), (0, 0, 0))
            center = fitz.Point(vertex["x"], vertex["y"])
            marked.draw_circle(center, 6, color=color, fill=color, width=1.2)
            marked.insert_text(center + (8, -8), vertex.get("id", "V?"), fontsize=8, fontname="helv", color=color)

        for dimension in mapping.get("dimensions", []):
            label_center = dimension.get("label_center")
            if not label_center:
                continue
            if dimension.get("status") in {"cross_sheet_reference", "handwheel", "unresolved", "invalid_overlap"}:
                continue
            center = fitz.Point(*label_center)
            rect = fitz.Rect(center.x - 18, center.y - 10, center.x + 22, center.y + 10)
            marked.draw_rect(rect, color=(0.0, 0.55, 0.15), fill=None, width=1.2)

        output.save(str(output_pdf))
        output.close()


def save_clean_local_markup_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Pure QA overlay: vertex markers and minimal boxes around selected dimension numbers only."""
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=page.rect.width, height=page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)

        for vertex in mapping.get("vertices", []):
            color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex.get("role"), (0, 0, 0))
            center = fitz.Point(vertex["x"], vertex["y"])
            marked.draw_circle(center, 6, color=color, fill=color, width=1.2)
            marked.insert_text(center + (8, -8), vertex.get("id", "V?"), fontsize=8, fontname="helv", color=color)

        for dimension in mapping.get("dimensions", []):
            label_center = dimension.get("label_center")
            if not label_center:
                continue
            if dimension.get("status") in {"cross_sheet_reference", "handwheel", "unresolved", "invalid_overlap"}:
                continue
            if not dimension.get("valid", True):
                continue
            center = fitz.Point(*label_center)
            rect = fitz.Rect(center.x - 20, center.y - 12, center.x + 24, center.y + 12)
            marked.draw_rect(rect, color=(0.0, 0.55, 0.15), fill=None, width=1.2)

        output.save(str(output_pdf))
        output.close()


def save_clean_graph_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    with fitz.open(str(pdf_path)) as document:
        source_page = document[page_number - 1]
        output = fitz.open()
        scale = CLEAN_PDF_SCALE
        page = output.new_page(width=source_page.rect.width * scale, height=source_page.rect.height * scale)
        page.draw_rect(page.rect, color=(0.82, 0.84, 0.88), width=0.8)

        def point(values: list[float]) -> fitz.Point:
            return fitz.Point(values[0] * scale, values[1] * scale)

        edge_by_id = {edge["id"]: edge for edge in mapping["edges"]}
        for edge in mapping["edges"]:
            page.draw_line(
                point(edge["start"]),
                point(edge["end"]),
                color=(0.08, 0.32, 0.72),
                width=2.0 * scale,
            )

        for vertex in mapping["vertices"]:
            color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex["role"], (0, 0, 0))
            center = point([vertex["x"], vertex["y"]])
            page.draw_circle(center, 6 * scale, color=color, fill=color, width=1.2 * scale)
            page.insert_text(center + (9 * scale, -8 * scale), vertex["id"], fontsize=10 * scale, fontname="helv", color=color)

        for dimension in mapping["dimensions"]:
            anchor = dimension.get("pipe_anchor")
            if not anchor or dimension.get("status") == "invalid_overlap":
                continue
            anchor_point = point(anchor["point"])
            page.draw_circle(anchor_point, 4 * scale, color=(0.8, 0.05, 0.05), fill=(1.0, 0.75, 0.1), width=1.2 * scale)
            page.insert_text(anchor_point + (6 * scale, 5 * scale), f"A{dimension['id'][1:]}", fontsize=7 * scale, fontname="helv", color=(0.75, 0.05, 0.05))

        for dimension in mapping["dimensions"]:
            stroke = dimension.get("dimension_stroke")
            if not stroke:
                continue
            stroke_start = point(stroke["start"])
            stroke_end = point(stroke["end"])
            color = (0.8, 0.08, 0.08) if dimension["status"] in {"unresolved", "invalid_overlap"} else (0.95, 0.45, 0.05)
            page.draw_line(stroke_start, stroke_end, color=color, width=1.4 * scale)

            # Keep the number on its original leader, separate from the pipe contour.
            label_point = point(dimension["label_center"])
            leader_stroke = dimension.get("leader_stroke")
            if leader_stroke:
                leader_target = point(leader_stroke["start"])
                page.draw_line(
                    point(leader_stroke["start"]),
                    point(leader_stroke["end"]),
                    color=color,
                    width=0.9 * scale,
                )
            else:
                leader_target = stroke_start
            page.draw_line(
                label_point,
                leader_target,
                color=color,
                width=0.8 * scale,
                dashes="[3 2] 0",
            )
            page.insert_text(
                label_point + (4 * scale, 3 * scale),
                dimension["text"],
                fontsize=10 * scale,
                fontname="helv",
                color=color,
            )

        page.insert_text(
            fitz.Point(24 * scale, 28 * scale),
            f"DIMENSION GRAPH  /  page {page_number}",
            fontsize=11 * scale,
            fontname="helv",
            color=(0.08, 0.12, 0.2),
        )
        output.save(str(output_pdf))
        output.close()


def save_skeleton_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    with fitz.open(str(pdf_path)) as document:
        source_page = document[page_number - 1]
        scale = CLEAN_PDF_SCALE
        output = fitz.open()
        page = output.new_page(width=source_page.rect.width * scale, height=source_page.rect.height * scale)

        dimensions = mapping.get("dimensions", [])
        def point(values: list[float]) -> fitz.Point:
            return fitz.Point(values[0] * scale, values[1] * scale)

        for edge in mapping.get("edges", []):
            page.draw_line(
                point(edge["start"]),
                point(edge["end"]),
                color=(0.06, 0.28, 0.72),
                width=2.0 * scale,
            )

        rendered_strokes = set()
        for dimension in dimensions:
            for stroke_key in ("dimension_stroke", "leader_stroke"):
                stroke = dimension.get(stroke_key)
                if not stroke:
                    continue
                stroke_id = (tuple(stroke["start"]), tuple(stroke["end"]))
                if stroke_id in rendered_strokes:
                    continue
                rendered_strokes.add(stroke_id)
                page.draw_line(
                    point(stroke["start"]),
                    point(stroke["end"]),
                    color=(0.95, 0.45, 0.05),
                    width=1.0 * scale,
                )
            for stroke in dimension.get("extension_strokes", []):
                stroke_id = (tuple(stroke["start"]), tuple(stroke["end"]))
                if stroke_id in rendered_strokes:
                    continue
                rendered_strokes.add(stroke_id)
                page.draw_line(
                    point(stroke["start"]),
                    point(stroke["end"]),
                    color=(0.95, 0.45, 0.05),
                    width=0.9 * scale,
                )

        for vertex in mapping.get("vertices", []):
            color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex["role"], (0, 0, 0))
            center = point([vertex["x"], vertex["y"]])
            page.draw_circle(center, 5 * scale, color=color, fill=color, width=1.0 * scale)
            page.insert_text(center + (7 * scale, -7 * scale), vertex["id"], fontsize=8 * scale, fontname="helv", color=color)

        for dimension in dimensions:
            label = point(dimension["label_center"])
            invalid = dimension.get("status") in {"invalid_overlap", "unresolved"}
            color = (0.85, 0.05, 0.05) if invalid else (0.0, 0.5, 0.12)
            page.insert_text(
                label + (3 * scale, 4 * scale),
                dimension["text"],
                fontsize=10 * scale,
                fontname="helv",
                color=color,
            )
            page.draw_rect(
                fitz.Rect(label.x - 2 * scale, label.y - 10 * scale, label.x + 34 * scale, label.y + 6 * scale),
                color=color,
                width=0.8 * scale,
            )

        page.insert_text(
            fitz.Point(24 * scale, 28 * scale),
            f"DIMENSION SKELETON  /  page {page_number}",
            fontsize=11 * scale,
            fontname="helv",
            color=(0.08, 0.12, 0.2),
        )
        output.save(str(output_pdf))
        output.close()


def run_dimension_mapping(pdf_path: str | Path, page_number: int, output_pdf: str | Path, output_json: str | Path) -> dict[str, Any]:
    mapping = map_dimensions(pdf_path, page_number)
    save_dimension_mapping_pdf(pdf_path, page_number, output_pdf, mapping)
    Path(output_json).write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping


__all__ = [
    "map_dimensions",
    "run_dimension_mapping",
    "save_dimension_mapping_pdf",
    "save_preprocess_annotation_pdf",
    "save_clean_graph_pdf",
    "save_skeleton_pdf",
]
