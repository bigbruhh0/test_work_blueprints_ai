from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from .models import (
    AnalysisResult,
    Annotation,
    Candidate,
    CandidateClassification,
    DimensionBinding,
    GraphData,
    LineGroup,
    LineResult,
    PointResult,
    SegmentResult,
    SourceRef,
    UnresolvedEdge,
)
from .route_reconstruction import (
    SMALL_BRANCH_RATIO,
    _parallel_offset_support,
    extract_axis_graph,
)
from .route_reconstruction import _point_segment_distance
from .validation import _apply_candidate_gate, _parse_positive_number


CANDIDATE_ID_RE = re.compile(r"C-\d{3}-\d{4}")
EDGE_PATH_PROXIMITY_PX = 30.0
FREE_DIM_SEARCH_PX = 45.0
MAX_LENGTH_MM = 100000.0
EDGE_ANNOTATION_COLOR = "#0d9488"


def apply_graph_reconstruction(
    result: AnalysisResult,
    group: LineGroup,
    candidates: list[Candidate],
    graph_data: GraphData,
    pdf_path=None,
) -> bool:
    """Собирает длины по графу провайдера с детерминированными проверками.

    Возвращает True, если граф обработан; False — граф пуст/невалиден и нужен fallback.
    """
    if graph_data is None or (not graph_data.nodes and not graph_data.edges):
        return False

    for classification in result.candidate_classifications:
        _apply_candidate_gate(classification)

    line_id = group.line_id
    page = group.pages[0].page_number if group.pages else None
    line_candidates = [candidate for candidate in candidates if candidate.line_id == line_id]
    candidate_by_id = {candidate.id: candidate for candidate in line_candidates}
    classification_by_id = {
        classification.candidate_id: classification
        for classification in result.candidate_classifications
    }

    edges = [edge for edge in graph_data.edges if edge.line_id == line_id]
    nodes = list(graph_data.nodes)
    node_by_id = {node.id: node for node in nodes}
    edge_ids = {edge.id for edge in edges}
    if not edges and not nodes:
        return False

    vector_graph = None
    if pdf_path is not None and page:
        try:
            vector_graph = extract_axis_graph(pdf_path, page, line_candidates)
        except Exception:
            vector_graph = None

    report: dict[str, Any] = {
        "method": "graph_solver + vector_graph",
        "line_id": line_id,
        "page": page,
        "main_sum_mm": 0.0,
        "branch_sum_mm": 0.0,
        "total_sum_mm": 0.0,
        "skipped_coordinate_deltas": [],
        "ignored_duplicates": [],
        "rejected_candidates": [],
        "unassigned_drawing_dims": [],
        "suspected_missing_length": [],
        "candidate_reasons": {},
        "notes": [],
        "graph": vector_graph.to_report() if vector_graph is not None else {"status": "no_vector_data"},
    }

    bindings = [binding for binding in graph_data.dimension_bindings if binding.edge_id in edge_ids]
    bindings, duplicate_actions = _dedup_bindings(bindings, candidate_by_id, classification_by_id)
    report["ignored_duplicates"].extend(duplicate_actions)
    bindings_by_edge: dict[str, list[DimensionBinding]] = defaultdict(list)
    for binding in bindings:
        bindings_by_edge[binding.edge_id].append(binding)

    resolved: list[tuple[Any, dict[str, Any]]] = []
    unresolved: list[dict[str, Any]] = []
    proposal_by_edge = {item.edge_id: item for item in graph_data.unresolved_edges}
    used_global: set[str] = set()

    for edge in sorted(edges, key=lambda item: item.id):
        decision = _solve_edge(
            edge,
            bindings_by_edge.get(edge.id, []),
            node_by_id=node_by_id,
            candidate_by_id=candidate_by_id,
            classification_by_id=classification_by_id,
            vector_graph=vector_graph,
            report=report,
            proposal_by_edge=proposal_by_edge,
            used_global=used_global,
        )
        if decision["status"] == "resolved":
            resolved.append((edge, decision))
            for candidate_id in decision.get("used_candidates", []):
                used_global.add(candidate_id)
        else:
            unresolved.append({"edge": edge, **decision})
            report["suspected_missing_length"].append(
                {
                    "reason": f"ребро {edge.id} из графа: {decision.get('reason', 'не решено')}",
                    "edge_id": edge.id,
                    "suggested_action": decision.get("suggested_action", "требует разбора"),
                    "value": None,
                }
            )

    _rebuild_line(
        result=result,
        group=group,
        candidates=line_candidates,
        nodes=nodes,
        resolved=resolved,
        unresolved=unresolved,
        report=report,
        candidate_by_id=candidate_by_id,
        classification_by_id=classification_by_id,
        vector_graph=vector_graph,
    )

    result.graph_nodes.extend(nodes)
    result.graph_edges.extend(edges)
    result.dimension_bindings.extend(bindings)
    for item in unresolved:
        result.unresolved_edges.append(
            UnresolvedEdge(
                edge_id=item["edge"].id,
                reason=item.get("reason", ""),
                suggested_action=item.get("suggested_action", ""),
            )
        )
    for item in graph_data.ignored_candidates:
        if isinstance(item, dict):
            result.ignored_candidates.append(item)
        else:
            result.ignored_candidates.append(
                {"candidate_id": getattr(item, "candidate_id", ""), "reason": getattr(item, "reason", "")}
            )
    return True


