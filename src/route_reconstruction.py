from __future__ import annotations

import heapq
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pdfplumber

from .candidate_extractor import _coordinate_blocks, classify_zone
from .models import (
    AnalysisResult,
    Annotation,
    Candidate,
    CandidateClassification,
    LineGroup,
    LineResult,
    PointResult,
    SegmentResult,
    SourceRef,
    Uncertainty,
    VertexMark,
)
from .validation import _apply_candidate_gate, _parse_positive_number


CANDIDATE_ID_RE = re.compile(r"C-\d{3}-\d{4}")
BRANCH_MARKERS = ("см.", "смо", "ответвл", "ветв", "отвод", "штуц", "сборочн", "детал")
NON_LENGTH_MARKERS = ("нельзя принять", "не относится", "не подтвержд", "не является", "привязк", "позиция")

THIN_LW_MAX = 0.8
THICK_LW_MIN = 0.95
MIN_STROKE_LENGTH = 5.0
MIN_DIMENSION_STROKE_LENGTH = 10.0
DIMENSION_DISTANCE_PX = 9.0
GRAPH_EPS = 4.0
ANCHOR_SEARCH_PX = 80.0
SMALL_BRANCH_RATIO = 0.15
MAX_UNATTACHED_REPORTED = 8

AXIS_ANNOTATION_COLOR = "#f59e0b"
SEGMENT_ANNOTATION_COLOR = "#ca8a04"


def apply_route_reconstruction(
    result: AnalysisResult,
    group: LineGroup,
    candidates: list[Candidate],
    pdf_path=None,
) -> AnalysisResult:
    """Deterministic global reconstruction of line lengths.

    Честная сборка длин (rule layer), не зависящая от картинки и модели:
    - принятые rule gate размерные длины (route_segment) на чертеже -> main;
    - малые неклассифицированные размеры с явным признаком ветви -> branch,
      но НЕ если их штрих параллелен большому размеру (это привязка опоры/детали);
    - координатные перепады только как проверка/подсказка, не как источник длин;
    - векторный граф PDF (advisory) добавляет привязки и overlay-аннотации.
    """
    line = _ensure_line(result, group)
    page = group.pages[0].page_number if group.pages else None

    for classification in result.candidate_classifications:
        _apply_candidate_gate(classification)

    line_candidates = [candidate for candidate in candidates if candidate.line_id == group.line_id]
    classifications_by_id = {
        classification.candidate_id: classification
        for classification in result.candidate_classifications
    }

    graph = AxisGraph()
    if pdf_path is not None:
        try:
            graph = extract_axis_graph(pdf_path, page or 0, line_candidates)
        except Exception:
            graph = AxisGraph()

    report = _build_report()
    _reconstruct_line(result, group, line, line_candidates, classifications_by_id, report, graph)

    if graph.status == "loaded":
        report["graph"] = graph.to_report()
        for candidate_id, stroke_index in graph.attached.items():
            if candidate_id in report["candidate_reasons"]:
                report["candidate_reasons"][candidate_id]["basis"] += f"; размерная линия S{stroke_index}"
        for item in graph.unattached:
            report["suspected_missing_length"].append(
                {
                    "reason": (
                        f"тонкая размерная линия без подписанного размера "
                        f"(штрих S{item['stroke']}, длина ~{item['length_px']} px)"
                    ),
                    "value": None,
                    "stroke": item["stroke"],
                }
            )
        _annotate_axis_path(result, line.id, page or 0, graph)

    report["notes"].append(
        "Правило: принятые rule gate размерные длины = main; координатные перепады не включаются в длины автоматически."
    )
    line.route_reconstruction = report
    return result


def _parallel_offset_support(graph: "AxisGraph", candidate_id: str) -> bool:
    """Правило: малый размер, чей штрих параллелен и рядом с большим размером,
    — это привязка опоры/детали, а не длина участка."""
    if graph is None or graph.status != "loaded":
        return False
    stroke_index = graph.attached.get(candidate_id)
    if stroke_index is None:
        return False
    small = graph.strokes[stroke_index]
    if small.kind != "dimension" or small.length > 30.0:
        return False
    for stroke in graph.strokes:
        if stroke.kind != "dimension" or stroke.index == small.index or stroke.length <= 40.0:
            continue
        if _strokes_parallel(small, stroke) and _stroke_offset(small, stroke) <= 30.0:
            return True
    return False


def _strokes_parallel(a: _Stroke, b: _Stroke, tolerance: float = 0.10) -> bool:
    ax, ay = a.x1 - a.x0, a.y1 - a.y0
    bx, by = b.x1 - b.x0, b.y1 - b.y0
    la = math.hypot(ax, ay)
    lb = math.hypot(bx, by)
    if la <= 0 or lb <= 0:
        return False
    cross = abs(ax * by - ay * bx) / (la * lb)
    same = cross <= tolerance
    opposite = cross >= 1.0 - 0.0
    return same or opposite


