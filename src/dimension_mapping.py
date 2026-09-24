from __future__ import annotations

import json
import heapq
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
        has_explicit_sheet_reference = bool(re.search(r"\bЛИСТ\b\s*[:№]?\s*(?:№\s*)?\d+", label, flags=re.IGNORECASE | re.UNICODE))
        if not any(keyword in upper for keyword in ("ПОДКЛЮЧЕНИЕ", "TIE-IN", "TIE IN")) and not has_explicit_sheet_reference:
            continue
        connection_type, target_sheet, has_sheet_hint = _connection_classify(label)
        if connection_type == "other" and not has_sheet_hint and not any(keyword in upper for keyword in ("ПОДКЛЮЧЕНИЕ", "TIE-IN", "TIE IN")):
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


def _stroke_axis_interval(
    stroke: dict[str, Any],
    axis_stroke: dict[str, Any],
) -> tuple[float, float] | None:
    start = stroke.get("start")
    end = stroke.get("end")
    axis_start = axis_stroke.get("start")
    axis_end = axis_stroke.get("end")
    if not start or not end or not axis_start or not axis_end:
        return None
    ax = float(axis_end[0]) - float(axis_start[0])
    ay = float(axis_end[1]) - float(axis_start[1])
    axis_length = math.hypot(ax, ay)
    if axis_length <= 1e-6:
        return None
    unit_x = ax / axis_length
    unit_y = ay / axis_length
    origin_x = float(axis_start[0])
    origin_y = float(axis_start[1])
    values = []
    for point in (start, end):
        values.append((float(point[0]) - origin_x) * unit_x + (float(point[1]) - origin_y) * unit_y)
    return min(values), max(values)


def _dimension_contains_dimension(container: dict[str, Any], child: dict[str, Any]) -> bool:
    container_stroke = container.get("dimension_stroke")
    child_stroke = child.get("dimension_stroke")
    if not isinstance(container_stroke, dict) or not isinstance(child_stroke, dict):
        return False
    container_interval = _stroke_axis_interval(container_stroke, container_stroke)
    child_interval = _stroke_axis_interval(child_stroke, container_stroke)
    if container_interval is None or child_interval is None:
        return False
    container_length = max(1.0, container_interval[1] - container_interval[0])
    child_length = child_interval[1] - child_interval[0]
    if child_length <= 0:
        return False
    overlap_start = max(container_interval[0], child_interval[0])
    overlap_end = min(container_interval[1], child_interval[1])
    overlap = max(0.0, overlap_end - overlap_start)
    child_coverage = overlap / child_length
    overshoot = max(container_interval[0] - child_interval[0], child_interval[1] - container_interval[1], 0.0)
    return child_coverage >= 0.82 and overshoot <= max(8.0, 0.08 * container_length)


def _parallel_nested_dimension_lines(container: dict[str, Any], child: dict[str, Any]) -> bool:
    container_stroke = container.get("dimension_stroke")
    child_stroke = child.get("dimension_stroke")
    if not isinstance(container_stroke, dict) or not isinstance(child_stroke, dict):
        return False
    container_start = container_stroke.get("start")
    container_end = container_stroke.get("end")
    child_start = child_stroke.get("start")
    child_end = child_stroke.get("end")
    if not container_start or not container_end or not child_start or not child_end:
        return False
    cdx = float(container_end[0]) - float(container_start[0])
    cdy = float(container_end[1]) - float(container_start[1])
    sdx = float(child_end[0]) - float(child_start[0])
    sdy = float(child_end[1]) - float(child_start[1])
    container_length = math.hypot(cdx, cdy)
    child_length = math.hypot(sdx, sdy)
    if container_length <= 1e-6 or child_length <= 1e-6 or container_length < child_length * 1.08:
        return False
    direction_cosine = abs((cdx * sdx + cdy * sdy) / (container_length * child_length))
    if direction_cosine < 0.965:
        return False
    if not _dimension_contains_dimension(container, child):
        return False
    gaps = [
        _distance_to_segment(tuple(child_start), tuple(container_start), tuple(container_end))[0],
        _distance_to_segment(tuple(child_end), tuple(container_start), tuple(container_end))[0],
    ]
    average_gap = sum(gaps) / len(gaps)
    # A smaller line inside a larger parallel line is an obvious duplicate only when it
    # is drawn as a separate offset dimension line. Same-line chains stay out of this rule.
    return 6.0 <= average_gap <= 38.0


def _nested_by_merged_dimension_indices(container: dict[str, Any], child: dict[str, Any]) -> bool:
    container_stroke = container.get("dimension_stroke")
    child_stroke = child.get("dimension_stroke")
    if not isinstance(container_stroke, dict) or not isinstance(child_stroke, dict):
        return False
    container_indices = set(container_stroke.get("merged_indices") or [])
    child_indices = set(child_stroke.get("merged_indices") or [])
    if not container_indices or not child_indices or not child_indices < container_indices:
        return False
    container_length = float(container_stroke.get("length_px") or 0.0)
    child_length = float(child_stroke.get("length_px") or 0.0)
    if container_length <= child_length * 1.12:
        return False
    return _dimension_contains_dimension(container, child)


def _extension_pipe_edges(dimension: dict[str, Any]) -> set[str]:
    return {
        str(stroke.get("pipe_edge_id"))
        for stroke in (dimension.get("extension_strokes") or [])
        if isinstance(stroke, dict) and stroke.get("pipe_edge_id")
    }


def _extension_indices(dimension: dict[str, Any]) -> set[int]:
    indices: set[int] = set()
    for stroke in dimension.get("extension_strokes") or []:
        if not isinstance(stroke, dict):
            continue
        index = stroke.get("index")
        if isinstance(index, int):
            indices.add(index)
    return indices


def _dimension_stroke_indices(dimension: dict[str, Any]) -> set[int]:
    stroke = dimension.get("dimension_stroke")
    if not isinstance(stroke, dict):
        return set()
    return {
        int(index)
        for index in (stroke.get("merged_indices") or [])
        if isinstance(index, int)
    }


def _short_endpoint_leader_overlap(dimension: dict[str, Any]) -> bool:
    if dimension.get("attachment_kind") != "leader_to_dimension_arrow":
        return False
    if float(dimension.get("value") or 0.0) >= 250.0:
        return False
    stroke = dimension.get("dimension_stroke")
    leader = dimension.get("leader_stroke")
    resolution = dimension.get("leader_resolution") or {}
    if not isinstance(stroke, dict) or not isinstance(leader, dict):
        return False
    target_position = resolution.get("target_position")
    if target_position is None:
        return False
    # The leader hits the end of a very short dimension arrow: these are small local
    # callouts inside a larger chain, not a standalone covered section. If the leader
    # hits the middle, keep it green for now.
    return (
        float(stroke.get("length_px") or 0.0) <= 24.0
        and float(leader.get("length_px") or 0.0) >= 55.0
        and (float(target_position) <= 0.12 or float(target_position) >= 0.88)
    )


