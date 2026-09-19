from __future__ import annotations

import re
from collections import defaultdict

from .models import AnalysisResult, Candidate, CandidateClassification, ElementResult, LineResult, SegmentResult, Uncertainty


ITEM_CALLOUT_RE = re.compile(r"(^|\s)[ОO]\s*\d+\b")
CANDIDATE_ID_RE = re.compile(r"C-\d{3}-\d{4}")
MIN_ROUTE_SEGMENT_CONFIDENCE = 0.7
MIN_REVIEW_ROUTE_SEGMENT_CONFIDENCE = 0.5


def validate_and_normalize(result: AnalysisResult) -> AnalysisResult:
    """Apply generic deterministic checks from the task statement."""

    existing_uncertainty_keys = {
        (item.line_id, item.target, item.reason)
        for item in result.uncertainties
    }

    def add_uncertainty(
        line_id: str,
        target: str,
        reason: str,
        severity: str = "warning",
        page: int | None = None,
    ) -> None:
        key = (line_id, target, reason)
        if key in existing_uncertainty_keys:
            return
        existing_uncertainty_keys.add(key)
        result.uncertainties.append(
            Uncertainty(
                id=f"V-{len(result.uncertainties) + 1:03d}",
                line_id=line_id,
                page=page,
                target=target,
                reason=reason,
                severity=severity,  # type: ignore[arg-type]
            )
        )

    points_by_line = defaultdict(set)
    for point in result.points:
        points_by_line[point.line_id].add(point.id)
        if point.source == "unknown" and any(value == 0 for value in (point.x, point.y, point.z)):
            point.x = None if point.x == 0 else point.x
            point.y = None if point.y == 0 else point.y
            point.z = None if point.z == 0 else point.z
            add_uncertainty(
                point.line_id,
                point.id,
                "Неизвестные координаты не должны заменяться нулями; нули очищены до null.",
                "warning",
                point.source_ref.page if point.source_ref else None,
            )

    segments_by_line = defaultdict(list)
    candidate_by_id = {candidate.id: candidate for candidate in result.candidates}
    classification_by_candidate_id = {
        item.candidate_id: item for item in result.candidate_classifications
    }
    for classification in result.candidate_classifications:
        _apply_candidate_gate(classification)

    for segment in result.segments:
        matched_classification = _match_segment_classification(
            segment,
            result.candidates,
            classification_by_candidate_id,
        )
        matched_candidate = candidate_by_id.get(matched_classification.candidate_id) if matched_classification else None
        if (
            not segment.id.startswith("G-")
            and result.candidate_classifications
            and matched_classification is None
            and segment.length_mm is not None
        ):
            segment.length_mm = None
            add_uncertainty(
                segment.line_id,
                segment.id,
                "AI-участок не связан с подтвержденным PDF-кандидатом; длина исключена из расчета.",
                "warning",
                segment.source_ref.page if segment.source_ref else None,
            )
        elif matched_classification and not matched_classification.accepted:
            segment.length_mm = None
            add_uncertainty(
                segment.line_id,
                segment.id,
                f"Источник размера отклонен rule gate: {matched_classification.gate_reason}",
                "warning",
                segment.source_ref.page if segment.source_ref else None,
            )
        elif matched_classification and matched_classification.accepted:
            if segment.length_mm is None:
                restored_length = _parse_positive_number(segment.source_size)
                if restored_length is not None:
                    segment.length_mm = restored_length
                    add_uncertainty(
                        segment.line_id,
                        segment.id,
                        "Длина восстановлена из source_size, потому что PDF-кандидат принят rule gate.",
                        "info",
                        segment.source_ref.page if segment.source_ref else None,
                    )
            _sync_segment_route_type(segment, matched_classification, matched_candidate, add_uncertainty)
        segments_by_line[segment.line_id].append(segment)
        known_points = points_by_line.get(segment.line_id, set())
        if segment.start_point_id not in known_points or segment.end_point_id not in known_points:
            add_uncertainty(
                segment.line_id,
                segment.id,
                "Участок ссылается на неизвестную начальную или конечную точку.",
                "critical",
                segment.source_ref.page if segment.source_ref else None,
            )
        if segment.length_mm is None:
            add_uncertainty(
                segment.line_id,
                segment.id,
                "Участок не имеет подтвержденной длины.",
                "warning",
                segment.source_ref.page if segment.source_ref else None,
            )

    accepted_elements_by_line = defaultdict(list)
    for element in result.elements:
        if element.element_type == "support" and _looks_like_material_callout(element):
            element.status = "needs_review"
            add_uncertainty(
                element.line_id,
                element.id,
                "Обозначение похоже на позицию детали опоры, а не на подтвержденное физическое место опирания; элемент исключен из автоматического количества опор.",
                "warning",
                element.source_ref.page if element.source_ref else None,
            )
            continue
        accepted_elements_by_line[element.line_id].append(element)

    for line in result.lines:
        _normalize_lengths(line, segments_by_line.get(line.id, []), add_uncertainty)
        _normalize_element_counts(line, accepted_elements_by_line.get(line.id, []))
        line_uncertainties = [
            uncertainty for uncertainty in result.uncertainties if uncertainty.line_id == line.id
        ]
        _normalize_line_status(line, line_uncertainties)

    return result