def _stroke_offset(a: _Stroke, b: _Stroke) -> float:
    mx = (a.x0 + a.x1) / 2
    my = (a.y0 + a.y1) / 2
    return _point_segment_distance(mx, my, b.x0, b.y0, b.x1, b.y1)


def _build_report() -> dict[str, Any]:
    return {
        "method": "rule_layer + vector_graph",
        "line_id": "",
        "page": None,
        "main_sum_mm": 0.0,
        "branch_sum_mm": 0.0,
        "total_sum_mm": 0.0,
        "candidate_main_sum_mm": 0.0,
        "candidate_branch_sum_mm": 0.0,
        "skipped_coordinate_deltas": [],
        "ignored_duplicates": [],
        "rejected_candidates": [],
        "unassigned_drawing_dims": [],
        "suspected_missing_length": [],
        "candidate_reasons": {},
        "notes": [],
        "graph": {"status": "no_vector_data"},
    }


def _reconstruct_line(
    result: AnalysisResult,
    group: LineGroup,
    line: LineResult,
    candidates: list[Candidate],
    classifications_by_id: dict[str, CandidateClassification],
    report: dict[str, Any],
    graph: "AxisGraph | None" = None,
) -> None:
    line_id = group.line_id
    page = group.pages[0].page_number if group.pages else None
    report["line_id"] = line_id
    report["page"] = page
    candidate_by_id = {candidate.id: candidate for candidate in candidates}

    drawing_dims = [
        candidate for candidate in candidates
        if candidate.kind == "dimension" and candidate.zone == "drawing"
    ]
    deltas = [
        candidate for candidate in candidates
        if candidate.kind == "coordinate_delta" and candidate.zone == "drawing"
    ]

    def value_of(candidate: Candidate) -> float:
        parsed = _parse_positive_number(candidate.text)
        return parsed if parsed is not None else 0.0

    main_parts: list[tuple[str, float]] = []
    branch_parts: list[tuple[str, float]] = []

    for candidate in sorted(drawing_dims, key=lambda item: candidate_by_id[item.id].id):
        classification = classifications_by_id.get(candidate.id)
        parsed = _parse_positive_number(candidate.text)
        value = parsed if parsed is not None else 0.0
        if classification and classification.classification == "route_segment" and classification.accepted:
            if classification.route_type == "branch":
                if _parallel_offset_support(graph, candidate.id):
                    classification.classification = "support_offset"
                    classification.accepted = False
                    classification.gate_reason = "support offset: малый размер параллелен основному размеру"
                    report["rejected_candidates"].append(
                        {
                            "candidate_id": candidate.id,
                            "value": value,
                            "reason": "малый размер параллелен большому размерному штриху — привязка опоры/детали, не длина участка",
                        }
                    )
                    report["candidate_reasons"][candidate.id] = {
                        "value": value,
                        "assigned": None,
                        "basis": "параллельный малый размер — привязка опоры/детали, не длина",
                    }
                    continue
                branch_parts.append((candidate.id, value))
                report["candidate_reasons"][candidate.id] = {
                    "value": value,
                    "assigned": "branch",
                    "basis": "accepted route_segment dimension, явный тип branch от модели",
                }
                continue
            if classification.route_type == "unknown":
                classification.route_type = "main"
                basis = "accepted route_segment dimension; неизвестный route_type по умолчанию main (физический размер на оси)"
            else:
                basis = "accepted route_segment dimension"
            main_parts.append((candidate.id, value))
            report["candidate_reasons"][candidate.id] = {
                "value": value,
                "assigned": "main",
                "basis": basis,
            }
            continue

        if classification and classification.classification == "route_segment":
            report["rejected_candidates"].append(
                {
                    "candidate_id": candidate.id,
                    "value": value,
                    "reason": classification.gate_reason or "rejected by rule gate",
                }
            )
            report["candidate_reasons"][candidate.id] = {
                "value": value,
                "assigned": None,
                "basis": "route_segment, но отклонен rule gate; не включен в длины",
            }
            continue

        unassigned = {
            "candidate_id": candidate.id,
            "value": value,
            "kind": candidate.kind,
            "zone": candidate.zone,
        }
        if classification:
            unassigned["classification"] = classification.classification
            unassigned["reason"] = classification.reason
        report["unassigned_drawing_dims"].append(unassigned)
        report["candidate_reasons"][candidate.id] = {
            "value": value,
            "assigned": None,
            "basis": "не назначен: нет подтвержденной классификации route_segment",
        }

    max_main = max((value for _candidate_id, value in main_parts), default=0.0)
    small_limit = max(SMALL_BRANCH_RATIO * max_main, 5.0)
    evidence = _evidence_by_candidate(line_id, candidates, classifications_by_id, result)
    for unassigned in report["unassigned_drawing_dims"]:
        candidate = candidate_by_id.get(unassigned["candidate_id"])
        if not candidate:
            continue
        value = unassigned["value"]
        reason_text = evidence.get(candidate.id, "")
        if value <= 0 or value > small_limit or not _branch_evidence(reason_text):
            if _parallel_offset_support(graph, candidate.id):
                reason_text_note = (
                    "малый размер параллелен большому размерному штриху — привязка опоры/детали, не длина"
                )
                report["candidate_reasons"][candidate.id]["basis"] = reason_text_note
            continue
        classification = classifications_by_id.get(candidate.id)
        if classification is None:
            classification = CandidateClassification(
                id=f"RC-{candidate.id}",
                line_id=line_id,
                page=candidate.page,
                candidate_id=candidate.id,
                text=candidate.text,
                classification="route_segment",
                route_type="branch",
                reason="Принят как ветвь геометрическим правилом (малый изолированный размер).",
                confidence=0.7,
                accepted=True,
                gate_reason="reconstructed: branch by geometry rule",
            )
            result.candidate_classifications.append(classification)
            classifications_by_id[candidate.id] = classification
        else:
            classification.classification = "route_segment"
            classification.route_type = "branch"
            classification.confidence = max(classification.confidence or 0.5, 0.7)
            classification.reason = (
                f"{classification.reason or ''} Принят как ветвь геометрическим правилом (малый изолированный размер)."
            ).strip()
            classification.accepted = True
        classification_text = candidate.text
        new_reason = report["candidate_reasons"][candidate.id]
        new_reason["assigned"] = "branch"
        new_reason["basis"] = (
            "малый drawing dimension, переклассифицирован в ветвь геометрическим правилом "
            f"({classification_text} мм); признаки в тексте модели: \"{_shorten(reason_text)}\""
        )
        branch_parts.append((candidate.id, value))
        _add_uncertainty(
            result,
            line_id,
            candidate.id,
            f"Кандидат {candidate.id} ({candidate.text} мм) переклассифицирован как ветвь геометрическим правилом.",
            "info",
            candidate.page,
        )

    unassigned_reasons = {item["candidate_id"] for item in report["unassigned_drawing_dims"]}
    report["unassigned_drawing_dims"] = [
        item for item in report["unassigned_drawing_dims"]
        if item["candidate_id"] not in {candidate_id for candidate_id, _value in branch_parts}
    ]
    del unassigned_reasons

    real_dim_values: dict[str, str] = {}
    for candidate in drawing_dims:
        parsed = _parse_positive_number(candidate.text)
        if parsed is not None and candidate.text not in real_dim_values:
            real_dim_values[candidate.text] = candidate.id

    for candidate in deltas:
        parsed = _parse_positive_number(candidate.text)
        value = parsed if parsed is not None else 0.0
        if candidate.text in real_dim_values:
            report["ignored_duplicates"].append(
                {
                    "candidate_id": candidate.id,
                    "value": value,
                    "of_candidate_id": real_dim_values[candidate.text],
                    "reason": "координатный перепад уже представлен размерной длиной на чертеже",
                }
            )
            continue
        report["skipped_coordinate_deltas"].append(
            {
                "candidate_id": candidate.id,
                "value": value,
                "reason": "координатный перепад не имеет совпадающей размерной длины; не включается как самостоятельный участок",
            }
        )
        report["candidate_reasons"][candidate.id] = {
            "value": value,
            "assigned": None,
            "basis": "coordinate_delta без парной размерной длины; пропущен",
        }
        report["suspected_missing_length"].append(
            {
                "reason": f"координатный перепад {candidate.id} = {candidate.text} мм без подписанной размерной длины",
                "value": value,
            }
        )

    main_sum = round(sum(value for _candidate_id, value in main_parts), 3)
    branch_sum = round(sum(value for _candidate_id, value in branch_parts), 3)
    report["main_sum_mm"] = main_sum
    report["branch_sum_mm"] = branch_sum
    report["total_sum_mm"] = round(main_sum + branch_sum, 3)
    report["candidate_main_sum_mm"] = main_sum
    report["candidate_branch_sum_mm"] = branch_sum

    _reconcile_segments(
        result,
        group,
        candidates,
        classifications_by_id,
        main_parts,
        branch_parts,
        report,
    )

    line.main_length_mm = main_sum
    line.branches_length_mm = branch_sum
    line.total_length_mm = main_sum + branch_sum