def _solve_edge(
    edge,
    edge_bindings: list[DimensionBinding],
    node_by_id: dict[str, Any],
    candidate_by_id: dict[str, Candidate],
    classification_by_id: dict[str, CandidateClassification],
    vector_graph,
    report: dict[str, Any],
    proposal_by_edge: dict[str, Any],
    used_global: set[str] | None = None,
) -> dict[str, Any]:
    start_node = node_by_id.get(edge.from_node_id)
    end_node = node_by_id.get(edge.to_node_id)
    if start_node is None or end_node is None:
        return {
            "status": "unresolved",
            "reason": "ребро ссылается на неизвестный узел графа",
            "suggested_action": "проверить топологию графа",
        }

    length: float | None = None
    length_basis = ""
    note = ""
    used_candidates: list[str] = list(used_global) if used_global else []
    label_candidates: list[str] = []

    accepted_bindings = []
    for binding in edge_bindings:
        candidate = candidate_by_id.get(binding.candidate_id)
        classification = classification_by_id.get(binding.candidate_id)
        if candidate is None or classification is None:
            report["rejected_candidates"].append(
                {
                    "candidate_id": binding.candidate_id,
                    "edge_id": edge.id,
                    "reason": "привязка отклонена: кандидат отсутствует в классификации",
                }
            )
            continue
        value = _parse_positive_number(candidate.text)
        if candidate.kind != "dimension" or value is None:
            report["rejected_candidates"].append(
                {
                    "candidate_id": binding.candidate_id,
                    "edge_id": edge.id,
                    "reason": "привязка отклонена: кандидат не является числовым размером",
                }
            )
            continue
        if not classification.accepted:
            supported = _graph_supported(
                candidate,
                vector_graph,
                edge,
                classification,
            )
            if supported is not None:
                classification.classification = "route_segment"
                classification.route_type = edge.path_type if edge.path_type in {"main", "branch"} else classification.route_type or "unknown"
                classification.accepted = True
                classification.confidence = max(classification.confidence or 0.0, 0.55)
                classification.gate_reason = "graph-supported: привязка ребра подтверждена векторным штрихом"
                classification.reason = (
                    f"{classification.reason} Привязка к ребру {edge.id} подтверждена векторным графом "
                    f"(штрих S{supported}); conf ниже порога, длина принята solver'ом."
                ).strip()
                report["candidate_reasons"][candidate.id] = {
                    "value": value,
                    "assigned": edge.path_type if edge.path_type in {"main", "branch"} else None,
                    "basis": f"ребро {edge.id} графа; привязка поддержана векторным штрихом; conf ниже порога",
                }
                report["notes"].append(
                    f"ребро {edge.id}: размер {candidate.id} не прошел rule gate по conf, но привязка и векторный штрих подтверждают связь"
                )
            else:
                report["rejected_candidates"].append(
                    {
                        "candidate_id": binding.candidate_id,
                        "edge_id": edge.id,
                        "reason": "привязка отклонена: кандидат не принят rule gate и векторный штрих не подтверждает связь",
                    }
                )
                continue
        elif edge.path_type in {"main", "branch"} and classification.route_type not in {"main", "branch"}:
            classification.route_type = edge.path_type
        accepted_bindings.append((binding, candidate, value))

    if accepted_bindings:
        binding, candidate, value = accepted_bindings[0]
        length = value
        length_basis = "dimension"
        used_candidates.append(candidate.id)
        label_candidates.append(candidate.id)
        note = f"размерная привязка {candidate.id} ({candidate.text} мм)"
        report["candidate_reasons"][candidate.id] = {
            "value": value,
            "assigned": edge.path_type if edge.path_type in {"main", "branch"} else None,
            "basis": f"ребро {edge.id} графа; {binding.reason or note}",
        }

    if length is None and edge.length_mm and 0 < edge.length_mm < MAX_LENGTH_MM:
        length = float(edge.length_mm)
        length_basis = edge.length_source or "dimension"
        note = f"длина из графа провайдера ({length_basis})"
        for candidate_id in edge.source_candidate_ids:
            if candidate_id in candidate_by_id and candidate_id not in used_candidates:
                used_candidates.append(candidate_id)
            if candidate_id in candidate_by_id and candidate_id not in label_candidates:
                label_candidates.append(candidate_id)

    if length is None:
        proposal = proposal_by_edge.get(edge.id)
        if proposal:
            report["notes"].append(
                f"ребро {edge.id}: модель пометила unresolved ({proposal.reason}); пробуем закрыть программно"
            )

        candidate = _nearest_free_candidate(
            edge,
            candidate_by_id,
            classification_by_id,
            used_candidates,
            value_limit=_small_limit(candidate_by_id),
        )
        if candidate is not None and vector_graph is not None and _parallel_offset_support(vector_graph, candidate.id):
            report["notes"].append(
                f"ребро {edge.id}: кандидат-кандидат {candidate.id} отклонен — малый размер параллелен основному (привязка опоры), не длина"
            )
            candidate = None
        if candidate is not None:
            value = _parse_positive_number(candidate.text)
            if value is not None:
                classification = classification_by_id.get(candidate.id)
                if classification is not None and not classification.accepted:
                    classification.classification = "route_segment"
                    classification.route_type = edge.path_type if edge.path_type in {"main", "branch"} else "branch"
                    classification.accepted = True
                    classification.confidence = max(classification.confidence or 0.0, 0.55)
                    classification.gate_reason = "solver: свободный малый размер рядом с ребром графа"
                    classification.reason = (
                        f"{classification.reason} Solver принял ребро {edge.id} по ближайшему малому свободному размеру."
                    ).strip()
                length = value
                length_basis = "dimension"
                used_candidates.append(candidate.id)
                label_candidates.append(candidate.id)
                note = f"закрыто ближайшим свободным размером {candidate.id} ({candidate.text} мм)"
                report["candidate_reasons"][candidate.id] = {
                    "value": value,
                    "assigned": None,
                    "basis": f"solver: свободный размер рядом с ребром {edge.id}",
                }
                report["notes"].append(f"ребро {edge.id}: закрыто свободным размером {candidate.id}")

        if (
            length is None
            and start_node.x is not None and start_node.y is not None and start_node.z is not None
            and end_node.x is not None and end_node.y is not None and end_node.z is not None
        ):
            distance = math.dist(
                (start_node.x, start_node.y, start_node.z),
                (end_node.x, end_node.y, end_node.z),
            )
            if 0 < distance < MAX_LENGTH_MM:
                length = round(distance, 1)
                length_basis = "coordinate"
                note = "закрыто по координатам узлов (евклидово расстояние)"

    path_type = edge.path_type if edge.path_type in {"main", "branch"} else "unknown"
    on_path = False
    if vector_graph is not None and edge.bbox is not None:
        center_x = (edge.bbox[0] + edge.bbox[2]) / 2
        center_y = (edge.bbox[1] + edge.bbox[3]) / 2
        on_path = _distance_to_primary_path(vector_graph, center_x, center_y) <= EDGE_PATH_PROXIMITY_PX
    if path_type == "unknown" and length is not None and on_path:
        path_type = "main"
        report["notes"].append(f"ребро {edge.id}: path_type unknown->main по совпадению с векторным путем оси")
    elif path_type == "unknown" and length is not None and length <= _small_limit(candidate_by_id):
        path_type = "branch"
        report["notes"].append(f"ребро {edge.id}: малый изолированный размер ({length:g} мм) -> branch по геометрическому правилу")

    if length is None:
        proposal = proposal_by_edge.get(edge.id)
        return {
            "status": "unresolved",
            "reason": proposal.reason if proposal else "длину не удалось подтвердить ни размером, ни координатами",
            "suggested_action": (
                proposal.suggested_action if proposal else "найти свободный размер у ребра или добавить подпись длины"
            ),
        }

    return {
        "status": "resolved",
        "length": length,
        "length_basis": length_basis,
        "path_type": path_type,
        "note": note,
        "used_candidates": used_candidates,
        "label_candidates": label_candidates,
        "on_path": on_path,
    }