def _apply_candidate_gate(classification: CandidateClassification) -> None:
    classification.accepted = False
    if classification.classification != "route_segment":
        classification.gate_reason = f"classification={classification.classification}"
        return
    confidence = classification.confidence if classification.confidence is not None else 0.0
    if confidence < MIN_REVIEW_ROUTE_SEGMENT_CONFIDENCE:
        classification.gate_reason = f"confidence {confidence} < {MIN_REVIEW_ROUTE_SEGMENT_CONFIDENCE}"
        return
    classification.accepted = True
    if classification.route_type in {"main", "branch"} and confidence >= MIN_ROUTE_SEGMENT_CONFIDENCE:
        classification.gate_reason = "accepted route segment"
    elif classification.route_type in {"main", "branch"}:
        classification.gate_reason = "accepted route segment with low confidence"
    else:
        classification.route_type = "unknown"
        classification.gate_reason = "accepted route segment with unknown route type"


def _sync_segment_route_type(
    segment: SegmentResult,
    classification: CandidateClassification,
    candidate: Candidate | None,
    add_uncertainty,
) -> None:
    if candidate and candidate.kind == "coordinate_delta":
        segment.route_type = "unknown"
        add_uncertainty(
            segment.line_id,
            segment.id,
            "Участок опирается только на координатный перепад; по правилу route reconstruction перепады не включаются в длины main/branch.",
            "warning",
            segment.source_ref.page if segment.source_ref else None,
        )
        return

    if classification.route_type in {"main", "branch"}:
        if segment.route_type != classification.route_type:
            add_uncertainty(
                segment.line_id,
                segment.id,
                f"Тип маршрута участка скорректирован по классификации кандидата: {segment.route_type} -> {classification.route_type}.",
                "info",
                segment.source_ref.page if segment.source_ref else None,
            )
            segment.route_type = classification.route_type
        if (classification.confidence or 0.0) < MIN_ROUTE_SEGMENT_CONFIDENCE:
            add_uncertainty(
                segment.line_id,
                segment.id,
                "Длина принята, но уверенность классификации ниже порога полного подтверждения.",
                "warning",
                segment.source_ref.page if segment.source_ref else None,
            )
        return

    if segment.route_type != "unknown":
        add_uncertainty(
            segment.line_id,
            segment.id,
            f"Длина принята в общий итог, но main/branch не подтвержден; тип маршрута изменен с {segment.route_type} на unknown.",
            "warning",
            segment.source_ref.page if segment.source_ref else None,
        )
        segment.route_type = "unknown"
    add_uncertainty(
        segment.line_id,
        segment.id,
        "Размер найден на чертеже, но не включен в итоговые длины: принадлежность main/branch не подтверждена.",
        "warning",
        segment.source_ref.page if segment.source_ref else None,
    )


def _match_segment_classification(
    segment: SegmentResult,
    candidates: list[Candidate],
    classification_by_candidate_id: dict[str, CandidateClassification],
) -> CandidateClassification | None:
    if segment.id.startswith("G-"):
        return None
    if not segment.source_ref:
        return None

    label = segment.source_ref.label or ""
    source_size = str(segment.source_size)
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    label_candidate_ids = CANDIDATE_ID_RE.findall(label)
    label_matches = [
        classification_by_candidate_id[candidate_id]
        for candidate_id in label_candidate_ids
        if candidate_id in classification_by_candidate_id
    ]
    if label_matches:
        def _match_priority(classification: CandidateClassification) -> tuple[int, int]:
            candidate = candidate_by_id.get(classification.candidate_id)
            kind = candidate.kind if candidate else ""
            size_ok = candidate is not None and str(candidate.text) == source_size
            if kind == "dimension" and classification.classification == "route_segment":
                return (0, 0 if size_ok else 1)
            if kind == "dimension":
                return (1, 0 if size_ok else 1)
            if kind == "coordinate_delta":
                return (2, 0 if size_ok else 1)
            return (3, 0 if size_ok else 1)

        return min(label_matches, key=_match_priority)

    if segment.source_ref.bbox:
        for candidate in candidates:
            if _bbox_close(segment.source_ref.bbox, candidate.bbox):
                classification = classification_by_candidate_id.get(candidate.id)
                if classification:
                    return classification

    matching_candidates = [
        candidate for candidate in candidates
        if candidate.text == source_size and candidate.zone == "drawing"
    ]
    if len(matching_candidates) == 1:
        return classification_by_candidate_id.get(matching_candidates[0].id)
    return None