def _reconcile_segments(
    result: AnalysisResult,
    group: LineGroup,
    candidates: list[Candidate],
    classifications_by_id: dict[str, CandidateClassification],
    main_parts: list[tuple[str, float]],
    branch_parts: list[tuple[str, float]],
    report: dict[str, Any],
) -> None:
    line_id = group.line_id
    page = group.pages[0].page_number if group.pages else None
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    assigned: dict[str, str] = {
        candidate_id: "main" for candidate_id, _value in main_parts
    }
    assigned.update(
        {candidate_id: "branch" for candidate_id, _value in branch_parts}
    )
    segments = [segment for segment in result.segments if segment.line_id == line_id]
    referenced: dict[str, list[SegmentResult]] = defaultdict(list)
    for segment in segments:
        label = segment.source_ref.label if segment.source_ref else ""
        for candidate_id in CANDIDATE_ID_RE.findall(label):
            if candidate_id in candidate_by_id:
                referenced[candidate_id].append(segment)
    used_segment_ids = {segment.id for segment in segments}
    used_point_ids = {point.id for point in result.points}

    def create_segment(candidate: Candidate, route_type: str, value: float, index: int) -> None:
        segment_id = f"R-{line_id}-{route_type.upper()}{index:02d}"
        while segment_id in used_segment_ids:
            index += 1
            segment_id = f"R-{line_id}-{route_type.upper()}{index:02d}"
        start_point_id = f"{segment_id}-P01"
        end_point_id = f"{segment_id}-P02"
        for point_id, role in ((start_point_id, "route start"), (end_point_id, "route end")):
            if point_id not in used_point_ids:
                result.points.append(
                    PointResult(
                        id=point_id,
                        line_id=line_id,
                        role=role,
                        x=None,
                        y=None,
                        z=None,
                        source="calculated",
                        source_ref=SourceRef(
                            page=candidate.page,
                            label=f"route_reconstruction {candidate.id}",
                        ),
                    )
                )
                used_point_ids.add(point_id)
        result.segments.append(
            SegmentResult(
                id=segment_id,
                line_id=line_id,
                start_point_id=start_point_id,
                end_point_id=end_point_id,
                dn="unknown",
                length_mm=value,
                route_type=route_type,  # type: ignore[arg-type]
                source_size=candidate.text,
                source_ref=SourceRef(
                    page=candidate.page,
                    label=f"{candidate.id} route_reconstruction {candidate.text}",
                    bbox=candidate.bbox,
                ),
            )
        )
        used_segment_ids.add(segment_id)
        result.annotations.append(
            Annotation(
                id=f"A-{segment_id}",
                page=candidate.page,
                label=f"{segment_id} {candidate.text}",
                kind="route_reconstruction_segment",
                bbox=candidate.bbox,
                color=SEGMENT_ANNOTATION_COLOR,
            )
        )

    for candidate_id, route_type in assigned.items():
        for segment in referenced.get(candidate_id, []):
            segment.route_type = route_type  # type: ignore[arg-type]
            if segment.length_mm is None:
                segment.length_mm = float(report["candidate_reasons"][candidate_id]["value"])

    route_order = {"main": 0, "branch": 1}
    counted_dims = set(assigned)
    for candidate_id, route_type in assigned.items():
        if referenced.get(candidate_id):
            continue
        candidate = candidate_by_id[candidate_id]
        create_segment(
            candidate,
            route_type,
            float(report["candidate_reasons"][candidate_id]["value"]),
            route_order[route_type],
        )

    for segment in segments:
        lead_id = _lead_candidate(segment, candidate_by_id)
        if lead_id is None or lead_id in counted_dims:
            continue
        candidate = candidate_by_id[lead_id]
        if candidate.kind == "coordinate_delta":
            segment.route_type = "unknown"


