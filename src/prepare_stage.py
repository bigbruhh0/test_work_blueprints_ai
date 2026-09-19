from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "mark_pipeline_mod", Path(__file__).resolve().parent / "mark_pipeline.py"
)
mark_pipeline = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mark_pipeline)


@dataclass
class PrepareResult:
    page_number: int
    numbers_pdf: Path
    vertices_pdf: Path
    numbers_txt: Path
    numbers: list[dict[str, Any]] = field(default_factory=list)
    vertices: list[dict[str, Any]] = field(default_factory=list)
    skipped_numbers: list[dict[str, Any]] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)


def run_prepare(pdf_path: str | Path, page_number: int, out_dir: str | Path) -> PrepareResult:
    """Подготовить 3 файла для провайдера и вернуть их пути + данные.

    Страница передаётся 1-based (как в тексте задания), внутри скрипта она
    конвертируется в 0-based для PyMuPDF.
    """
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    page_1based = int(page_number)
    page_0based = page_1based - 1
    prefix = out_dir / f"{pdf_path.stem}_page{page_1based}"

    numbers_pdf = Path(f"{prefix}_numbers_marked.pdf")
    vertices_pdf = Path(f"{prefix}_vertices_marked.pdf")
    numbers_txt = Path(f"{prefix}_numbers.txt")

    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    page = doc[page_0based]
    drawing_area, _fmt = mark_pipeline.get_drawing_area(page)
    dims, rectangles, discarded = mark_pipeline.extract_dimension_numbers(page, drawing_area)
    doc.close()

    vertices = mark_pipeline.extract_vertices(str(pdf_path), page_1based)
    components = mark_pipeline.components_report(str(pdf_path), page_1based)
    mark_pipeline.save_numbers_pdf(str(pdf_path), page_0based, str(numbers_pdf), dims, rectangles, drawing_area)
    mark_pipeline.save_vertices_pdf(str(pdf_path), page_0based, str(vertices_pdf), vertices)
    mark_pipeline.save_numbers_txt(str(numbers_txt), dims, vertices, drawing_area)

    numbers = [
        {
            "text": text,
            "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
            "center": [(rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2],
        }
        for text, rect in dims
    ]
    discards = [
        {
            "text": text,
            "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
            "reason": reason,
        }
        for text, rect, reason in discarded
    ]
    vertices_rows = [
        {
            "id": f"V{index:02d}",
            "role": vertex["role"],
            "color": mark_pipeline.VERTEX_ROLE_NAMES_RU.get(vertex["role"], "чёрный"),
            "x": vertex["x"],
            "y": vertex["y"],
            "degree": vertex["degree"],
        }
        for index, vertex in enumerate(vertices, start=1)
    ]
    return PrepareResult(
        page_number=page_1based,
        numbers_pdf=numbers_pdf,
        vertices_pdf=vertices_pdf,
        numbers_txt=numbers_txt,
        numbers=numbers,
        vertices=vertices_rows,
        skipped_numbers=discards,
        components=components,
    )


__all__ = ["PrepareResult", "run_prepare"]