def _dedup_bindings(
    bindings: list[DimensionBinding],
    candidate_by_id: dict[str, Candidate],
    classification_by_id: dict[str, CandidateClassification],
) -> tuple[list[DimensionBinding], list[dict[str, Any]]]:
    by_candidate: dict[str, list[DimensionBinding]] = defaultdict(list)
    for binding in bindings:
        by_candidate[binding.candidate_id].append(binding)

    actions: list[dict[str, Any]] = []
    output: list[DimensionBinding] = []
    for candidate_id, candidate_bindings in by_candidate.items():
        if len(candidate_bindings) == 1:
            output.append(candidate_bindings[0])
            continue

        def priority(binding: DimensionBinding) -> tuple[int, int, str]:
            candidate = candidate_by_id.get(candidate_id)
            classification = classification_by_id.get(candidate_id)
            accepted = 0 if (candidate and classification and classification.accepted) else 1
            rank = 0 if candidate and candidate.kind == "dimension" else 1
            return (accepted, rank, binding.edge_id)

        kept = min(candidate_bindings, key=priority)
        output.append(kept)
        for binding in candidate_bindings:
            if binding is not kept:
                actions.append(
                    {
                        "candidate_id": candidate_id,
                        "kept_edge": kept.edge_id,
                        "edge_id": binding.edge_id,
                        "reason": "кандидат привязан к нескольким ребрам; оставлена привязка принятого размерного кандидата",
                    }
                )
    return output, actions