def _lead_candidate(segment: SegmentResult, candidate_by_id: dict[str, Candidate]) -> str | None:
    labels = CANDIDATE_ID_RE.findall(segment.source_ref.label if segment.source_ref else "")
    if not labels:
        return None
    best: str | None = None
    best_priority = 99
    for candidate_id in labels:
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            continue
        if candidate.kind == "dimension":
            priority = 0
        elif candidate.kind == "coordinate_delta":
            priority = 1
        else:
            priority = 2
        if priority < best_priority:
            best_priority = priority
            best = candidate_id
    return best


def _evidence_by_candidate(
    line_id: str,
    candidates: list[Candidate],
    classifications_by_id: dict[str, CandidateClassification],
    result: AnalysisResult,
) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for candidate in candidates:
        texts = []
        classification = classifications_by_id.get(candidate.id)
        if classification:
            texts.append(classification.reason or "")
        for uncertainty in result.uncertainties:
            if uncertainty.line_id != line_id:
                continue
            target = f"{uncertainty.target} {uncertainty.reason}"
            if candidate.id in uncertainty.target or candidate.text in uncertainty.target:
                texts.append(f"{uncertainty.target}: {uncertainty.reason}")
        joined = " ".join(text for text in texts if text).strip()
        evidence[candidate.id] = joined
    return evidence


def _branch_evidence(text: str) -> bool:
    lowered = text.lower()
    has_branch = any(marker in lowered for marker in BRANCH_MARKERS)
    has_negative = any(marker in lowered for marker in NON_LENGTH_MARKERS)
    if has_negative and not has_branch:
        return False
    return has_branch


