from __future__ import annotations

import hashlib
import pickle
from pathlib import Path
from typing import Callable

from .ai_client import AIClient, DeepSeekAIClient, StubAIClient
from .candidate_extractor import extract_candidates_for_groups
from .geometry_solver import apply_geometry_solution
from .models import AnalysisResult, Annotation, LineGroup, ProjectResult, ProviderTrace
from .pdf_ingest import group_pages_by_line, read_pdf_pages
from .route_reconstruction import apply_route_reconstruction
from .route_reconstruction import extract_local_vertices
from .validation import validate_and_normalize


ProgressCallback = Callable[[int, int, str], None]
PDF_CACHE_DIR = Path(".cache") / "pdf_groups"
PDF_CACHE_VERSION = "v5-p21-line-prefix"


def run_pipeline(
    pdf_path: str | Path,
    source_name: str | None = None,
    ai_client: AIClient | None = None,
    progress: ProgressCallback | None = None,
    target_line_id: str | None = None,
    max_groups: int | None = None,
    stop_stage: str | None = None,
) -> ProjectResult:
    path = Path(pdf_path)
    ai = ai_client or StubAIClient()

    pages, groups = prepare_pdf_groups(path)
    return run_pipeline_from_groups(
        source_name=source_name or path.name,
        pages_count=len(pages),
        groups=groups,
        ai_client=ai,
        progress=progress,
        target_line_id=target_line_id,
        max_groups=max_groups,
        pdf_path=path,
        stop_stage=stop_stage,
    )


def prepare_pdf_groups(pdf_path: str | Path, use_disk_cache: bool = True) -> tuple[list[PageInfo], list[LineGroup]]:
    path = Path(pdf_path)
    if use_disk_cache:
        cached = _read_pdf_cache(path)
        if cached is not None:
            return cached

    pages = read_pdf_pages(path)
    groups = group_pages_by_line(pages)
    if use_disk_cache:
        _write_pdf_cache(path, pages, groups)
    return pages, groups


def get_pdf_cache_status(pdf_path: str | Path) -> str:
    path = Path(pdf_path)
    cache_path = _cache_path_for(path)
    return "hit" if cache_path.exists() else "miss"