def _nearest_free_candidate(
    edge,
    candidate_by_id: dict[str, Candidate],
    classification_by_id: dict[str, CandidateClassification],
    used_candidates: list[str],
    value_limit: float | None = None,
) -> Candidate | None:
    if edge.bbox is None:
        return None
    center_x = (edge.bbox[0] + edge.bbox[2]) / 2
    center_y = (edge.bbox[1] + edge.bbox[3]) / 2
    best: Candidate | None = None
    best_distance = FREE_DIM_SEARCH_PX
    for candidate in candidate_by_id.values():
        if candidate.id in used_candidates:
            continue
        if candidate.kind != "dimension" or candidate.zone != "drawing":
            continue
        classification = classification_by_id.get(candidate.id)
        value = _parse_positive_number(candidate.text)
        if value is None:
            continue
        if classification is None or not classification.accepted:
            if value_limit is None or value > value_limit:
                continue
            if classification is not None and classification.classification in {"excluded_table", "coordinate", "number", "dn", "adjacent_line"}:
                continue
        candidate_center_x = (candidate.bbox[0] + candidate.bbox[2]) / 2
        candidate_center_y = (candidate.bbox[1] + candidate.bbox[3]) / 2
        distance = math.hypot(center_x - candidate_center_x, center_y - candidate_center_y)
        if distance < best_distance:
            best_distance = distance
            best = candidate
    return best