def _shorten(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ensure_line(result: AnalysisResult, group: LineGroup) -> LineResult:
    for line in result.lines:
        if line.id == group.line_id:
            return line
    line = LineResult(
        id=group.line_id,
        pages=[page.page_number for page in group.pages],
        status="partial",
        completeness_note="Линия обработана универсальным правилом реконструкции маршрута.",
    )
    result.lines.append(line)
    return line


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
            id=f"R-U-{len(result.uncertainties) + 1:03d}",
            line_id=line_id,
            page=page,
            target=target,
            reason=reason,
            severity=severity,  # type: ignore[arg-type]
        )
    )


def _annotate_axis_path(result: AnalysisResult, line_id: str, page: int, graph: "AxisGraph") -> None:
    indices = graph.primary_path or []
    for stroke_index in indices:
        stroke = graph.strokes[stroke_index]
        result.annotations.append(
            Annotation(
                id=f"AX-{line_id}-S{stroke_index}",
                page=page,
                label=f"axis S{stroke_index} {stroke.kind}",
                kind="route_axis",
                bbox=(stroke.x0, stroke.y0, stroke.x1, stroke.y1),
                color=AXIS_ANNOTATION_COLOR,
            )
        )


@dataclass(slots=True)
class _Stroke:
    index: int
    x0: float
    y0: float
    x1: float
    y1: float
    length: float
    linewidth: float
    kind: str


@dataclass(slots=True)
class AxisGraph:
    status: str = "no_vector_data"
    strokes: list[_Stroke] = field(default_factory=list)
    attached: dict[str, int] = field(default_factory=dict)
    unattached: list[dict[str, Any]] = field(default_factory=list)
    primary_path: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_report(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "strokes_total": len(self.strokes),
            "dimension_strokes": sum(1 for stroke in self.strokes if stroke.kind == "dimension"),
            "axis_strokes": sum(1 for stroke in self.strokes if stroke.kind == "axis"),
            "attached_candidates": len(self.attached),
            "unattached_dimension_strokes": len(self.unattached),
            "primary_path_strokes": len(self.primary_path),
            "notes": self.notes,
        }


def extract_axis_graph(pdf_path, page_number: int, candidates: list[Candidate]) -> AxisGraph:
    graph = AxisGraph()
    graph.status = "loaded"
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not (1 <= page_number <= len(pdf.pages)):
                graph.status = "no_page"
                return graph
            page = pdf.pages[page_number - 1]
            width = float(page.width)
            height = float(page.height)
            objects = list(page.objects.get("line", [])) + list(page.objects.get("curve", []))
    except Exception as error:
        graph.status = "error"
        graph.notes.append(f"vector extraction failed: {error}")
        return graph

    for index, obj in enumerate(objects):
        pts = obj.get("pts")
        if not pts or len(pts) < 2:
            continue
        x0, y0 = float(pts[0][0]), float(pts[0][1])
        x1, y1 = float(pts[-1][0]), float(pts[-1][1])
        bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if classify_zone(bbox, width, height) != "drawing":
            continue
        length = math.hypot(x1 - x0, y1 - y0)
        if length < MIN_STROKE_LENGTH:
            continue
        if _is_frame_line(bbox, length, width, height):
            continue
        linewidth = float(obj.get("linewidth") or obj.get("stroke_width") or 0.0)
        if linewidth <= THIN_LW_MAX and length >= MIN_DIMENSION_STROKE_LENGTH:
            kind = "dimension"
        elif linewidth >= THICK_LW_MIN:
            kind = "axis"
        else:
            continue
        graph.strokes.append(
            _Stroke(
                index=len(graph.strokes),
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                length=length,
                linewidth=linewidth,
                kind=kind,
            )
        )

    if not graph.strokes:
        graph.status = "no_strokes"
        return graph

    _attach_dimensions(graph, candidates)
    line_id = candidates[0].line_id if candidates else ""
    _find_primary_path(graph, line_id, page_number, candidates)

    attached_stroke_ids = set(graph.attached.values())
    unattached = [
        stroke for stroke in graph.strokes
        if stroke.kind == "dimension" and stroke.index not in attached_stroke_ids
    ]
    unattached.sort(key=lambda stroke: stroke.length, reverse=True)
    for stroke in unattached[:MAX_UNATTACHED_REPORTED]:
        graph.unattached.append(
            {
                "stroke": stroke.index,
                "length_px": round(stroke.length, 1),
                "x0": round(stroke.x0, 1),
                "y0": round(stroke.y0, 1),
                "x1": round(stroke.x1, 1),
                "y1": round(stroke.y1, 1),
            }
        )
    if graph.unattached:
        graph.notes.append(f"найдены тонкие размерные линии без подписанных размеров: {len(graph.unattached)}")

    if not graph.primary_path:
        graph.status = "no_primary_path"
    return graph


