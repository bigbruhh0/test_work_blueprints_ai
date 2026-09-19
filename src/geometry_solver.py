from __future__ import annotations

from .candidate_extractor import _coordinate_blocks
from .models import AnalysisResult, Candidate, LineGroup, LineResult, Uncertainty


def apply_geometry_solution(
    result: AnalysisResult,
    group: LineGroup,
    candidates: list[Candidate],
) -> AnalysisResult:
    """Apply deterministic geometry rules that are valid for any line.

    Координатные перепады больше не добавляются как самостоятельные участки:
    по правилу route reconstruction они используются только как проверка
    размерных длин чертежа (dedupe/skip), а не как источник длин main/branch.
    """
    _ensure_line(result, group)
    line_id = group.line_id
    pages = group.pages
    drawing_coord = [
        candidate for candidate in candidates
        if candidate.line_id == line_id
        and candidate.zone == "drawing"
        and candidate.kind in {"coordinate_label", "coordinate_value"}
    ]
    page = pages[0].page_number if pages else None
    if drawing_coord:
        blocks = _coordinate_blocks(line_id, page or 0, drawing_coord)
        if len(blocks) >= 2:
            _add_uncertainty(
                result,
                line_id,
                "geometry_solver",
                "Найдены координатные блоки привязок. Координатные перепады учитываются правилом route reconstruction (как проверка размерных длин), а не добавляются как самостоятельные участки.",
                "info",
                page,
            )
        else:
            _add_uncertainty(
                result,
                line_id,
                "geometry_solver",
                "Координатных блоков привязок меньше двух; перепадные участки не выводятся.",
                "info",
                page,
            )
    else:
        _add_uncertainty(
            result,
            line_id,
            "geometry_solver",
            "Извлеченные кандидаты и rule layer будут использованы при реконструкции маршрута; geometry solver не добавляет участков.",
            "info",
            page,
        )
    return result


def _ensure_line(result: AnalysisResult, group: LineGroup) -> None:
    if any(line.id == group.line_id for line in result.lines):
        return
    result.lines.append(
        LineResult(
            id=group.line_id,
            pages=[page.page_number for page in group.pages],
            status="partial",
            completeness_note="Линия добавлена без специальных правил под конкретный пример.",
        )
    )


def _add_uncertainty(
    result: AnalysisResult,
    line_id: str,
    target: str,
    reason: str,
    severity: str,
    page: int | None,
) -> None:
    if any(item.line_id == line_id and item.target == target and item.reason == reason for item in result.uncertainties):
        return
    result.uncertainties.append(
        Uncertainty(
            id=f"G-U-{len(result.uncertainties) + 1:03d}",
            line_id=line_id,
            page=page,
            target=target,
            reason=reason,
            severity=severity,  # type: ignore[arg-type]
        )
    )