def _graph_supported(candidate: Candidate, vector_graph, edge, classification: CandidateClassification) -> int | None:
    """Векторное подтверждение привязки: у размера есть близкая тонкая размерная линия."""
    if candidate.zone != "drawing" or candidate.kind != "dimension":
        return None
    if classification.classification in {"excluded_table", "coordinate", "dn", "adjacent_line"}:
        return None
    if vector_graph is None or vector_graph.status != "loaded":
        return None
    stroke_index = vector_graph.attached.get(candidate.id)
    if stroke_index is None:
        return None
    stroke = vector_graph.strokes[stroke_index]
    if stroke.kind == "dimension" and _bbox_touches(stroke, edge.bbox):
        return stroke_index
    return None


def _bbox_touches(stroke, edge_bbox) -> bool:
    if edge_bbox is None:
        return False
    ex0, ey0, ex1, ey1 = edge_bbox
    pad = 40.0
    return not (
        max(stroke.x0, stroke.x1) < ex0 - pad
        or min(stroke.x0, stroke.x1) > ex1 + pad
        or max(stroke.y0, stroke.y1) < ey0 - pad
        or min(stroke.y0, stroke.y1) > ey1 + pad
    )


def _distance_to_primary_path(vector_graph, px: float, py: float) -> float:
    path = vector_graph.primary_path or []
    if not path:
        return float("inf")
    best = float("inf")
    for stroke_index in path:
        stroke = vector_graph.strokes[stroke_index]
        best = min(best, _point_segment_distance(px, py, stroke.x0, stroke.y0, stroke.x1, stroke.y1))
    return best


def _small_limit(candidate_by_id: dict[str, Candidate]) -> float:
    values = []
    for candidate in candidate_by_id.values():
        if candidate.kind != "dimension" or candidate.zone != "drawing":
            continue
        parsed = _parse_positive_number(candidate.text)
        if parsed is not None:
            values.append(parsed)
    max_value = max(values, default=0.0)
    return max(SMALL_BRANCH_RATIO * max_value, 5.0)