def extract_local_vertices(
    pdf_path: str,
    page_number: int,
    line_id: str,
    candidates: list[Candidate] | None = None,
) -> list[VertexMark]:
    """Find endpoints, corners and joins of thick vector pipe strokes."""
    graph = extract_axis_graph(pdf_path, page_number, candidates or [])
    axis_strokes = [stroke for stroke in graph.strokes if stroke.kind == "axis"]
    if not axis_strokes:
        return []

    nodes, adjacency = _build_node_graph(axis_strokes)
    allowed_nodes = _pipe_component_nodes(adjacency, nodes)
    vertices: list[VertexMark] = []
    for node_index, connected in adjacency.items():
        if node_index not in allowed_nodes:
            continue
        if not connected:
            continue
        degree = len(connected)
        is_corner = False
        if degree == 2:
            first = axis_strokes[connected[0][1]]
            second = axis_strokes[connected[1][1]]
            first_dx, first_dy = _outgoing_vector(first, nodes[node_index])
            second_dx, second_dy = _outgoing_vector(second, nodes[node_index])
            first_length = math.hypot(first_dx, first_dy)
            second_length = math.hypot(second_dx, second_dy)
            if first_length > 0 and second_length > 0:
                cosine = (first_dx * second_dx + first_dy * second_dy) / (first_length * second_length)
                is_corner = cosine > -0.94
        if degree == 1:
            role = "конец"
        elif degree >= 3:
            role = "стык"
        elif is_corner:
            role = "угол"
        else:
            continue
        x, y = nodes[node_index]
        vertices.append(
            VertexMark(
                id=f"V{len(vertices) + 1:02d}",
                line_id=line_id,
                page=page_number,
                label=f"V{len(vertices) + 1:02d}",
                role=role,
                x=round(x, 2),
                y=round(y, 2),
                confidence=1.0,
                reason=f"толстые осевые линии: degree={degree}",
            )
        )
    return _merge_local_vertex_marks(vertices)


def _merge_local_vertex_marks(vertices: list[VertexMark], radius: float = 20.0) -> list[VertexMark]:
    if not vertices:
        return []
    merged: list[VertexMark] = []
    for role in ("конец", "угол", "стык"):
        role_vertices = [vertex for vertex in vertices if vertex.role == role]
        clusters: list[list[VertexMark]] = []
        for vertex in role_vertices:
            matching = [
                cluster_index
                for cluster_index, cluster in enumerate(clusters)
                if any(math.hypot(vertex.x - item.x, vertex.y - item.y) <= radius for item in cluster)
            ]
            if not matching:
                clusters.append([vertex])
                continue
            target = clusters[matching[0]]
            target.append(vertex)
            for cluster_index in reversed(matching[1:]):
                target.extend(clusters.pop(cluster_index))

        for cluster in clusters:
            # Two nearby endpoints with no corner between them are a broken
            # continuation of the same contour, not a physical vertex.
            if role == "конец" and len(cluster) > 1:
                continue
            index = len(merged) + 1
            x = sum(item.x for item in cluster) / len(cluster)
            y = sum(item.y for item in cluster) / len(cluster)
            reason = "объединены близкие элементы одного контура" if len(cluster) > 1 else cluster[0].reason
            merged.append(
                VertexMark(
                    id=f"V{index:02d}",
                    line_id=cluster[0].line_id,
                    page=cluster[0].page,
                    label=f"V{index:02d}",
                    role=role,
                    x=round(x, 2),
                    y=round(y, 2),
                    confidence=min(item.confidence for item in cluster),
                    reason=reason,
                )
            )
    if len(merged) <= 1:
        return merged
    endpoint_indices = [index for index, vertex in enumerate(merged) if vertex.role == "конец"]
    start_pool = endpoint_indices or list(range(len(merged)))
    start_index = min(
        start_pool,
        key=lambda index: (merged[index].y, -merged[index].x),
    )
    ordered = [merged.pop(start_index)]
    while merged:
        previous = ordered[-1]
        next_index = min(
            range(len(merged)),
            key=lambda index: math.hypot(merged[index].x - previous.x, merged[index].y - previous.y),
        )
        ordered.append(merged.pop(next_index))
    renumbered: list[VertexMark] = []
    for index, vertex in enumerate(ordered, start=1):
        renumbered.append(
            VertexMark(
                id=f"V{index:02d}",
                line_id=vertex.line_id,
                page=vertex.page,
                label=f"V{index:02d}",
                role=vertex.role,
                x=vertex.x,
                y=vertex.y,
                confidence=vertex.confidence,
                reason=vertex.reason,
            )
        )
    return renumbered


def _pipe_component_nodes(adjacency: dict[int, list[tuple[int, int]]], nodes: list[list[float]]) -> set[int]:
    components: list[set[int]] = []
    seen: set[int] = set()
    for start in adjacency:
        if start in seen:
            continue
        component: set[int] = set()
        queue = [start]
        seen.add(start)
        while queue:
            node_index = queue.pop()
            component.add(node_index)
            for neighbor, _stroke_index in adjacency.get(node_index, []):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    if not components:
        return set()

    main = max(components, key=len)
    main_x = [nodes[index][0] for index in main]
    main_y = [nodes[index][1] for index in main]
    main_bbox = (min(main_x), min(main_y), max(main_x), max(main_y))

    def bbox_gap(component: set[int]) -> float:
        xs = [nodes[index][0] for index in component]
        ys = [nodes[index][1] for index in component]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        dx = max(main_bbox[0] - bbox[2], bbox[0] - main_bbox[2], 0.0)
        dy = max(main_bbox[1] - bbox[3], bbox[1] - main_bbox[3], 0.0)
        return math.hypot(dx, dy)

    allowed: set[int] = set()
    for component in components:
        if component is main or bbox_gap(component) <= 55.0:
            allowed.update(component)
    return allowed