def run_pipeline_from_groups(
    source_name: str,
    pages_count: int,
    groups: list[LineGroup],
    ai_client: AIClient,
    progress: ProgressCallback | None = None,
    target_line_id: str | None = None,
    max_groups: int | None = None,
    pdf_path: str | Path | None = None,
    extract_candidates: bool = True,
    stop_stage: str | None = None,
) -> ProjectResult:
    selected_groups = groups
    if target_line_id:
        normalized_target = target_line_id.replace("-", "_").upper()
        selected_groups = [group for group in selected_groups if group.line_id == normalized_target]
    if max_groups is not None:
        selected_groups = selected_groups[: max(0, max_groups)]

    aggregate = AnalysisResult()
    candidates = []
    if extract_candidates and pdf_path and selected_groups:
        candidates = extract_candidates_for_groups(pdf_path, selected_groups)
        ai_client.set_candidates(candidates)

    total = len(selected_groups)
    for index, group in enumerate(selected_groups, start=1):
        if progress:
            progress(index, total, group.line_id)
        group_candidates = [candidate for candidate in candidates if candidate.line_id == group.line_id]
        if stop_stage == "extract":
            partial = AnalysisResult(candidates=list(group_candidates))
            aggregate.extend(partial)
            continue
        if stop_stage == "local_vertices":
            vertices = extract_local_vertices(
                pdf_path,
                group.pages[0].page_number if group.pages else 0,
                group.line_id,
                group_candidates,
            ) if pdf_path else []
            aggregate.extend(AnalysisResult(
                vertices=list(vertices),
                annotations=[
                    Annotation(
                        id=f"VERTEX-{vertex.id}",
                        page=vertex.page,
                        label=vertex.label,
                        kind="vertex_local",
                        bbox=(vertex.x - 5, vertex.y - 5, vertex.x + 5, vertex.y + 5),
                        color="#dc2626",
                    )
                    for vertex in vertices
                ],
            ))
            continue
        if stop_stage == "vertices":
            vertices = ai_client.recognize_vertices(group)
            aggregate.extend(AnalysisResult(
                vertices=list(vertices),
                annotations=[
                    Annotation(
                        id=f"VERTEX-{vertex.id}",
                        page=vertex.page,
                        label=vertex.label,
                        kind="vertex_ai",
                        bbox=(vertex.x - 5, vertex.y - 5, vertex.x + 5, vertex.y + 5),
                        color="#dc2626",
                    )
                    for vertex in vertices
                ],
            ))
            aggregate.provider_traces.extend(ai_client.drain_provider_traces())
            continue
        classifications = [] if stop_stage == "analyze_no_classify" else ai_client.classify_candidates(group)
        ai_client.set_candidate_classifications(classifications)
        if stop_stage in {"classify", "classify_ai"}:
            aggregate.extend(AnalysisResult(candidates=list(group_candidates), candidate_classifications=list(classifications)))
            traces = ai_client.drain_provider_traces()
            if stop_stage == "classify" and not traces and pdf_path:
                preview_client = DeepSeekAIClient(api_key="", pdf_path=pdf_path)
                preview_client.set_candidates(group_candidates)
                aggregate.provider_traces.append(
                    ProviderTrace(
                        id=f"AI-{group.line_id}-classify-preview-0001",
                        line_id=group.line_id,
                        stage="classify",
                        model=preview_client.model,
                        prompt=preview_client.preview_prompt(group, "classify"),
                        status="preview",
                        error="Запрос не отправлялся: выбран локальный этап классификации.",
                    )
                )
            else:
                aggregate.provider_traces.extend(traces)
            continue
        raw_result = ai_client.analyze_page_or_line(group)
        raw_result.candidate_classifications.clear()
        raw_result.candidates.extend(group_candidates)
        raw_result.candidate_classifications.extend(classifications)
        local_vertices = extract_local_vertices(
            pdf_path,
            group.pages[0].page_number if group.pages else 0,
            group.line_id,
            group_candidates,
        ) if pdf_path else []
        raw_result.vertices = list(local_vertices) + raw_result.vertices
        raw_result.annotations.extend(
            Annotation(
                id=f"VERTEX-LOCAL-{vertex.id}",
                page=vertex.page,
                label=vertex.label,
                kind="vertex_local",
                bbox=(vertex.x - 5, vertex.y - 5, vertex.x + 5, vertex.y + 5),
                color="#dc2626",
            )
            for vertex in local_vertices
        )
        apply_geometry_solution(raw_result, group, group_candidates)
        if stop_stage in {"geometry", "analyze_no_classify"}:
            aggregate.extend(raw_result)
            continue
        apply_route_reconstruction(raw_result, group, group_candidates, pdf_path)
        raw_result.provider_traces.extend(ai_client.drain_provider_traces())
        aggregate.extend(validate_and_normalize(raw_result))

    if not stop_stage or stop_stage in {"reconstruct"}:
        validate_and_normalize(aggregate)

    return ProjectResult.create(
        source_name=source_name,
        pages_count=pages_count,
        line_groups_count=len(selected_groups),
        model_mode=ai_client.mode,
        result=aggregate,
    )


def _cache_key_for(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    raw = f"{PDF_CACHE_VERSION}|{resolved}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cache_path_for(path: Path) -> Path:
    return PDF_CACHE_DIR / f"{_cache_key_for(path)}.pkl"


def _read_pdf_cache(path: Path) -> tuple[list[PageInfo], list[LineGroup]] | None:
    cache_path = _cache_path_for(path)
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("rb") as file:
            cached = pickle.load(file)
        return cached["pages"], cached["groups"]
    except Exception:
        return None


def _write_pdf_cache(path: Path, pages: list[PageInfo], groups: list[LineGroup]) -> None:
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path_for(path)
    with cache_path.open("wb") as file:
        pickle.dump({"pages": pages, "groups": groups}, file)
