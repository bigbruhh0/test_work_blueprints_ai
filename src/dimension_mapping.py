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
        if -back_tolerance <= distance <= max_distance and side_gap <= side_tolerance:
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
                continue
            _score, _distance, point, side_gap, hit_edge, used_ray_start, ray_source = best_hit
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
    return candidates[0][3], candidates[0][4]


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


def map_dimensions(pdf_path: str | Path, page_number: int) -> dict[str, Any]:
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
                should_resolve_leader = pipe_edge is None or not _stroke_angle_matches_edge(attached, pipe_edge)
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
        compute_extension_vertices(result)
        result["handwheels"] = _handwheel_details(page, result)
        annotate_valve_edges(result)
        result["handwheel_glyphs"] = _handwheel_glyph_rows(page)
        result["handwheel_vertices"] = _glyph_pipe_vertex_rows(result)
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
            found.append({"center": [round(cross[0], 2), round(cross[1], 2)], "corners": corners})
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
    """Две вершины на контуре трубы — начало и конец штурвала по его розовой метке."""
    edges = mapping.get("edges") or []
    vertices: list[dict[str, Any]] = []
    for glyph in mapping.get("handwheel_glyphs") or []:
        corners = glyph.get("corners") or []
        center = glyph.get("center")
        if not center or len(center) < 2 or len(corners) != 4:
            continue
        center_point = (float(center[0]), float(center[1]))
        best = None
        for edge in edges:
            gap, _position, projection = _distance_to_segment(center_point, tuple(edge["start"]), tuple(edge["end"]))
            if best is None or gap < best[0]:
                best = (gap, edge, projection)
        if best is None or best[0] > HANDWHEEL_GLYPH_VERTEX_MAX_EDGE_GAP_PX:
            continue
        _gap, edge, projection = best
        edge_start = edge["start"]
        edge_end = edge["end"]
        edge_length = _point_distance(tuple(edge_start), tuple(edge_end)) or 1.0
        unit_x = (edge_end[0] - edge_start[0]) / edge_length
        unit_y = (edge_end[1] - edge_start[1]) / edge_length

        def offset_along(point: list[float]) -> float:
            return (point[0] - edge_start[0]) * unit_x + (point[1] - edge_start[1]) * unit_y

        offsets = [offset_along(corner) for corner in corners]
        for suffix, role, position in (("A", "start", min(offsets)), ("B", "end", max(offsets))):
            vertices.append(
                {
                    "id": f"{glyph['id']}-{suffix}",
                    "source": "handwheel_glyph_span",
                    "role": role,
                    "handwheel_id": glyph.get("id"),
                    "edge_id": edge.get("id"),
                    "point": [round(edge_start[0] + position * unit_x, 2), round(edge_start[1] + position * unit_y, 2)],
                }
            )
    return vertices


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

        extension_vertex_color = (0.45, 0.05, 0.75)
        handwheel_vertices = mapping.get("handwheel_vertices") or []
        handwheel_points = [
            tuple(vertex["point"])
            for vertex in handwheel_vertices
            if isinstance(vertex.get("point"), list) and len(vertex.get("point") or []) >= 2
        ]
        for vertex in mapping.get("extension_vertices", []):
            point = vertex.get("point")
            ray_start = vertex.get("ray_start")
            if not point or len(point) < 2 or not ray_start or len(ray_start) < 2:
                continue
            if any(_point_distance((float(point[0]), float(point[1])), handwheel_point) <= 8.0 for handwheel_point in handwheel_points):
                continue
            from_point = fitz.Point(float(ray_start[0]), float(ray_start[1]))
            center = fitz.Point(float(point[0]), float(point[1]))
            marked.draw_line(
                from_point,
                center,
                color=extension_vertex_color,
                width=0.8,
                dashes="[2 2] 0",
            )
            marked.draw_circle(center, 9.0, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.72, width=0.8)
            marked.draw_circle(center, 6.5, color=extension_vertex_color, fill=None, width=2.2)
            marked.draw_line(center + (-4.5, 0), center + (4.5, 0), color=extension_vertex_color, width=1.6)
            marked.draw_line(center + (0, -4.5), center + (0, 4.5), color=extension_vertex_color, width=1.6)
            label = str(vertex.get("id") or "VE?")
            dimensions_label = ",".join(str(item) for item in (vertex.get("dimension_ids") or []) if item)
            if dimensions_label:
                label = f"{label}/{dimensions_label}"
            label_point = center + (11, -11)
            label_rect = fitz.Rect(label_point.x - 2, label_point.y - 9, label_point.x + 66, label_point.y + 4)
            marked.draw_rect(label_rect, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.85, width=0)
            marked.insert_text(label_point, label, fontsize=8, fontname="helv", color=extension_vertex_color)

        handwheel_vertex_color = (0.14, 0.39, 0.92)
        for vertex in handwheel_vertices:
            point = vertex.get("point")
            if not point or len(point) < 2:
                continue
            center = fitz.Point(float(point[0]), float(point[1]))
            marked.draw_circle(center, 11.0, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.82, width=0.8)
            marked.draw_circle(center, 7.8, color=handwheel_vertex_color, fill=None, width=2.8)
            marked.draw_line(center + (-5.8, -5.8), center + (5.8, 5.8), color=handwheel_vertex_color, width=2.0)
            marked.draw_line(center + (-5.8, 5.8), center + (5.8, -5.8), color=handwheel_vertex_color, width=2.0)
            label = str(vertex.get("id") or "VH?")
            detail = str(vertex.get("handwheel_id") or "HW?")
            if vertex.get("dimension_id"):
                detail = f"{detail} / {vertex['dimension_id']}"
            elif vertex.get("edge_id"):
                detail = f"{detail} / {vertex['edge_id']}"
            if vertex.get("point_source"):
                detail = f"{detail} / {vertex['point_source']}"
            label_point = center + (13, -13)
            label_rect = fitz.Rect(label_point.x - 2, label_point.y - 21, label_point.x + 160, label_point.y + 8)
            marked.draw_rect(label_rect, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.92, width=0)
            marked.insert_text(label_point, label, fontsize=11, fontname="helv", color=handwheel_vertex_color)
            marked.insert_text(label_point + (0, 12), detail, fontsize=8.5, fontname="helv", color=handwheel_vertex_color)

        for item in mapping.get("handwheels") or _handwheel_details(page, mapping):
            arrow_end = item.get("arrow_end")
            if not arrow_end or len(arrow_end) < 2:
                continue
            center = fitz.Point(float(arrow_end[0]), float(arrow_end[1]))
            marked.draw_circle(center, 4.2, color=handwheel_vertex_color, fill=handwheel_vertex_color, fill_opacity=0.88, width=1.0)
            lead_label = f"{item.get('id', 'HW?')} lead -> {item.get('edge_id') or '?'}"
            label_point = center + (7, 7)
            label_rect = fitz.Rect(label_point.x - 2, label_point.y - 9, label_point.x + 92, label_point.y + 4)
            marked.draw_rect(label_rect, color=(1.0, 1.0, 1.0), fill=(1.0, 1.0, 1.0), fill_opacity=0.88, width=0)
            marked.insert_text(label_point, lead_label, fontsize=7.5, fontname="helv", color=handwheel_vertex_color)

        marked.insert_text(
            fitz.Point(24, 28),
            "LOCAL DIMENSION FILTER: green include, red exclude; violet new vertices on extension-to-pipe projection",
            fontsize=9,
            fontname="helv",
            color=(0.05, 0.18, 0.14),
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


def run_dimension_mapping(pdf_path: str | Path, page_number: int, output_pdf: str | Path, output_json: str | Path) -> dict[str, Any]:
    mapping = map_dimensions(pdf_path, page_number)
    save_dimension_mapping_pdf(pdf_path, page_number, output_pdf, mapping)
    Path(output_json).write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    return mapping


__all__ = [
    "map_dimensions",
    "run_dimension_mapping",
    "compute_endpoint_adjustments",
    "compute_extension_vertices",
    "annotate_valve_edges",
    "apply_local_dimension_filter",
    "save_dimension_mapping_pdf",
    "save_preprocess_annotation_pdf",
    "save_local_dimension_filter_pdf",
    "save_clean_graph_pdf",
    "save_skeleton_pdf",
]
