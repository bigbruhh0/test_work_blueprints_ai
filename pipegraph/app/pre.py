from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.candidate_extractor import extract_candidates_for_groups  # noqa: E402
from src.pdf_ingest import group_pages_by_line, read_pdf_pages  # noqa: E402
from src.route_reconstruction import extract_local_vertices  # noqa: E402


def read_document(pdf_path: str | Path):
    pages = read_pdf_pages(pdf_path)
    groups = group_pages_by_line(pages)
    return pages, groups


def render_page_image(pdf_path: str | Path, page_number: int, zoom: float = 1.5) -> "Image.Image":
    with pymupdf.open(str(pdf_path)) as document:
        page = document.load_page(page_number - 1)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return Image.open(__import__("io").BytesIO(pixmap.tobytes("png"))).convert("RGB")


def label_vertices_on_image(image: "Image.Image", vertices: list[dict], zoom: float) -> "Image.Image":
    marked = image.copy()
    draw = ImageDraw.Draw(marked, "RGBA")
    try:
        font = ImageFont.truetype("arial.ttf", size=max(10, int(12 * zoom)))
    except Exception:
        try:
            font = ImageFont.load_default(size=max(10, int(12 * zoom)))
        except TypeError:
            font = ImageFont.load_default()
    radius = max(3, int(4 * zoom))
    for vertex in vertices:
        x = float(vertex.get("x") or 0) * zoom
        y = float(vertex.get("y") or 0) * zoom
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(220, 38, 38, 240))
        label = str(vertex.get("id") or "")
        draw.text((x + radius + 2, y - radius - 2), label, fill=(185, 28, 28, 255), font=font)
    return marked


def vertex_dicts(vertices) -> list[dict]:
    output: list[dict] = []
    for vertex in vertices:
        output.append(
            {
                "id": getattr(vertex, "id", None) or getattr(vertex, "label", None),
                "role": getattr(vertex, "role", None),
                "x": getattr(vertex, "x", None),
                "y": getattr(vertex, "y", None),
            }
        )
    return output


def drawing_numbers(pdf_path: str | Path, group) -> list[dict]:
    """Числовые кандидаты только из графической зоны чертежа."""
    candidates = extract_candidates_for_groups(pdf_path, [group])
    drawing: list[dict] = []
    for candidate in candidates:
        if candidate.zone != "drawing" or candidate.kind not in {"dimension", "numeric"}:
            continue
        text = str(candidate.text).strip()
        try:
            float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            continue
        drawing.append(
            {"id": candidate.id, "text": candidate.text, "bbox": [round(v, 1) for v in candidate.bbox]}
        )
    return drawing


def pipe_center_distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(second[0] - first[0], second[1] - first[1])