def _rebuild_line(
    result: AnalysisResult,
    group: LineGroup,
    candidates: list[Candidate],
    nodes,
    resolved: list[tuple[Any, dict[str, Any]]],
    unresolved: list[dict[str, Any]],
    report: dict[str, Any],
    candidate_by_id: dict[str, Candidate],
    classification_by_id: dict[str, CandidateClassification],
    vector_graph,
) -> None:
    line = _graph_line(result, group)
    line_id = group.line_id

    result.points = [point for point in result.points if point.line_id != line_id]
    result.segments = [segment for segment in result.segments if segment.line_id != line_id]

    node_point_ids: dict[str, str] = {}
    for node in nodes:
        point_id = node.id
        node_point_ids[node.id] = point_id
        result.points.append(
            PointResult(
                id=point_id,
                line_id=line_id,
                role=node.role or "узел",
                x=node.x,
                y=node.y,
                z=node.z,
                source="drawing" if node.bbox else "calculated",  # type: ignore[arg-type]
                source_ref=(
                    SourceRef(
                        page=node.page,
                        label=" ".join(node.source_candidate_ids) if node.source_candidate_ids else (node.reason or node.id),
                        bbox=node.bbox,
                    )
                    if node.bbox
                    else SourceRef(page=node.page, label=node.reason or node.id)
                ),
            )
        )

    segment_ids: set[str] = set()
    segments: list[SegmentResult] = []
    synthetic_index = 0
    for edge, decision in resolved:
        edge_index = synthetic_index + 1
        edge_segment_id = edge.id if re.match(r"^[A-Z]{1,3}\d+$", edge.id or "") else f"GE{edge_index:02d}"
        while edge_segment_id in segment_ids:
            synthetic_index += 1
            edge_segment_id = f"GE{synthetic_index:02d}"
        segment_ids.add(edge_segment_id)
        start_point_id = node_point_ids.get(edge.from_node_id)
        end_point_id = node_point_ids.get(edge.to_node_id)
        if start_point_id is None or end_point_id is None:
            continue
        used = list(decision.get("label_candidates", [])) or list(decision.get("used_candidates", []))
        source_label = " + ".join(used) if used else f"graph edge {edge.id}"
        page_number = edge.page or (group.pages[0].page_number if group.pages else 0)
        segment = SegmentResult(
            id=edge_segment_id,
            line_id=line_id,
            start_point_id=start_point_id,
            end_point_id=end_point_id,
            dn=edge.nominal_size or "unknown",
            length_mm=float(decision["length"]),
            route_type=decision["path_type"],  # type: ignore[arg-type]
            source_size=_edge_source_size(edge, decision, candidate_by_id),
            source_ref=SourceRef(page=page_number, label=source_label, bbox=edge.bbox),
        )
        segments.append(segment)
        result.annotations.append(
            Annotation(
                id=f"A-GE-{segment.id}",
                page=page_number,
                label=(
                    f"{segment.id} {segment.length_mm:g} {segment.route_type}"
                    f" ({decision.get('note', '')})"
                ),
                kind="graph_edge",
                bbox=edge.bbox,
                color=EDGE_ANNOTATION_COLOR,
            )
        )

    result.segments.extend(segments)

    main_sum = sum(
        segment.length_mm for segment in segments if segment.route_type == "main" and segment.length_mm is not None
    )
    branch_sum = sum(
        segment.length_mm for segment in segments if segment.route_type == "branch" and segment.length_mm is not None
    )
    report["main_sum_mm"] = main_sum
    report["branch_sum_mm"] = branch_sum
    report["total_sum_mm"] = round(main_sum + branch_sum, 3)

    used_ids = set(report["candidate_reasons"].keys())
    for candidate_id in used_ids:
        used = False
        for edge, decision in resolved:
            if candidate_id in decision.get("used_candidates", []):
                used = True
                break
        if not used:
            report["candidate_reasons"][candidate_id]["assigned"] = report["candidate_reasons"][candidate_id].get("assigned")
    for candidate in candidates:
        if candidate.id in used_ids:
            continue
        if candidate.kind != "dimension" or candidate.zone != "drawing":
            continue
        classification = classification_by_id.get(candidate.id)
        value = _parse_positive_number(candidate.text)
        report["candidate_reasons"][candidate.id] = {
            "value": value or 0.0,
            "assigned": None,
            "basis": "размер не привязан ни к одному ребру графа; не включен в длины",
        }
        report["unassigned_drawing_dims"].append(
            {
                "candidate_id": candidate.id,
                "value": value or 0.0,
                "kind": candidate.kind,
                "zone": candidate.zone,
                "classification": classification.classification if classification else "unknown",
            }
        )

    line.main_length_mm = main_sum
    line.branches_length_mm = branch_sum
    line.total_length_mm = report["total_sum_mm"]
    line.status = "needs_review" if unresolved else "partial"

    if vector_graph is not None:
        report["graph"] = vector_graph.to_report()
        for stroke_index in vector_graph.primary_path or []:
            stroke = vector_graph.strokes[stroke_index]
            result.annotations.append(
                Annotation(
                    id=f"AX-{line_id}-S{stroke_index}",
                    page=group.pages[0].page_number if group.pages else 0,
                    label=f"axis S{stroke_index} {stroke.kind}",
                    kind="route_axis",
                    bbox=(stroke.x0, stroke.y0, stroke.x1, stroke.y1),
                    color="#f59e0b",
                )
            )
    report["notes"].append(
        "Правило: длины берутся из ребер графа; каждое ребро принимается только с размерной привязкой или координатами; без подтверждения - unresolved."
    )
    line.route_reconstruction = report


def _edge_source_size(edge, decision: dict[str, Any], candidate_by_id: dict[str, Candidate]) -> str:
    used: list[str] = decision.get("label_candidates") or decision.get("used_candidates", [])
    if used:
        candidate = candidate_by_id.get(used[0])
        if candidate is not None:
            return candidate.text
    if edge.length_mm is not None:
        return f"{edge.length_mm:g}" if isinstance(edge.length_mm, float) else str(edge.length_mm)
    return decision.get("length_basis", "unknown")


def _graph_line(result: AnalysisResult, group: LineGroup) -> LineResult:
    for line in result.lines:
        if line.id == group.line_id:
            return line
    line = LineResult(
        id=group.line_id,
        pages=[page.page_number for page in group.pages],
        status="partial",
        completeness_note="Линия собрана детерминированным solver'ом по графу трубопровода.",
    )
    result.lines.append(line)
    return line
