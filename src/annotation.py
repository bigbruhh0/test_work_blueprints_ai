from __future__ import annotations

import io
import math
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

from .models import Annotation


def annotations_for_page(annotations: list[Annotation], page_number: int) -> list[Annotation]:
    return [item for item in annotations if item.page == page_number]


def drawable_annotations(annotations: list[Annotation]) -> list[Annotation]:
    return [item for item in annotations if _is_valid_bbox(item.bbox)]


def render_page_with_annotations(
    pdf_path: str | Path,
    page_number: int,
    annotations: list[Annotation],
    zoom: float = 1.35,
) -> bytes:
    document = fitz.open(str(pdf_path))
    try:
        page = document.load_page(page_number - 1)
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    finally:
        document.close()

    draw = ImageDraw.Draw(image, "RGBA")
    font = ImageFont.load_default()
    for annotation in drawable_annotations(annotations):
        x0, y0, x1, y1 = [value * zoom for value in annotation.bbox]
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        color = annotation.color
        if annotation.kind.startswith("vertex_"):
            center_x = (x0 + x1) / 2
            center_y = (y0 + y1) / 2
            draw.ellipse((center_x - 7, center_y - 7, center_x + 7, center_y + 7), fill=color, outline=(255, 255, 255, 255), width=2)
            draw.text(
                (x0 + 4, max(0, y0 - 17)),
                annotation.label,
                fill=color,
                font=font,
                stroke_width=2,
                stroke_fill=(255, 255, 255, 230),
            )
        else:
            draw.rectangle((x0, y0, x1, y1), outline=color, width=4)
            draw.rectangle((x0, max(0, y0 - 20), x0 + max(70, len(annotation.label) * 7), y0), fill=(255, 255, 255, 225))
            draw.text((x0 + 4, max(0, y0 - 17)), annotation.label, fill=color, font=font)

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def render_pdf_with_annotations(pdf_path: str | Path, annotations: list[Annotation]) -> bytes:
    """Create a new PDF with point marks drawn in source PDF coordinates."""
    document = fitz.open(str(pdf_path))
    try:
        for annotation in drawable_annotations(annotations):
            if not annotation.kind.startswith("vertex_"):
                continue
            page = document.load_page(annotation.page - 1)
            x0, y0, x1, y1 = annotation.bbox
            center = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
            page.draw_circle(center, 5, color=(0.86, 0.08, 0.15), fill=(0.86, 0.08, 0.15), width=1.2)
            page.insert_text(fitz.Point(x1 + 6, y0), annotation.label, fontsize=8, fontname="helv", color=(0.86, 0.08, 0.15))
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()


def render_clean_drawing(
    pdf_path: str | Path,
    page_number: int,
    mask_rects: list[tuple[float, float, float, float]],
    zoom: float = 1.35,
) -> bytes:
    """Рендер страницы без лишних зон (таблицы, спецификация, штамп).

    Оставляет чистый чертеж: линию трубопровода, размерные линии со стрелками,
    числа размеров и выноски — всё, что физически находится в зоне drawing.
    """
    document = fitz.open(str(pdf_path))
    try:
        page = document.load_page(page_number - 1)
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    finally:
        document.close()

    draw = ImageDraw.Draw(image, "RGBA")
    for rect in mask_rects:
        x0, y0, x1, y1 = [value * zoom for value in rect]
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        draw.rectangle((x0 - 4, y0 - 4, x1 + 4, y1 + 4), fill=(255, 255, 255))

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def union_rect(rects: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    if not rects:
        return None
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )


