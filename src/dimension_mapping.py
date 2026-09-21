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


def _extract_sheet_number(text: str) -> str | None:
    if not text:
        return None
    patterns = (
        r"(?:ЛИСТ|SHEET)\s*[:№]?\s*(?:№\s*)?(\d+)",
        r"(?:SEE\s+SHEET|CONTINUATION)\s*(?:[A-Z0-9\-_/]+\s+)?(\d+)",
        r"(?:СМ\.?\s*(?:ЛИСТ|SHEET)\s*[:№]?\s*(?:№\s*)?(\d+))",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE)
        if match:
            return match.group(1)
    return None


def _connection_classify(text: str) -> tuple[str, str | None, bool]:
    normalized = (text or "").strip()
    if not normalized:
        return "other", None, False
    upper = normalized.upper()
    tie_in_keywords = (
        "ПОДКЛЮЧЕНИЕ",
        "ПОДСОЕДИНЕНИЕ",
        "ВРЕЗКА",
        "TIE-IN",
        "TIE IN",
        "CONNECTION",
    )
    if any(keyword in upper for keyword in tie_in_keywords):
        return "tie_in", None, False
    sheet_number = _extract_sheet_number(normalized)
    if sheet_number is not None:
        return "continuation", sheet_number, True
    continuation_keywords = (
        "СМ.",
        "СМ ",
        "SEE SHEET",
        "CONTINUATION",
        "ЛИСТ",
        "SHEET",
    )
    has_sheet_hint = any(keyword in upper for keyword in continuation_keywords)
    if has_sheet_hint:
        return "other", None, True
    return "other", None, False