def _outgoing_vector(stroke: _Stroke, node: list[float]) -> tuple[float, float]:
    distance_to_start = math.hypot(stroke.x0 - node[0], stroke.y0 - node[1])
    distance_to_end = math.hypot(stroke.x1 - node[0], stroke.y1 - node[1])
    if distance_to_start <= distance_to_end:
        return stroke.x1 - node[0], stroke.y1 - node[1]
    return stroke.x0 - node[0], stroke.y0 - node[1]


def _is_frame_line(bbox: tuple[float, float, float, float], length: float, width: float, height: float) -> bool:
    if length <= 0.5 * max(width, height):
        return False
    margin_x = width * 0.06
    margin_y = height * 0.05
    near_edges = (
        min(bbox[0], bbox[2]) < margin_x
        or max(bbox[0], bbox[2]) > width - margin_x
        or min(bbox[1], bbox[3]) < margin_y
        or max(bbox[1], bbox[3]) > height - margin_y
    )
    return near_edges


def _attach_dimensions(graph: AxisGraph, candidates: list[Candidate]) -> None:
    dimension_strokes = [stroke for stroke in graph.strokes if stroke.kind == "dimension"]
    if not dimension_strokes:
        return
    for candidate in candidates:
        if candidate.kind != "dimension" or candidate.zone != "drawing":
            continue
        center_x = (candidate.bbox[0] + candidate.bbox[2]) / 2
        center_y = (candidate.bbox[1] + candidate.bbox[3]) / 2
        best_stroke: _Stroke | None = None
        best_distance = DIMENSION_DISTANCE_PX
        for stroke in dimension_strokes:
            distance = _point_segment_distance(center_x, center_y, stroke.x0, stroke.y0, stroke.x1, stroke.y1)
            if distance < best_distance:
                best_distance = distance
                best_stroke = stroke
        if best_stroke is not None:
            graph.attached[candidate.id] = best_stroke.index


def _point_segment_distance(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx = x1 - x0
    dy = y1 - y0
    if dx == 0 and dy == 0:
        return math.hypot(px - x0, py - y0)
    projection = ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)
    projection = max(0.0, min(1.0, projection))
    near_x = x0 + projection * dx
    near_y = y0 + projection * dy
    return math.hypot(px - near_x, py - near_y)


def _find_primary_path(graph: AxisGraph, line_id: str, page_number: int, candidates: list[Candidate]) -> None:
    axis_strokes = [stroke for stroke in graph.strokes if stroke.kind == "axis"]
    if not axis_strokes:
        graph.status = "no_axis_edges"
        graph.notes.append("толстые штрихи (жесткая ось) не найдены; путь не строится")
        return
    nodes, adjacency = _build_node_graph(axis_strokes)
    if not nodes:
        graph.status = "no_nodes"
        return

    start_node, end_node, anchor_notes = _anchor_nodes(nodes, line_id, page_number, candidates)
    graph.notes.extend(anchor_notes)

    path = None
    if start_node is not None and end_node is not None and start_node != end_node:
        path = _dijkstra_stroke_path(axis_strokes, nodes, adjacency, start_node, end_node)
    if path is None and start_node is not None:
        path = _diameter_stroke_path(axis_strokes, nodes, adjacency, start_node)
    if path:
        graph.primary_path = path
        graph.notes.append(f"основной путь построен из {len(path)} штрихов оси")


def _build_node_graph(
    axis_strokes: list[_Stroke],
) -> tuple[list[list[float]], dict[int, list[tuple[int, int]]]]:
    nodes: list[list[float]] = []
    counts: list[int] = []
    eps2 = GRAPH_EPS * GRAPH_EPS

    def nearest(ex: float, ey: float) -> int | None:
        best: int | None = None
        best_distance = eps2
        for index, (cx, cy) in enumerate(nodes):
            distance = (ex - cx) ** 2 + (ey - cy) ** 2
            if distance < best_distance:
                best_distance = distance
                best = index
        return best

    def add(ex: float, ey: float) -> int:
        best = nearest(ex, ey)
        if best is None:
            nodes.append([ex, ey])
            counts.append(1)
            return len(nodes) - 1
        total = counts[best] + 1
        nodes[best][0] = (nodes[best][0] * counts[best] + ex) / total
        nodes[best][1] = (nodes[best][1] * counts[best] + ey) / total
        counts[best] = total
        return best

    adjacency: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for stroke_index, stroke in enumerate(axis_strokes):
        node_a = add(stroke.x0, stroke.y0)
        node_b = add(stroke.x1, stroke.y1)
        if node_a == node_b:
            continue
        adjacency[node_a].append((node_b, stroke_index))
        adjacency[node_b].append((node_a, stroke_index))
    return nodes, adjacency