def render_skeleton_drawing(
    pdf_path: str | Path,
    page_number: int,
    dimension_texts: list[tuple[str, tuple[float, float, float, float]]],
    candidates=None,
    zoom: float = 2.0,
) -> bytes:
    """Перерисовать лист в «скелет»: только труба (толстые штрихи),
    размерные линии, привязанные к числовым размерам, и числа длин.

    Выноски опор, позиции деталей и их стрелки НЕ рисуются: тонкий штрих
    сохраняется только если он прикреплен к размерному числу в векторном графе.
    """
    from .candidate_extractor import classify_zone
    from .graph_solver import MAX_LENGTH_MM
    from .route_reconstruction import (
        MIN_STROKE_LENGTH as MIN_DRAWING_STROKE_LENGTH,
        _is_frame_line,
    )

    width_pt = height_pt = None
    objects: list[dict] = []
    rect_objects: list[dict] = []
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            if not (1 <= page_number <= len(pdf.pages)):
                return render_clean_drawing(pdf_path, page_number, [], zoom)
            page = pdf.pages[page_number - 1]
            width_pt = float(page.width)
            height_pt = float(page.height)
            objects = list(page.objects.get("line", [])) + list(page.objects.get("curve", []))
            rect_objects = list(page.objects.get("rect", []))
    except Exception:
        return render_clean_drawing(pdf_path, page_number, [], zoom)

    addons = [
        candidate for candidate in (candidates or []) if getattr(candidate, "page", None) == page_number
    ]
    squares: list[tuple[float, float, float, float]] = []

    def _normalize_rect(rect) -> tuple[float, float, float, float] | None:
        if rect is None:
            return None
        if isinstance(rect, dict):
            x0 = float(rect.get("x0", 0.0))
            y0 = float(rect.get("y0", 0.0))
            x1 = float(rect.get("x1", 0.0))
            y1 = float(rect.get("y1", 0.0))
        else:
            x0, y0, x1, y1 = rect
        if max(x1, x0) - min(x0, x1) <= 1.0 and max(y1, y0) - min(y0, y1) <= 1.0:
            return None
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    for rect in rect_objects:
        normalized = _normalize_rect(rect)
        if normalized is None:
            continue
        bbox = normalized
        if classify_zone(bbox, width_pt, height_pt) != "drawing":
            continue
        square_side = min(abs(bbox[2] - bbox[0]), abs(bbox[3] - bbox[1]))
        if square_side <= 30.0:
            squares.append(bbox)

    addons = [
        candidate for candidate in (candidates or []) if getattr(candidate, "page", None) == page_number
    ]

    parsed_objects: list[dict] = []
    for obj in objects:
        pts = obj.get("pts")
        if not pts or len(pts) < 2:
            continue
        x0, y0 = float(pts[0][0]), float(pts[0][1])
        x1, y1 = float(pts[-1][0]), float(pts[-1][1])
        bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if classify_zone(bbox, width_pt, height_pt) != "drawing":
            continue
        length = math.hypot(x1 - x0, y1 - y0)
        if length < MIN_DRAWING_STROKE_LENGTH or length > MAX_LENGTH_MM:
            continue
        if _is_frame_line(bbox, length, width_pt, height_pt):
            continue
        linewidth = float(obj.get("linewidth") or obj.get("stroke_width") or 0.6)
        parsed_objects.append(
            {
                "pts": pts,
                "bbox": bbox,
                "length": length,
                "linewidth": linewidth,
                "is_axis": linewidth >= 0.95,
            }
        )

    squares.extend(_small_square_strokes(parsed_objects))

    canvas_w = int(width_pt * zoom)
    canvas_h = int(height_pt * zoom)
    image = Image.new("RGB", (canvas_w, canvas_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")

    for stroke in parsed_objects:
        if stroke.get("square_member"):
            continue
        linewidth = stroke["linewidth"]
        is_axis = stroke["is_axis"]
        color_value = 20 if is_axis else 90
        alpha_value = 255 if is_axis else 210
        width_px = max(2, int(round(linewidth * 3.2))) if is_axis else 2
        pts = stroke["pts"]
        if len(pts) == 2:
            draw.line(
                ((pts[0][0]) * zoom, (pts[0][1]) * zoom, (pts[-1][0]) * zoom, (pts[-1][1]) * zoom),
                fill=(color_value, color_value, color_value, alpha_value),
                width=width_px,
            )
        else:
            points = []
            for px, py in pts:
                points.append((float(px) * zoom, float(py) * zoom))
            draw.line(points, fill=(color_value, color_value, color_value, alpha_value), width=width_px, joint="curve")

    stroke_font = _load_font(max(12, 9 * zoom))
    for text, bbox in dimension_texts:
        if len(bbox) != 4:
            continue
        x = bbox[0] * zoom
        y = bbox[1] * zoom
        display = str(text)
        draw.text((x, y), display, fill=(10, 10, 10), font=stroke_font)

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _small_square_strokes(parsed_objects: list[dict]) -> list[tuple[float, float, float, float]]:
    """Найти замкнутые «квадратики» из коротких тонких штрихов.

    Квадрат = замкнутая петля: у компонента вершин <= рёбер (цикл),
    габариты <= 34 px, сегментов >= 3. Пунктирные цепочки не замкнуты
    (конец != начало), потому не считаются квадратами.
    Помечает штрихи-члены флагом "square_member", их рендер пропускает.
    """
    short = [stroke for stroke in parsed_objects if not stroke.get("is_axis") and stroke["length"] <= 30.0]
    if not short:
        return []

    node_points: list[tuple[float, float]] = []
    stroke_edges: dict[int, tuple[int, int]] = {}

    def node_id(point: tuple[float, float]) -> int:
        for index, (nx, ny) in enumerate(node_points):
            if math.hypot(nx - point[0], ny - point[1]) <= 4.0:
                return index
        node_points.append(point)
        return len(node_points) - 1

    for index, stroke in enumerate(short):
        pts = stroke["pts"]
        first_node = node_id((float(pts[0][0]), float(pts[0][1])))
        last_node = node_id((float(pts[-1][0]), float(pts[-1][1])))
        stroke["square_member"] = False
        stroke.setdefault("_loop_edges", []).append((first_node, last_node))
        stroke_edges[index] = (first_node, last_node)

    parent = list(range(len(node_points)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for node_a, node_b in stroke_edges.values():
        root_a, root_b = find(node_a), find(node_b)
        if root_a != root_b:
            parent[root_b] = root_a

    component_nodes: dict[int, set[int]] = {}
    component_edges: dict[int, int] = {}
    for endpoint in range(len(node_points)):
        root = find(endpoint)
        component_nodes.setdefault(root, set()).add(endpoint)
    for node_a, node_b in stroke_edges.values():
        root = find(node_a)
        component_edges[root] = component_edges.get(root, 0) + 1

    squares: list[tuple[float, float, float, float]] = []
    for index, stroke in enumerate(short):
        first_node, _last_node = stroke_edges[index]
        root = find(first_node)
        edges_in_component = component_edges.get(root, 0)
        vertices_in_component = len(component_nodes.get(root, set()))
        points_in_root = [node_points[node] for node in component_nodes.get(root, set())]
        xs = [pt[0] for pt in points_in_root]
        ys = [pt[1] for pt in points_in_root]
        if (
            edges_in_component >= 3
            and vertices_in_component <= edges_in_component
            and max(xs) - min(xs) <= 34.0
            and max(ys) - min(ys) <= 34.0
        ):
            stroke["square_member"] = True
            squares.append((min(xs), min(ys), max(xs), max(ys)))
    return squares


def _stroke_attached(axis_graph, bbox, attached_strokes) -> bool:
    for stroke_index in attached_strokes:
        stroke = axis_graph.strokes[stroke_index]
        if bbox == (min(stroke.x0, stroke.x1), min(stroke.y0, stroke.y1), max(stroke.x0, stroke.x1), max(stroke.y0, stroke.y1)):
            return True
        overlap = not (
            max(stroke.x0, stroke.x1) < bbox[0]
            or min(stroke.x0, stroke.x1) > bbox[2]
            or max(stroke.y0, stroke.y1) < bbox[1]
            or min(stroke.y0, stroke.y1) > bbox[3]
        )
        if overlap:
            return True
    return False


def _load_font(size: float = 15.0) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", size=int(size))
    except Exception:
        try:
            return ImageFont.load_default(size=int(size))
        except TypeError:
            return ImageFont.load_default()


def _is_valid_bbox(bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None or len(bbox) != 4:
        return False
    return all(isinstance(value, (int, float)) for value in bbox)
