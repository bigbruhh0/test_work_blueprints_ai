"""Build a diagnostic dimension map for one PDF page.

The script intentionally does not call an AI provider and does not decide
which dimensions are valid. It only shows the geometry and all candidates
that can be mapped to a pipe edge.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import fitz


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import dimension_mapping, mark_pipeline  # noqa: E402
from src.dimension_mapping import _distance_to_segment  # noqa: E402


EDGE_COLOR = (0.05, 0.38, 0.85)
UNRESOLVED_EDGE_COLOR = (0.9, 0.5, 0.05)
UNCERTAIN_VERTEX_COLOR = (1.0, 0.55, 0.0)
DIMENSION_COLORS = {
    "mapped": (0.0, 0.55, 0.15),
    "ambiguous": (0.95, 0.6, 0.0),
    "unresolved": (0.85, 0.05, 0.05),
}
GEOMETRY_CONFIG = {
    "candidate_edge_gap_px": 45.0,
    "vertex_anchor_gap_px": 18.0,
    "graph_eps_px": 4.0,
    "collinear_cosine": 0.97,
    "stroke_match_gap_px": 2.0,
}


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _nearest_node(point: tuple[float, float], nodes: list[list[float]]) -> tuple[int | None, float]:
    if not nodes:
        return None, float("inf")
    index, node = min(
        enumerate(nodes),
        key=lambda item: _distance(point, (item[1][0], item[1][1])),
    )
    return index, _distance(point, (node[0], node[1]))


def _stroke_graph(pdf_path: Path, page_number: int) -> dict[str, Any]:
    strokes = mark_pipeline.extract_axis_strokes(str(pdf_path), page_number)
    if not strokes:
        return {"strokes": [], "nodes": [], "adjacency": {}, "allowed": set()}
    nodes, adjacency = mark_pipeline.build_node_graph(strokes)
    adjacency = {node: list(neighbors) for node, neighbors in adjacency.items()}
    endpoint_nodes = []
    for stroke in strokes:
        start_node, start_gap = _nearest_node((stroke.x0, stroke.y0), nodes)
        end_node, end_gap = _nearest_node((stroke.x1, stroke.y1), nodes)
        endpoint_nodes.append((start_node, end_node, start_gap, end_gap))
    for first_index, first in enumerate(strokes):
        first_nodes = endpoint_nodes[first_index]
        for second_index in range(first_index + 1, len(strokes)):
            second = strokes[second_index]
            second_nodes = endpoint_nodes[second_index]
            first_dx = first.x1 - first.x0
            first_dy = first.y1 - first.y0
            second_dx = second.x1 - second.x0
            second_dy = second.y1 - second.y0
            cosine = abs(
                (first_dx * second_dx + first_dy * second_dy)
                / ((math.hypot(first_dx, first_dy) or 1.0) * (math.hypot(second_dx, second_dy) or 1.0))
            )
            if cosine < GEOMETRY_CONFIG["collinear_cosine"]:
                continue
            endpoint_pairs = [
                (first_nodes[1], second_nodes[0], _distance((first.x1, first.y1), (second.x0, second.y0))),
                (first_nodes[0], second_nodes[1], _distance((first.x0, first.y0), (second.x1, second.y1))),
                (first_nodes[0], second_nodes[0], _distance((first.x0, first.y0), (second.x0, second.y0))),
                (first_nodes[1], second_nodes[1], _distance((first.x1, first.y1), (second.x1, second.y1))),
            ]
            node_a, node_b, gap = min(endpoint_pairs, key=lambda item: item[2])
            if node_a is None or node_b is None or node_a == node_b or gap > GEOMETRY_CONFIG["vertex_anchor_gap_px"]:
                continue
            adjacency.setdefault(node_a, []).append((node_b, -1))
            adjacency.setdefault(node_b, []).append((node_a, -1))
    allowed = mark_pipeline.pipe_component_nodes(adjacency, nodes)
    return {
        "strokes": strokes,
        "nodes": nodes,
        "adjacency": adjacency,
        "allowed": allowed,
    }


def _vertex_rows(pdf_path: Path, page_number: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exact = mark_pipeline.extract_vertices(str(pdf_path), page_number)
    uncertain = mark_pipeline.extract_uncertain_vertices(str(pdf_path), page_number, exact)
    vertices = [
        {
            "id": f"V{index:02d}",
            "role": item["role"],
            "x": round(float(item["x"]), 2),
            "y": round(float(item["y"]), 2),
            "confidence": 1.0,
            "source": "confirmed",
            "degree": item.get("degree"),
        }
        for index, item in enumerate(exact, start=1)
    ]
    uncertain_rows = [
        {
            "id": f"C{index:02d}",
            "role": "uncertain",
            "x": round(float(item["x"]), 2),
            "y": round(float(item["y"]), 2),
            "confidence": float(item.get("confidence", 0.0)),
            "source": item.get("source", "dimension_chain"),
            "exact": bool(item.get("exact", False)),
            "reason": item.get("reason", ""),
        }
        for index, item in enumerate(uncertain, start=1)
    ]
    return vertices, uncertain_rows


def _path_between(
    start: int,
    end: int,
    adjacency: dict[int, list[tuple[int, int]]],
    strokes: list[Any],
    allowed: set[int],
) -> list[int] | None:
    queue = deque([start])
    previous: dict[int, tuple[int | None, int | None]] = {start: (None, None)}
    while queue:
        node = queue.popleft()
        if node == end:
            break
        neighbors = sorted(
            adjacency.get(node, []),
            key=lambda item: strokes[item[1]].length if item[1] >= 0 else 0.0,
        )
        for neighbor, stroke_index in neighbors:
            if neighbor not in allowed or neighbor in previous:
                continue
            previous[neighbor] = (node, stroke_index)
            queue.append(neighbor)
    if end not in previous:
        return None
    path: list[int] = []
    current = end
    while current != start:
        parent, stroke_index = previous[current]
        if parent is None or stroke_index is None:
            return None
        if stroke_index >= 0:
            path.append(stroke_index)
        current = parent
    path.reverse()
    return path


def _build_edges(
    graph: dict[str, Any],
    vertices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes = graph["nodes"]
    adjacency = graph["adjacency"]
    strokes = graph["strokes"]
    allowed = graph["allowed"]
    anchors: dict[str, int] = {}
    for vertex in vertices:
        node, gap = _nearest_node((vertex["x"], vertex["y"]), nodes)
        if node is not None and gap <= GEOMETRY_CONFIG["vertex_anchor_gap_px"]:
            anchors[vertex["id"]] = node

    ordered_vertices = list(vertices)
    edges: list[dict[str, Any]] = []
    used_pairs: set[tuple[str, str]] = set()
    used_strokes: set[int] = set()
    for left_index, left in enumerate(ordered_vertices):
        for right in ordered_vertices[left_index + 1:]:
            start = anchors.get(left["id"])
            end = anchors.get(right["id"])
            if start is None or end is None or start == end:
                continue
            path = _path_between(start, end, adjacency, strokes, allowed)
            if not path:
                continue
            path_nodes = {start, end}
            for stroke_index in path:
                stroke = strokes[stroke_index]
                first, first_gap = _nearest_node((stroke.x0, stroke.y0), nodes)
                last, last_gap = _nearest_node((stroke.x1, stroke.y1), nodes)
                if first is not None and first_gap <= GEOMETRY_CONFIG["graph_eps_px"]:
                    path_nodes.add(first)
                if last is not None and last_gap <= GEOMETRY_CONFIG["graph_eps_px"]:
                    path_nodes.add(last)
            intermediate_anchors = [
                vertex_id for vertex_id, node in anchors.items()
                if vertex_id not in {left["id"], right["id"]} and node in path_nodes
            ]
            if intermediate_anchors:
                continue
            pair = tuple(sorted((left["id"], right["id"])))
            if pair in used_pairs:
                continue
            used_pairs.add(pair)
            used_strokes.update(index for index in path if index >= 0)
            points = []
            for stroke_index in path:
                stroke = strokes[stroke_index]
                if not points:
                    points.append([round(stroke.x0, 2), round(stroke.y0, 2)])
                points.append([round(stroke.x1, 2), round(stroke.y1, 2)])
            edges.append(
                {
                    "id": f"E{len(edges) + 1:03d}",
                    "from_vertex": left["id"],
                    "to_vertex": right["id"],
                    "start": [left["x"], left["y"]],
                    "end": [right["x"], right["y"]],
                    "path_points": points,
                    "stroke_ids": path,
                    "pixel_length": round(sum(strokes[index].length for index in path if index >= 0), 2),
                    "status": "mapped",
                }
            )
    for stroke_index, stroke in enumerate(strokes):
        if stroke_index in used_strokes:
            continue
        start_node, start_gap = _nearest_node((stroke.x0, stroke.y0), nodes)
        end_node, end_gap = _nearest_node((stroke.x1, stroke.y1), nodes)
        if start_node not in allowed or end_node not in allowed or start_gap > GEOMETRY_CONFIG["graph_eps_px"] or end_gap > GEOMETRY_CONFIG["graph_eps_px"]:
            continue
        edges.append(
            {
                "id": f"E{len(edges) + 1:03d}",
                "from_vertex": None,
                "to_vertex": None,
                "start": [round(stroke.x0, 2), round(stroke.y0, 2)],
                "end": [round(stroke.x1, 2), round(stroke.y1, 2)],
                "path_points": [
                    [round(stroke.x0, 2), round(stroke.y0, 2)],
                    [round(stroke.x1, 2), round(stroke.y1, 2)],
                ],
                "stroke_ids": [stroke_index],
                "pixel_length": round(stroke.length, 2),
                "status": "unresolved_geometry",
            }
        )
    return edges


def _edge_distance(center: tuple[float, float], edge: dict[str, Any]) -> tuple[float, float]:
    path = edge.get("path_points") or [edge["start"], edge["end"]]
    best_distance = float("inf")
    best_position = 0.0
    accumulated = 0.0
    total = sum(_distance(tuple(path[index]), tuple(path[index + 1])) for index in range(len(path) - 1)) or 1.0
    for index in range(len(path) - 1):
        start = tuple(path[index])
        end = tuple(path[index + 1])
        distance, position, _projection = _distance_to_segment(center, start, end)
        if distance < best_distance:
            best_distance = distance
            best_position = (accumulated + position * _distance(start, end)) / total
        accumulated += _distance(start, end)
    return best_distance, best_position


def _parallel_score(dimension: dict[str, Any], edge: dict[str, Any]) -> float:
    bbox = dimension["bbox"]
    dx = bbox[2] - bbox[0]
    dy = bbox[3] - bbox[1]
    edge_dx = edge["end"][0] - edge["start"][0]
    edge_dy = edge["end"][1] - edge["start"][1]
    dimension_length = math.hypot(dx, dy) or 1.0
    edge_length = math.hypot(edge_dx, edge_dy) or 1.0
    return round(abs(dx * edge_dx + dy * edge_dy) / (dimension_length * edge_length), 4)


def _collinear_edges(first: dict[str, Any], second: dict[str, Any]) -> bool:
    first_dx = first["end"][0] - first["start"][0]
    first_dy = first["end"][1] - first["start"][1]
    second_dx = second["end"][0] - second["start"][0]
    second_dy = second["end"][1] - second["start"][1]
    cosine = abs(
        (first_dx * second_dx + first_dy * second_dy)
        / ((math.hypot(first_dx, first_dy) or 1.0) * (math.hypot(second_dx, second_dy) or 1.0))
    )
    shared_endpoint = min(
        _distance(tuple(first["start"]), tuple(second["start"])),
        _distance(tuple(first["start"]), tuple(second["end"])),
        _distance(tuple(first["end"]), tuple(second["start"])),
        _distance(tuple(first["end"]), tuple(second["end"])),
    )
    return cosine >= GEOMETRY_CONFIG["collinear_cosine"] and shared_endpoint <= GEOMETRY_CONFIG["graph_eps_px"]


def _stroke_index_for_source_edge(source_edge: dict[str, Any], strokes: list[Any]) -> int | None:
    source_start = tuple(source_edge["start"])
    source_end = tuple(source_edge["end"])
    matches = []
    for index, stroke in enumerate(strokes):
        direct = _distance(source_start, (stroke.x0, stroke.y0)) + _distance(source_end, (stroke.x1, stroke.y1))
        reverse = _distance(source_start, (stroke.x1, stroke.y1)) + _distance(source_end, (stroke.x0, stroke.y0))
        matches.append((min(direct, reverse), index))
    if not matches:
        return None
    distance, index = min(matches)
    return index if distance <= GEOMETRY_CONFIG["stroke_match_gap_px"] else None


def _map_dimensions(
    pdf_path: Path,
    page_number: int,
    edges: list[dict[str, Any]],
    graph: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[tuple[str, fitz.Rect, str]]]:
    with fitz.open(str(pdf_path)) as document:
        page = document[page_number - 1]
        drawing_area, _format = mark_pipeline.get_drawing_area(page)
        dimensions, _rectangles, discarded = mark_pipeline.extract_dimension_numbers(page, drawing_area)

    existing = dimension_mapping.map_dimensions(pdf_path, page_number)
    existing_dimensions = {item["id"]: item for item in existing.get("dimensions", [])}
    source_edges = dimension_mapping._stroke_graph(pdf_path, page_number)
    source_edges_by_id = {edge["id"]: edge for edge in source_edges}
    target_edges_by_stroke = {
        stroke_id: edge
        for edge in edges
        if edge["status"] == "mapped"
        for stroke_id in edge.get("stroke_ids", [])
    }
    mapped: list[dict[str, Any]] = []
    for index, (text, rect) in enumerate(dimensions, start=1):
        value = float(str(text).replace(",", "."))
        bbox = [round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2)]
        center = [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]
        base = existing_dimensions.get(f"D{index:03d}", {})
        target_edge = None
        source_edge = source_edges_by_id.get(base.get("edge_id"))
        if source_edge is not None:
            source_stroke_index = _stroke_index_for_source_edge(source_edge, graph["strokes"])
            if source_stroke_index is not None:
                target_edge = target_edges_by_stroke.get(source_stroke_index)
        if target_edge is None and base.get("dimension_stroke"):
            stroke = base["dimension_stroke"]
            stroke_center = (
                (stroke["start"][0] + stroke["end"][0]) / 2,
                (stroke["start"][1] + stroke["end"][1]) / 2,
            )
            fallback = []
            for edge in edges:
                if edge["status"] != "mapped":
                    continue
                distance, position = _edge_distance(stroke_center, edge)
                fallback.append((distance, edge, position))
            if fallback:
                distance, candidate, _position = min(fallback, key=lambda item: item[0])
                if distance <= GEOMETRY_CONFIG["candidate_edge_gap_px"]:
                    target_edge = candidate
        candidate_edges = [target_edge] if target_edge is not None else []
        if target_edge is not None and base.get("status") == "leader_attached":
            candidate_edges.extend(
                edge
                for edge in edges
                if edge["status"] == "mapped"
                and edge["id"] != target_edge["id"]
                and _collinear_edges(target_edge, edge)
            )
        possibilities = []
        for candidate_edge in candidate_edges:
            distance, position = _edge_distance((center[0], center[1]), candidate_edge)
            possibilities.append(
                {
                    "edge_id": candidate_edge["id"],
                    "distance_px": round(distance, 2),
                    "position": round(position, 4),
                    "parallel_score": _parallel_score({"bbox": bbox}, candidate_edge),
                    "source": "dimension_mapping" if candidate_edge["id"] == target_edge["id"] else "junction_ambiguity",
                }
            )
        status = "unresolved" if not possibilities else ("ambiguous" if len(possibilities) > 1 else "mapped")
        mapped.append(
            {
                "id": f"D{index:03d}",
                "page": page_number,
                "value_mm": value,
                "text": text,
                "bbox": bbox,
                "label_center": [round(center[0], 2), round(center[1], 2)],
                "edge_candidates": possibilities,
                "selected_edge_id": possibilities[0]["edge_id"] if possibilities else None,
                "status": status,
                "source": "drawing_dimension",
                "mapping_method": "dimension_mapping",
                "existing_mapping": {
                    key: value
                    for key, value in base.items()
                    if key not in {"id", "value", "text", "label_center", "edge_id"}
                },
            }
        )
    return mapped, discarded


def _group_dimensions(edges: list[dict[str, Any]], dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for dimension in dimensions:
        for candidate in dimension.get("edge_candidates", []):
            grouped[candidate["edge_id"]].append(dimension)
    groups = []
    for edge in edges:
        if edge["status"] != "mapped":
            continue
        items = grouped.get(edge["id"], [])
        groups.append(
            {
                "from_vertex": edge["from_vertex"],
                "to_vertex": edge["to_vertex"],
                "edge_ids": [edge["id"]],
                "candidate_ids": [item["id"] for item in items],
                "candidate_values_mm": [item["value_mm"] for item in items],
                "status": "ambiguous" if any(item["status"] == "ambiguous" for item in items) else ("mapped" if items else "no_candidates"),
            }
        )
    return groups


def build_map(pdf_path: Path, page_number: int) -> dict[str, Any]:
    graph = _stroke_graph(pdf_path, page_number)
    vertices, uncertain_vertices = _vertex_rows(pdf_path, page_number)
    edges = _build_edges(graph, vertices)
    dimensions, discarded = _map_dimensions(pdf_path, page_number, edges, graph)
    groups = _group_dimensions(edges, dimensions)
    unresolved_dimensions = [
        {
            "candidate_id": dimension["id"],
            "reason": "no_pipe_edge_within_tolerance",
        }
        for dimension in dimensions
        if dimension["status"] == "unresolved"
    ]
    unresolved_edges = [
        {
            "edge_id": edge["id"],
            "from_vertex": edge["from_vertex"],
            "to_vertex": edge["to_vertex"],
            "reason": edge["status"],
        }
        for edge in edges
        if edge["status"] == "unresolved_geometry"
    ]
    return {
        "pdf": pdf_path.name,
        "page": page_number,
        "tolerance_px": GEOMETRY_CONFIG["candidate_edge_gap_px"],
        "vertices": vertices,
        "uncertain_vertices": uncertain_vertices,
        "edges": edges,
        "dimensions": dimensions,
        "edge_candidate_groups": groups,
        "unresolved_dimensions": unresolved_dimensions,
        "unresolved_edges": unresolved_edges,
        "discarded_numbers": [
            {"text": text, "reason": reason}
            for text, _rect, reason in discarded
        ],
    }


def _text_lines_for_group(group: dict[str, Any], dimensions_by_id: dict[str, dict[str, Any]]) -> list[str]:
    lines = [f"{group['from_vertex']} -> {group['to_vertex']}"]
    for candidate_id in group["candidate_ids"]:
        dimension = dimensions_by_id[candidate_id]
        lines.append(f"{candidate_id}: {dimension['value_mm']:g}")
    if len(lines) == 1:
        lines.append("candidates: []")
    return lines


def render_pdf(pdf_path: Path, page_number: int, output_pdf: Path, mapping: dict[str, Any]) -> None:
    with fitz.open(str(pdf_path)) as source:
        source_page = source[page_number - 1]
        output = fitz.open()
        page = output.new_page(width=source_page.rect.width, height=source_page.rect.height)
        page.show_pdf_page(page.rect, source, page_number - 1)

        for edge in mapping["edges"]:
            points = edge.get("path_points") or [edge["start"], edge["end"]]
            color = EDGE_COLOR if edge["status"] == "mapped" else UNRESOLVED_EDGE_COLOR
            for index in range(len(points) - 1):
                page.draw_line(fitz.Point(*points[index]), fitz.Point(*points[index + 1]), color=color, width=1.3)
            midpoint = points[len(points) // 2]
            page.insert_text(
                fitz.Point(midpoint[0] + 4, midpoint[1] - 4),
                edge["id"],
                fontsize=7,
                fontname="helv",
                color=color,
            )

        for vertex in mapping["vertices"]:
            role_color = mark_pipeline.VERTEX_ROLE_COLORS.get(vertex["role"], (0.8, 0.05, 0.05))
            point = fitz.Point(vertex["x"], vertex["y"])
            page.draw_circle(point, 5, color=role_color, fill=role_color, width=1.0)
            page.insert_text(point + (7, -7), vertex["id"], fontsize=8, fontname="helv", color=role_color)

        for vertex in mapping["uncertain_vertices"]:
            point = fitz.Point(vertex["x"], vertex["y"])
            page.draw_circle(point, 5, color=UNCERTAIN_VERTEX_COLOR, fill=None, width=1.3)
            page.insert_text(point + (7, 10), vertex["id"], fontsize=7, fontname="helv", color=UNCERTAIN_VERTEX_COLOR)

        dimensions_by_id = {dimension["id"]: dimension for dimension in mapping["dimensions"]}
        for dimension in mapping["dimensions"]:
            center = fitz.Point(*dimension["label_center"])
            color = DIMENSION_COLORS[dimension["status"]]
            page.draw_rect(
                fitz.Rect(center.x - 2, center.y - 8, center.x + 38, center.y + 5),
                color=color,
                width=0.8,
            )
            page.insert_text(center + (1, 3), dimension["text"], fontsize=6, fontname="helv", color=color)

        for group in mapping["edge_candidate_groups"]:
            edge = next((item for item in mapping["edges"] if item["id"] in group["edge_ids"]), None)
            if edge is None:
                continue
            points = edge.get("path_points") or [edge["start"], edge["end"]]
            midpoint = points[len(points) // 2]
            lines = _text_lines_for_group(group, dimensions_by_id)
            text = "\n".join(lines)
            width = max(92, max(len(line) for line in lines) * 4.2)
            height = 14 + len(lines) * 10
            rect = fitz.Rect(midpoint[0] + 8, midpoint[1] + 8, midpoint[0] + 8 + width, midpoint[1] + 8 + height)
            if rect.x1 > page.rect.width - 6:
                rect.x0 = max(6, midpoint[0] - width - 8)
                rect.x1 = rect.x0 + width
            if rect.y1 > page.rect.height - 6:
                rect.y0 = max(6, midpoint[1] - height - 8)
                rect.y1 = rect.y0 + height
            page.draw_rect(rect, color=(0.1, 0.2, 0.35), fill=(1, 1, 1), width=0.7, overlay=True)
            text_rect = rect + (3, 3, -3, -3)
            page.insert_textbox(text_rect, text, fontsize=6.5, fontname="helv", color=(0.05, 0.12, 0.25), overlay=True)

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        output.save(str(output_pdf))
        output.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a diagnostic dimension map for one PDF page")
    parser.add_argument("--pdf", type=Path, required=True, help="Path to the source PDF")
    parser.add_argument("--page", type=int, required=True, help="1-based PDF page number")
    parser.add_argument("--output", type=Path, help="Annotated PDF output path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_path = args.pdf.resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")
    with fitz.open(str(pdf_path)) as document:
        if args.page < 1 or args.page > len(document):
            raise SystemExit(f"Page must be between 1 and {len(document)}")

    output_pdf = args.output or ROOT / ".cache" / "dimension_maps" / f"{pdf_path.stem}_page{args.page}_dimension_map.pdf"
    output_json = output_pdf.with_suffix(".json")
    mapping = build_map(pdf_path, args.page)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    render_pdf(pdf_path, args.page, output_pdf, mapping)
    print(json.dumps({
        "pdf": str(output_pdf),
        "json": str(output_json),
        "vertices": len(mapping["vertices"]),
        "uncertain_vertices": len(mapping["uncertain_vertices"]),
        "edges": len(mapping["edges"]),
        "dimensions": len(mapping["dimensions"]),
        "mapped_dimensions": sum(1 for item in mapping["dimensions"] if item["status"] == "mapped"),
        "ambiguous_dimensions": sum(1 for item in mapping["dimensions"] if item["status"] == "ambiguous"),
        "unresolved_dimensions": len(mapping["unresolved_dimensions"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