def _connection_rows_from_page(
    page: Any,
    page_number: int,
    vertices: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Найти только основные connection-метки: подключение/см. лист/continuation."""
    words = page.get_text("words") or []
    if not words:
        return []
    lines: list[list[tuple[float, float, float, float, str]]] = []
    current: list[tuple[float, float, float, float, str]] = []
    last_y: float | None = None
    for item in words:
        if len(item) < 5:
            continue
        text = str(item[4]).strip()
        if not text:
            continue
        x0, y0, x1, y1 = float(item[0]), float(item[1]), float(item[2]), float(item[3])
        if current and (last_y is not None) and abs(y0 - last_y) > 8:
            lines.append(current)
            current = []
        current.append((x0, y0, x1, y1, text))
        last_y = (y0 + y1) / 2.0
    if current:
        lines.append(current)

    rows: list[dict[str, Any]] = []
    drawings = page.get_drawings()
    rectangles = mark_pipeline.find_rectangles(page, page.rect)
    known_vertices = vertices or []
    for index, line in enumerate(lines, start=1):
        label = " ".join(part[4] for part in sorted(line, key=lambda item: item[0]))
        upper = label.upper()
        if not any(keyword in upper for keyword in ("ПОДКЛЮЧЕНИЕ", "TIE-IN", "TIE IN", "СМ.", "SEE SHEET", "CONTINUATION", "ЛИСТ", "SHEET")):
            continue
        connection_type, target_sheet, has_sheet_hint = _connection_classify(label)
        if connection_type == "other" and not has_sheet_hint and not any(
            keyword in upper for keyword in ("ПОДКЛЮЧЕНИЕ", "TIE-IN", "TIE IN", "СМ.", "SEE SHEET", "CONTINUATION", "ЛИСТ", "SHEET")
        ):
            continue
        x0 = min(item[0] for item in line)
        y0 = min(item[1] for item in line)
        x1 = max(item[2] for item in line)
        y1 = max(item[3] for item in line)
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        text_rect = fitz.Rect(x0, y0, x1, y1)
        arrow_paths = _handwheel_arrow_paths([text_rect], drawings, rectangles)
        arrow_path = arrow_paths[0] if arrow_paths and arrow_paths[0] else []
        arrow_start = arrow_path[0][0] if arrow_path else None
        arrow_end = arrow_path[-1][1] if arrow_path else None
        nearest_vertex = None
        if arrow_end is not None:
            nearest_vertex = min(
                known_vertices,
                key=lambda vertex: math.hypot(
                    float(vertex["x"]) - arrow_end[0],
                    float(vertex["y"]) - arrow_end[1],
                ),
                default=None,
            )
        rows.append(
            {
                "id": f"CN-{page_number}-{index:03d}",
                "page": page_number,
                "label": label,
                "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                "center": [round(cx, 2), round(cy, 2)],
                "connection_type": connection_type,
                "vertex_id": nearest_vertex.get("id") if nearest_vertex else None,
                "target_point": [round(arrow_end[0], 2), round(arrow_end[1], 2)] if arrow_end else None,
                "target_sheet": target_sheet,
                "text_has_sheet_ref": has_sheet_hint,
                "arrow_found": bool(arrow_path),
                "arrow_start": [round(arrow_start[0], 2), round(arrow_start[1], 2)] if arrow_start else None,
                "arrow_end": [round(arrow_end[0], 2), round(arrow_end[1], 2)] if arrow_end else None,
                "arrow_segments": [
                    {
                        "start": [round(segment[0][0], 2), round(segment[0][1], 2)],
                        "end": [round(segment[1][0], 2), round(segment[1][1], 2)],
                    }
                    for segment in arrow_path
                ],
            }
        )
    return rows


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


def _merge_dimension_stroke(
    stroke: Any,
    strokes: list[Any],
    blocked_indices: set[int] | None = None,
) -> dict[str, Any]:
    blocked = blocked_indices or set()
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
            if candidate.index in used or candidate.index in blocked or candidate.kind != "dimension":
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


def _find_extension_strokes(
    dimension: dict[str, Any],
    edge: dict[str, Any] | None,
    strokes: list[Any],
    edges: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    target = dimension.get("dimension_stroke")
    if not target:
        return []
    candidate_edges = list(edges or [])
    if edge is not None and all(item.get("id") != edge.get("id") for item in candidate_edges):
        candidate_edges.insert(0, edge)
    if not candidate_edges:
        return []
    target_points = [tuple(target["start"]), tuple(target["end"])]
    target_indices = set(target.get("merged_indices", []))
    target_dx = target_points[1][0] - target_points[0][0]
    target_dy = target_points[1][1] - target_points[0][1]
    target_length = math.hypot(target_dx, target_dy) or 1.0
    candidates_by_endpoint: dict[int, tuple[float, dict[str, Any]]] = {}
    for stroke in strokes:
        if stroke.kind != "dimension" or stroke.index in target_indices:
            continue
        if stroke.length < 8.0:
            continue
        stroke_dx = stroke.x1 - stroke.x0
        stroke_dy = stroke.y1 - stroke.y0
        stroke_length = math.hypot(stroke_dx, stroke_dy) or 1.0
        parallel_score = abs((target_dx * stroke_dx + target_dy * stroke_dy) / (target_length * stroke_length))
        if parallel_score > 0.92:
            continue
        endpoints = [(stroke.x0, stroke.y0), (stroke.x1, stroke.y1)]
        variants = []
        for endpoint_index, target_point in enumerate(target_points):
            target_gap, _position, touch_point = _distance_to_segment(target_point, endpoints[0], endpoints[1])
            touch_endpoint_gap = min(math.hypot(touch_point[0] - point[0], touch_point[1] - point[1]) for point in endpoints)
            for pipe_candidate in endpoints:
                nearest_edge, pipe_gap = min(
                    (
                        (candidate_edge, _distance_to_segment(pipe_candidate, tuple(candidate_edge["start"]), tuple(candidate_edge["end"]))[0])
                        for candidate_edge in candidate_edges
                    ),
                    key=lambda item: item[1],
                )
                selected_bonus = 0.0 if edge is not None and nearest_edge.get("id") == edge.get("id") else 6.0
                variants.append((target_gap * 3.0 + touch_endpoint_gap + pipe_gap + parallel_score * 8.0 + selected_bonus, endpoint_index, target_gap, touch_endpoint_gap, pipe_gap, nearest_edge.get("id")))
        score, endpoint_index, target_gap, touch_endpoint_gap, pipe_gap, pipe_edge_id = min(variants, key=lambda item: item[0])
        short_stroke = stroke.length < 18.0
        target_limit = 5.0 if short_stroke else 9.0
        touch_endpoint_limit = 4.0 if short_stroke else 12.0
        pipe_limit = 12.0 if short_stroke else 36.0
        if target_gap <= target_limit and touch_endpoint_gap <= touch_endpoint_limit and pipe_gap <= pipe_limit:
            row = {
                "index": stroke.index,
                "start": [round(stroke.x0, 2), round(stroke.y0, 2)],
                "end": [round(stroke.x1, 2), round(stroke.y1, 2)],
                "length_px": round(stroke.length, 2),
                "target_endpoint": "start" if endpoint_index == 0 else "end",
                "target_gap_px": round(target_gap, 2),
                "touch_endpoint_gap_px": round(touch_endpoint_gap, 2),
                "pipe_gap_px": round(pipe_gap, 2),
                "pipe_edge_id": pipe_edge_id,
                "parallel_score": round(parallel_score, 4),
            }
            previous = candidates_by_endpoint.get(endpoint_index)
            if previous is None or score < previous[0]:
                candidates_by_endpoint[endpoint_index] = (score, row)
    return [item[1] for item in sorted(candidates_by_endpoint.values(), key=lambda item: item[0])][:2]


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
        attached_indices = {stroke.index for stroke in attached_strokes.values()}
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
                blocked_indices = attached_indices - {attached.index}
                initial_stroke = _merge_dimension_stroke(attached, vector_graph.strokes, blocked_indices)
                mapped[-1]["dimension_stroke"] = initial_stroke
                mapped[-1]["leader_stroke"] = None
                pipe_edge = next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None)
                if pipe_edge is not None and not _stroke_angle_matches_edge(attached, pipe_edge):
                    target = _resolve_leader_target(attached, pipe_edge, vector_graph.strokes)
                    if target is not None:
                        mapped[-1]["leader_stroke"] = initial_stroke
                        mapped[-1]["dimension_stroke"] = _merge_dimension_stroke(target, vector_graph.strokes, attached_indices - {target.index})
                        mapped[-1]["leader_attached"] = True
                        mapped[-1]["attachment_kind"] = "leader_to_dimension_arrow"
                else:
                    mapped[-1]["attachment_kind"] = "direct_dimension_arrow"
                _add_pipe_anchor(mapped[-1], edges)
                mapped[-1]["extension_strokes"] = _find_extension_strokes(
                    mapped[-1],
                    next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None),
                    vector_graph.strokes,
                    edges,
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
        connections = _connection_rows_from_page(page, page_number, vertices)
        return {
            "page_number": page_number,
            "vertices": vertices,
            "edges": edges,
            "dimensions": mapped,
            "connections": connections,
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
            status = dimension.get("status")
            if status in {"cross_sheet_reference", "unresolved", "invalid_overlap"}:
                continue
            color = (0.0, 0.55, 0.15)
            if status == "handwheel":
                color = (0.14, 0.39, 0.92)
            center = fitz.Point(*label_center)
            rect = fitz.Rect(center.x - 18, center.y - 10, center.x + 22, center.y + 10)
            marked.draw_rect(rect, color=color, fill=None, width=1.2)

        output.save(str(output_pdf))
        output.close()


def _handwheel_text_rects(words: list[tuple[Any, ...]]) -> list[tuple[str, fitz.Rect]]:
    rows: dict[tuple[Any, Any], list[tuple[Any, ...]]] = {}
    for word in words:
        text = str(word[4] or "").strip()
        if not text:
            continue
        key = (word[5], word[6])
        rows.setdefault(key, []).append(word)

    handwheel_rows = []
    for row_words in rows.values():
        if not any("штурвал" in str(word[4]).casefold() for word in row_words):
            continue
        row_words.sort(key=lambda word: (word[0], word[7]))
        rect = fitz.Rect(
            min(word[0] for word in row_words),
            min(word[1] for word in row_words),
            max(word[2] for word in row_words),
            max(word[3] for word in row_words),
        )
        handwheel_rows.append((" ".join(str(word[4]).strip() for word in row_words), rect))
    return handwheel_rows


def _distance_to_rect(point: tuple[float, float], rect: fitz.Rect) -> float:
    dx = max(rect.x0 - point[0], 0.0, point[0] - rect.x1)
    dy = max(rect.y0 - point[1], 0.0, point[1] - rect.y1)
    return math.hypot(dx, dy)


def _drawing_segments(
    drawings: list[dict[str, Any]],
    rectangles: list[fitz.Rect] | None = None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments = []
    for drawing in drawings:
        for item in drawing.get("items", []):
            if not item or item[0] not in {"l", "c"}:
                continue
            points = [point for point in item[1:] if hasattr(point, "x")]
            if len(points) < 2:
                continue
            start, end = points[0], points[-1]
            first = (float(start.x), float(start.y))
            second = (float(end.x), float(end.y))
            if math.hypot(second[0] - first[0], second[1] - first[1]) >= 6.0:
                if rectangles and _is_rectangle_side(first, second, rectangles):
                    continue
                segments.append((first, second))
    return segments


def _is_rectangle_side(
    start: tuple[float, float],
    end: tuple[float, float],
    rectangles: list[fitz.Rect],
    tolerance: float = 1.5,
) -> bool:
    for rect in rectangles:
        on_left = abs(start[0] - rect.x0) <= tolerance and abs(end[0] - rect.x0) <= tolerance
        on_right = abs(start[0] - rect.x1) <= tolerance and abs(end[0] - rect.x1) <= tolerance
        on_top = abs(start[1] - rect.y0) <= tolerance and abs(end[1] - rect.y0) <= tolerance
        on_bottom = abs(start[1] - rect.y1) <= tolerance and abs(end[1] - rect.y1) <= tolerance
        if (on_left or on_right) and min(start[1], end[1]) >= rect.y0 - tolerance and max(start[1], end[1]) <= rect.y1 + tolerance:
            return True
        if (on_top or on_bottom) and min(start[0], end[0]) >= rect.x0 - tolerance and max(start[0], end[0]) <= rect.x1 + tolerance:
            return True
    return False


def _has_explicit_arrowhead(
    tip: tuple[float, float],
    main_segment: tuple[tuple[float, float], tuple[float, float]],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> bool:
    main_length = math.hypot(
        main_segment[1][0] - main_segment[0][0],
        main_segment[1][1] - main_segment[0][1],
    )
    arrow_legs = []
    for start, end in segments:
        if (start, end) == main_segment or (end, start) == main_segment:
            continue
        start_gap = math.hypot(start[0] - tip[0], start[1] - tip[1])
        end_gap = math.hypot(end[0] - tip[0], end[1] - tip[1])
        if min(start_gap, end_gap) > 3.0:
            continue
        outer = end if start_gap <= end_gap else start
        leg_length = math.hypot(outer[0] - tip[0], outer[1] - tip[1])
        if 3.0 <= leg_length <= min(24.0, max(8.0, main_length * 0.3)):
            arrow_legs.append(outer)

    for first_index, first in enumerate(arrow_legs):
        for second in arrow_legs[first_index + 1:]:
            first_length = math.hypot(first[0] - tip[0], first[1] - tip[1]) or 1.0
            second_length = math.hypot(second[0] - tip[0], second[1] - tip[1]) or 1.0
            cosine = (
                (first[0] - tip[0]) * (second[0] - tip[0])
                + (first[1] - tip[1]) * (second[1] - tip[1])
            ) / (first_length * second_length)
            if -0.6 <= cosine <= 0.95:
                return True
    return False


def _handwheel_arrow_segments(
    text_rects: list[fitz.Rect],
    drawings: list[dict[str, Any]],
    rectangles: list[fitz.Rect] | None = None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [path[0] for path in _handwheel_arrow_paths(text_rects, drawings, rectangles)]


def _handwheel_arrow_paths(
    text_rects: list[fitz.Rect],
    drawings: list[dict[str, Any]],
    rectangles: list[fitz.Rect] | None = None,
) -> list[list[tuple[tuple[float, float], tuple[float, float]]]]:
    segments = _drawing_segments(drawings, rectangles)

    selected: list[list[tuple[tuple[float, float], tuple[float, float]]]] = []
    for text_rect in text_rects:
        candidates = []
        for segment_index, (start, end) in enumerate(segments):
            start_gap = _distance_to_rect(start, text_rect)
            end_gap = _distance_to_rect(end, text_rect)
            nearest_gap = min(start_gap, end_gap)
            if nearest_gap > 24.0:
                continue
            if end_gap < start_gap:
                start, end = end, start
            queue = [(segment_index, end, [(start, end)], {segment_index})]
            while queue:
                current_index, current_tip, path, visited = queue.pop(0)
                current_segment = path[-1]
                if _has_explicit_arrowhead(current_tip, current_segment, segments):
                    candidates.append((nearest_gap, path))
                    break
                if len(path) >= 12:
                    continue
                for next_index, (next_start, next_end) in enumerate(segments):
                    if next_index in visited:
                        continue
                    start_distance = math.hypot(next_start[0] - current_tip[0], next_start[1] - current_tip[1])
                    end_distance = math.hypot(next_end[0] - current_tip[0], next_end[1] - current_tip[1])
                    if min(start_distance, end_distance) > 3.0:
                        continue
                    if end_distance < start_distance:
                        next_start, next_end = next_end, next_start
                    queue.append((next_index, next_end, path + [(next_start, next_end)], visited | {next_index}))
        if candidates:
            _gap, path = min(candidates, key=lambda item: (item[0], len(item[1])))
            selected.append(path)
    return selected


def _first_arrow_third(segment: dict[str, list[float]]) -> tuple[list[float], list[float]]:
    start = segment["start"]
    end = segment["end"]
    return [
        start,
        [
            round(start[0] + (end[0] - start[0]) / 3.0, 2),
            round(start[1] + (end[1] - start[1]) / 3.0, 2),
        ],
    ]


def _handwheel_details(
    page: Any,
    mapping: dict[str, Any],
) -> list[dict[str, Any]]:
    text_rows = _handwheel_text_rects(page.get_text("words"))
    details: list[dict[str, Any]] = []
    edges = mapping.get("edges", [])
    drawings = page.get_drawings()
    rectangles = mark_pipeline.find_rectangles(page, page.rect)
    for index, (label, text_rect) in enumerate(text_rows, start=1):
        row: dict[str, Any] = {
            "id": f"HW-{index:03d}",
            "label": label,
            "bbox": [round(text_rect.x0, 2), round(text_rect.y0, 2), round(text_rect.x1, 2), round(text_rect.y1, 2)],
            "arrow_found": False,
            "arrow_start": None,
            "arrow_end": None,
            "edge_id": None,
            "edge_distance_px": None,
        }
        paths = _handwheel_arrow_paths([text_rect], drawings, rectangles)
        if not paths:
            details.append(row)
            continue
        path = paths[0]
        start = path[0][0]
        end = path[-1][1]
        row["arrow_found"] = True
        row["arrow_start"] = [round(start[0], 2), round(start[1], 2)]
        row["arrow_end"] = [round(end[0], 2), round(end[1], 2)]
        row["arrow_segments"] = [
            {
                "start": [round(segment[0][0], 2), round(segment[0][1], 2)],
                "end": [round(segment[1][0], 2), round(segment[1][1], 2)],
            }
            for segment in path
        ]
        nearest = []
        for edge in edges:
            gap, _position, _projection = _distance_to_segment(
                end,
                tuple(edge["start"]),
                tuple(edge["end"]),
            )
            nearest.append((gap, edge.get("id")))
        if nearest:
            gap, edge_id = min(nearest, key=lambda item: item[0])
            if gap <= 45.0:
                row["edge_id"] = edge_id
                row["edge_distance_px"] = round(gap, 2)
        if row["edge_id"] is None:
            row["edge_id"] = f"HW_EDGE_{index:03d}"
            row["edge_created"] = True
            row["edge_kind"] = "handwheel_attachment"
            row["is_pipe_edge"] = False
            row["edge_reason"] = "Для штурвала не найдено существующее трубное ребро; создано отдельное ребро привязки арматуры."
        else:
            row["edge_created"] = False
            row["edge_kind"] = "pipe_edge"
            row["is_pipe_edge"] = True
        details.append(row)
    return details


def save_clean_local_markup_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Pure QA overlay: vertices, dimension labels, and highlighted dimension geometry."""
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=page.rect.width, height=page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)

        handwheel_color = (0.14, 0.39, 0.92)
        handwheel_details = _handwheel_details(page, mapping)
        for item in handwheel_details:
            marked.draw_rect(
                fitz.Rect(*item["bbox"]),
                color=handwheel_color,
                fill=None,
                width=1.6,
            )
        for item in handwheel_details:
            if not item["arrow_found"]:
                continue
            segments = item.get("arrow_segments", [])
            if not segments:
                continue
            segment = segments[0]
            first_start, first_end = _first_arrow_third(segment)
            marked.draw_line(
                fitz.Point(*first_start),
                fitz.Point(*first_end),
                color=handwheel_color,
                width=1.6,
            )

        for vertex in mapping.get("vertices", []):
            color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex.get("role"), (0, 0, 0))
            center = fitz.Point(vertex["x"], vertex["y"])
            marked.draw_circle(center, 6, color=color, fill=color, width=1.2)
            marked.insert_text(center + (8, -8), vertex.get("id", "V?"), fontsize=8, fontname="helv", color=color)

        def _stroke_points(stroke: dict[str, Any]) -> tuple[fitz.Point, fitz.Point] | None:
            start = stroke.get("start")
            end = stroke.get("end")
            if not start or not end or len(start) < 2 or len(end) < 2:
                return None
            return fitz.Point(float(start[0]), float(start[1])), fitz.Point(float(end[0]), float(end[1]))

        def _stroke_key(stroke: dict[str, Any]) -> tuple[float, float, float, float] | None:
            points = _stroke_points(stroke)
            if points is None:
                return None
            start, end = points
            first = (round(start.x, 2), round(start.y, 2))
            second = (round(end.x, 2), round(end.y, 2))
            ordered = sorted((first, second))
            return (*ordered[0], *ordered[1])

        highlighted_strokes: set[tuple[float, float, float, float]] = set()
        dimension_line_color = (0.0, 0.72, 0.20)
        extension_line_color = (0.0, 0.86, 0.28)
        for dimension in mapping.get("dimensions", []):
            status = dimension.get("status")
            if status in {"cross_sheet_reference", "unresolved", "invalid_overlap"}:
                continue
            if status != "handwheel" and not dimension.get("valid", True):
                continue
            stroke_specs: list[tuple[dict[str, Any] | None, tuple[float, float, float], float]] = [
                (dimension.get("dimension_stroke"), dimension_line_color, 2.2),
                (dimension.get("leader_stroke"), extension_line_color, 1.7),
            ]
            stroke_specs.extend(
                (stroke, extension_line_color, 1.7)
                for stroke in (dimension.get("extension_strokes") or [])
                if isinstance(stroke, dict)
            )
            for stroke, color, width in stroke_specs:
                if not isinstance(stroke, dict):
                    continue
                key = _stroke_key(stroke)
                points = _stroke_points(stroke)
                if key is None or points is None or key in highlighted_strokes:
                    continue
                highlighted_strokes.add(key)
                marked.draw_line(points[0], points[1], color=color, width=width)

        for dimension in mapping.get("dimensions", []):
            label_center = dimension.get("label_center")
            if not label_center:
                continue
            status = dimension.get("status")
            if status in {"cross_sheet_reference", "unresolved", "invalid_overlap"}:
                continue
            if status != "handwheel" and not dimension.get("valid", True):
                continue
            color = (0.0, 0.55, 0.15)
            if status == "handwheel":
                color = (0.14, 0.39, 0.92)
            center = fitz.Point(*label_center)
            rect = fitz.Rect(center.x - 20, center.y - 12, center.x + 24, center.y + 12)
            marked.draw_rect(rect, color=color, fill=None, width=1.2)

        connection_color = (0.78, 0.30, 0.67)
        for connection in mapping.get("connections", []):
            bbox = connection.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            marked.draw_rect(
                fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                color=connection_color,
                fill=None,
                width=1.4,
            )
            label = connection.get("label") or connection.get("connection_type") or "connection"
            center = fitz.Point((float(bbox[0]) + float(bbox[2])) / 2.0, float(bbox[1]) - 8)
            marked.insert_text(center, str(label)[:30], fontsize=7, fontname="helv", color=connection_color)

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