def _edge_point(edge: dict[str, Any], key: str) -> tuple[float, float] | None:
    value = edge.get(key)
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _segment_intersection(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> tuple[float, tuple[float, float]] | None:
    x1, y1 = first_start
    x2, y2 = first_end
    x3, y3 = second_start
    x4, y4 = second_end
    r_x = x2 - x1
    r_y = y2 - y1
    s_x = x4 - x3
    s_y = y4 - y3
    denominator = r_x * s_y - r_y * s_x
    if abs(denominator) <= 1e-9:
        return None
    q_x = x3 - x1
    q_y = y3 - y1
    first_position = (q_x * s_y - q_y * s_x) / denominator
    second_position = (q_x * r_y - q_y * r_x) / denominator
    if -0.02 <= first_position <= 1.02 and -0.02 <= second_position <= 1.02:
        clamped = max(0.0, min(1.0, first_position))
        return clamped, (x1 + clamped * r_x, y1 + clamped * r_y)
    return None


def _line_intersection_on_first_segment(
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
    line_start: tuple[float, float],
    line_end: tuple[float, float],
) -> tuple[float, tuple[float, float]] | None:
    x1, y1 = segment_start
    x2, y2 = segment_end
    x3, y3 = line_start
    x4, y4 = line_end
    r_x = x2 - x1
    r_y = y2 - y1
    s_x = x4 - x3
    s_y = y4 - y3
    denominator = r_x * s_y - r_y * s_x
    if abs(denominator) <= 1e-9:
        return None
    q_x = x3 - x1
    q_y = y3 - y1
    segment_position = (q_x * s_y - q_y * s_x) / denominator
    if -0.03 <= segment_position <= 1.03:
        clamped = max(0.0, min(1.0, segment_position))
        return clamped, (x1 + clamped * r_x, y1 + clamped * r_y)
    return None


def _line_intersection(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> tuple[float, tuple[float, float]] | None:
    x1, y1 = first_start
    x2, y2 = first_end
    x3, y3 = second_start
    x4, y4 = second_end
    r_x = x2 - x1
    r_y = y2 - y1
    s_x = x4 - x3
    s_y = y4 - y3
    denominator = r_x * s_y - r_y * s_x
    if abs(denominator) <= 1e-9:
        return None
    q_x = x3 - x1
    q_y = y3 - y1
    position = (q_x * s_y - q_y * s_x) / denominator
    return position, (x1 + position * r_x, y1 + position * r_y)


def _stroke_contact_on_edge(stroke: dict[str, Any], edge: dict[str, Any]) -> tuple[float, float, tuple[float, float]] | None:
    start = _edge_point(stroke, "start")
    end = _edge_point(stroke, "end")
    edge_start = _edge_point(edge, "start")
    edge_end = _edge_point(edge, "end")
    if start is None or end is None or edge_start is None or edge_end is None:
        return None
    intersection = _segment_intersection(edge_start, edge_end, start, end)
    if intersection is not None:
        position, point = intersection
        return 0.0, position, point
    candidates = [
        _distance_to_segment(start, edge_start, edge_end),
        _distance_to_segment(end, edge_start, edge_end),
    ]
    gap, position, projection = min(candidates, key=lambda item: item[0])
    return gap, position, projection


def _nearest_pipe_endpoint_for_extension(
    stroke: dict[str, Any],
    edges: list[dict[str, Any]],
) -> tuple[tuple[float, float], float, str | None] | None:
    start = _edge_point(stroke, "start")
    end = _edge_point(stroke, "end")
    if start is None or end is None or not edges:
        return None
    best: tuple[float, tuple[float, float], str | None] | None = None
    for point in (start, end):
        for edge in edges:
            edge_start = _edge_point(edge, "start")
            edge_end = _edge_point(edge, "end")
            if edge_start is None or edge_end is None:
                continue
            gap, _position, _projection = _distance_to_segment(point, edge_start, edge_end)
            if best is None or gap < best[0]:
                best = (gap, point, edge.get("id"))
    if best is None:
        return None
    gap, point, edge_id = best
    return point, gap, str(edge_id) if edge_id else None


def compute_endpoint_adjustments(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Suggest endpoint positions from the nearest pipe-side extension endpoint."""
    vertices = mapping.get("vertices") or []
    edges = mapping.get("edges") or []
    dimensions = mapping.get("dimensions") or []
    endpoint_vertices = [vertex for vertex in vertices if vertex.get("role") == "endpoint"]
    adjustments: list[dict[str, Any]] = []
    if not endpoint_vertices or not edges or not dimensions:
        mapping["endpoint_adjustments"] = adjustments
        return adjustments

    correct_endpoint_tolerance_px = 7.0
    max_nearest_extension_endpoint_gap_px = 80.0
    all_extensions: list[tuple[dict[str, Any], dict[str, Any], str | None, tuple[float, float], float, str | None]] = []
    for dimension in dimensions:
        for stroke in dimension.get("extension_strokes") or []:
            if not isinstance(stroke, dict):
                continue
            edge_id = stroke.get("pipe_edge_id") or dimension.get("edge_id")
            pipe_endpoint = _nearest_pipe_endpoint_for_extension(stroke, edges)
            if pipe_endpoint is None:
                continue
            point, pipe_gap, nearest_edge_id = pipe_endpoint
            all_extensions.append((dimension, stroke, str(edge_id) if edge_id else None, point, pipe_gap, nearest_edge_id))

    for vertex in endpoint_vertices:
        vertex_point = (float(vertex.get("x", 0.0)), float(vertex.get("y", 0.0)))
        if not all_extensions:
            continue
        nearest_dimension, nearest_stroke, declared_edge_id, pipe_endpoint, pipe_gap, nearest_edge_id = min(
            all_extensions,
            key=lambda item: _point_distance(vertex_point, item[3]),
        )
        pipe_endpoint_gap = _point_distance(vertex_point, pipe_endpoint)
        if pipe_endpoint_gap <= correct_endpoint_tolerance_px:
            continue
        if pipe_endpoint_gap > max_nearest_extension_endpoint_gap_px:
            continue

        candidates: list[tuple[float, float, float, dict[str, Any]]] = []
        for edge in edges:
            edge_id = edge.get("id")
            if not edge_id:
                continue
            edge_start = _edge_point(edge, "start")
            edge_end = _edge_point(edge, "end")
            if edge_start is None or edge_end is None:
                continue
            edge_length = float(edge.get("pixel_length") or _point_distance(edge_start, edge_end) or 0.0)
            start_gap = _point_distance(vertex_point, edge_start)
            end_gap = _point_distance(vertex_point, edge_end)
            if min(start_gap, end_gap) > 18.0:
                continue
            endpoint_at_start = start_gap <= end_gap
            stroke_start = _edge_point(nearest_stroke, "start")
            stroke_end = _edge_point(nearest_stroke, "end")
            if stroke_start is None or stroke_end is None:
                continue
            intersection = _line_intersection(edge_start, edge_end, stroke_start, stroke_end)
            if intersection is not None:
                edge_position, projection = intersection
                contact_gap = 0.0
            else:
                contact_gap, edge_position, projection = _distance_to_segment(pipe_endpoint, edge_start, edge_end)
            raw_distance_from_endpoint = edge_position if endpoint_at_start else 1.0 - edge_position
            distance_px = abs(raw_distance_from_endpoint) * edge_length
            if distance_px <= correct_endpoint_tolerance_px:
                continue
            if distance_px > 180.0:
                continue
            candidates.append(
                (
                    min(start_gap, end_gap),
                    0.0 if nearest_edge_id == str(edge_id) else 1.0,
                    contact_gap,
                    {
                        "vertex_id": vertex.get("id"),
                        "role": vertex.get("role"),
                        "source": "nearest_extension_endpoint",
                        "rule": "endpoint_to_nearest_extension_pipe_endpoint",
                        "original": [round(vertex_point[0], 2), round(vertex_point[1], 2)],
                        "adjusted": [round(projection[0], 2), round(projection[1], 2)],
                        "edge_id": edge_id,
                        "dimension_id": nearest_dimension.get("id"),
                        "extension_index": nearest_stroke.get("index"),
                        "pipe_endpoint": [round(pipe_endpoint[0], 2), round(pipe_endpoint[1], 2)],
                        "pipe_endpoint_gap_px": round(pipe_endpoint_gap, 2),
                        "pipe_gap_px": round(pipe_gap, 2),
                        "distance_from_endpoint_px": round(distance_px, 2),
                        "edge_position": round(edge_position, 4),
                        "contact_gap_px": round(contact_gap, 2),
                        "declared_pipe_edge_id": declared_edge_id,
                        "nearest_pipe_edge_id": nearest_edge_id,
                    },
                )
            )
        if candidates:
            _edge_gap, _edge_penalty, _contact_gap, adjustment = min(candidates, key=lambda item: (item[0], item[1], item[2]))
            adjustments.append(adjustment)
    mapping["endpoint_adjustments"] = adjustments
    return adjustments


def _ray_segment_hit(
    ray_start: tuple[float, float],
    direction: tuple[float, float],
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
    max_distance: float,
    back_tolerance: float = 6.0,
) -> tuple[float, tuple[float, float]] | None:
    """First point where a ray meets a segment; (distance along ray, point)."""
    dx, dy = direction
    s_x = segment_end[0] - segment_start[0]
    s_y = segment_end[1] - segment_start[1]
    denominator = dx * s_y - dy * s_x
    if abs(denominator) <= 1e-9:
        return None
    q_x = segment_start[0] - ray_start[0]
    q_y = segment_start[1] - ray_start[1]
    distance = (q_x * s_y - q_y * s_x) / denominator
    position = (q_x * dy - q_y * dx) / denominator
    if not (-back_tolerance <= distance <= max_distance):
        return None
    if not (-0.02 <= position <= 1.02):
        return None
    return distance, (ray_start[0] + distance * dx, ray_start[1] + distance * dy)


def _ray_segment_near_miss(
    ray_start: tuple[float, float],
    direction: tuple[float, float],
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
    max_distance: float,
    side_tolerance: float = 10.0,
    back_tolerance: float = 6.0,
) -> tuple[float, tuple[float, float], float] | None:
    """Closest segment point when a projected extension line passes just beside pipe contour."""
    dx, dy = direction
    nx, ny = -dy, dx
    candidates: list[tuple[float, tuple[float, float], float]] = []

    def add_candidate(point: tuple[float, float]) -> None:
        qx = point[0] - ray_start[0]
        qy = point[1] - ray_start[1]
        distance = qx * dx + qy * dy
        side_gap = abs(qx * nx + qy * ny)
        # A near-miss is still a forward ray hit.  Allowing points behind the
        # origin makes a branch close to the dimension marker win incorrectly.
        if 0.0 <= distance <= max_distance and side_gap <= side_tolerance:
            candidates.append((distance, point, side_gap))

    add_candidate(segment_start)
    add_candidate(segment_end)

    side_start = (segment_start[0] - ray_start[0]) * nx + (segment_start[1] - ray_start[1]) * ny
    side_end = (segment_end[0] - ray_start[0]) * nx + (segment_end[1] - ray_start[1]) * ny
    if abs(side_start - side_end) > 1e-9:
        position = side_start / (side_start - side_end)
        if -0.02 <= position <= 1.02:
            point = (
                segment_start[0] + (segment_end[0] - segment_start[0]) * position,
                segment_start[1] + (segment_end[1] - segment_start[1]) * position,
            )
            add_candidate(point)

    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[2], abs(item[0])))


EXTENSION_VERTEX_MAX_PROJECTION_PX = 90.0
EXTENSION_VERTEX_MERGE_PX = 9.0
EXTENSION_VERTEX_REPLACE_PX = 11.0
EXTENSION_VERTEX_NEAR_MISS_PX = 16.0
EXTENSION_VERTEX_NODE_SNAP_SIDE_PX = 6.0
EXTENSION_VERTEX_NODE_SNAP_DISTANCE_PX = 22.0


def _edges_touch(first: dict[str, Any], second: dict[str, Any], tolerance: float = 12.0) -> bool:
    first_points = (_edge_point(first, "start"), _edge_point(first, "end"))
    second_points = (_edge_point(second, "start"), _edge_point(second, "end"))
    return any(
        p1 is not None and p2 is not None and _point_distance(p1, p2) <= tolerance
        for p1 in first_points
        for p2 in second_points
    )


def _dimension_target_points(dimension: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    stroke = dimension.get("dimension_stroke") or {}
    start = _edge_point(stroke, "start")
    end = _edge_point(stroke, "end")
    if start is not None and end is not None:
        return start, end
    center = dimension.get("label_center") or [0.0, 0.0]
    point = (float(center[0]), float(center[1]))
    return point, point


def _snap_near_miss_to_source_vertex(
    mapping: dict[str, Any],
    ray_start: tuple[float, float],
    direction: tuple[float, float],
    point: tuple[float, float],
) -> tuple[float, float] | None:
    dx, dy = direction
    nx, ny = -dy, dx
    candidates = []
    for vertex in mapping.get("vertices") or []:
        role = str(vertex.get("role") or "")
        if role not in {"junction", "corner", "endpoint"}:
            continue
        vertex_point = (float(vertex.get("x", 0.0)), float(vertex.get("y", 0.0)))
        qx = vertex_point[0] - ray_start[0]
        qy = vertex_point[1] - ray_start[1]
        distance = qx * dx + qy * dy
        if not (-6.0 <= distance <= EXTENSION_VERTEX_MAX_PROJECTION_PX):
            continue
        side_gap = abs(qx * nx + qy * ny)
        if side_gap > EXTENSION_VERTEX_NODE_SNAP_SIDE_PX:
            continue
        point_gap = _point_distance(point, vertex_point)
        if point_gap > EXTENSION_VERTEX_NODE_SNAP_DISTANCE_PX:
            continue
        role_rank = 0 if role == "junction" else 1 if role == "corner" else 2
        candidates.append((side_gap, role_rank, point_gap, abs(distance), vertex_point))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[:4])[4]


def _extension_vertex_candidate_for_stroke(
    dimension: dict[str, Any],
    stroke: dict[str, Any],
    edges: list[dict[str, Any]],
    edge_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    start = _edge_point(stroke, "start")
    end = _edge_point(stroke, "end")
    if start is None or end is None:
        return None
    length = _point_distance(start, end)
    if length <= 1e-6:
        return None
    target_points = _dimension_target_points(dimension)
    target_endpoint = stroke.get("target_endpoint")
    if target_endpoint == "start":
        dimension_side_target = target_points[0]
    elif target_endpoint == "end":
        dimension_side_target = target_points[1]
    else:
        dimension_side_target = min(
            target_points,
            key=lambda point: _distance_to_segment(point, start, end)[0],
        )
    target_gap, _target_position, target_touch = _distance_to_segment(dimension_side_target, start, end)
    start_to_dimension = _point_distance(start, dimension_side_target)
    end_to_dimension = _point_distance(end, dimension_side_target)
    if start_to_dimension <= end_to_dimension:
        dimension_side = start
        ray_start = end
    else:
        dimension_side = end
        ray_start = start
    direction = (
        (ray_start[0] - dimension_side[0]) / length,
        (ray_start[1] - dimension_side[1]) / length,
    )

    declared_edge = edge_by_id.get(stroke.get("pipe_edge_id"))
    ordered_edges: list[dict[str, Any]] = []
    if declared_edge is not None:
        ordered_edges.append(declared_edge)
        ordered_edges.extend(
            edge
            for edge in edges
            if edge is not declared_edge and _edges_touch(edge, declared_edge)
        )
    else:
        ordered_edges.extend(edges)

    ray_options: list[tuple[int, str, tuple[float, float], tuple[float, float]]] = [(0, "opposite_dimension_endpoint", ray_start, direction)]
    if target_gap <= 3.0:
        for endpoint_name, endpoint in (("start", start), ("end", end)):
            endpoint_gap = _point_distance(endpoint, target_touch)
            if endpoint_gap <= 3.0:
                continue
            endpoint_direction = (
                (endpoint[0] - target_touch[0]) / endpoint_gap,
                (endpoint[1] - target_touch[1]) / endpoint_gap,
            )
            ray_options.append((1, f"target_touch_to_{endpoint_name}", endpoint, endpoint_direction))

    unique_ray_options: list[tuple[int, str, tuple[float, float], tuple[float, float]]] = []
    for option in ray_options:
        _priority, _source, option_start, option_direction = option
        if any(
            _point_distance(option_start, existing_start) <= 0.5
            and abs(option_direction[0] - existing_direction[0]) <= 0.02
            and abs(option_direction[1] - existing_direction[1]) <= 0.02
            for _existing_priority, _existing_source, existing_start, existing_direction in unique_ray_options
        ):
            continue
        unique_ray_options.append(option)

    best_hit = None
    for ray_priority, ray_source, option_start, option_direction in unique_ray_options:
        for edge in ordered_edges:
            edge_start = _edge_point(edge, "start")
            edge_end = _edge_point(edge, "end")
            if edge_start is None or edge_end is None:
                continue
            edge_rank = 0 if edge is declared_edge else 1
            candidate = _ray_segment_hit(
                option_start,
                option_direction,
                edge_start,
                edge_end,
                EXTENSION_VERTEX_MAX_PROJECTION_PX,
            )
            if candidate is not None:
                distance, point = candidate
                score = (0, edge_rank, ray_priority, abs(distance))
                row = (score, distance, point, 0.0, edge, option_start, ray_source)
                if best_hit is None or row[0] < best_hit[0]:
                    best_hit = row
                continue
            near_candidate = _ray_segment_near_miss(
                option_start,
                option_direction,
                edge_start,
                edge_end,
                EXTENSION_VERTEX_MAX_PROJECTION_PX,
                side_tolerance=EXTENSION_VERTEX_NEAR_MISS_PX,
            )
            if near_candidate is None:
                continue
            distance, point, side_gap = near_candidate
            score = (1, edge_rank, ray_priority, side_gap, abs(distance))
            row = (score, distance, point, side_gap, edge, option_start, ray_source)
            if best_hit is None or row[0] < best_hit[0]:
                best_hit = row
    if best_hit is None:
        return None
    _score, distance, point, side_gap, hit_edge, used_ray_start, ray_source = best_hit
    return {
        "dimension_id": dimension.get("id"),
        "extension_index": stroke.get("index"),
        "pipe_edge_id": hit_edge.get("id"),
        "ray_start": [round(used_ray_start[0], 2), round(used_ray_start[1], 2)],
        "ray_source": ray_source,
        "point": [round(point[0], 2), round(point[1], 2)],
        "projection_gap_px": round(distance, 2),
        "side_gap_px": round(side_gap, 2),
        "projection_kind": "near_miss" if side_gap > 0 else "intersection",
    }


def compute_extension_vertices(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Отметить новые вершины там, где выносные линии (продолженные до трубы) её пересекают.

    Для каждого валидного (include) размера берём его выносные линии (не lead), продолжаем
    каждую мнимой линией в сторону трубы и точку пересечения с ребром трубы отмечаем
    как новую вершину. Близкие вершины схлопываются в одну.
    """
    edges = mapping.get("edges") or []
    edge_by_id = {edge.get("id"): edge for edge in edges if edge.get("id")}

    def edge_gap(point: tuple[float, float], edge: dict[str, Any] | None) -> float:
        if edge is None:
            return float("inf")
        return _distance_to_segment(point, tuple(edge["start"]), tuple(edge["end"]))[0]

    candidates: list[dict[str, Any]] = []
    for dimension in mapping.get("dimensions") or []:
        if dimension.get("local_filter_decision") != "include":
            continue
        if not isinstance(dimension.get("dimension_stroke"), dict):
            continue
        for stroke in dimension.get("extension_strokes") or []:
            if not isinstance(stroke, dict):
                continue
            start = _edge_point(stroke, "start")
            end = _edge_point(stroke, "end")
            if start is None or end is None:
                continue
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                continue
            target_points = _dimension_target_points(dimension)
            target_endpoint = stroke.get("target_endpoint")
            if target_endpoint == "start":
                dimension_side_target = target_points[0]
            elif target_endpoint == "end":
                dimension_side_target = target_points[1]
            else:
                dimension_side_target = min(
                    target_points,
                    key=lambda point: _distance_to_segment(point, start, end)[0],
                )
            target_gap, _target_position, target_touch = _distance_to_segment(dimension_side_target, start, end)
            start_to_dimension = _point_distance(start, dimension_side_target)
            end_to_dimension = _point_distance(end, dimension_side_target)
            if start_to_dimension <= end_to_dimension:
                dimension_side = start
                ray_start = end
            else:
                dimension_side = end
                ray_start = start
            direction = (
                (ray_start[0] - dimension_side[0]) / length,
                (ray_start[1] - dimension_side[1]) / length,
            )

            declared_edge = edge_by_id.get(stroke.get("pipe_edge_id"))

            ordered_edges = []
            if declared_edge is not None:
                ordered_edges.append(declared_edge)
                ordered_edges.extend(
                    edge
                    for edge in edges
                    if edge is not declared_edge and _edges_touch(edge, declared_edge)
                )
            else:
                ordered_edges.extend(edges)

            ray_options: list[tuple[int, str, tuple[float, float], tuple[float, float]]] = [(0, "opposite_dimension_endpoint", ray_start, direction)]
            if target_gap <= 3.0:
                for endpoint_name, endpoint in (("start", start), ("end", end)):
                    endpoint_gap = _point_distance(endpoint, target_touch)
                    if endpoint_gap <= 3.0:
                        continue
                    endpoint_direction = (
                        (endpoint[0] - target_touch[0]) / endpoint_gap,
                        (endpoint[1] - target_touch[1]) / endpoint_gap,
                    )
                    ray_options.append((1, f"target_touch_to_{endpoint_name}", endpoint, endpoint_direction))

            unique_ray_options: list[tuple[int, str, tuple[float, float], tuple[float, float]]] = []
            for option in ray_options:
                _priority, _source, option_start, option_direction = option
                if any(
                    _point_distance(option_start, existing_start) <= 0.5
                    and abs(option_direction[0] - existing_direction[0]) <= 0.02
                    and abs(option_direction[1] - existing_direction[1]) <= 0.02
                    for _existing_priority, _existing_source, existing_start, existing_direction in unique_ray_options
                ):
                    continue
                unique_ray_options.append(option)

            best_hit = None
            for ray_priority, ray_source, option_start, option_direction in unique_ray_options:
                for edge in ordered_edges:
                    edge_start = _edge_point(edge, "start")
                    edge_end = _edge_point(edge, "end")
                    if edge_start is None or edge_end is None:
                        continue
                    edge_rank = 0 if edge is declared_edge else 1
                    candidate = _ray_segment_hit(
                        option_start,
                        option_direction,
                        edge_start,
                        edge_end,
                        EXTENSION_VERTEX_MAX_PROJECTION_PX,
                    )
                    if candidate is not None:
                        distance, point = candidate
                        score = (0, edge_rank, ray_priority, abs(distance))
                        row = (score, distance, point, 0.0, edge, option_start, ray_source, option_direction)
                        if best_hit is None or row[0] < best_hit[0]:
                            best_hit = row
                        continue
                    near_candidate = _ray_segment_near_miss(
                        option_start,
                        option_direction,
                        edge_start,
                        edge_end,
                        EXTENSION_VERTEX_MAX_PROJECTION_PX,
                        side_tolerance=EXTENSION_VERTEX_NEAR_MISS_PX,
                    )
                    if near_candidate is None:
                        continue
                    distance, point, side_gap = near_candidate
                    score = (1, edge_rank, ray_priority, side_gap, abs(distance))
                    row = (score, distance, point, side_gap, edge, option_start, ray_source, option_direction)
                    if best_hit is None or row[0] < best_hit[0]:
                        best_hit = row
            if best_hit is None:
                continue
            _score, _distance, point, side_gap, hit_edge, used_ray_start, ray_source, used_direction = best_hit
            if side_gap > 0:
                snapped_point = _snap_near_miss_to_source_vertex(mapping, used_ray_start, used_direction, point)
                if snapped_point is not None:
                    point = snapped_point
            candidates.append(
                {
                    "dimension_id": dimension.get("id"),
                    "extension_index": stroke.get("index"),
                    "pipe_edge_id": hit_edge.get("id"),
                    "ray_start": [round(used_ray_start[0], 2), round(used_ray_start[1], 2)],
                    "ray_source": ray_source,
                    "point": [round(point[0], 2), round(point[1], 2)],
                    "projection_gap_px": round(_distance, 2),
                    "side_gap_px": round(side_gap, 2),
                    "projection_kind": "near_miss" if side_gap > 0 else "intersection",
                }
            )

    vertices: list[dict[str, Any]] = []
    for candidate in candidates:
        point = tuple(candidate["point"])
        merged = next(
            (vertex for vertex in vertices if _point_distance(tuple(vertex["point"]), point) <= EXTENSION_VERTEX_MERGE_PX),
            None,
        )
        if merged is None:
            vertices.append(
                {
                    "dimension_ids": [candidate["dimension_id"]],
                    "extension_indices": [candidate["extension_index"]],
                    "pipe_edge_id": candidate["pipe_edge_id"],
                    "ray_start": candidate["ray_start"],
                    "ray_source": candidate.get("ray_source", "opposite_dimension_endpoint"),
                    "point": candidate["point"],
                    "projection_gap_px": candidate["projection_gap_px"],
                    "side_gap_px": candidate.get("side_gap_px", 0.0),
                    "projection_kind": candidate.get("projection_kind", "intersection"),
                }
            )
        else:
            merged["dimension_ids"].append(candidate["dimension_id"])
            merged["extension_indices"].append(candidate["extension_index"])
            if abs(float(candidate["projection_gap_px"])) < abs(float(merged["projection_gap_px"])):
                merged["point"] = candidate["point"]
                merged["projection_gap_px"] = candidate["projection_gap_px"]
                merged["pipe_edge_id"] = candidate["pipe_edge_id"]
                merged["ray_start"] = candidate["ray_start"]
                merged["ray_source"] = candidate.get("ray_source", "opposite_dimension_endpoint")
                merged["side_gap_px"] = candidate.get("side_gap_px", 0.0)
                merged["projection_kind"] = candidate.get("projection_kind", "intersection")
    for index, vertex in enumerate(vertices, start=1):
        vertex["id"] = f"VE-{index:02d}"

    suppressed_vertex_ids: list[str] = []
    for source_vertex in mapping.get("vertices") or []:
        source_id = source_vertex.get("id")
        if not source_id:
            continue
        source_point = (float(source_vertex.get("x", 0.0)), float(source_vertex.get("y", 0.0)))
        replacement = next(
            (
                vertex
                for vertex in vertices
                if _point_distance(source_point, tuple(vertex["point"])) <= EXTENSION_VERTEX_REPLACE_PX
            ),
            None,
        )
        if replacement is None:
            continue
        suppressed_vertex_ids.append(str(source_id))
        replacement.setdefault("replaces_vertex_ids", []).append(str(source_id))

    mapping["extension_vertices"] = vertices
    mapping["suppressed_vertex_ids"] = suppressed_vertex_ids
    return vertices


def apply_local_dimension_filter(mapping: dict[str, Any]) -> dict[str, Any]:
    """Mark obvious local include/exclude decisions without provider input."""
    dimensions = mapping.get("dimensions") or []
    edges = mapping.get("edges") or []
    edge_by_id = {edge.get("id"): edge for edge in edges if edge.get("id")}

    for dimension in dimensions:
        status = dimension.get("status")
        valid = dimension.get("valid", True)
        if status in {"cross_sheet_reference", "unresolved"} or valid is False:
            if status == "invalid_overlap":
                dimension["local_filter_decision"] = "exclude"
                dimension["local_filter_reason"] = dimension.get("reason") or "overlaps_larger_dimension"
            else:
                dimension["local_filter_decision"] = "ambiguous"
                dimension["local_filter_reason"] = dimension.get("reason") or status or "not_enough_geometry"
            continue
        if status == "handwheel":
            dimension["local_filter_decision"] = "exclude"
            dimension["local_filter_reason"] = "handwheel_dimension"
            continue
        dimension["local_filter_decision"] = "include"
        dimension["local_filter_reason"] = "no_larger_covering_dimension_found"

    comparable = [
        item
        for item in dimensions
        if item.get("local_filter_decision") == "include"
        and isinstance(item.get("dimension_stroke"), dict)
        and item.get("value") is not None
    ]
    for index, first in enumerate(comparable):
        for second in comparable[index + 1:]:
            same_edge = first.get("edge_id") and first.get("edge_id") == second.get("edge_id")
            same_contour = _same_directed_contour(first, second, edge_by_id)
            if float(first["value"]) == float(second["value"]):
                continue
            smaller, larger = (first, second) if float(first["value"]) < float(second["value"]) else (second, first)
            smaller_is_leader_callout = smaller.get("attachment_kind") == "leader_to_dimension_arrow"
            shared_extension_reference = (
                bool(_extension_indices(larger) & _extension_indices(smaller))
                or bool(_extension_pipe_edges(larger) & _extension_pipe_edges(smaller))
            )
            value_ratio = float(smaller["value"]) / max(float(larger["value"]), 1.0)
            substantial_nested_dimension = float(smaller["value"]) >= 500.0 and value_ratio >= 0.18
            nested_on_section = (not smaller_is_leader_callout) and (same_edge or same_contour) and _dimension_contains_dimension(larger, smaller)
            nested_parallel_line = (
                (not smaller_is_leader_callout)
                and (same_edge or same_contour or shared_extension_reference or substantial_nested_dimension)
                and _parallel_nested_dimension_lines(larger, smaller)
            )
            short_endpoint_overlap = same_edge and _short_endpoint_leader_overlap(smaller)
            large_leader_reference = smaller_is_leader_callout and float(smaller["value"]) >= 500.0 and shared_extension_reference
            if not nested_on_section and not nested_parallel_line and not short_endpoint_overlap and not large_leader_reference:
                continue
            smaller["local_filter_decision"] = "exclude"
            smaller["local_filter_reason"] = (
                "covered_by_short_endpoint_leader_overlap"
                if short_endpoint_overlap
                else "covered_by_shared_extension_reference"
                if large_leader_reference
                else "covered_by_larger_dimension_on_same_section"
            )
            smaller["local_filter_conflict_with"] = larger.get("id")

    excluded = [
        item
        for item in dimensions
        if item.get("local_filter_decision") == "exclude"
        and _dimension_stroke_indices(item)
    ]
    for dimension in dimensions:
        if dimension.get("local_filter_decision") != "include":
            continue
        dimension_indices = _dimension_stroke_indices(dimension)
        if not dimension_indices:
            continue
        for excluded_dimension in excluded:
            excluded_indices = _dimension_stroke_indices(excluded_dimension)
            same_stroke = dimension_indices == excluded_indices
            subset_of_excluded = bool(dimension_indices < excluded_indices)
            if not same_stroke and not subset_of_excluded:
                continue
            if dimension.get("attachment_kind") != "leader_to_dimension_arrow" and not same_stroke:
                continue
            dimension["local_filter_decision"] = "exclude"
            dimension["local_filter_reason"] = "shares_excluded_dimension_stroke"
            dimension["local_filter_conflict_with"] = excluded_dimension.get("id")
            break
    return mapping


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
    arrowhead_points: list[list[float]] = []
    used_strokes = [candidate for candidate in strokes if candidate.index in used]
    for candidate in used_strokes:
        endpoints = [
            ((candidate.x0, candidate.y0), bool(getattr(candidate, "arrowhead_start", False))),
            ((candidate.x1, candidate.y1), bool(getattr(candidate, "arrowhead_end", False))),
        ]
        for endpoint, has_arrowhead in endpoints:
            if not has_arrowhead:
                continue
            if min(
                math.hypot(endpoint[0] - start[0], endpoint[1] - start[1]),
                math.hypot(endpoint[0] - end[0], endpoint[1] - end[1]),
            ) <= 4.0:
                arrowhead_points.append([round(endpoint[0], 2), round(endpoint[1], 2)])
    return {
        "index": stroke.index,
        "start": [round(start[0], 2), round(start[1], 2)],
        "end": [round(end[0], 2), round(end[1], 2)],
        "length_px": round(math.hypot(end[0] - start[0], end[1] - start[1]), 2),
        "merged_indices": sorted(used),
        "arrowhead_points": arrowhead_points,
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


def _point_distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _stroke_endpoint_points(stroke: Any) -> list[tuple[float, float]]:
    return [(float(stroke.x0), float(stroke.y0)), (float(stroke.x1), float(stroke.y1))]


def _leader_target_endpoint(stroke: Any, label_center: tuple[float, float] | None = None) -> tuple[float, float]:
    endpoints = _stroke_endpoint_points(stroke)
    if label_center is None:
        return endpoints[1]
    label_gap_0 = _point_distance(endpoints[0], label_center)
    label_gap_1 = _point_distance(endpoints[1], label_center)
    return endpoints[1] if label_gap_0 <= label_gap_1 else endpoints[0]


def _leader_gap_follows_direction(
    stroke: Any,
    label_center: tuple[float, float] | None,
    target_endpoint: tuple[float, float],
    target_projection: tuple[float, float],
    *,
    min_cosine: float = 0.82,
) -> bool:
    gap_dx = target_projection[0] - target_endpoint[0]
    gap_dy = target_projection[1] - target_endpoint[1]
    gap_length = math.hypot(gap_dx, gap_dy)
    if gap_length <= 1.0:
        return True
    endpoints = _stroke_endpoint_points(stroke)
    if label_center is not None:
        label_gaps = [_point_distance(endpoint, label_center) for endpoint in endpoints]
        label_side = endpoints[0] if label_gaps[0] <= label_gaps[1] else endpoints[1]
    else:
        label_side = endpoints[0] if _point_distance(endpoints[1], target_endpoint) <= _point_distance(endpoints[0], target_endpoint) else endpoints[1]
    lead_dx = target_endpoint[0] - label_side[0]
    lead_dy = target_endpoint[1] - label_side[1]
    lead_length = math.hypot(lead_dx, lead_dy)
    if lead_length <= 0.01:
        return False
    cosine = (lead_dx * gap_dx + lead_dy * gap_dy) / (lead_length * gap_length)
    return cosine >= min_cosine


def _has_dimension_leader_arrowhead(
    stroke: dict[str, Any],
    label_center: tuple[float, float],
) -> bool:
    """A lead must have one arrowhead at the dimension-line side only."""
    start = tuple(stroke.get("start") or [])
    end = tuple(stroke.get("end") or [])
    if len(start) < 2 or len(end) < 2:
        return False
    target = end if _point_distance(end, label_center) >= _point_distance(start, label_center) else start
    label_side = start if target is end else end
    arrowheads = [tuple(point) for point in stroke.get("arrowhead_points") or []]
    target_has_arrowhead = any(_point_distance(point, target) <= 5.0 for point in arrowheads)
    starts_near_number = min(
        _point_distance(start, label_center),
        _point_distance(end, label_center),
    ) <= 28.0
    return target_has_arrowhead and starts_near_number


def _resolve_leader_target(
    stroke: Any,
    edge: dict[str, Any] | None,
    strokes: list[Any],
    label_center: tuple[float, float] | None = None,
    blocked_indices: set[int] | None = None,
) -> tuple[Any, dict[str, Any]] | None:
    blocked = blocked_indices or set()
    target_endpoint = _leader_target_endpoint(stroke, label_center)
    candidates = []
    for candidate in strokes:
        if candidate.index == stroke.index or candidate.index in blocked or candidate.kind != "dimension":
            continue
        target_gap, target_position, target_projection = _distance_to_segment(
            target_endpoint,
            (candidate.x0, candidate.y0),
            (candidate.x1, candidate.y1),
        )
        endpoint_gap = min(
            _point_distance(target_endpoint, endpoint)
            for endpoint in _stroke_endpoint_points(candidate)
        )
        leader_endpoint_gap = min(
            _distance_to_segment(endpoint, (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0]
            for endpoint in _stroke_endpoint_points(stroke)
        )
        if min(target_gap, leader_endpoint_gap) > 22.0:
            continue
        if target_gap > 1.0 and not _leader_gap_follows_direction(stroke, label_center, target_endpoint, target_projection):
            continue
        if candidate.length < 24.0 and target_gap > 1.0:
            continue
        edge_distance = 0.0
        angle_bonus = 0.0
        if edge is not None:
            edge_distance = min(
                _distance_to_segment((edge["start"][0], edge["start"][1]), (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0],
                _distance_to_segment((edge["end"][0], edge["end"][1]), (candidate.x0, candidate.y0), (candidate.x1, candidate.y1))[0],
            )
            angle_bonus = -8.0 if _stroke_angle_matches_edge(candidate, edge) else 0.0
        # Prefer the line actually touched by the leader endpoint. The old edge-angle
        # preference is only a weak bonus because projected edge_id is often wrong for
        # callout dimensions.
        score = target_gap * 4.0 + endpoint_gap * 0.25 + edge_distance * 0.12 + angle_bonus - min(candidate.length, 120.0) * 0.02
        candidates.append(
            (
                score,
                target_gap,
                -candidate.length,
                candidate,
                {
                    "leader_target_point": [round(target_endpoint[0], 2), round(target_endpoint[1], 2)],
                    "target_gap_px": round(target_gap, 2),
                    "target_position": round(target_position, 4),
                    "target_projection": [round(target_projection[0], 2), round(target_projection[1], 2)],
                    "candidate_index": candidate.index,
                    "candidate_length_px": round(candidate.length, 2),
                    "edge_distance_px": round(edge_distance, 2) if edge is not None else None,
                },
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    target = candidates[0][3]
    meta = candidates[0][4]
    suspect_reasons = []
    if float(meta.get("candidate_length_px") or 0.0) < 18.0:
        suspect_reasons.append("target_line_too_short")
    target_position = float(meta.get("target_position") or 0.0)
    if target_position <= 0.08 or target_position >= 0.92:
        suspect_reasons.append("target_hit_near_endpoint")
    if float(meta.get("target_gap_px") or 0.0) > 12.0:
        suspect_reasons.append("target_gap_too_large")
    if suspect_reasons:
        meta["suspect"] = True
        meta["suspect_reasons"] = suspect_reasons
    return target, meta


def _fallback_leader_stroke_from_label(
    label_center: tuple[float, float],
    strokes: list[Any],
    blocked_indices: set[int] | None = None,
) -> Any | None:
    blocked = blocked_indices or set()
    candidates = []
    for stroke in strokes:
        if stroke.index in blocked or stroke.kind != "dimension":
            continue
        if stroke.length < 10.0 or stroke.length > 85.0:
            continue
        segment_gap, position, projection = _distance_to_segment(
            label_center,
            (stroke.x0, stroke.y0),
            (stroke.x1, stroke.y1),
        )
        endpoint_gap = min(_point_distance(label_center, endpoint) for endpoint in _stroke_endpoint_points(stroke))
        if segment_gap > 14.0 and endpoint_gap > 18.0:
            continue
        # Prefer a line that starts/ends near the label. If the label projects into the
        # middle of a long line, it is probably the actual dimension line, not a lead.
        middle_penalty = 10.0 if 0.25 < position < 0.75 and endpoint_gap > 12.0 else 0.0
        candidates.append((segment_gap * 2.0 + endpoint_gap + middle_penalty + stroke.length * 0.03, stroke))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _direct_dimension_stroke_from_label(
    label_center: tuple[float, float],
    edge: dict[str, Any] | None,
    strokes: list[Any],
    blocked_indices: set[int] | None = None,
) -> Any | None:
    if edge is None:
        return None
    blocked = blocked_indices or set()
    candidates = []
    for stroke in strokes:
        if stroke.index in blocked or stroke.kind != "dimension":
            continue
        if stroke.length < 18.0 or stroke.length > 180.0:
            continue
        if not _stroke_angle_matches_edge(stroke, edge):
            continue
        segment_gap, position, _projection = _distance_to_segment(
            label_center,
            (stroke.x0, stroke.y0),
            (stroke.x1, stroke.y1),
        )
        if segment_gap > 8.0:
            continue
        endpoint_gap = min(_point_distance(label_center, endpoint) for endpoint in _stroke_endpoint_points(stroke))
        candidates.append((segment_gap * 3.0 + endpoint_gap * 0.15 + abs(position - 0.5), -stroke.length, stroke))
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


def _annotation_arrow_segments(
    page: Any,
    connections: list[dict[str, Any]] | None = None,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Collect arrow strokes that belong to handwheel labels and connection (СМ) marks.

    These annotations own their leader arrows; the arrows must never be reused as
    dimension strokes, extension lines or leaders of a real dimension.
    """
    drawings = page.get_drawings()
    rectangles = mark_pipeline.find_rectangles(page, page.rect)
    handwheel_rects = [rect for _label, rect in _handwheel_text_rects(page.get_text("words"))]
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for path in _handwheel_arrow_paths(handwheel_rects, drawings, rectangles):
        segments.extend(path)
    for row in connections or []:
        for segment in row.get("arrow_segments") or []:
            start = segment.get("start")
            end = segment.get("end")
            if start and end:
                segments.append(((float(start[0]), float(start[1])), (float(end[0]), float(end[1]))))
    return segments


def _reserved_annotation_strokes(
    strokes: list[Any],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    tolerance: float = 3.0,
) -> set[int]:
    """Stroke indices that lie on an annotation arrow and are therefore not dimensions."""
    reserved: set[int] = set()
    for stroke in strokes:
        endpoints = ((float(stroke.x0), float(stroke.y0)), (float(stroke.x1), float(stroke.y1)))
        for start, end in segments:
            if all(_distance_to_segment(point, start, end)[0] <= tolerance for point in endpoints):
                reserved.add(stroke.index)
                break
    return reserved


def _find_extension_strokes(
    dimension: dict[str, Any],
    edge: dict[str, Any] | None,
    strokes: list[Any],
    edges: list[dict[str, Any]] | None = None,
    blocked_indices: set[int] | None = None,
) -> list[dict[str, Any]]:
    target = dimension.get("dimension_stroke")
    if not target:
        return []
    blocked = blocked_indices or set()
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
    target_unit = (target_dx / target_length, target_dy / target_length)
    candidates_by_endpoint: dict[int, tuple[float, dict[str, Any]]] = {}
    for stroke in strokes:
        if stroke.kind != "dimension" or stroke.index in target_indices or stroke.index in blocked:
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
                tight_endpoint_hit = target_gap <= 1.5 and touch_endpoint_gap <= 5.5
                pipe_score = pipe_gap * (0.22 if tight_endpoint_hit else 1.0)
                endpoint_bonus = -42.0 if tight_endpoint_hit else 0.0
                tight_length_penalty = stroke.length * 0.55 if tight_endpoint_hit else 0.0
                variants.append((target_gap * 3.0 + touch_endpoint_gap + pipe_score + parallel_score * 8.0 + selected_bonus + endpoint_bonus + tight_length_penalty, endpoint_index, target_gap, touch_endpoint_gap, pipe_gap, nearest_edge.get("id")))
        score, endpoint_index, target_gap, touch_endpoint_gap, pipe_gap, pipe_edge_id = min(variants, key=lambda item: item[0])
        short_stroke = stroke.length < 18.0
        target_limit = 5.0 if short_stroke else 9.0
        touch_endpoint_limit = 4.0 if short_stroke else 12.0
        pipe_limit = 12.0 if short_stroke else 36.0
        relaxed_target_limit = 8.0 if short_stroke else 18.0
        relaxed_touch_endpoint_limit = 8.0 if short_stroke else 24.0
        relaxed_pipe_limit = 16.0 if short_stroke else 48.0
        tight_endpoint_hit = target_gap <= 1.5 and touch_endpoint_gap <= 5.5 and stroke.length <= max(42.0, target_length * 1.8)
        effective_pipe_limit = max(pipe_limit, 70.0) if tight_endpoint_hit else pipe_limit
        if target_gap <= target_limit and touch_endpoint_gap <= touch_endpoint_limit and pipe_gap <= effective_pipe_limit:
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
        elif (
            target_gap > target_limit
            and target_gap <= relaxed_target_limit
            and touch_endpoint_gap <= relaxed_touch_endpoint_limit
            and pipe_gap <= relaxed_pipe_limit
            and parallel_score < 0.82
        ):
            # Some PDFs split or trim extension lines so they stop a little before
            # the dimension arrow endpoint. Keep these as lower-priority fallback
            # candidates instead of letting a distant extension drive endpoint shifts.
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
                "fallback": "relaxed_extension_endpoint",
            }
            previous = candidates_by_endpoint.get(endpoint_index)
            relaxed_score = score + 18.0
            if previous is None or relaxed_score < previous[0]:
                candidates_by_endpoint[endpoint_index] = (relaxed_score, row)
    if len(candidates_by_endpoint) == 1:
        known_endpoint_index, (_known_score, known_row) = next(iter(candidates_by_endpoint.items()))
        missing_endpoint_index = 1 - known_endpoint_index
        known_start = tuple(known_row["start"])
        known_end = tuple(known_row["end"])
        known_center = ((known_start[0] + known_end[0]) / 2.0, (known_start[1] + known_end[1]) / 2.0)
        known_dx = known_end[0] - known_start[0]
        known_dy = known_end[1] - known_start[1]
        known_length = math.hypot(known_dx, known_dy) or 1.0
        expected_sign = 1.0 if missing_endpoint_index > known_endpoint_index else -1.0
        selected_indices = {int(item[1]["index"]) for item in candidates_by_endpoint.values() if item[1].get("index") is not None}

        def mirrored_candidates_for(
            mirror_points: list[tuple[float, float]],
            mirror_indices: set[int],
            source: str,
        ) -> list[tuple[float, dict[str, Any]]]:
            mirror_dx = mirror_points[1][0] - mirror_points[0][0]
            mirror_dy = mirror_points[1][1] - mirror_points[0][1]
            mirror_length = math.hypot(mirror_dx, mirror_dy) or 1.0
            mirror_unit = (mirror_dx / mirror_length, mirror_dy / mirror_length)
            output: list[tuple[float, dict[str, Any]]] = []
            for stroke in strokes:
                if stroke.kind != "dimension" or stroke.index in mirror_indices or stroke.index in selected_indices or stroke.index in blocked:
                    continue
                if stroke.length < 8.0 or stroke.length > 110.0:
                    continue
                stroke_dx = stroke.x1 - stroke.x0
                stroke_dy = stroke.y1 - stroke.y0
                stroke_length = math.hypot(stroke_dx, stroke_dy) or 1.0
                extension_parallel = abs((known_dx * stroke_dx + known_dy * stroke_dy) / (known_length * stroke_length))
                if extension_parallel < 0.90:
                    continue
                stroke_center = ((stroke.x0 + stroke.x1) / 2.0, (stroke.y0 + stroke.y1) / 2.0)
                center_delta = (stroke_center[0] - known_center[0], stroke_center[1] - known_center[1])
                along = center_delta[0] * mirror_unit[0] + center_delta[1] * mirror_unit[1]
                signed_along = along * expected_sign
                perpendicular = abs(center_delta[0] * -mirror_unit[1] + center_delta[1] * mirror_unit[0])
                distance_error = abs(signed_along - mirror_length)
                if signed_along <= mirror_length * 0.55:
                    continue
                if distance_error > max(22.0, mirror_length * 0.24):
                    continue
                perpendicular_limit = max(24.0 if mirror_length < 40.0 else 18.0, mirror_length * 0.18)
                if perpendicular > perpendicular_limit:
                    continue
                endpoints = [(stroke.x0, stroke.y0), (stroke.x1, stroke.y1)]
                missing_target = mirror_points[missing_endpoint_index]
                target_gap, _target_position, touch_point = _distance_to_segment(missing_target, endpoints[0], endpoints[1])
                if target_gap > max(34.0, mirror_length * 0.34):
                    continue
                touch_endpoint_gap = min(math.hypot(touch_point[0] - point[0], touch_point[1] - point[1]) for point in endpoints)
                nearest_edge, pipe_gap = min(
                    (
                        (candidate_edge, min(_distance_to_segment(point, tuple(candidate_edge["start"]), tuple(candidate_edge["end"]))[0] for point in endpoints))
                        for candidate_edge in candidate_edges
                    ),
                    key=lambda item: item[1],
                )
                row = {
                    "index": stroke.index,
                    "start": [round(stroke.x0, 2), round(stroke.y0, 2)],
                    "end": [round(stroke.x1, 2), round(stroke.y1, 2)],
                    "length_px": round(stroke.length, 2),
                    "target_endpoint": "start" if missing_endpoint_index == 0 else "end",
                    "target_gap_px": round(target_gap, 2),
                    "touch_endpoint_gap_px": round(touch_endpoint_gap, 2),
                    "pipe_gap_px": round(pipe_gap, 2),
                    "pipe_edge_id": nearest_edge.get("id"),
                    "parallel_score": round(abs((mirror_dx * stroke_dx + mirror_dy * stroke_dy) / (mirror_length * stroke_length)), 4),
                    "fallback": "mirrored_missing_extension",
                    "mirrored_from_index": known_row.get("index"),
                    "mirror_source": source,
                    "mirror_distance_error_px": round(distance_error, 2),
                    "mirror_perpendicular_gap_px": round(perpendicular, 2),
                }
                score = distance_error * 2.0 + perpendicular * 1.5 + target_gap * 0.5 + (1.0 - extension_parallel) * 35.0
                output.append((score, row))
            return output

        mirrored_candidates = mirrored_candidates_for(target_points, target_indices, "merged_dimension_stroke")
        if not mirrored_candidates and len(target_indices) > 1:
            base_stroke = next((stroke for stroke in strokes if stroke.index == target.get("index")), None)
            if base_stroke is not None:
                base_points = [(float(base_stroke.x0), float(base_stroke.y0)), (float(base_stroke.x1), float(base_stroke.y1))]
                mirrored_candidates = mirrored_candidates_for(base_points, {base_stroke.index}, "base_dimension_stroke")
        if mirrored_candidates:
            mirrored_candidates.sort(key=lambda item: item[0])
            candidates_by_endpoint[missing_endpoint_index] = (mirrored_candidates[0][0] + 36.0, mirrored_candidates[0][1])
    return [item[1] for item in sorted(candidates_by_endpoint.values(), key=lambda item: item[0])][:2]


def _local_processing_snapshot(mapping: dict[str, Any]) -> dict[str, Any]:
    """Return the pre-finalization geometry used by the length-analysis stage."""
    snapshot = dict(mapping)
    snapshot["analysis_stage"] = "local_processing"
    snapshot["base_vertices"] = [dict(vertex) for vertex in mapping.get("vertices") or []]
    snapshot["base_edges"] = [dict(edge) for edge in mapping.get("edges") or []]
    snapshot["local_decisions"] = [
        {
            "candidate_id": dimension.get("id"),
            "value_mm": dimension.get("value"),
            "decision": dimension.get("local_filter_decision"),
            "reason": dimension.get("local_filter_reason"),
            "conflict_with": dimension.get("local_filter_conflict_with"),
            "status": dimension.get("status"),
        }
        for dimension in mapping.get("dimensions") or []
        if dimension.get("id")
    ]
    # Handwheel locations are diagnostic evidence, not final V/E topology.
    annotations = mapping.get("handwheel_annotations") or {}
    snapshot["handwheel_annotations"] = {
        "handwheels": [dict(item) for item in annotations.get("handwheels") or []],
        "glyphs": [dict(item) for item in annotations.get("glyphs") or []],
    }
    # The snapshot deliberately has no final contour data.  Keep only the
    # base V/E layer and local candidate evidence for the next analysis.
    for key in (
        "final_vertices", "handwheel_vertices", "edge_segments", "final_contour",
        "handwheels", "handwheel_edges", "valve_edges", "handwheel_glyphs",
        "source_edges", "parent_edges",
    ):
        snapshot.pop(key, None)
    return snapshot


def map_dimensions(pdf_path: str | Path, page_number: int, *, finalize: bool = True) -> dict[str, Any]:
    pdf_path = str(pdf_path)
    with fitz.open(pdf_path) as document:
        page = document[page_number - 1]
        drawing_area, _format = mark_pipeline.get_drawing_area(page)
        dimensions, _rectangles, discarded = mark_pipeline.extract_dimension_numbers(page, drawing_area)

        edges = _stroke_graph(pdf_path, page_number)
        vertices = _vertex_rows(pdf_path, page_number)
        connections = _connection_rows_from_page(page, page_number, vertices)
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
        reserved_annotation_indices = _reserved_annotation_strokes(
            vector_graph.strokes,
            _annotation_arrow_segments(page, connections),
        )
        attached_strokes = {
            candidate_id: vector_graph.strokes[stroke_index]
            for candidate_id, stroke_index in vector_graph.attached.items()
            if 0 <= stroke_index < len(vector_graph.strokes)
            and stroke_index not in reserved_annotation_indices
        }
        attached_indices = {stroke.index for stroke in attached_strokes.values()}
        reserved_indices = attached_indices | reserved_annotation_indices
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
                        "bbox": [round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)],
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
                        "bbox": [round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)],
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
            attached_source = "vector_attachment"
            if attached is None:
                attached = _fallback_leader_stroke_from_label(center, vector_graph.strokes, reserved_indices)
                if attached is not None:
                    attached_source = "fallback_nearest_label_leader"
            if attached is not None:
                blocked_indices = reserved_indices - {attached.index}
                initial_stroke = _merge_dimension_stroke(attached, vector_graph.strokes, blocked_indices)
                mapped[-1]["dimension_stroke"] = initial_stroke
                mapped[-1]["leader_stroke"] = None
                mapped[-1]["attachment_source"] = attached_source
                pipe_edge = next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None)
                attached_is_leader = _has_dimension_leader_arrowhead(initial_stroke, center)
                should_resolve_leader = (
                    attached_is_leader
                    and (
                        pipe_edge is None
                        or not _stroke_angle_matches_edge(attached, pipe_edge)
                    )
                )
                if should_resolve_leader:
                    direct_stroke = _direct_dimension_stroke_from_label(
                        center,
                        pipe_edge,
                        vector_graph.strokes,
                        reserved_indices - {attached.index},
                    )
                    if direct_stroke is not None:
                        mapped[-1]["dimension_stroke"] = _merge_dimension_stroke(
                            direct_stroke,
                            vector_graph.strokes,
                            reserved_indices - {direct_stroke.index},
                        )
                        mapped[-1]["attachment_kind"] = "direct_dimension_arrow"
                        mapped[-1]["attachment_source"] = "near_label_direct_dimension"
                    else:
                        resolved = _resolve_leader_target(
                            attached,
                            pipe_edge,
                            vector_graph.strokes,
                            center,
                            {attached.index} | reserved_annotation_indices,
                        )
                        if resolved is not None:
                            target, leader_resolution = resolved
                            mapped[-1]["leader_stroke"] = initial_stroke
                            mapped[-1]["dimension_stroke"] = _merge_dimension_stroke(target, vector_graph.strokes, reserved_indices - {target.index})
                            mapped[-1]["leader_attached"] = True
                            mapped[-1]["attachment_kind"] = "leader_to_dimension_arrow"
                            mapped[-1]["leader_resolution"] = leader_resolution
                        else:
                            mapped[-1]["attachment_kind"] = "leader_target_unresolved"
                else:
                    mapped[-1]["attachment_kind"] = "direct_dimension_arrow"
                _add_pipe_anchor(mapped[-1], edges)
                mapped[-1]["extension_strokes"] = _find_extension_strokes(
                    mapped[-1],
                    next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None),
                    vector_graph.strokes,
                    edges,
                    blocked_indices=reserved_annotation_indices,
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

            # A leader may be drawn as several raw PDF sectors.  Prefer the
            # reconstructed chain when it can prove the path from the number
            # to an arrowhead touching a dimension line.
            raw_leader = _raw_dimension_leader_chain(page, center, _rectangles)
            if (
                raw_leader is not None
                and mapped[-1].get("status") not in {"cross_sheet_reference", "handwheel"}
                and mapped[-1].get("attachment_kind") != "direct_dimension_arrow"
            ):
                raw_leader_stroke, raw_dimension_stroke, raw_projection = raw_leader
                mapped[-1]["leader_stroke"] = raw_leader_stroke
                mapped[-1]["dimension_stroke"] = raw_dimension_stroke
                mapped[-1]["leader_attached"] = True
                mapped[-1]["attachment_kind"] = "leader_to_dimension_arrow"
                mapped[-1]["attachment_source"] = "raw_drawing_leader_chain"
                mapped[-1]["leader_resolution"] = {
                    "leader_target_point": list(raw_leader_stroke["end"]),
                    "target_gap_px": 0.0,
                    "target_projection": [round(raw_projection[0], 2), round(raw_projection[1], 2)],
                    "candidate_index": raw_dimension_stroke["index"],
                    "candidate_length_px": raw_dimension_stroke["length_px"],
                    "edge_distance_px": None,
                }
                raw_position = _distance_to_segment(
                    tuple(raw_projection),
                    tuple(raw_dimension_stroke["start"]),
                    tuple(raw_dimension_stroke["end"]),
                )[1]
                suspect_reasons = []
                if float(raw_dimension_stroke["length_px"] or 0.0) < 18.0:
                    suspect_reasons.append("target_line_too_short")
                if raw_position <= 0.08 or raw_position >= 0.92:
                    suspect_reasons.append("target_hit_near_endpoint")
                if suspect_reasons:
                    mapped[-1]["leader_resolution"]["suspect"] = True
                    mapped[-1]["leader_resolution"]["suspect_reasons"] = suspect_reasons
                mapped[-1]["extension_strokes"] = _find_extension_strokes(
                    mapped[-1],
                    next((item for item in edges if item["id"] == mapped[-1].get("edge_id")), None),
                    vector_graph.strokes,
                    edges,
                    blocked_indices=reserved_annotation_indices,
                )

            if mapped[-1].get("id") == "D002" and round(float(mapped[-1].get("value") or 0.0)) == 704:
                raw_segments = _drawing_segments(page.get_drawings(), _rectangles)
                mapped[-1]["debug_lead_segments"] = [
                    [[round(start[0], 2), round(start[1], 2)], [round(end[0], 2), round(end[1], 2)]]
                    for start, end in raw_segments
                    if min(_point_distance(start, center), _point_distance(end, center)) <= 36.0
                    and _point_distance(start, end) <= 90.0
                ]

        _mark_overlapping_dimensions(mapped, edges)
        result = {
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
        compute_endpoint_adjustments(result)
        apply_local_dimension_filter(result)
        result["handwheel_annotations"] = {
            "handwheels": _handwheel_details(page, result),
            "glyphs": _handwheel_glyph_rows(page),
        }
        if not finalize:
            return _local_processing_snapshot(result)
        compute_extension_vertices(result)
        result["handwheels"] = result["handwheel_annotations"]["handwheels"]
        annotate_valve_edges(result)
        result["handwheel_glyphs"] = result["handwheel_annotations"]["glyphs"]
        compute_final_vertices(result)
        split_edges_by_final_vertices(result)
        return result


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


def _segment_unit(
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float] | None:
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length <= 0.01:
        return None
    return ((end[0] - start[0]) / length, (end[1] - start[1]) / length)


def _path_direction_is_stable(
    path: list[tuple[tuple[float, float], tuple[float, float]]],
    near: tuple[float, float],
    far: tuple[float, float],
    *,
    min_cosine: float = 0.72,
) -> bool:
    overall = _segment_unit(near, far)
    if overall is None:
        return False
    previous: tuple[float, float] | None = None
    current = near
    for segment in path:
        start, end = segment
        next_point = end if _point_distance(current, start) <= _point_distance(current, end) else start
        unit = _segment_unit(current, next_point)
        if unit is None:
            return False
        if unit[0] * overall[0] + unit[1] * overall[1] < min_cosine:
            return False
        if previous is not None and unit[0] * previous[0] + unit[1] * previous[1] < min_cosine:
            return False
        previous = unit
        current = next_point
    return _point_distance(current, far) <= 5.0


def _leader_target_for_raw_segment(
    tip: tuple[float, float],
    used_indices: set[int],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    *,
    target_search_px: float,
    min_target_length_px: float,
) -> tuple[tuple[tuple[float, float], tuple[float, float]], tuple[float, float], float] | None:
    dimension_candidates = [
        segment
        for index, segment in enumerate(segments)
        if index not in used_indices
        and _point_distance(segment[0], segment[1]) >= min_target_length_px
        and _distance_to_segment(tip, segment[0], segment[1])[0] <= target_search_px
    ]
    if not dimension_candidates:
        return None
    dimension_segment = min(
        dimension_candidates,
        key=lambda segment: _distance_to_segment(tip, segment[0], segment[1])[0],
    )
    target_gap, position, projection = _distance_to_segment(
        tip,
        dimension_segment[0],
        dimension_segment[1],
    )
    if position < 0.08 or position > 0.92:
        return None
    return dimension_segment, projection, target_gap


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


HANDWHEEL_GLYPH_MIN_LEG_PX = 8.0
HANDWHEEL_GLYPH_MAX_LEG_PX = 90.0
HANDWHEEL_GLYPH_CROSS_MIN = 0.28
HANDWHEEL_GLYPH_CROSS_MAX = 0.72
HANDWHEEL_GLYPH_PAD_PX = 4.0
HANDWHEEL_GLYPH_ARROW_RADIUS_PX = 36.0
HANDWHEEL_GLYPH_TEXT_RADIUS_PX = 95.0


def _handwheel_glyph_parallelograms(
    drawings: list[dict[str, Any]],
    rectangles: list[fitz.Rect] | None = None,
) -> list[dict[str, Any]]:
    """Найти обозначения штурвалов: две короткие линии, крестящиеся около своих середин.

    Символ может быть наклонён в любую сторону (по x, y, z изометрии), поэтому
    параллелограмм строится по направлениям двух найденных линий крестики.
    """
    candidates: list[tuple[tuple[float, float], tuple[float, float], float, float, float, float]] = []
    for start, end in _drawing_segments(drawings, rectangles):
        length = _point_distance(start, end)
        if not (HANDWHEEL_GLYPH_MIN_LEG_PX <= length <= HANDWHEEL_GLYPH_MAX_LEG_PX):
            continue
        candidates.append((start, end, length, (end[0] - start[0]) / length, (end[1] - start[1]) / length))

    found: list[dict[str, Any]] = []
    for first_index, (first_start, first_end, first_length, first_ux, first_uy) in enumerate(candidates):
        dx_first = first_end[0] - first_start[0]
        dy_first = first_end[1] - first_start[1]
        for second_index in range(first_index + 1, len(candidates)):
            second_start, second_end, second_length, second_ux, second_uy = candidates[second_index]
            if first_length / second_length > 2.0 or second_length / first_length > 2.0:
                continue
            dx_second = second_end[0] - second_start[0]
            dy_second = second_end[1] - second_start[1]
            denominator = dx_first * dy_second - dy_first * dx_second
            if abs(denominator) <= 0.05 * first_length * second_length:
                continue
            q_x = second_start[0] - first_start[0]
            q_y = second_start[1] - first_start[1]
            first_position = (q_x * dy_second - q_y * dx_second) / denominator
            second_position = (q_x * dy_first - q_y * dx_first) / denominator
            if not (
                HANDWHEEL_GLYPH_CROSS_MIN <= first_position <= HANDWHEEL_GLYPH_CROSS_MAX
                and HANDWHEEL_GLYPH_CROSS_MIN <= second_position <= HANDWHEEL_GLYPH_CROSS_MAX
            ):
                continue
            cross = (
                first_start[0] + first_position * dx_first,
                first_start[1] + first_position * dy_first,
            )
            if any(_point_distance(cross, glyph["center"]) <= 10.0 for glyph in found):
                continue
            first_half = first_length * max(first_position, 1.0 - first_position) + HANDWHEEL_GLYPH_PAD_PX
            second_half = second_length * max(second_position, 1.0 - second_position) + HANDWHEEL_GLYPH_PAD_PX
            corners = []
            for sign_first in (-1.0, 1.0):
                for sign_second in (-1.0, 1.0):
                    corners.append(
                        [
                            round(cross[0] + sign_first * first_half * first_ux + sign_second * second_half * second_ux, 2),
                            round(cross[1] + sign_first * first_half * first_uy + sign_second * second_half * second_uy, 2),
                        ]
                    )
            found.append(
                {
                    "center": [round(cross[0], 2), round(cross[1], 2)],
                    "corners": corners,
                    "legs": [
                        [[round(first_start[0], 2), round(first_start[1], 2)], [round(first_end[0], 2), round(first_end[1], 2)]],
                        [[round(second_start[0], 2), round(second_start[1], 2)], [round(second_end[0], 2), round(second_end[1], 2)]],
                    ],
                }
            )
    for index, glyph in enumerate(found, start=1):
        glyph["id"] = f"HG-{index:02d}"
    return found


def _handwheel_glyph_rows(page: Any) -> list[dict[str, Any]]:
    rectangles = mark_pipeline.find_rectangles(page, page.rect)
    drawings = page.get_drawings()
    glyphs = _handwheel_glyph_parallelograms(drawings, rectangles)
    if not glyphs:
        return []

    text_rows = _handwheel_text_rects(page.get_text("words"))
    text_rects = [rect for _text, rect in text_rows]
    arrow_paths = _handwheel_arrow_paths(text_rects, drawings, rectangles)
    if not text_rects and not arrow_paths:
        return []

    filtered: list[dict[str, Any]] = []
    for glyph in glyphs:
        center_values = glyph.get("center") or []
        if len(center_values) < 2:
            continue
        center = (float(center_values[0]), float(center_values[1]))
        arrow_gap = min(
            (
                _distance_to_segment(center, tuple(segment[0]), tuple(segment[1]))[0]
                for path in arrow_paths
                for segment in path
            ),
            default=None,
        )
        text_gap = min((_distance_to_rect(center, rect) for rect in text_rects), default=None)
        if arrow_gap is not None and arrow_gap <= HANDWHEEL_GLYPH_ARROW_RADIUS_PX:
            glyph["matched_source"] = "handwheel_lead"
            glyph["matched_gap_px"] = round(arrow_gap, 2)
            filtered.append(glyph)
            continue
        if text_gap is not None and text_gap <= HANDWHEEL_GLYPH_TEXT_RADIUS_PX:
            glyph["matched_source"] = "handwheel_text"
            glyph["matched_gap_px"] = round(text_gap, 2)
            filtered.append(glyph)
    for index, glyph in enumerate(filtered, start=1):
        glyph["id"] = f"HG-{index:02d}"
    return filtered


def annotate_valve_edges(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    valve_edges: list[dict[str, Any]] = []
    edge_by_id = {edge.get("id"): edge for edge in mapping.get("edges") or [] if edge.get("id")}
    for item in mapping.get("handwheels") or []:
        edge_id = item.get("edge_id")
        if not edge_id or item.get("edge_created"):
            continue
        edge = edge_by_id.get(edge_id)
        if edge is None:
            continue
        handwheel_id = item.get("id")
        edge["element_type"] = "valve"
        edge["is_valve_edge"] = True
        edge["valve_source"] = "handwheel_lead"
        ids = edge.setdefault("valve_handwheel_ids", [])
        if handwheel_id and handwheel_id not in ids:
            ids.append(handwheel_id)
        valve_edges.append(
            {
                "edge_id": edge_id,
                "element_type": "valve",
                "source": "handwheel_lead",
                "handwheel_id": handwheel_id,
                "arrow_end": item.get("arrow_end"),
                "edge_distance_px": item.get("edge_distance_px"),
            }
        )
    mapping["valve_edges"] = valve_edges
    return valve_edges


def _dimension_projected_on_edge(dimension: dict[str, Any], edge_id: str | None) -> bool:
    if not edge_id:
        return False
    return dimension.get("edge_id") == edge_id


def _dimension_related_to_edge(dimension: dict[str, Any], edge_id: str | None) -> bool:
    if _dimension_projected_on_edge(dimension, edge_id):
        return True
    return bool(edge_id) and any(stroke.get("pipe_edge_id") == edge_id for stroke in dimension.get("extension_strokes") or [] if isinstance(stroke, dict))


HANDWHEEL_GLYPH_VERTEX_MAX_EDGE_GAP_PX = 45.0


def _glyph_pipe_vertex_rows(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Начало и конец штурвала на контуре трубы — по фактическим крайним точкам символа."""
    edges = mapping.get("edges") or []
    vertices: list[dict[str, Any]] = []
    for glyph in mapping.get("handwheel_glyphs") or []:
        legs = glyph.get("legs") or []
        center = glyph.get("center")
        if not center or len(center) < 2 or len(legs) != 2:
            continue
        symbol_points = [
            [float(point[0]), float(point[1])]
            for leg in legs
            for point in leg
            if isinstance(point, list) and len(point) >= 2
        ]
        if len(symbol_points) < 4:
            continue
        center_point = (float(center[0]), float(center[1]))
        best = None
        for edge in edges:
            gap, position, projection = _distance_to_segment(center_point, tuple(edge["start"]), tuple(edge["end"]))
            if best is None or gap < best[0]:
                best = (gap, edge, position, projection)
        if best is None or best[0] > HANDWHEEL_GLYPH_VERTEX_MAX_EDGE_GAP_PX:
            continue
        _gap, edge, center_position, center_projection = best
        matched_handwheel = min(
            (
                item
                for item in mapping.get("handwheels") or []
                if item.get("edge_id") == edge.get("id") and item.get("arrow_end")
            ),
            key=lambda item: _point_distance(
                center_projection,
                (float(item["arrow_end"][0]), float(item["arrow_end"][1])),
            ),
            default=None,
        )
        actual_handwheel_id = matched_handwheel.get("id") if matched_handwheel else glyph.get("id")
        edge_start = edge["start"]
        edge_end = edge["end"]
        edge_length = _point_distance(tuple(edge_start), tuple(edge_end)) or 1.0
        unit_x = (edge_end[0] - edge_start[0]) / edge_length
        unit_y = (edge_end[1] - edge_start[1]) / edge_length

        def offset_along(point: list[float]) -> float:
            return (point[0] - edge_start[0]) * unit_x + (point[1] - edge_start[1]) * unit_y

        symbol_offsets = [offset_along(point) for point in symbol_points]
        raw_low, raw_high = min(symbol_offsets), max(symbol_offsets)
        if min(raw_high, edge_length) - max(raw_low, 0.0) >= 1.0:
            # штурвал на трубе: вершины зажимаются границами ребра
            positions = [min(max(offset, 0.0), edge_length) for offset in symbol_offsets]
            position_by_role = {"A": min(positions), "B": max(positions)}
            clamped_template = True
        else:
            # штурвал в разрыве за концом ребра: вершины по фактическим крайним точкам символа
            position_by_role = {"A": raw_low, "B": raw_high}
            clamped_template = False
        for suffix, role in (("A", "start"), ("B", "end")):
            position = position_by_role[suffix]
            vertices.append(
                {
                    "id": f"{glyph['id']}-{suffix}",
                    "source": "handwheel_glyph_span",
                    "role": role,
                    "handwheel_id": actual_handwheel_id,
                    "glyph_id": glyph.get("id"),
                    "edge_id": edge.get("id"),
                    "source_point": [round(center_projection[0], 2), round(center_projection[1], 2)],
                    "symbol_points": [[round(point[0], 2), round(point[1], 2)] for point in symbol_points],
                    "symbol_span_px": [round(raw_low, 2), round(raw_high, 2)],
                    "point": [round(edge_start[0] + position * unit_x, 2), round(edge_start[1] + position * unit_y, 2)],
                }
            )
    return vertices


HANDWHEEL_VERTEX_DEDUP_PX = 28.0


def compute_final_vertices(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Собрать финальные VE/HG без общей дедупликации.

    HG имеет приоритет на своём участке трубы. Для каждого конца HG удаляется
    только ближайшая VE на том же ребре, если она находится не дальше 28 px.
    Если VE рядом нет (в том числе на конце трубы), HG остаётся единственной
    финальной вершиной.
    """
    handwheel_vertices = _glyph_pipe_vertex_rows(mapping)
    final: list[dict[str, Any]] = []
    extension_vertices = list(mapping.get("extension_vertices") or [])
    removed_extension_ids: set[str] = set()

    for row in handwheel_vertices:
        point = row.get("point")
        if not isinstance(point, list) or len(point) < 2:
            continue
        candidates = [
            vertex
            for vertex in extension_vertices
            if isinstance(vertex.get("point"), list)
            and len(vertex.get("point") or []) >= 2
            and _point_distance(tuple(point), tuple(vertex["point"])) <= HANDWHEEL_VERTEX_DEDUP_PX
        ]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda vertex: _point_distance(tuple(point), tuple(vertex["point"])))
        vertex_id = nearest.get("id")
        if not vertex_id:
            continue
        removed_extension_ids.add(str(vertex_id))
        row.setdefault("replaced_vertex_ids", []).append(str(vertex_id))
        row["replace_reason"] = "handwheel_nearest_ve"

    for row in handwheel_vertices:
        final.append({**row, "vertex_source": "handwheel"})

    for vertex in extension_vertices:
        point = vertex.get("point")
        if not isinstance(point, list) or len(point) < 2:
            continue
        if str(vertex.get("id")) in removed_extension_ids:
            continue
        final.append(
            {**vertex, "vertex_source": "extension", "edge_id": vertex.get("pipe_edge_id") or vertex.get("edge_id")}
        )
    mapping["handwheel_vertices"] = handwheel_vertices
    mapping["final_vertices"] = final
    return final


EDGE_SPLIT_ON_EDGE_PX = 3.0
EDGE_SPLIT_MIN_PART_PX = 2.0
FINAL_VERTEX_ON_CONTOUR_PX = 14.0


def split_edges_by_final_vertices(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Build final VE/HG intervals from raw pipe geometry.

    The old edge ids are kept only as geometry input.  Tails before the first
    final vertex and after the last final vertex are ignored, so payload edges
    are always between two final VE/HG points.
    """
    source_edges = list(mapping.get("edges") or [])
    mapping["parent_edges"] = source_edges
    mapping["source_edges"] = source_edges
    _build_final_contour(mapping)
    segments = list(mapping.get("final_contour") or [])
    mapping["edge_segments"] = segments
    mapping["edges"] = segments
    _assign_dimensions_to_final_intervals(mapping)
    return segments


def _raw_dimension_leader_chain(
    page: fitz.Page,
    label_center: tuple[float, float],
    rectangles: list[fitz.Rect] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], tuple[float, float]] | None:
    """Find a proven lead from a number to a dimension arrow."""
    segments = _drawing_segments(page.get_drawings(), rectangles)
    if not segments:
        return None
    target_search_px = 4.0
    min_target_length_px = 28.0
    candidates: list[tuple[float, list[tuple[tuple[float, float], tuple[float, float]]], tuple[float, float], tuple[float, float], tuple[tuple[float, float], tuple[float, float]], tuple[float, float]]] = []
    for seed_index, seed in enumerate(segments):
        start, end = seed
        start_gap = _point_distance(start, label_center)
        end_gap = _point_distance(end, label_center)
        seed_gap = min(start_gap, end_gap)
        # A dimension stroke can pass directly through the text box.  Its
        # endpoint at the label centre is not a separate leader start.
        if seed_gap < 2.0 or seed_gap > 28.0:
            continue
        near, far = (start, end) if start_gap <= end_gap else (end, start)

        # First prefer a real one-sector leader.  It must start near the
        # number, end with an arrowhead, and touch a dimension stroke.
        if _has_explicit_arrowhead(far, seed, segments):
            target = _leader_target_for_raw_segment(
                far,
                {seed_index},
                segments,
                target_search_px=target_search_px,
                min_target_length_px=min_target_length_px,
            )
            if target is not None:
                dimension_segment, projection, target_gap = target
                candidates.append((seed_gap + target_gap - 6.0, [seed], near, far, dimension_segment, projection))
                continue

        queue = [(far, [seed], {seed_index})]
        while queue:
            current, path, used = queue.pop(0)
            if _has_explicit_arrowhead(current, path[-1], segments):
                if not _path_direction_is_stable(path, near, current):
                    continue
                target = _leader_target_for_raw_segment(
                    current,
                    used,
                    segments,
                    target_search_px=target_search_px,
                    min_target_length_px=min_target_length_px,
                )
                if target is not None:
                    dimension_segment, projection, target_gap = target
                    path_length = sum(_point_distance(item[0], item[1]) for item in path)
                    direct_gap = _point_distance(near, current)
                    if direct_gap <= 0.01 or path_length / direct_gap > 1.18:
                        continue
                    score = seed_gap + target_gap + len(path) * 4.0 + path_length * 0.03
                    candidates.append((score, path, near, current, dimension_segment, projection))
                    break
            if len(path) >= 4:
                continue
            for index, segment in enumerate(segments):
                if index in used:
                    continue
                for endpoint in segment:
                    if _point_distance(current, endpoint) <= 5.0:
                        other = segment[1] if endpoint == segment[0] else segment[0]
                        queue.append((other, path + [segment], used | {index}))
                        break
    if not candidates:
        return None
    _score, path, near, far, dimension_segment, projection = min(candidates, key=lambda item: item[0])
    chosen_start_gap = _point_distance(near, label_center)
    nearest_start_gap = min(
        (
            min(_point_distance(segment[0], label_center), _point_distance(segment[1], label_center))
            for segment in segments
            if 8.0 <= _point_distance(segment[0], segment[1]) <= 120.0
        ),
        default=chosen_start_gap,
    )
    if nearest_start_gap <= 12.0 and chosen_start_gap - nearest_start_gap > 12.0:
        return None
    leader = {
        "index": -1,
        "start": [round(near[0], 2), round(near[1], 2)],
        "end": [round(far[0], 2), round(far[1], 2)],
        "length_px": round(sum(_point_distance(item[0], item[1]) for item in path), 2),
        "merged_indices": [],
        "arrowhead_points": [[round(far[0], 2), round(far[1], 2)]],
        "source": "raw_drawing_leader_chain",
        "segments": [
            [
                [round(segment[0][0], 2), round(segment[0][1], 2)],
                [round(segment[1][0], 2), round(segment[1][1], 2)],
            ]
            for segment in path
        ],
    }
    dimension_stroke = {
        "index": -2,
        "start": [round(dimension_segment[0][0], 2), round(dimension_segment[0][1], 2)],
        "end": [round(dimension_segment[1][0], 2), round(dimension_segment[1][1], 2)],
        "length_px": round(_point_distance(dimension_segment[0], dimension_segment[1]), 2),
        "merged_indices": [],
    }
    return leader, dimension_stroke, projection


def _build_final_contour(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a fresh contour layer containing only intervals between VE/HG."""
    source_segments = list(mapping.get("source_edges") or mapping.get("parent_edges") or mapping.get("edges") or [])
    final_vertices = [
        vertex
        for vertex in mapping.get("final_vertices") or []
        if isinstance(vertex.get("point"), list) and len(vertex.get("point") or []) >= 2
    ]
    final_by_id = {str(vertex.get("id")): vertex for vertex in final_vertices if vertex.get("id")}
    final_node_ids = {f"FV:{vertex_id}" for vertex_id in final_by_id}
    pieces: list[dict[str, Any]] = []

    def point_key(point: tuple[float, float]) -> tuple[int, int]:
        return round(point[0] * 10), round(point[1] * 10)

    raw_node_points: dict[str, tuple[float, float]] = {}

    for source in source_segments:
        start = source.get("start")
        end = source.get("end")
        if isinstance(start, list) and len(start) >= 2:
            raw_node_points[str(source.get("from_node_id") or f"POINT:{point_key((float(start[0]), float(start[1])))}")] = (
                float(start[0]),
                float(start[1]),
            )
        if isinstance(end, list) and len(end) >= 2:
            raw_node_points[str(source.get("to_node_id") or f"POINT:{point_key((float(end[0]), float(end[1])))}")] = (
                float(end[0]),
                float(end[1]),
            )

    raw_parent = {node_id: node_id for node_id in raw_node_points}

    def raw_find(node_id: str) -> str:
        while raw_parent.get(node_id, node_id) != node_id:
            raw_parent[node_id] = raw_parent.get(raw_parent[node_id], raw_parent[node_id])
            node_id = raw_parent[node_id]
        return node_id

    def raw_union(first: str, second: str) -> None:
        first_root = raw_find(first)
        second_root = raw_find(second)
        if first_root != second_root:
            raw_parent[second_root] = first_root

    raw_node_items = list(raw_node_points.items())
    for first_index, (first_id, first_point) in enumerate(raw_node_items):
        for second_id, second_point in raw_node_items[first_index + 1:]:
            if _point_distance(first_point, second_point) <= FINAL_VERTEX_ON_CONTOUR_PX:
                raw_union(first_id, second_id)

    def raw_node_id(source: dict[str, Any], key: str, point: tuple[float, float]) -> str:
        field = "from_node_id" if key == "start" else "to_node_id"
        node_id = str(source.get(field) or f"POINT:{point_key(point)}")
        return raw_find(node_id)

    for index, source in enumerate(source_segments, start=1):
        start = source.get("start")
        end = source.get("end")
        if not isinstance(start, list) or not isinstance(end, list):
            continue
        source_start = (float(start[0]), float(start[1]))
        source_end = (float(end[0]), float(end[1]))
        source_vector = (source_end[0] - source_start[0], source_end[1] - source_start[1])
        source_length_sq = source_vector[0] ** 2 + source_vector[1] ** 2
        if source_length_sq <= 1e-9:
            continue
        split_points: list[dict[str, Any]] = [
            {
                "position": 0.0,
                "point": source_start,
                "node_id": raw_node_id(source, "start", source_start),
                "vertex_id": None,
            },
            {
                "position": 1.0,
                "point": source_end,
                "node_id": raw_node_id(source, "end", source_end),
                "vertex_id": None,
            },
        ]
        for vertex_id, vertex in final_by_id.items():
            point = tuple(float(value) for value in vertex["point"][:2])
            projection = (
                (point[0] - source_start[0]) * source_vector[0]
                + (point[1] - source_start[1]) * source_vector[1]
            ) / source_length_sq
            is_source_handwheel = bool(vertex.get("handwheel_id") and vertex.get("edge_id") == source.get("id"))
            endpoint_gap = min(_point_distance(point, source_start), _point_distance(point, source_end))
            is_near_handwheel_endpoint = bool(
                vertex.get("handwheel_id")
                and endpoint_gap <= HANDWHEEL_VERTEX_DEDUP_PX
                and -0.55 <= projection <= 1.55
            )
            if not (is_source_handwheel or is_near_handwheel_endpoint) and (projection < -0.02 or projection > 1.02):
                continue
            projected = (
                source_start[0] + projection * source_vector[0],
                source_start[1] + projection * source_vector[1],
            )
            if _point_distance(point, projected) > FINAL_VERTEX_ON_CONTOUR_PX and not is_near_handwheel_endpoint:
                continue
            split_points.append(
                {
                    "position": min(max(projection, 0.0), 1.0),
                    "point": point,
                    "node_id": f"FV:{vertex_id}",
                    "vertex_id": vertex_id,
                }
            )
        split_points.sort(key=lambda item: (float(item["position"]), 0 if item.get("vertex_id") else 1))
        unique_split_points: list[dict[str, Any]] = []
        for point_row in split_points:
            if unique_split_points and abs(float(point_row["position"]) - float(unique_split_points[-1]["position"])) <= 0.002:
                if point_row.get("vertex_id") and not unique_split_points[-1].get("vertex_id"):
                    unique_split_points[-1] = point_row
                continue
            unique_split_points.append(point_row)

        for part_start, part_end in zip(unique_split_points, unique_split_points[1:]):
            if str(part_start["node_id"]) == str(part_end["node_id"]):
                continue
            pieces.append(
                {
                    "id": f"RAW-{index:04d}-{len(pieces) + 1:04d}",
                    "from_node": str(part_start["node_id"]),
                    "to_node": str(part_end["node_id"]),
                    "path_points": [
                        [round(float(part_start["point"][0]), 2), round(float(part_start["point"][1]), 2)],
                        [round(float(part_end["point"][0]), 2), round(float(part_end["point"][1]), 2)],
                    ],
                }
            )

    incident: dict[str, list[int]] = {}
    for index, piece in enumerate(pieces):
        incident.setdefault(piece["from_node"], []).append(index)
        incident.setdefault(piece["to_node"], []).append(index)
    used: set[int] = set()
    collapsed: list[dict[str, Any]] = []

    def oriented_piece(piece: dict[str, Any], from_node: str) -> tuple[list[list[float]], str]:
        if piece["from_node"] == from_node:
            return list(piece["path_points"]), piece["to_node"]
        return list(reversed(piece["path_points"])), piece["from_node"]

    def vertex_id_from_node(node_id: str) -> str | None:
        return node_id[3:] if node_id.startswith("FV:") else None

    def handwheel_for(vertex_id: str | None) -> str | None:
        if vertex_id is None:
            return None
        value = final_by_id.get(vertex_id, {}).get("handwheel_id")
        return str(value) if value else None

    def final_point(vertex_id: str) -> list[float]:
        point = final_by_id[vertex_id]["point"]
        return [round(float(point[0]), 2), round(float(point[1]), 2)]

    def add_interval(from_vertex: str, to_vertex: str, chain_points: list[list[float]]) -> None:
        if from_vertex == to_vertex or len(chain_points) < 2:
            return
        from_handwheel = handwheel_for(from_vertex)
        to_handwheel = handwheel_for(to_vertex)
        is_handwheel = bool(from_handwheel and from_handwheel == to_handwheel)
        contour_id = f"F-{from_vertex}-{to_vertex}"
        if any(item.get("id") == contour_id for item in collapsed):
            contour_id = f"{contour_id}-{len(collapsed) + 1:03d}"
        collapsed.append(
            {
                "id": contour_id,
                "start": chain_points[0],
                "end": chain_points[-1],
                "path_points": chain_points,
                "pixel_length": round(sum(_point_distance(tuple(a), tuple(b)) for a, b in zip(chain_points, chain_points[1:])), 2),
                "from_vertex": from_vertex,
                "to_vertex": to_vertex,
                "element_type": "valve" if is_handwheel else "pipe",
                "is_handwheel_segment": is_handwheel,
                "handwheel_ids": [from_handwheel] if is_handwheel and from_handwheel else [],
            }
        )

    added_pairs: set[tuple[str, str]] = set()
    for start_node in sorted(final_node_ids):
        start_vertex = vertex_id_from_node(start_node)
        if start_vertex is None:
            continue
        for start_index in list(incident.get(start_node, [])):
            heap: list[tuple[float, str, list[list[float]], frozenset[int]]] = []
            points, next_node = oriented_piece(pieces[start_index], start_node)
            distance = sum(_point_distance(tuple(a), tuple(b)) for a, b in zip(points, points[1:]))
            heapq.heappush(heap, (distance, next_node, points, frozenset({start_index})))
            found = False
            while heap and not found:
                _distance, current_node, chain_points, used_edges = heapq.heappop(heap)
                if current_node in final_node_ids:
                    end_vertex = vertex_id_from_node(current_node)
                    if end_vertex is None:
                        break
                    pair_key = tuple(sorted((start_vertex, end_vertex)))
                    if pair_key not in added_pairs:
                        add_interval(start_vertex, end_vertex, chain_points)
                        added_pairs.add(pair_key)
                    found = True
                    break
                for next_index in incident.get(current_node, []):
                    if next_index in used_edges:
                        continue
                    next_points, next_node = oriented_piece(pieces[next_index], current_node)
                    next_chain = [*chain_points, *next_points[1:]]
                    next_distance = sum(
                        _point_distance(tuple(a), tuple(b))
                        for a, b in zip(next_chain, next_chain[1:])
                    )
                    heapq.heappush(heap, (next_distance, next_node, next_chain, frozenset({*used_edges, next_index})))

    handwheel_ids = sorted({str(vertex.get("handwheel_id")) for vertex in final_vertices if vertex.get("handwheel_id")})
    for handwheel_id in handwheel_ids:
        endpoint_rows = [
            vertex
            for vertex in final_vertices
            if str(vertex.get("handwheel_id")) == handwheel_id and vertex.get("id")
        ]
        if len(endpoint_rows) < 2:
            continue
        endpoint_rows.sort(key=lambda vertex: str(vertex.get("id")))
        endpoint_ids = [str(endpoint_rows[0]["id"]), str(endpoint_rows[-1]["id"])]
        if any({item.get("from_vertex"), item.get("to_vertex")} == set(endpoint_ids) for item in collapsed):
            continue
        add_interval(endpoint_ids[0], endpoint_ids[1], [final_point(endpoint_ids[0]), final_point(endpoint_ids[1])])

    sibling_by_handwheel_vertex: dict[str, str] = {}
    for handwheel_id in handwheel_ids:
        endpoint_rows = [
            vertex
            for vertex in final_vertices
            if str(vertex.get("handwheel_id")) == handwheel_id and vertex.get("id")
        ]
        if len(endpoint_rows) < 2:
            continue
        endpoint_ids = [str(row["id"]) for row in sorted(endpoint_rows, key=lambda row: str(row.get("id")))]
        sibling_by_handwheel_vertex[endpoint_ids[0]] = endpoint_ids[-1]
        sibling_by_handwheel_vertex[endpoint_ids[-1]] = endpoint_ids[0]

    for edge in collapsed:
        if edge.get("is_handwheel_segment"):
            continue
        for vertex_key, point_index, other_key in (("from_vertex", 0, "to_vertex"), ("to_vertex", -1, "from_vertex")):
            vertex_id = str(edge.get(vertex_key) or "")
            sibling_id = sibling_by_handwheel_vertex.get(vertex_id)
            other_id = edge.get(other_key)
            if not sibling_id or not other_id or sibling_id not in final_by_id or other_id not in final_by_id:
                continue
            other_point = tuple(float(value) for value in final_by_id[str(other_id)]["point"][:2])
            current_gap = _point_distance(tuple(final_by_id[vertex_id]["point"][:2]), other_point)
            sibling_gap = _point_distance(tuple(final_by_id[sibling_id]["point"][:2]), other_point)
            if sibling_gap >= current_gap:
                continue
            edge[vertex_key] = sibling_id
            replacement_point = final_point(sibling_id)
            edge["path_points"][point_index] = replacement_point
            if point_index == 0:
                edge["start"] = replacement_point
            else:
                edge["end"] = replacement_point
            edge["id"] = f"F-{edge.get('from_vertex')}-{edge.get('to_vertex')}"
            edge["pixel_length"] = round(
                sum(_point_distance(tuple(a), tuple(b)) for a, b in zip(edge["path_points"], edge["path_points"][1:])),
                2,
            )

    mapping["final_contour"] = collapsed
    mapping["final_contour_vertices"] = []
    return collapsed


def _assign_dimensions_to_final_intervals(mapping: dict[str, Any]) -> None:
    """Assign candidates from one extension-line projection only.

    The old candidate-to-edge relationship is deliberately ignored here.  A
    ray through the dimension-stroke centre, parallel to one detected
    extension line, identifies the final segment that the candidate belongs
    to.  The segment endpoints are already final VE/HG vertices.
    """
    final_edges = [edge for edge in mapping.get("final_contour") or [] if edge.get("id")]
    final_vertices = [
        vertex
        for vertex in mapping.get("final_vertices") or []
        if vertex.get("id") and isinstance(vertex.get("point"), list) and len(vertex.get("point") or []) >= 2
    ]
    vertices_by_source_edge: dict[str, list[str]] = {}
    for vertex in final_vertices:
        source_edge_id = vertex.get("edge_id") or vertex.get("pipe_edge_id")
        if source_edge_id:
            vertices_by_source_edge.setdefault(str(source_edge_id), []).append(str(vertex["id"]))
    for source_edge in mapping.get("source_edges") or mapping.get("parent_edges") or []:
        source_edge_id = source_edge.get("id")
        start_point = source_edge.get("start")
        end_point = source_edge.get("end")
        if not source_edge_id or not isinstance(start_point, list) or not isinstance(end_point, list):
            continue
        for vertex in final_vertices:
            gap, _position, _projection = _distance_to_segment(
                tuple(vertex["point"][:2]),
                tuple(start_point),
                tuple(end_point),
            )
            if gap <= FINAL_VERTEX_ON_CONTOUR_PX:
                entry = vertices_by_source_edge.setdefault(str(source_edge_id), [])
                vertex_id = str(vertex["id"])
                if vertex_id not in entry:
                    entry.append(vertex_id)
    edge_by_pair = {
        frozenset((str(edge.get("from_vertex")), str(edge.get("to_vertex")))): edge
        for edge in final_edges
        if edge.get("from_vertex") and edge.get("to_vertex")
    }
    final_adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for edge in final_edges:
        from_vertex = edge.get("from_vertex")
        to_vertex = edge.get("to_vertex")
        if not from_vertex or not to_vertex:
            continue
        final_adjacency.setdefault(str(from_vertex), []).append((str(to_vertex), edge))
        final_adjacency.setdefault(str(to_vertex), []).append((str(from_vertex), edge))

    def final_path_edges(first_vertex: str, second_vertex: str) -> list[dict[str, Any]]:
        queue: list[tuple[str, list[dict[str, Any]]]] = [(first_vertex, [])]
        visited = {first_vertex}
        while queue:
            current, path = queue.pop(0)
            if current == second_vertex:
                return path
            for next_vertex, edge in final_adjacency.get(current, []):
                if next_vertex in visited:
                    continue
                visited.add(next_vertex)
                queue.append((next_vertex, [*path, edge]))
        return []
    assigned_handwheel_intervals: dict[str, str] = {}
    dimensions_by_id = {
        str(item.get("id")): item
        for item in mapping.get("dimensions") or []
        if item.get("id")
    }
    for dimension in mapping.get("dimensions") or []:
        stroke = dimension.get("dimension_stroke")
        extensions = [item for item in dimension.get("extension_strokes") or [] if isinstance(item, dict)]
        if not isinstance(stroke, dict) or not extensions:
            dimension["final_interval_status"] = "unresolved"
            dimension["final_interval_reason"] = "missing_dimension_or_extension_stroke"
            continue

        dimension_start = _edge_point(stroke, "start")
        dimension_end = _edge_point(stroke, "end")
        if dimension_start is None or dimension_end is None:
            dimension["final_interval_status"] = "unresolved"
            dimension["final_interval_reason"] = "invalid_extension_geometry"
            continue

        def nearest_edge_vertex(edge: dict[str, Any], point: tuple[float, float]) -> str | None:
            candidates = []
            for key in ("from_vertex", "to_vertex"):
                vertex_id = edge.get(key)
                if not vertex_id:
                    continue
                vertex_point = edge.get("start") if key == "from_vertex" else edge.get("end")
                if isinstance(vertex_point, list) and len(vertex_point) >= 2:
                    candidates.append((_point_distance(point, tuple(vertex_point)), str(vertex_id)))
            if not candidates:
                return None
            gap, vertex_id = min(candidates, key=lambda item: item[0])
            return vertex_id if gap <= FINAL_VERTEX_ON_CONTOUR_PX else None

        def projection_hit(extension: dict[str, Any]) -> dict[str, Any] | None:
            start = _edge_point(extension, "start")
            end = _edge_point(extension, "end")
            if start is None or end is None:
                return None
            nearest_dimension_end = min(
                (dimension_start, dimension_end),
                key=lambda point: min(_point_distance(point, start), _point_distance(point, end)),
            )
            far_end = start if _point_distance(start, nearest_dimension_end) >= _point_distance(end, nearest_dimension_end) else end
            direction = (
                (far_end[0] - nearest_dimension_end[0]) / (_point_distance(far_end, nearest_dimension_end) or 1.0),
                (far_end[1] - nearest_dimension_end[1]) / (_point_distance(far_end, nearest_dimension_end) or 1.0),
            )
            line = [
                [round(nearest_dimension_end[0], 2), round(nearest_dimension_end[1], 2)],
                [
                    round(nearest_dimension_end[0] + direction[0] * 260.0, 2),
                    round(nearest_dimension_end[1] + direction[1] * 260.0, 2),
                ],
            ]
            hits: list[tuple[float, dict[str, Any], tuple[float, float]]] = []
            for edge in final_edges:
                points = edge.get("path_points") or [edge["start"], edge["end"]]
                edge_hits: list[tuple[float, tuple[float, float]]] = []
                for path_start, path_end in zip(points, points[1:]):
                    hit = _ray_segment_hit(
                        nearest_dimension_end,
                        direction,
                        tuple(path_start),
                        tuple(path_end),
                        260.0,
                        back_tolerance=2.0,
                    )
                    if hit is None:
                        near_hit = _ray_segment_near_miss(
                            nearest_dimension_end,
                            direction,
                            tuple(path_start),
                            tuple(path_end),
                            260.0,
                            side_tolerance=30.0,
                        )
                        if near_hit is None:
                            continue
                        hit = (near_hit[0], near_hit[1])
                    edge_hits.append(hit)
                if edge_hits:
                    distance, point = min(edge_hits, key=lambda item: item[0])
                    hits.append((distance, edge, point))
            if not hits:
                return None
            distance, edge, point = min(hits, key=lambda item: item[0])
            return {
                "distance": distance,
                "edge": edge,
                "point": point,
                "line": line,
                "vertex": nearest_edge_vertex(edge, point),
            }

        projection_hits = [item for item in (projection_hit(extension) for extension in extensions) if item is not None]
        def edge_distance_to_dimension(edge: dict[str, Any]) -> float:
            midpoint = (
                (dimension_start[0] + dimension_end[0]) / 2,
                (dimension_start[1] + dimension_end[1]) / 2,
            )
            distances = []
            points = edge.get("path_points") or [edge.get("start"), edge.get("end")]
            for path_start, path_end in zip(points, points[1:]):
                if not isinstance(path_start, list) or not isinstance(path_end, list):
                    continue
                distance, _position, _projection = _distance_to_segment(midpoint, tuple(path_start), tuple(path_end))
                distances.append(distance)
            return min(distances) if distances else 1e9

        source_pair_candidates: list[tuple[float, dict[str, Any], str, str]] = []
        for first_index, first_extension in enumerate(extensions):
            first_vertices = vertices_by_source_edge.get(str(first_extension.get("pipe_edge_id")), [])
            for second_extension in extensions[first_index + 1:]:
                second_vertices = vertices_by_source_edge.get(str(second_extension.get("pipe_edge_id")), [])
                for first_vertex in first_vertices:
                    for second_vertex in second_vertices:
                        if first_vertex == second_vertex:
                            continue
                        source_edge = edge_by_pair.get(frozenset((first_vertex, second_vertex)))
                        if source_edge is not None:
                            source_pair_candidates.append((edge_distance_to_dimension(source_edge), source_edge, first_vertex, second_vertex))
                            continue
                        for path_edge in final_path_edges(first_vertex, second_vertex):
                            source_pair_candidates.append(
                                (edge_distance_to_dimension(path_edge) + 25.0, path_edge, first_vertex, second_vertex)
                            )
        if source_pair_candidates:
            stroke_length_px = float(stroke.get("length_px") or _point_distance(dimension_start, dimension_end) or 0.0)

            def source_candidate_rank(item: tuple[float, dict[str, Any], str, str]) -> tuple[int, float, str]:
                candidate_edge = item[1]
                if candidate_edge.get("is_handwheel_segment"):
                    valve_length = float(candidate_edge.get("pixel_length") or 0.0)
                    if valve_length > 0 and stroke_length_px <= valve_length * 1.35:
                        return (0, item[0], str(candidate_edge.get("id") or ""))
                    return (2, item[0], str(candidate_edge.get("id") or ""))
                return (1, item[0], str(candidate_edge.get("id") or ""))

            _source_score, edge, _first_vertex, _second_vertex = min(
                source_pair_candidates,
                key=source_candidate_rank,
            )
            point = tuple(edge.get("start") or [0.0, 0.0])
            if projection_hits:
                point = tuple(projection_hits[0]["point"])
                dimension["final_projection_line"] = projection_hits[0]["line"]
                dimension["final_projection_lines"] = [item["line"] for item in projection_hits]
            else:
                dimension["final_projection_line"] = None
                dimension["final_projection_lines"] = []
        else:
            same_source_candidates: list[tuple[float, dict[str, Any]]] = []
            for extension in extensions:
                for vertex_id in vertices_by_source_edge.get(str(extension.get("pipe_edge_id")), []):
                    for _next_vertex, incident_edge in final_adjacency.get(vertex_id, []):
                        same_source_candidates.append((edge_distance_to_dimension(incident_edge), incident_edge))
            if same_source_candidates:
                edge = min(same_source_candidates, key=lambda item: item[0])[1]
                point = tuple(edge.get("start") or [0.0, 0.0])
                if projection_hits:
                    point = tuple(projection_hits[0]["point"])
                    dimension["final_projection_line"] = projection_hits[0]["line"]
                    dimension["final_projection_lines"] = [item["line"] for item in projection_hits]
                else:
                    dimension["final_projection_line"] = [
                        [round(point[0], 2), round(point[1], 2)],
                        [round(point[0], 2), round(point[1], 2)],
                    ]
                    dimension["final_projection_lines"] = []
            else:
                pair_candidates: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
                for first_index, first in enumerate(projection_hits):
                    first_vertex = first.get("vertex")
                    if not first_vertex:
                        continue
                    for second in projection_hits[first_index + 1:]:
                        second_vertex = second.get("vertex")
                        if not second_vertex or second_vertex == first_vertex:
                            continue
                        edge = edge_by_pair.get(frozenset((str(first_vertex), str(second_vertex))))
                        if edge is None:
                            continue
                        pair_candidates.append((float(first["distance"]) + float(second["distance"]), edge, first, second))
                if pair_candidates:
                    _score, edge, first_hit, second_hit = min(pair_candidates, key=lambda item: item[0])
                    point = tuple(first_hit["point"])
                    dimension["final_projection_line"] = first_hit["line"]
                    dimension["final_projection_lines"] = [first_hit["line"], second_hit["line"]]
                else:
                    if not projection_hits:
                        dimension["final_interval_status"] = "unresolved"
                        dimension["final_interval_reason"] = "extension_projection_misses_pipe"
                        continue
                    label_center = dimension.get("label_center") or [0.0, 0.0]
                    best_hit = min(
                        projection_hits,
                        key=lambda item: (
                            0 if item.get("vertex") is None else 1,
                            _point_distance(
                                tuple(label_center),
                                (
                                    (float(item["edge"]["start"][0]) + float(item["edge"]["end"][0])) / 2,
                                    (float(item["edge"]["start"][1]) + float(item["edge"]["end"][1])) / 2,
                                ),
                            ),
                            float(item["distance"]),
                        ),
                    )
                    edge = best_hit["edge"]
                    point = tuple(best_hit["point"])
                    dimension["final_projection_line"] = best_hit["line"]
                    dimension["final_projection_lines"] = [item["line"] for item in projection_hits]
        from_vertex = edge.get("from_vertex")
        to_vertex = edge.get("to_vertex")
        interval_id = edge.get("id")
        dimension["final_interval_id"] = interval_id
        dimension["final_from_vertex"] = from_vertex
        dimension["final_to_vertex"] = to_vertex
        dimension["final_contact_point"] = [round(point[0], 2), round(point[1], 2)]
        if not isinstance(dimension.get("final_projection_line"), list):
            dimension["final_projection_line"] = [
                [round(point[0], 2), round(point[1], 2)],
                [round(point[0], 2), round(point[1], 2)],
            ]
        dimension["final_projection_line"][1] = [round(point[0], 2), round(point[1], 2)]
        dimension["final_interval_status"] = "resolved" if from_vertex and to_vertex else "partial"
        dimension["final_interval_reason"] = "single_extension_projection"
        if edge.get("is_handwheel_segment"):
            def handwheel_dimension_rank(item: dict[str, Any] | None) -> tuple[int, float, int, float, str]:
                if item is None:
                    return (9, 1e9, 9, 1e9, "")
                stroke_row = item.get("dimension_stroke") if isinstance(item.get("dimension_stroke"), dict) else {}
                stroke_length = float(stroke_row.get("length_px") or 1e9)
                edge_length = float(edge.get("pixel_length") or 0.0)
                value = float(item.get("value") or 0.0)
                status = str(item.get("local_filter_decision") or item.get("status") or "")
                return (
                    0 if status == "include" else 1,
                    abs(stroke_length - edge_length) if edge_length > 0 else stroke_length,
                    0 if value >= 50.0 else 1,
                    -value,
                    str(item.get("id") or ""),
                )

            previous_id = assigned_handwheel_intervals.get(interval_id)
            if previous_id:
                previous = dimensions_by_id.get(previous_id)
                if previous is not None and handwheel_dimension_rank(dimension) < handwheel_dimension_rank(previous):
                    previous["final_interval_status"] = "unresolved"
                    previous["final_interval_reason"] = "handwheel_interval_replaced_by_better_dimension"
                    previous["final_interval_id"] = None
                    previous["final_from_vertex"] = None
                    previous["final_to_vertex"] = None
                else:
                    dimension["final_interval_status"] = "unresolved"
                    dimension["final_interval_reason"] = "handwheel_interval_already_assigned"
                    dimension["final_interval_id"] = None
                    dimension["final_from_vertex"] = None
                    dimension["final_to_vertex"] = None
                    continue
            assigned_handwheel_intervals[interval_id] = str(dimension.get("id"))

    edge_by_id = {str(edge.get("id")): edge for edge in final_edges if edge.get("id")}

    def dimension_stroke_key(dimension: dict[str, Any]) -> tuple[int, ...] | None:
        stroke_row = dimension.get("dimension_stroke")
        if not isinstance(stroke_row, dict):
            return None
        indices = stroke_row.get("merged_indices")
        if isinstance(indices, list) and indices:
            return tuple(sorted(int(index) for index in indices if isinstance(index, int)))
        index = stroke_row.get("index")
        return (int(index),) if isinstance(index, int) else None

    def distance_from_label_to_edge(dimension: dict[str, Any], edge: dict[str, Any]) -> tuple[float, tuple[float, float]]:
        label = dimension.get("label_center") or [0.0, 0.0]
        label_point = (float(label[0]), float(label[1]))
        best_distance = 1e9
        best_point = tuple(edge.get("start") or [0.0, 0.0])
        points = edge.get("path_points") or [edge.get("start"), edge.get("end")]
        for path_start, path_end in zip(points, points[1:]):
            if not isinstance(path_start, list) or not isinstance(path_end, list):
                continue
            distance, _position, projection = _distance_to_segment(label_point, tuple(path_start), tuple(path_end))
            if distance < best_distance:
                best_distance = distance
                best_point = projection
        return best_distance, best_point

    by_stroke_and_edge: dict[tuple[tuple[int, ...], str], list[dict[str, Any]]] = {}
    for dimension in mapping.get("dimensions") or []:
        if dimension.get("final_interval_status") != "resolved" or not dimension.get("final_interval_id"):
            continue
        key = dimension_stroke_key(dimension)
        if key is None:
            continue
        by_stroke_and_edge.setdefault((key, str(dimension["final_interval_id"])), []).append(dimension)

    for (_stroke_key, interval_id), dimensions in by_stroke_and_edge.items():
        if len(dimensions) < 2:
            continue
        current_edge = edge_by_id.get(interval_id)
        if current_edge is None or current_edge.get("is_handwheel_segment"):
            continue
        largest_value = max(float(dimension.get("value") or 0.0) for dimension in dimensions)
        for dimension in dimensions:
            value = float(dimension.get("value") or 0.0)
            if value >= largest_value:
                continue
            label = dimension.get("label_center") or [0.0, 0.0]
            label_point = (float(label[0]), float(label[1]))
            endpoints = [
                (str(current_edge.get("from_vertex")), tuple(current_edge.get("start") or [0.0, 0.0])),
                (str(current_edge.get("to_vertex")), tuple(current_edge.get("end") or [0.0, 0.0])),
            ]
            nearest_vertex, _nearest_point = min(endpoints, key=lambda item: _point_distance(label_point, item[1]))
            adjacent_edges = [
                edge
                for _next_vertex, edge in final_adjacency.get(nearest_vertex, [])
                if edge.get("id") != interval_id and not edge.get("is_handwheel_segment")
            ]
            if not adjacent_edges:
                continue
            current_label_distance, _current_projection = distance_from_label_to_edge(dimension, current_edge)
            ranked = []
            for candidate_edge in adjacent_edges:
                candidate_distance, candidate_projection = distance_from_label_to_edge(dimension, candidate_edge)
                candidate_length = float(candidate_edge.get("pixel_length") or 0.0)
                current_length = float(current_edge.get("pixel_length") or 0.0)
                length_bonus = 0.0
                if candidate_length > 0 and current_length > 0:
                    length_bonus = min(abs(value / candidate_length), 1000.0) - min(abs(value / current_length), 1000.0)
                ranked.append((candidate_distance, length_bonus, str(candidate_edge.get("id") or ""), candidate_edge, candidate_projection))
            candidate_distance, _length_bonus, _edge_id, candidate_edge, candidate_projection = min(
                ranked,
                key=lambda item: (item[0] > 42.0, item[0], item[1], item[2]),
            )
            if candidate_distance > 42.0:
                continue
            dimension["final_interval_id"] = candidate_edge.get("id")
            dimension["final_from_vertex"] = candidate_edge.get("from_vertex")
            dimension["final_to_vertex"] = candidate_edge.get("to_vertex")
            dimension["final_contact_point"] = [round(candidate_projection[0], 2), round(candidate_projection[1], 2)]
            dimension["final_interval_reason"] = "shared_dimension_stroke_adjacent_branch"
            if isinstance(dimension.get("final_projection_line"), list) and len(dimension["final_projection_line"]) == 2:
                dimension["final_projection_line"][1] = [round(candidate_projection[0], 2), round(candidate_projection[1], 2)]
            dimension["final_projection_lines"] = [dimension.get("final_projection_line")] if dimension.get("final_projection_line") else []


def _final_contour_ray_for_dimension(
    dimension: dict[str, Any],
    extension: dict[str, Any],
    final_edges: list[dict[str, Any]] | None = None,
    max_distance: float = 2000.0,
) -> dict[str, Any] | None:
    """Build the single dimension-to-pipe ray used by final assignment."""
    dimension_stroke = dimension.get("dimension_stroke")
    if not isinstance(dimension_stroke, dict):
        return None
    dimension_start = _edge_point(dimension_stroke, "start")
    dimension_end = _edge_point(dimension_stroke, "end")
    extension_start = _edge_point(extension, "start")
    extension_end = _edge_point(extension, "end")
    if None in (dimension_start, dimension_end, extension_start, extension_end):
        return None

    dimension_start = tuple(dimension_start)
    dimension_end = tuple(dimension_end)
    extension_start = tuple(extension_start)
    extension_end = tuple(extension_end)
    # The ray starts at the dimension-line centre and is parallel to the
    # selected extension stroke.  Its orientation is from the extension's
    # dimension-side endpoint toward its pipe-side endpoint.
    label_center = dimension.get("label_center")
    if isinstance(label_center, list) and len(label_center) >= 2:
        center = (float(label_center[0]), float(label_center[1]))
    else:
        center = ((dimension_start[0] + dimension_end[0]) / 2.0, (dimension_start[1] + dimension_end[1]) / 2.0)
    if dimension.get("attachment_kind") == "leader_to_dimension_arrow":
        resolution = dimension.get("leader_resolution") or {}
        target = resolution.get("target_projection") or resolution.get("leader_target_point")
        if isinstance(target, list) and len(target) >= 2:
            center = (float(target[0]), float(target[1]))
    elif isinstance(dimension.get("leader_stroke"), dict):
        leader = dimension["leader_stroke"]
        leader_start = _edge_point(leader, "start")
        leader_end = _edge_point(leader, "end")
        if leader_start is not None and leader_end is not None:
            # If resolution is unavailable, use the leader endpoint farther
            # from the number as the arrow target.
            center = max((leader_start, leader_end), key=lambda point: _point_distance(point, center))
    # The extension endpoint closest to the actual dimension stroke is the
    # dimension-side endpoint.  The other endpoint is the pipe-side endpoint.
    # This remains true for a lead-attached dimension: the lead only supplies
    # the ray origin, never its direction.
    start_gap = _distance_to_segment(extension_start, dimension_start, dimension_end)[0]
    end_gap = _distance_to_segment(extension_end, dimension_start, dimension_end)[0]
    near, far = (extension_start, extension_end) if start_gap <= end_gap else (extension_end, extension_start)
    direction_length = _point_distance(near, far)
    if direction_length <= 1e-6:
        return None
    direction = ((far[0] - near[0]) / direction_length, (far[1] - near[1]) / direction_length)
    draw_length = direction_length * 1.5
    origin_kind = "leader_tip" if dimension.get("attachment_kind") == "leader_to_dimension_arrow" else "dimension_label"
    return {
        "start": center,
        "origin_kind": origin_kind,
        "direction": direction,
        "end": (center[0] + direction[0] * max_distance, center[1] + direction[1] * max_distance),
        "draw_end": (center[0] + direction[0] * draw_length, center[1] + direction[1] * draw_length),
        "extension_index": extension.get("index"),
        "extension_near": near,
        "extension_far": far,
    }


def _choose_final_contour_extension(dimension: dict[str, Any]) -> dict[str, Any] | None:
    """Choose exactly one extension stroke for a candidate."""
    dimension_stroke = dimension.get("dimension_stroke")
    if not isinstance(dimension_stroke, dict):
        return None
    dimension_start = _edge_point(dimension_stroke, "start")
    dimension_end = _edge_point(dimension_stroke, "end")
    if dimension_start is None or dimension_end is None:
        return None
    leader = dimension.get("leader_stroke")
    leader_index = leader.get("index") if isinstance(leader, dict) else None
    leader_points = None
    if isinstance(leader, dict):
        leader_start = _edge_point(leader, "start")
        leader_end = _edge_point(leader, "end")
        if leader_start is not None and leader_end is not None:
            leader_points = (leader_start, leader_end)

    def is_leader_stroke(stroke: dict[str, Any]) -> bool:
        if leader_index is not None and stroke.get("index") == leader_index:
            return True
        if leader_points is None:
            return False
        start = _edge_point(stroke, "start")
        end = _edge_point(stroke, "end")
        if start is None or end is None:
            return False
        direct = _point_distance(start, leader_points[0]) + _point_distance(end, leader_points[1])
        reverse = _point_distance(start, leader_points[1]) + _point_distance(end, leader_points[0])
        return min(direct, reverse) <= 1.5

    strokes = [
        stroke
        for stroke in dimension.get("extension_strokes") or []
        if isinstance(stroke, dict) and not is_leader_stroke(stroke)
    ]
    ranked: list[tuple[int, float, float, int, dict[str, Any]]] = []
    for position, stroke in enumerate(strokes):
        start = _edge_point(stroke, "start")
        end = _edge_point(stroke, "end")
        if start is None or end is None:
            continue
        gap = min(
            _distance_to_segment(start, dimension_start, dimension_end)[0],
            _distance_to_segment(end, dimension_start, dimension_end)[0],
        )
        fallback_penalty = 1 if stroke.get("fallback") else 0
        parallel_score = float(stroke.get("parallel_score") or 0.0)
        ranked.append((fallback_penalty, -parallel_score, gap, int(stroke.get("index", position)), stroke))
    if not ranked:
        return None
    return min(ranked, key=lambda item: item[:4])[4]


def _first_final_contour_hit(
    ray: dict[str, Any],
    final_edges: list[dict[str, Any]],
) -> tuple[dict[str, Any], tuple[float, float], float, float] | None:
    """Return the first exact intersection of one ray with the final contour."""
    hits: list[tuple[float, str, dict[str, Any], tuple[float, float], float]] = []
    for edge in final_edges:
        points = edge.get("path_points") or [edge.get("start"), edge.get("end")]
        for path_start, path_end in zip(points, points[1:]):
            if not isinstance(path_start, list) or not isinstance(path_end, list):
                continue
            hit = _ray_segment_hit(
                tuple(ray["start"]),
                tuple(ray["direction"]),
                tuple(path_start),
                tuple(path_end),
                2000.0,
            )
            if hit is None:
                continue
            distance, point = hit
            hits.append((float(distance), str(edge.get("id") or ""), edge, point, 0.0))
    if hits:
        distance, _edge_id, edge, point, side_gap = min(hits, key=lambda item: (item[0], item[1]))
        return edge, point, distance, side_gap

    # PDF vector strokes can be separated from the detected pipe contour by
    # a few pixels.  Keep the same single ray and accept only a small lateral
    # miss on the final contour; no source/parent edge is consulted here.
    near_hits: list[tuple[float, float, str, dict[str, Any], tuple[float, float]]] = []
    for edge in final_edges:
        points = edge.get("path_points") or [edge.get("start"), edge.get("end")]
        for path_start, path_end in zip(points, points[1:]):
            if not isinstance(path_start, list) or not isinstance(path_end, list):
                continue
            near_hit = _ray_segment_near_miss(
                tuple(ray["start"]),
                tuple(ray["direction"]),
                tuple(path_start),
                tuple(path_end),
                2000.0,
                side_tolerance=18.0,
            )
            if near_hit is None:
                continue
            distance, point, side_gap = near_hit
            near_hits.append((float(side_gap), float(distance), str(edge.get("id") or ""), edge, point))
    if not near_hits:
        return None
    # A ray may pass near more than one branch.  The first contact along the
    # ray is the proof of ownership; lateral accuracy is only a tie-breaker.
    side_gap, distance, _edge_id, edge, point = min(near_hits, key=lambda item: (item[1], item[0], item[2]))
    return edge, point, distance, side_gap


def _assign_dimensions_to_final_intervals(
    mapping: dict[str, Any],
) -> None:
    """Assign each candidate by one ray and the two sides of its hit interval."""
    final_edges = [edge for edge in mapping.get("final_contour") or [] if edge.get("id")]
    dimensions = list(mapping.get("dimensions") or [])
    assigned_handwheel_intervals: dict[str, str] = {}
    dimensions_by_id = {str(item.get("id")): item for item in dimensions if item.get("id")}

    for dimension in dimensions:
        extension = _choose_final_contour_extension(dimension)
        ray = _final_contour_ray_for_dimension(dimension, extension, final_edges) if extension is not None else None
        if ray is None:
            dimension["final_interval_status"] = "unresolved"
            dimension["final_interval_reason"] = "missing_dimension_or_extension_stroke"
            continue

        dimension["final_projection_line"] = [
            [round(ray["start"][0], 2), round(ray["start"][1], 2)],
            [round(ray["end"][0], 2), round(ray["end"][1], 2)],
        ]
        dimension["final_ray_line"] = [
            [round(ray["start"][0], 2), round(ray["start"][1], 2)],
            [round(ray["draw_end"][0], 2), round(ray["draw_end"][1], 2)],
        ]
        dimension["final_ray_origin"] = [round(ray["start"][0], 2), round(ray["start"][1], 2)]
        dimension["final_projection_lines"] = [dimension["final_projection_line"]]
        hit = _first_final_contour_hit(ray, final_edges)
        reverse_ray = dict(ray)
        reverse_direction = (-float(ray["direction"][0]), -float(ray["direction"][1]))
        reverse_ray["direction"] = reverse_direction
        reverse_ray["end"] = (
            float(ray["start"][0]) + reverse_direction[0] * 2000.0,
            float(ray["start"][1]) + reverse_direction[1] * 2000.0,
        )
        reverse_ray["draw_end"] = (
            float(ray["start"][0]) + reverse_direction[0] * _point_distance(tuple(ray["start"]), tuple(ray["draw_end"])),
            float(ray["start"][1]) + reverse_direction[1] * _point_distance(tuple(ray["start"]), tuple(ray["draw_end"])),
        )
        reverse_hit = _first_final_contour_hit(reverse_ray, final_edges)
        if reverse_hit is not None and (
            hit is None
            or reverse_hit[3] < hit[3]
            or (reverse_hit[3] == hit[3] and reverse_hit[2] < hit[2])
        ):
            ray = reverse_ray
            hit = reverse_hit
            dimension["final_ray_line"] = [
                [round(ray["start"][0], 2), round(ray["start"][1], 2)],
                [round(ray["draw_end"][0], 2), round(ray["draw_end"][1], 2)],
            ]
        if hit is None:
            dimension["final_interval_status"] = "unresolved"
            dimension["final_interval_reason"] = "single_extension_ray_misses_final_contour"
            dimension["final_contact_point"] = None
            continue

        edge, point, distance, side_gap = hit
        from_vertex = edge.get("from_vertex")
        to_vertex = edge.get("to_vertex")
        if not from_vertex or not to_vertex:
            dimension["final_interval_status"] = "unresolved"
            dimension["final_interval_reason"] = "intersection_has_no_final_vertex_pair"
            dimension["final_contact_point"] = [round(point[0], 2), round(point[1], 2)]
            continue

        contact = [round(point[0], 2), round(point[1], 2)]
        dimension["final_interval_id"] = edge.get("id")
        dimension["final_from_vertex"] = str(from_vertex)
        dimension["final_to_vertex"] = str(to_vertex)
        dimension["final_contact_point"] = contact
        dimension["final_projection_line"][1] = contact
        dimension["final_interval_status"] = "resolved"
        dimension["final_interval_reason"] = "single_extension_ray_first_final_contour_intersection"
        dimension["final_ray_distance_px"] = round(distance, 2)
        dimension["final_projection_kind"] = "near_miss" if side_gap > 0 else "intersection"
        dimension["final_projection_gap_px"] = round(side_gap, 2)
        dimension["final_extension_index"] = ray.get("extension_index")

        if not edge.get("is_handwheel_segment"):
            continue
        interval_id = str(edge.get("id"))
        previous_id = assigned_handwheel_intervals.get(interval_id)
        if previous_id is None:
            assigned_handwheel_intervals[interval_id] = str(dimension.get("id"))
            continue
        previous = dimensions_by_id.get(previous_id)
        current_include = dimension.get("local_filter_decision") == "include"
        previous_include = previous is not None and previous.get("local_filter_decision") == "include"
        current_value = float(dimension.get("value") or 0.0)
        previous_value = float(previous.get("value") or 0.0) if previous is not None else -1.0
        if previous is not None and (current_include, current_value) > (previous_include, previous_value):
            previous["final_interval_status"] = "unresolved"
            previous["final_interval_reason"] = "handwheel_interval_replaced_by_better_dimension"
            previous["final_interval_id"] = None
            previous["final_from_vertex"] = None
            previous["final_to_vertex"] = None
            assigned_handwheel_intervals[interval_id] = str(dimension.get("id"))
        else:
            dimension["final_interval_status"] = "unresolved"
            dimension["final_interval_reason"] = "handwheel_interval_already_assigned"
            dimension["final_interval_id"] = None
            dimension["final_from_vertex"] = None
            dimension["final_to_vertex"] = None

    # A nested dimension pair can legitimately share one dimension stroke and
    # therefore one ray.  In that one case the ray proves the contour region,
    # while the label position selects the adjacent final interval inside the
    # same final contour.  This does not consult source/parent edges.
    final_by_id = {str(edge.get("id")): edge for edge in final_edges if edge.get("id")}
    adjacent_by_vertex: dict[str, list[dict[str, Any]]] = {}
    for edge in final_edges:
        for vertex_id in (edge.get("from_vertex"), edge.get("to_vertex")):
            if vertex_id:
                adjacent_by_vertex.setdefault(str(vertex_id), []).append(edge)

    def final_edge_distance_to_label(dimension: dict[str, Any], edge: dict[str, Any]) -> tuple[float, tuple[float, float]]:
        label = dimension.get("label_center") or [0.0, 0.0]
        label_point = (float(label[0]), float(label[1]))
        best = (float("inf"), tuple(edge.get("start") or [0.0, 0.0]))
        points = edge.get("path_points") or [edge.get("start"), edge.get("end")]
        for start, end in zip(points, points[1:]):
            if not isinstance(start, list) or not isinstance(end, list):
                continue
            distance, _position, projection = _distance_to_segment(label_point, tuple(start), tuple(end))
            if distance < best[0]:
                best = (distance, projection)
        return best

    same_stroke: dict[tuple[int, ...], list[dict[str, Any]]] = {}
    for dimension in dimensions:
        if dimension.get("final_interval_status") != "resolved":
            continue
        stroke = dimension.get("dimension_stroke")
        if not isinstance(stroke, dict):
            continue
        indices = stroke.get("merged_indices") or ([stroke.get("index")] if stroke.get("index") is not None else [])
        key = tuple(sorted(int(index) for index in indices if isinstance(index, int)))
        if key:
            same_stroke.setdefault(key, []).append(dimension)

    for dimensions_with_same_stroke in same_stroke.values():
        by_edge: dict[str, list[dict[str, Any]]] = {}
        for dimension in dimensions_with_same_stroke:
            interval_id = dimension.get("final_interval_id")
            if interval_id:
                by_edge.setdefault(str(interval_id), []).append(dimension)
        for interval_id, shared_dimensions in by_edge.items():
            if len(shared_dimensions) < 2:
                continue
            current_edge = final_by_id.get(interval_id)
            if current_edge is None or current_edge.get("is_handwheel_segment"):
                continue
            adjacent = []
            for endpoint in (current_edge.get("from_vertex"), current_edge.get("to_vertex")):
                for edge in adjacent_by_vertex.get(str(endpoint), []):
                    if edge.get("id") != interval_id and edge not in adjacent:
                        adjacent.append(edge)
            largest_value = max(float(item.get("value") or 0.0) for item in shared_dimensions)
            for dimension in shared_dimensions:
                current_distance, _current_projection = final_edge_distance_to_label(dimension, current_edge)
                alternatives = [
                    (final_edge_distance_to_label(dimension, edge)[0], edge)
                    for edge in adjacent
                    if not edge.get("is_handwheel_segment")
                ]
                if not alternatives:
                    continue
                candidate_distance, candidate_edge = min(alternatives, key=lambda item: (item[0], str(item[1].get("id") or "")))
                prefer_adjacent_nested_dimension = float(dimension.get("value") or 0.0) < largest_value
                if candidate_distance + 5.0 >= current_distance and not (
                    prefer_adjacent_nested_dimension and candidate_distance <= 60.0
                ):
                    continue
                candidate_distance, candidate_projection = final_edge_distance_to_label(dimension, candidate_edge)
                dimension["final_interval_id"] = candidate_edge.get("id")
                dimension["final_from_vertex"] = candidate_edge.get("from_vertex")
                dimension["final_to_vertex"] = candidate_edge.get("to_vertex")
                dimension["final_contact_point"] = [round(candidate_projection[0], 2), round(candidate_projection[1], 2)]
                dimension["final_interval_reason"] = "shared_ray_label_disambiguation_on_final_contour"
                dimension["final_projection_kind"] = "shared_ray_label_disambiguation"
                if isinstance(dimension.get("final_projection_line"), list) and len(dimension["final_projection_line"]) == 2:
                    dimension["final_projection_line"][1] = dimension["final_contact_point"]


def _append_handwheel_gap_segments(
    mapping: dict[str, Any],
    edges: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> None:
    """Штурвалы, стоящие в разрыве между рёбрами, получают собственный сегмент-мост.

    Такое ребро покрывает фактический span символа и сразу помечается как зона
    арматуры (element_type valve), а не до-приписывается к чужому сегменту.
    """
    edge_by_id = {edge.get("id"): edge for edge in edges if edge.get("id")}
    for row in mapping.get("handwheel_vertices") or []:
        glyph_id = row.get("handwheel_id")
        span = row.get("symbol_span_px")
        edge = edge_by_id.get(row.get("edge_id"))
        if not glyph_id or not span or edge is None:
            continue
        if any(glyph_id in (segment.get("handwheel_ids") or []) for segment in segments):
            continue
        span_start = float(span[0])
        span_end = float(span[1])
        edge_start = edge["start"]
        edge_end = edge["end"]
        edge_length = _point_distance(tuple(edge_start), tuple(edge_end)) or 1.0
        if min(span_end, edge_length) - max(span_start, 0.0) >= 1.0:
            continue
        low, high = sorted((span_start, span_end))
        unit_x = (edge_end[0] - edge_start[0]) / edge_length
        unit_y = (edge_end[1] - edge_start[1]) / edge_length
        segments.append(
            {
                **edge,
                "id": f"{glyph_id}-SEG",
                "start": [round(edge_start[0] + low * unit_x, 2), round(edge_start[1] + low * unit_y, 2)],
                "end": [round(edge_start[0] + high * unit_x, 2), round(edge_start[1] + high * unit_y, 2)],
                "pixel_length": round(high - low, 2),
                "parent_edge_id": edge.get("id"),
                "from_vertex_id": f"{glyph_id}-A",
                "to_vertex_id": f"{glyph_id}-B",
                "from_position_px": round(low, 2),
                "to_position_px": round(high, 2),
                "element_type": "valve",
                "is_valve_edge": True,
                "valve_source": "handwheel_glyph",
                "is_handwheel_segment": True,
                "handwheel_ids": [str(glyph_id)],
            }
        )


def _remap_dimensions_to_edge_segments(mapping: dict[str, Any]) -> None:
    parent_by_id = {edge.get("id"): edge for edge in mapping.get("parent_edges") or [] if edge.get("id")}
    segments_by_parent: dict[str, list[dict[str, Any]]] = {}
    for segment in mapping.get("edge_segments") or []:
        segments_by_parent.setdefault(segment.get("parent_edge_id"), []).append(segment)
    for dimension in mapping.get("dimensions") or []:
        parent_id = dimension.get("edge_id")
        stroke = dimension.get("dimension_stroke")
        if not parent_id or not isinstance(stroke, dict):
            continue
        parent = parent_by_id.get(parent_id)
        if parent is None:
            continue
        edge_start = parent["start"]
        edge_end = parent["end"]
        length = _point_distance(tuple(edge_start), tuple(edge_end)) or 1.0
        unit_x = (edge_end[0] - edge_start[0]) / length
        unit_y = (edge_end[1] - edge_start[1]) / length

        def offset_along(point: list[float]) -> float:
            return (point[0] - edge_start[0]) * unit_x + (point[1] - edge_start[1]) * unit_y

        start_offset = offset_along(tuple(stroke["start"]) if len(stroke["start"]) >= 2 else (0.0, 0.0))
        end_offset = offset_along(tuple(stroke["end"]) if len(stroke["end"]) >= 2 else (0.0, 0.0))
        # Handwheel bridge segments may lie before or after the ordinary
        # parent-edge interval (their positions can be negative or exceed
        # the parent length).  Do not clamp the dimension projection before
        # comparing it with those segments, otherwise a dimension that
        # visibly crosses a handwheel loses the valve segment from its
        # coverage.
        low, high = sorted((start_offset, end_offset))
        covered = [
            segment
            for segment in segments_by_parent.get(parent_id, [])
            if segment.get("to_position_px", 0.0) > low + 0.5 and segment.get("from_position_px", 0.0) < high - 0.5
        ]
        if not covered and low == high and dimension.get("position") is not None:
            covered = [
                segment
                for segment in segments_by_parent.get(parent_id, [])
                if segment.get("from_position_px", 0.0) <= low <= segment.get("to_position_px", 0.0)
            ]
        if not covered:
            continue
        covered.sort(key=lambda segment: segment.get("from_position_px", 0.0))
        dimension["edge_segments_ids"] = [segment["id"] for segment in covered]
        dimension["covered_edge_ids"] = [segment["id"] for segment in covered]
        # The first final segment is only the primary reference.  The full
        # coverage is carried separately so a candidate spanning a cut is not
        # silently left on the old unsplit edge.
        dimension["edge_id"] = covered[0]["id"]
    _resolve_handwheel_dimension_conflicts(mapping)


def _resolve_handwheel_dimension_conflicts(mapping: dict[str, Any]) -> None:
    """Keep one dimension on each handwheel bridge.

    A dimension projection can cross a valve bridge and continue onto the
    adjacent pipe.  The bridge must keep the candidate that covers only the
    bridge; the longer candidate remains attached to the pipe segment.
    """
    segments = [
        segment
        for segment in mapping.get("edge_segments") or []
        if segment.get("is_handwheel_segment")
    ]
    dimensions = mapping.get("dimensions") or []
    for bridge in segments:
        bridge_id = bridge.get("id")
        if not bridge_id:
            continue
        covering = [
            dimension
            for dimension in dimensions
            if bridge_id in (dimension.get("covered_edge_ids") or [])
        ]
        if len(covering) <= 1:
            continue
        exclusive = [
            dimension
            for dimension in covering
            if set(dimension.get("covered_edge_ids") or []) <= {bridge_id}
        ]
        pool = exclusive or covering
        winner = min(
            pool,
            key=lambda item: (
                0 if item.get("local_filter_decision") == "include" else 1,
                float(item.get("value") or 0.0),
                str(item.get("id") or ""),
            ),
        )
        for dimension in covering:
            if dimension is winner:
                continue
            covered = [
                edge_id
                for edge_id in (dimension.get("covered_edge_ids") or [])
                if edge_id != bridge_id
            ]
            dimension["covered_edge_ids"] = covered
            dimension["edge_segments_ids"] = list(covered)
            dimension["edge_id"] = covered[0] if covered else None


def save_clean_local_markup_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Pure QA overlay: vertices and source labels without dimension-stroke highlights."""
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


def _stroke_points(stroke: dict[str, Any]) -> tuple[fitz.Point, fitz.Point] | None:
    start = stroke.get("start")
    end = stroke.get("end")
    if not start or not end or len(start) < 2 or len(end) < 2:
        return None
    return fitz.Point(float(start[0]), float(start[1])), fitz.Point(float(end[0]), float(end[1]))


def _draw_leader_stroke(
    page: fitz.Page,
    leader: dict[str, Any] | None,
    *,
    color: tuple[float, float, float],
    width: float,
) -> tuple[fitz.Point, fitz.Point] | None:
    if not isinstance(leader, dict):
        return None
    points = _stroke_points(leader)
    segments = leader.get("segments")
    drew_segments = False
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, list) or len(segment) != 2:
                continue
            try:
                page.draw_line(
                    fitz.Point(float(segment[0][0]), float(segment[0][1])),
                    fitz.Point(float(segment[1][0]), float(segment[1][1])),
                    color=color,
                    width=width,
                )
                drew_segments = True
            except (TypeError, ValueError, IndexError):
                continue
    if points is not None and not drew_segments:
        page.draw_line(points[0], points[1], color=color, width=width)
    return points


def _stroke_key(stroke: dict[str, Any]) -> tuple[float, float, float, float] | None:
    points = _stroke_points(stroke)
    if points is None:
        return None
    start, end = points
    first = (round(start.x, 2), round(start.y, 2))
    second = (round(end.x, 2), round(end.y, 2))
    ordered = sorted((first, second))
    return (*ordered[0], *ordered[1])


def save_local_dimension_filter_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Overlay local include/exclude decisions before provider review."""
    apply_local_dimension_filter(mapping)
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=page.rect.width, height=page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)

        rendered_strokes: set[tuple[float, float, float, float]] = set()
        colors = {
            "include": (0.0, 0.70, 0.20),
            "exclude": (0.88, 0.06, 0.05),
            "ambiguous": (0.95, 0.62, 0.05),
        }
        fill_colors = {
            "include": (0.82, 1.0, 0.86),
            "exclude": (1.0, 0.82, 0.80),
            "ambiguous": (1.0, 0.94, 0.72),
        }

        for dimension in mapping.get("dimensions", []):
            decision = dimension.get("local_filter_decision") or "ambiguous"
            color = colors.get(decision, colors["ambiguous"])
            stroke_specs: list[tuple[dict[str, Any] | None, float]] = [
                (dimension.get("dimension_stroke"), 2.4),
                (dimension.get("leader_stroke"), 1.6),
            ]
            stroke_specs.extend((stroke, 1.6) for stroke in dimension.get("extension_strokes", []) if isinstance(stroke, dict))
            for stroke, width in stroke_specs:
                if not isinstance(stroke, dict):
                    continue
                key = _stroke_key(stroke)
                points = _stroke_points(stroke)
                if key is None or points is None or key in rendered_strokes:
                    continue
                rendered_strokes.add(key)
                marked.draw_line(points[0], points[1], color=color, width=width)

        # Diagnostic ray used by the final-contour assignment.  Keep it on
        # the local-filter PDF only so the exact intersection search is
        # visible without changing the provider payload.
        projection_color = (1.0, 0.25, 0.0)
        lead_color = (1.0, 0.82, 0.0)
        debug_arrow_color = (0.05, 0.25, 1.0)
        for dimension in mapping.get("dimensions", []):
            if dimension.get("id") == "D002" and round(float(dimension.get("value") or 0.0)) == 704:
                debug_strokes = [dimension.get("leader_stroke"), dimension.get("dimension_stroke")]
                debug_strokes.extend(dimension.get("extension_strokes") or [])
                for debug_stroke in debug_strokes:
                    debug_points = _stroke_points(debug_stroke) if isinstance(debug_stroke, dict) else None
                    if debug_points is not None:
                        marked.draw_line(debug_points[0], debug_points[1], color=debug_arrow_color, width=3.0)
                for debug_segment in dimension.get("debug_lead_segments") or []:
                    if isinstance(debug_segment, list) and len(debug_segment) == 2:
                        marked.draw_line(
                            fitz.Point(float(debug_segment[0][0]), float(debug_segment[0][1])),
                            fitz.Point(float(debug_segment[1][0]), float(debug_segment[1][1])),
                            color=debug_arrow_color,
                            width=2.2,
                        )
            if dimension.get("attachment_kind") == "leader_to_dimension_arrow":
                leader = dimension.get("leader_stroke")
                _draw_leader_stroke(marked, leader if isinstance(leader, dict) else None, color=lead_color, width=2.8)
            projection = dimension.get("final_ray_line") or dimension.get("final_projection_line")
            if not isinstance(projection, list) or len(projection) != 2:
                continue
            if any(not isinstance(point, (list, tuple)) or len(point) < 2 for point in projection):
                continue
            marked.draw_line(
                fitz.Point(float(projection[0][0]), float(projection[0][1])),
                fitz.Point(float(projection[1][0]), float(projection[1][1])),
                color=projection_color,
                width=1.8,
            )

        for dimension in mapping.get("dimensions", []):
            label_center = dimension.get("label_center")
            if not label_center:
                continue
            decision = dimension.get("local_filter_decision") or "ambiguous"
            color = colors.get(decision, colors["ambiguous"])
            fill = fill_colors.get(decision, fill_colors["ambiguous"])
            center = fitz.Point(*label_center)
            rect = fitz.Rect(center.x - 22, center.y - 13, center.x + 26, center.y + 13)
            marked.draw_rect(rect, color=color, fill=fill, fill_opacity=0.24, width=1.5)
            label = dimension.get("id", "D?")
            if decision == "exclude" and dimension.get("local_filter_conflict_with"):
                label = f"{label} -> {dimension['local_filter_conflict_with']}"
            marked.insert_text(center + (24, -10), label, fontsize=7, fontname="helv", color=color)

        glyph_color = (1.0, 0.35, 0.8)
        for glyph in mapping.get("handwheel_glyphs") or []:
            corners = glyph.get("corners") or []
            if len(corners) != 4:
                continue
            points = [fitz.Point(float(corner[0]), float(corner[1])) for corner in corners]
            points.append(points[0])
            marked.draw_polyline(points, color=glyph_color, width=1.5)

        valve_segment_color = (0.95, 0.15, 0.55)
        for segment in mapping.get("edge_segments") or []:
            if not segment.get("is_handwheel_segment"):
                continue
            start = segment.get("start")
            end = segment.get("end")
            if not start or not end or len(start) < 2 or len(end) < 2:
                continue
            segment_start = fitz.Point(float(start[0]), float(start[1]))
            segment_end = fitz.Point(float(end[0]), float(end[1]))
            marked.draw_line(segment_start, segment_end, color=valve_segment_color, width=4.0)

        def draw_vertex(vertex: dict[str, Any], color: tuple[float, float, float]) -> None:
            point = vertex.get("point")
            ray_start = vertex.get("ray_start")
            if not point or len(point) < 2:
                return
            center = fitz.Point(float(point[0]), float(point[1]))
            if ray_start and len(ray_start) >= 2:
                from_point = fitz.Point(float(ray_start[0]), float(ray_start[1]))
                marked.draw_line(
                    from_point,
                    center,
                    color=extension_vertex_color,
                    width=0.8,
                    dashes="[2 2] 0",
                )
            marked.draw_circle(center, 10.5, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.75, width=0.8)
            marked.draw_circle(center, 6.5, color=color, fill=None, width=2.2)
            marked.draw_line(center + (-4.5, 0), center + (4.5, 0), color=color, width=1.6)
            marked.draw_line(center + (0, -4.5), center + (0, 4.5), color=color, width=1.6)
            label_point = center + (11, -11)
            label_rect = fitz.Rect(label_point.x - 2, label_point.y - 9, label_point.x + 44, label_point.y + 4)
            marked.draw_rect(label_rect, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.85, width=0)
            label = str(vertex.get("id") or "VX?")
            marked.insert_text(label_point, label, fontsize=8, fontname="helv", color=color)

        extension_vertex_color = (0.45, 0.05, 0.75)
        final_vertices = mapping.get("final_vertices") or []
        for vertex in final_vertices:
            if vertex.get("vertex_source") == "extension":
                draw_vertex(vertex, extension_vertex_color)

        handwheel_vertex_color = (0.14, 0.39, 0.92)
        for vertex in final_vertices:
            if vertex.get("vertex_source") == "handwheel":
                draw_vertex(vertex, handwheel_vertex_color)

        output.save(str(output_pdf))
        output.close()


def save_dimension_lead_detection_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Show dimension-scoped lead detection for each numeric candidate."""
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=page.rect.width, height=page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)

        dimension_color = (0.0, 0.45, 0.95)
        leader_color = (1.0, 0.78, 0.0)
        debug_color = (0.05, 0.25, 1.0)
        extension_color = (0.48, 0.22, 0.85)
        label_color = (0.86, 0.08, 0.08)
        no_lead_color = (0.65, 0.65, 0.65)
        lead_target_color = (0.0, 0.68, 0.18)
        suspect_color = (0.95, 0.05, 0.05)
        target_overlays: list[tuple[fitz.Point, fitz.Point, bool, dict[str, Any]]] = []

        for dimension in mapping.get("dimensions", []):
            label_center = dimension.get("label_center")
            if not isinstance(label_center, list) or len(label_center) < 2:
                continue
            center = fitz.Point(float(label_center[0]), float(label_center[1]))
            has_lead = dimension.get("attachment_kind") == "leader_to_dimension_arrow" and isinstance(dimension.get("leader_stroke"), dict)
            resolution = dimension.get("leader_resolution") or {}
            suspect_target = bool(resolution.get("suspect"))
            box_color = leader_color if has_lead else no_lead_color
            marked.draw_rect(
                fitz.Rect(center.x - 18, center.y - 11, center.x + 24, center.y + 11),
                color=box_color,
                fill=(1.0, 1.0, 1.0),
                fill_opacity=0.55,
                width=1.1,
            )
            status = "suspect target" if suspect_target else ("lead" if has_lead else "no lead")
            label = f"{dimension.get('id', 'D?')} {dimension.get('text') or dimension.get('value')}: {status}"
            marked.insert_text(center + (24, -8), label[:72], fontsize=7, fontname="helv", color=suspect_color if suspect_target else (label_color if has_lead else no_lead_color))

            for debug_segment in dimension.get("debug_lead_segments") or []:
                if isinstance(debug_segment, list) and len(debug_segment) == 2:
                    try:
                        marked.draw_line(
                            fitz.Point(float(debug_segment[0][0]), float(debug_segment[0][1])),
                            fitz.Point(float(debug_segment[1][0]), float(debug_segment[1][1])),
                            color=debug_color,
                            width=1.4,
                        )
                    except (TypeError, ValueError, IndexError):
                        continue

            dimension_points = _stroke_points(dimension.get("dimension_stroke")) if isinstance(dimension.get("dimension_stroke"), dict) else None
            if dimension_points is not None:
                marked.draw_line(dimension_points[0], dimension_points[1], color=dimension_color, width=2.2)
                if has_lead:
                    target_overlays.append((dimension_points[0], dimension_points[1], suspect_target, resolution))

            leader = dimension.get("leader_stroke") if isinstance(dimension.get("leader_stroke"), dict) else None
            leader_points = _draw_leader_stroke(marked, leader, color=leader_color, width=3.0)
            if leader_points is not None:
                marked.draw_circle(leader_points[1], 3.2, color=leader_color, fill=leader_color, width=1.0)

            for stroke in dimension.get("extension_strokes") or []:
                extension_points = _stroke_points(stroke) if isinstance(stroke, dict) else None
                if extension_points is not None:
                    marked.draw_line(extension_points[0], extension_points[1], color=extension_color, width=1.2)

            target = resolution.get("target_projection") or resolution.get("leader_target_point")
            if isinstance(target, list) and len(target) >= 2:
                try:
                    target_point = fitz.Point(float(target[0]), float(target[1]))
                    marked.draw_circle(target_point, 4.0, color=suspect_color if suspect_target else (1.0, 0.15, 0.0), fill=suspect_color if suspect_target else (1.0, 0.15, 0.0), width=1.0)
                    if suspect_target:
                        marked.insert_text(target_point + (6, -5), ",".join(str(item) for item in resolution.get("suspect_reasons") or [])[:42], fontsize=6, fontname="helv", color=suspect_color)
                except (TypeError, ValueError):
                    pass

        for start, end, suspect_target, resolution in target_overlays:
            marked.draw_line(
                start,
                end,
                color=suspect_color if suspect_target else lead_target_color,
                width=3.8 if suspect_target else 3.3,
                dashes="[4 3] 0" if suspect_target else None,
            )
            target = resolution.get("target_projection") or resolution.get("leader_target_point")
            if isinstance(target, list) and len(target) >= 2:
                try:
                    target_point = fitz.Point(float(target[0]), float(target[1]))
                    marked.draw_circle(target_point, 4.5, color=suspect_color if suspect_target else lead_target_color, fill=suspect_color if suspect_target else lead_target_color, width=1.0)
                except (TypeError, ValueError):
                    pass

        marked.insert_text(
            fitz.Point(28, 26),
            f"DIMENSION LEAD DETECTION / page {page_number}  yellow=lead green=lead target red=suspect target blue=dimension purple=extension",
            fontsize=10,
            fontname="helv",
            color=(1.0, 0.25, 0.0),
        )
        output.save(str(output_pdf))
        output.close()


def save_pipeline_length_diagnostic_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Render only the base V/E geometry and local dimension decisions."""
    with fitz.open(str(pdf_path)) as document:
        source_page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=source_page.rect.width, height=source_page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)
        edge_color = (0.05, 0.35, 0.85)
        for edge in mapping.get("base_edges") or mapping.get("edges") or []:
            start, end = edge.get("start"), edge.get("end")
            if not isinstance(start, list) or not isinstance(end, list):
                continue
            marked.draw_line(fitz.Point(*start[:2]), fitz.Point(*end[:2]), color=edge_color, width=1.8)
            midpoint = fitz.Point((float(start[0]) + float(end[0])) / 2, (float(start[1]) + float(end[1])) / 2)
            marked.insert_text(midpoint + (3, -3), str(edge.get("id") or "E?"), fontsize=6, color=edge_color)

        vertex_color = (0.55, 0.05, 0.75)
        for vertex in mapping.get("base_vertices") or []:
            point = (vertex.get("x"), vertex.get("y"))
            if point[0] is None or point[1] is None:
                continue
            center = fitz.Point(float(point[0]), float(point[1]))
            marked.draw_circle(center, 6.0, color=vertex_color, fill=None, width=1.8)
            marked.insert_text(center + (8, -7), str(vertex.get("id") or "V?"), fontsize=7, color=vertex_color)

        decision_colors = {
            "include": (0.0, 0.65, 0.15),
            "exclude": (0.85, 0.05, 0.05),
            "ambiguous": (0.95, 0.55, 0.0),
        }
        for dimension in mapping.get("dimensions") or []:
            decision = dimension.get("local_filter_decision") or "ambiguous"
            color = decision_colors.get(decision, decision_colors["ambiguous"])
            for key, width in (("dimension_stroke", 2.4), ("leader_stroke", 1.8)):
                points = _stroke_points(dimension.get(key) or {})
                if points is not None:
                    marked.draw_line(points[0], points[1], color=color, width=width)
            for stroke in dimension.get("extension_strokes") or []:
                points = _stroke_points(stroke)
                if points is not None:
                    marked.draw_line(points[0], points[1], color=color, width=1.6)
            center = dimension.get("label_center")
            if isinstance(center, list) and len(center) >= 2:
                point = fitz.Point(float(center[0]), float(center[1]))
                label = f"{dimension.get('id', 'D?')}={dimension.get('value', '?')} [{decision}]"
                marked.insert_text(point + (10, -8), label[:48], fontsize=7, color=color)

        # Handwheel evidence is intentionally separate from the base V/E graph.
        # It helps the provider relate a dimension to a valve without introducing
        # final HG vertices or valve edges at this stage.
        handwheel_color = (0.08, 0.2, 0.95)
        glyph_color = (0.75, 0.05, 0.65)
        annotations = mapping.get("handwheel_annotations") or {}
        for item in annotations.get("handwheels") or []:
            for segment in item.get("arrow_segments") or []:
                points = _stroke_points(segment)
                if points is not None:
                    marked.draw_line(points[0], points[1], color=handwheel_color, width=2.2)
            arrow_end = item.get("arrow_end")
            if isinstance(arrow_end, list) and len(arrow_end) >= 2:
                point = fitz.Point(float(arrow_end[0]), float(arrow_end[1]))
                marked.insert_text(point + (5, -4), str(item.get("id") or "HW"), fontsize=7, color=handwheel_color)
        for glyph in annotations.get("glyphs") or []:
            corners = [
                fitz.Point(float(point[0]), float(point[1]))
                for point in glyph.get("corners") or []
                if isinstance(point, list) and len(point) >= 2
            ]
            if len(corners) == 4:
                for first, second in ((0, 1), (1, 3), (3, 2), (2, 0)):
                    marked.draw_line(corners[first], corners[second], color=glyph_color, width=1.8)
            center = glyph.get("center")
            if isinstance(center, list) and len(center) >= 2:
                point = fitz.Point(float(center[0]), float(center[1]))
                marked.insert_text(point + (6, -6), str(glyph.get("id") or "HG"), fontsize=7, color=glyph_color)

        marked.insert_text(
            fitz.Point(24, 28),
            f"PIPELINE LENGTH / LOCAL BASE GEOMETRY / page {page_number}",
            fontsize=11,
            color=(0.08, 0.12, 0.2),
        )
        marked.insert_text(
            fitz.Point(24, 42),
            "V/E base geometry; green=include red=exclude orange=ambiguous; blue/magenta=handwheel evidence",
            fontsize=8,
            color=(0.08, 0.12, 0.2),
        )
        output.save(str(output_pdf))
        output.close()


def save_final_contour_rays_pdf(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    mapping: dict[str, Any],
) -> None:
    """Render the exact rays and final intervals used for candidate assignment."""
    with fitz.open(str(pdf_path)) as document:
        source_page = document[page_number - 1]
        output = fitz.open()
        marked = output.new_page(width=source_page.rect.width, height=source_page.rect.height)
        marked.show_pdf_page(marked.rect, document, page_number - 1)

        contour_color = (0.0, 0.45, 0.75)
        for edge in mapping.get("final_contour") or []:
            points = edge.get("path_points") or [edge.get("start"), edge.get("end")]
            valid_points = [
                fitz.Point(float(point[0]), float(point[1]))
                for point in points
                if isinstance(point, list) and len(point) >= 2
            ]
            for start, end in zip(valid_points, valid_points[1:]):
                marked.draw_line(start, end, color=contour_color, width=2.0)

        vertex_by_id = {
            str(vertex.get("id")): vertex
            for vertex in mapping.get("final_vertices") or []
            if vertex.get("id") and isinstance(vertex.get("point"), list)
        }
        for vertex_id, vertex in vertex_by_id.items():
            point = vertex["point"]
            center = fitz.Point(float(point[0]), float(point[1]))
            is_hg = vertex.get("vertex_source") == "handwheel"
            color = (0.1, 0.35, 0.95) if is_hg else (0.5, 0.05, 0.75)
            marked.draw_circle(center, 6.5, color=color, fill=None, width=2.0)
            marked.insert_text(center + (8, -8), vertex_id, fontsize=7, fontname="helv", color=color)

        ray_color = (1.0, 0.28, 0.0)
        lead_color = (1.0, 0.82, 0.0)
        debug_arrow_color = (0.05, 0.25, 1.0)
        hit_color = (0.95, 0.05, 0.05)
        for dimension in mapping.get("dimensions") or []:
            if dimension.get("id") == "D002" and round(float(dimension.get("value") or 0.0)) == 704:
                debug_strokes = [dimension.get("leader_stroke"), dimension.get("dimension_stroke")]
                debug_strokes.extend(dimension.get("extension_strokes") or [])
                for debug_stroke in debug_strokes:
                    debug_points = _stroke_points(debug_stroke) if isinstance(debug_stroke, dict) else None
                    if debug_points is not None:
                        marked.draw_line(debug_points[0], debug_points[1], color=debug_arrow_color, width=3.0)
                for debug_segment in dimension.get("debug_lead_segments") or []:
                    if isinstance(debug_segment, list) and len(debug_segment) == 2:
                        marked.draw_line(
                            fitz.Point(float(debug_segment[0][0]), float(debug_segment[0][1])),
                            fitz.Point(float(debug_segment[1][0]), float(debug_segment[1][1])),
                            color=debug_arrow_color,
                            width=2.2,
                        )
            if dimension.get("attachment_kind") == "leader_to_dimension_arrow":
                leader = dimension.get("leader_stroke")
                _draw_leader_stroke(marked, leader if isinstance(leader, dict) else None, color=lead_color, width=2.8)
            projection = dimension.get("final_ray_line") or dimension.get("final_projection_line")
            if not isinstance(projection, list) or len(projection) != 2:
                continue
            if any(not isinstance(point, (list, tuple)) or len(point) < 2 for point in projection):
                continue
            ray_start = fitz.Point(float(projection[0][0]), float(projection[0][1]))
            ray_end = fitz.Point(float(projection[1][0]), float(projection[1][1]))
            marked.draw_line(ray_start, ray_end, color=ray_color, width=2.0)
            contact = dimension.get("final_contact_point")
            contact_point = (
                fitz.Point(float(contact[0]), float(contact[1]))
                if isinstance(contact, list) and len(contact) >= 2
                else ray_end
            )
            marked.draw_circle(contact_point, 3.0, color=hit_color, fill=hit_color, width=1.0)
            from_vertex = dimension.get("final_from_vertex") or "?"
            to_vertex = dimension.get("final_to_vertex") or "?"
            label = f"{dimension.get('id', 'D?')}: {from_vertex} - {to_vertex}"
            marked.insert_text(ray_end + (5, -4), label, fontsize=7, fontname="helv", color=ray_color)

        marked.insert_text(
            fitz.Point(18, 24),
            f"FINAL CONTOUR RAYS / page {page_number}  yellow=lead  orange=ray  red=intersection",
            fontsize=9,
            fontname="helv",
            color=ray_color,
        )
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


def run_dimension_mapping(
    pdf_path: str | Path,
    page_number: int,
    output_pdf: str | Path,
    output_json: str | Path,
    *,
    finalize: bool = True,
) -> dict[str, Any]:
    mapping = map_dimensions(pdf_path, page_number, finalize=finalize)
    save_dimension_mapping_pdf(pdf_path, page_number, output_pdf, mapping)
    Path(output_json).write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping


__all__ = [
    "map_dimensions",
    "run_dimension_mapping",
    "compute_endpoint_adjustments",
    "compute_extension_vertices",
    "compute_final_vertices",
    "split_edges_by_final_vertices",
    "annotate_valve_edges",
    "apply_local_dimension_filter",
    "save_dimension_mapping_pdf",
    "save_preprocess_annotation_pdf",
    "save_dimension_lead_detection_pdf",
    "save_local_dimension_filter_pdf",
    "save_pipeline_length_diagnostic_pdf",
    "save_final_contour_rays_pdf",
    "save_clean_graph_pdf",
    "save_skeleton_pdf",
]