def _parse_positive_number(value: str) -> float | None:
    normalized = str(value).replace(",", ".").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return None
    parsed = float(normalized)
    return parsed if parsed > 0 else None


def _bbox_close(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    tolerance: float = 3.0,
) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def _looks_like_material_callout(element: ElementResult) -> bool:
    if element.x is not None or element.y is not None or element.z is not None:
        return False
    label = element.source_ref.label if element.source_ref else ""
    return bool(ITEM_CALLOUT_RE.search(label))


def _normalize_lengths(line: LineResult, segments: list[SegmentResult], add_uncertainty) -> None:
    known_segments = [segment for segment in segments if segment.length_mm is not None]
    if not known_segments:
        if segments:
            if any(value not in (None, 0) for value in (line.main_length_mm, line.branches_length_mm, line.total_length_mm)):
                add_uncertainty(
                    line.id,
                    "lengths",
                    "Все участки линии отклонены или не подтверждены; расчетные длины очищены до 0.",
                    "critical",
                    line.pages[0] if line.pages else None,
                )
            line.main_length_mm = 0
            line.branches_length_mm = 0
            line.total_length_mm = 0
        return

    main_length = sum(segment.length_mm for segment in known_segments if segment.route_type == "main")
    branch_length = sum(segment.length_mm for segment in known_segments if segment.route_type == "branch")
    unknown_route_length = sum(segment.length_mm for segment in known_segments if segment.route_type == "unknown")
    total_length = main_length + branch_length

    if line.main_length_mm is not None and line.main_length_mm != main_length:
        add_uncertainty(
            line.id,
            "main_length_mm",
            f"Заявленная длина основного маршрута {line.main_length_mm} мм не совпала с суммой принятых участков {main_length} мм; применена сумма участков.",
            "warning",
            line.pages[0] if line.pages else None,
        )
    if line.branches_length_mm is not None and line.branches_length_mm != branch_length:
        add_uncertainty(
            line.id,
            "branches_length_mm",
            f"Заявленная длина ветвей {line.branches_length_mm} мм не совпала с суммой принятых ветвей {branch_length} мм; применена сумма участков.",
            "warning",
            line.pages[0] if line.pages else None,
        )
    if line.total_length_mm is not None and line.total_length_mm != total_length:
        add_uncertainty(
            line.id,
            "total_length_mm",
            f"Заявленная общая длина {line.total_length_mm} мм не совпала с суммой принятых участков {total_length} мм; применена сумма участков.",
            "warning",
            line.pages[0] if line.pages else None,
        )
    if unknown_route_length:
        add_uncertainty(
            line.id,
            "unknown_route_length_mm",
            f"Найдено {unknown_route_length} мм размеров с неподтвержденным main/branch; они оставлены в участках, но не включены в итоговые длины.",
            "warning",
            line.pages[0] if line.pages else None,
        )

    line.main_length_mm = main_length
    line.branches_length_mm = branch_length
    line.total_length_mm = total_length


def _normalize_element_counts(line: LineResult, elements: list[ElementResult]) -> None:
    line.supports_count = sum(1 for item in elements if item.element_type == "support")
    line.valves_count = sum(1 for item in elements if item.element_type == "valve")


def _normalize_line_status(line: LineResult, uncertainties: list[Uncertainty]) -> None:
    severities = {item.severity for item in uncertainties}
    if "critical" in severities:
        line.status = "needs_review" if line.status != "failed" else line.status
    elif "warning" in severities and line.status == "complete":
        line.status = "partial"

    if uncertainties:
        suffix = " Проверено программным валидатором; спорные места вынесены в неопределенности."
        if suffix not in line.completeness_note:
            line.completeness_note = (line.completeness_note + suffix).strip()