def _anchor_nodes(nodes, line_id: str, page_number: int, candidates: list[Candidate]):
    blocks = _coordinate_blocks(
        line_id,
        page_number,
        [
            candidate for candidate in candidates
            if candidate.kind in {"coordinate_label", "coordinate_value"} and candidate.zone == "drawing"
        ],
    )
    notes: list[str] = []
    if len(blocks) < 2:
        notes.append("якорей координат меньше двух; путь строится по самой длинной связной цепочке")
        return None, None, notes

    start_block = max(blocks, key=lambda block: (block.values.get("X", float("-inf")), block.values.get("Z", float("-inf"))))
    end_block = min(blocks, key=lambda block: (block.values.get("X", float("inf")), -block.values.get("Z", float("inf"))))

    def project(bbox) -> int | None:
        center_x = (bbox[0] + bbox[2]) / 2
        center_y = (bbox[1] + bbox[3]) / 2
        best: int | None = None
        best_distance = ANCHOR_SEARCH_PX
        for index, (cx, cy) in enumerate(nodes):
            distance = math.hypot(cx - center_x, cy - center_y)
            if distance < best_distance:
                best_distance = distance
                best = index
        return best

    start_node = project(start_block.bbox)
    end_node = project(end_block.bbox)
    notes.append(
        f"якоря координат: старт X={start_block.values.get('X')}, конец X={end_block.values.get('X')} "
        f"({chart_labels(start_block.values)} / {chart_labels(end_block.values)})"
    )
    return start_node, end_node, notes


def chart_labels(values: dict[str, int]) -> str:
    return " ".join(f"{key}{value}" for key, value in sorted(values.items()))


def _dijkstra_stroke_path(axis_strokes, nodes, adjacency, start_node, end_node):
    if start_node == end_node:
        return None
    weight_by_edge = {
        (min(node_a, node_b), max(node_a, node_b)): axis_strokes[stroke_index].length
        for node_a, edges in adjacency.items()
        for node_b, stroke_index in edges
    }
    infinity = float("inf")
    distance = {node: infinity for node in adjacency}
    distance[start_node] = 0.0
    previous: dict[int, int | None] = {start_node: None}
    previous_edge: dict[int, int] = {}
    heap = [(0.0, start_node)]
    visited: set[int] = set()
    while heap:
        current_distance, current = heapq.heappop(heap)
        if current in visited:
            continue
        visited.add(current)
        if current == end_node:
            break
        for neighbor, stroke_index in adjacency.get(current, []):
            key = (min(current, neighbor), max(current, neighbor))
            edge_weight = weight_by_edge.get(key, 1.0)
            candidate = current_distance + edge_weight
            if candidate < distance.get(neighbor, infinity):
                distance[neighbor] = candidate
                previous[neighbor] = current
                previous_edge[neighbor] = stroke_index
                heapq.heappush(heap, (candidate, neighbor))
    if end_node not in previous or previous[end_node] is None:
        return None
    strokes: list[int] = []
    node = end_node
    while node != start_node:
        stroke_index = previous_edge.get(node)
        if stroke_index is None:
            return None
        strokes.append(stroke_index)
        predecessor = previous[node]
        if predecessor is None:
            return None
        node = predecessor
    strokes.reverse()
    return strokes


def _diameter_stroke_path(axis_strokes, nodes, adjacency, seed_node):
    def bfs(start):
        discovered = {start}
        queue = [(start, [])]
        forward: dict[int, int | None] = {start: None}
        forward_edge: dict[int, int] = {}
        while queue:
            node, _parent = queue.pop(0)
            for neighbor, stroke_index in adjacency.get(node, []):
                if neighbor not in discovered:
                    discovered.add(neighbor)
                    forward[neighbor] = node
                    forward_edge[neighbor] = stroke_index
                    queue.append((neighbor, node))
        return discovered, forward, forward_edge

    component, parents, parent_edge = bfs(seed_node)
    if not component:
        return None
    first = max(component, key=lambda node: _hop_distance(node, parents)) if parents else seed_node
    _, parents2, parent_edge2 = bfs(first)
    second = max(component, key=lambda node: _hop_distance(node, parents2))
    strokes: list[int] = []
    node = second
    while node != first:
        stroke_index = parent_edge2.get(node)
        if stroke_index is None:
            break
        strokes.append(stroke_index)
        predecessor = parents2[node]
        if predecessor is None:
            break
        node = predecessor
    strokes.reverse()
    return strokes or None


def _hop_distance(node, parents) -> int:
    steps = 0
    current = node
    seen: set[int] = set()
    while parents.get(current) is not None and current not in seen:
        seen.add(current)
        current = parents[current]
        steps += 1
    return steps