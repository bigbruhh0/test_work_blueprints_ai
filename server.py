from __future__ import annotations

import io
import json
import os
import shutil
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import db as run_db, prompts
from src.dimension_mapping import (
    _handwheel_details,
    attach_coordinate_leads,
    run_dimension_mapping,
    save_clean_graph_pdf,
    save_clean_local_markup_pdf,
    save_dimension_lead_detection_pdf,
    save_local_dimension_filter_pdf,
    save_local_markup_overview_pdf,
    save_final_contour_rays_pdf,
    save_pipeline_length_diagnostic_pdf,
    save_preprocess_annotation_pdf,
    save_skeleton_pdf,
)
from src.dimension_review import build_map_text, calculate_lengths, codex_login_command, codex_login_status, review_map_with_provider
from src.pipeline_length import build_pipeline_length_page_payload, build_pipeline_length_payload, build_pipeline_length_provider_payload, build_pipeline_length_result, build_pipeline_length_text, calculate_provider_length_summary, merge_pipeline_length_provider_traces, run_pipeline_length_provider
from src.eval_data import aggregate_eval_results, evaluate_line_for_prompt, list_eval_groups, summarize_prompt_eval_rows
from src.pdf_groups import get_pdf_cache_status, prepare_pdf_groups
from src.prepare_stage import run_prepare
from scripts.build_dimension_map import build_map as build_dimension_map, render_pdf as render_dimension_map


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_PDF = ROOT / "isometries.pdf"
ARTIFACTS_DIR = ROOT / ".cache" / "mark_runs"
load_dotenv(ROOT / ".env")

PIPELINE_STEPS = [
    ("local_processing", "Локальная обработка"),
    ("lead_detection", "Поиск lead-стрелок размеров"),
    ("pipeline_length", "Расчет длины трубопровода"),
    ("dimension_review", "Карта размеров + проверка провайдером"),
]

# Расширяемый реестр конфигураций запуска. stop_stage -> ключ + человекочитаемая метка.
RUN_KIND_BY_STAGE = {
    "local_processing": ("local_processing", "Локальная обработка"),
    "lead_detection": ("lead_detection", "Поиск lead-стрелок размеров"),
    "prepare": ("local_prepare", "Локальная обработка"),
    "dimensions": ("dimension_mapping", "Локальная обработка"),
    "pipeline_length": ("pipeline_length", "Расчет длины трубопровода"),
    "dimension_review": ("dimension_review", "Карта размеров + проверка провайдером"),
}
ENV: dict[str, str] = {}
app = FastAPI(title="Isometry Mark Pipeline")


@dataclass
class PageRun:
    page_number: int
    status: str = "pending"
    stage: str = "prepare"
    error: str = ""
    numbers: list[dict[str, Any]] = field(default_factory=list)
    vertices: list[dict[str, Any]] = field(default_factory=list)
    coordinates: list[dict[str, Any]] = field(default_factory=list)
    skipped_numbers: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    analysis: dict[str, Any] | None = None
    provider_trace: dict[str, Any] | None = None
    prompt_revision: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LineRun:
    line_id: str
    pages: list[int]
    status: str = "pending"
    error: str = ""
    page_results: dict[int, PageRun] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    analysis: dict[str, Any] | None = None
    provider_trace: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    document_id: str
    source_name: str
    pdf_path: str
    line_ids: list[str]
    excluded_pages: list[int] = field(default_factory=list)
    stop_stage: str = "dimension_review"
    status: str = "running"
    created_at: str = ""
    kind: str = ""
    kind_label: str = ""
    model: str = ""
    provider: str = "deepseek"
    lines: dict[str, LineRun] = field(default_factory=dict)


STATE: dict[str, dict[str, Any]] = {"documents": {}, "runs": {}}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.get("/favicon.ico")
def favicon() -> Any:
    return Response(status_code=204)


@app.on_event("startup")
def startup() -> None:
    ENV.update({key: value.strip() for key, value in os.environ.items() if key.startswith("DEEPSEEK")})
    run_db.init_db()
    run_db.mark_running_runs_interrupted("Прогон был прерван перезапуском сервера")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def config() -> dict[str, Any]:
    default_provider = ENV.get("AI_PROVIDER", "deepseek").strip().lower() or "deepseek"
    if default_provider not in {"deepseek", "codex_cli"}:
        default_provider = "deepseek"
    codex_status = codex_login_status()
    return {
        "pipeline_steps": [{"id": key, "label": label} for key, label in PIPELINE_STEPS],
        "deepseek": bool(ENV.get("DEEPSEEK_API_KEY")),
        "model": ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "default_provider": default_provider,
        "providers": [
            {"id": "deepseek", "label": "DeepSeek API", "available": bool(ENV.get("DEEPSEEK_API_KEY")), "model": ENV.get("DEEPSEEK_MODEL", "deepseek-flash")},
            {"id": "codex_cli", "label": "Codex CLI", "available": bool(codex_status.get("available")), "logged_in": bool(codex_status.get("logged_in")), "model": ENV.get("CODEX_CLI_MODEL", "default")},
        ],
        "codex_cli": codex_status,
        "default_pdf": DEFAULT_PDF.name if DEFAULT_PDF.exists() else "",
    }


@app.get("/api/providers/codex/status")
def codex_provider_status() -> dict[str, Any]:
    return codex_login_status()


@app.post("/api/providers/codex/login")
def codex_provider_login() -> dict[str, Any]:
    status = codex_login_status()
    if status.get("logged_in"):
        return {"status": status, "command": "", "note": "Codex CLI уже авторизован."}
    return {"status": status, **codex_login_command()}


@app.get("/api/providers/codex/login")
def codex_provider_login_info() -> dict[str, Any]:
    return codex_provider_login()


class LoadPdfBody(BaseModel):
    file_path: str = ""


@app.post("/api/pdf/load-default")
def load_default_pdf() -> dict[str, Any]:
    return load_pdf(LoadPdfBody(file_path=str(DEFAULT_PDF)))


@app.post("/api/pdf/load")
def load_pdf(body: LoadPdfBody) -> dict[str, Any]:
    resolved = Path((body.file_path or "").strip() or str(DEFAULT_PDF))
    if not resolved.exists():
        resolved = DEFAULT_PDF if DEFAULT_PDF.exists() else resolved
    if not resolved.exists():
        raise HTTPException(404, f"Файл не найден: {body.file_path}")
    cache_status = get_pdf_cache_status(resolved)
    pages, groups = prepare_pdf_groups(resolved, use_disk_cache=True)
    group_rows = [
        {"line_id": group.line_id, "pages": [page.page_number for page in group.pages]}
        for group in groups
    ]
    document_id = uuid.uuid4().hex[:12]
    STATE["documents"][document_id] = {
        "document_id": document_id,
        "pdf_path": str(resolved),
        "source_name": resolved.name,
        "pages_count": len(pages),
        "groups": group_rows,
        "cache": cache_status,
    }
    return {
        "document_id": document_id,
        "source_name": resolved.name,
        "pages_count": len(pages),
        "cache": cache_status,
        "groups": group_rows,
    }


@app.post("/api/pdf/upload")
async def upload_pdf(file: UploadFile) -> dict[str, Any]:
    UPLOAD_DIR = ROOT / ".cache" / "uploads"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / (file.filename or f"upload-{uuid.uuid4().hex}.pdf")
    with target.open("wb") as sink:
        shutil.copyfileobj(file.file, sink)
    return load_pdf(LoadPdfBody(file_path=str(target)))


def _group(document: dict[str, Any], line_id: str):
    for group in document["groups"]:
        if group.get("line_id") == line_id:
            return group
    return None


class AnalyzeBody(BaseModel):
    document_id: str
    line_ids: list[str]
    stop_stage: str = "dimension_review"
    excluded_pages: list[int] = []
    provider: str = "deepseek"


def _versioned_prompt_feedback_summary() -> list[dict[str, Any]]:
    """Resolve legacy feedback to a prompt revision by exact prompt-text hash."""
    from src import prompts

    grouped: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for row in run_db.feedback_rows():
        prompt_name = row.get("prompt_name")
        revision = prompts.resolve_recorded_revision(
            prompt_name, row.get("prompt_version"), row.get("prompt_sha256"),
            (row.get("context") or {}).get("prompt"),
        ) if prompt_name else {}
        prompt_version = revision.get("version")
        prompt_sha256 = revision.get("sha256")
        key = (prompt_name, prompt_version, prompt_sha256)
        summary = grouped.setdefault(key, {
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
            "positive": 0,
            "negative": 0,
            "total": 0,
        })
        summary["positive" if row.get("rating") == "up" else "negative"] += 1
        summary["total"] += 1
    return list(grouped.values())


def _prompt_feedback_counts(
    rows: list[dict[str, Any]], name: str, revision: dict[str, Any] | None = None,
    *, unversioned: bool = False,
) -> dict[str, int]:
    matching = [row for row in rows if row.get("prompt_name") == name]
    if revision is not None:
        matching = [row for row in matching
                    if str(row.get("prompt_version")) == str(revision.get("version"))
                    and row.get("prompt_sha256") == revision.get("sha256")]
    elif unversioned:
        matching = [row for row in matching
                    if row.get("prompt_version") in (None, "", "unversioned")
                    or not row.get("prompt_sha256")]
    positive = sum(int(row.get("positive") or 0) for row in matching)
    negative = sum(int(row.get("negative") or 0) for row in matching)
    return {"positive": positive, "negative": negative, "total": positive + negative}


@app.get("/api/prompts")
def list_prompts() -> list[dict[str, Any]]:
    from src import prompts

    summary_rows = _versioned_prompt_feedback_summary()
    output = []
    for item in prompts.list_prompts():
        active = prompts.load_prompt_revision(item["name"])
        active_feedback = _prompt_feedback_counts(summary_rows, item["name"], active)
        output.append({
            **item,
            "active_version": active.get("version", "default"),
            "active_sha256": active.get("sha256", ""),
            "feedback": active_feedback,
            "active_feedback": active_feedback,
            "prompt_feedback": _prompt_feedback_counts(summary_rows, item["name"]),
            "unversioned_feedback": _prompt_feedback_counts(summary_rows, item["name"], unversioned=True),
        })
    return output


class PromptBody(BaseModel):
    text: str


class FeedbackBody(BaseModel):
    run_id: str
    line_id: str
    page_number: int
    candidate_id: str
    rating: str


class ManualStatusBody(BaseModel):
    run_id: str
    line_id: str
    page_number: int
    candidate_id: str
    status: str


class PipelineLengthConfirmationBody(BaseModel):
    run_id: str
    line_id: str
    candidate_ids: list[str] = []
    keep_local_candidate_ids: list[str] = []
    intermediate_distance_indices: list[int] = []
    edge_ids: list[str] = []


def _feedback_revision(page: dict[str, Any]) -> dict[str, Any]:
    from src import prompts

    trace = page.get("provider_trace") or {}
    revision = page.get("prompt_revision") or {}
    version = revision.get("version")
    if version is None:
        version = trace.get("prompt_version")
    resolved = prompts.resolve_recorded_revision(
        revision.get("name") or trace.get("prompt_name") or "dimension_review",
        version, revision.get("sha256") or trace.get("prompt_sha256"), trace.get("prompt"),
    )
    return {
        "prompt_name": resolved["name"],
        "prompt_version": resolved["version"],
        "prompt_sha256": resolved["sha256"],
    }


@app.post("/api/feedback")
def save_analysis_feedback(body: FeedbackBody) -> dict[str, Any]:
    if body.rating not in {"up", "down"}:
        raise HTTPException(400, "rating must be up or down")
    run = get_run(body.run_id)
    line = next((item for item in run.get("lines", []) if item.get("line_id") == body.line_id), None)
    page = next((item for item in (line or {}).get("page_results", []) if item.get("page_number") == body.page_number), None)
    if page is None:
        raise HTTPException(404, "Страница результата не найдена")
    mapping = ((page.get("analysis") or {}).get("dimension_mapping") or {})
    dimension = next((item for item in mapping.get("dimensions", []) if item.get("id") == body.candidate_id), None)
    if dimension is None:
        raise HTTPException(404, "Кандидат размера не найден")
    review = (page.get("analysis") or {}).get("dimension_review") or {}
    if not review or not review.get("answer"):
        raise HTTPException(400, "Оценка доступна только после этапа провайдера")
    decision = next((item for item in (review.get("answer") or {}).get("candidate_decisions", []) if item.get("candidate_id") == body.candidate_id), {})
    trace = page.get("provider_trace") or {}
    context = {
        "source_name": run.get("source_name"),
        "dimension": dimension,
        "decision": decision,
        "lengths": review.get("lengths"),
        "prompt": trace.get("prompt"),
        "payload": trace.get("payload"),
        "response_raw": trace.get("response_raw"),
    }
    return run_db.save_feedback({
        "run_id": body.run_id,
        "line_id": body.line_id,
        "page_number": body.page_number,
        "candidate_id": body.candidate_id,
        "rating": body.rating,
        **_feedback_revision(page),
        "context": context,
    })


@app.get("/api/feedback")
def list_analysis_feedback(prompt_name: str | None = None) -> dict[str, Any]:
    summary = _versioned_prompt_feedback_summary()
    if prompt_name:
        summary = [row for row in summary if row.get("prompt_name") == prompt_name]
    return {"rows": run_db.feedback_rows(prompt_name), "summary": summary}


@app.post("/api/manual-status")
def update_manual_status(body: ManualStatusBody) -> dict[str, Any]:
    if body.status not in {"include", "exclude", "ambiguous"}:
        raise HTTPException(400, "status must be include, exclude, or ambiguous")
    run = get_run(body.run_id)
    line = next((item for item in run.get("lines", []) if item.get("line_id") == body.line_id), None)
    page = next((item for item in (line or {}).get("page_results", []) if item.get("page_number") == body.page_number), None)
    if page is None:
        raise HTTPException(404, "Страница результата не найдена")
    analysis = page.get("analysis") or {}
    if not analysis.get("dimension_review") or not (analysis["dimension_review"].get("answer") or {}).get("candidate_decisions"):
        raise HTTPException(400, "Ручная правка доступна только после этапа провайдера")
    mapping = analysis.get("dimension_mapping") or {}
    dimension = next((item for item in mapping.get("dimensions", []) if item.get("id") == body.candidate_id), None)
    review = analysis.get("dimension_review") or {}
    answer = review.get("answer") or {}
    decisions = answer.setdefault("candidate_decisions", [])
    decision = next((item for item in decisions if item.get("candidate_id") == body.candidate_id), None)
    if dimension is None or decision is None:
        raise HTTPException(404, "Кандидат размера не найден")
    old_status = decision.get("decision") or dimension.get("status")
    decision["decision"] = body.status
    decision["reason"] = f"Ручная правка пользователя: {old_status} -> {body.status}."
    manual_adjustments = analysis.setdefault("manual_adjustments", {})
    manual_adjustments[body.candidate_id] = {
        "old_status": old_status,
        "new_status": body.status,
        "changed_at": now(),
        "source": "user_manual_edit",
    }
    review["answer"] = answer
    review["lengths"] = calculate_lengths(mapping, answer)
    analysis["dimension_review"] = review
    page["analysis"] = analysis
    trace = page.get("provider_trace") or {}
    run_db.save_feedback({
        "run_id": body.run_id,
        "line_id": body.line_id,
        "page_number": body.page_number,
        "candidate_id": body.candidate_id,
        "rating": "down",
        **_feedback_revision(page),
        "context": {
            "source_name": run.get("source_name"),
            "manual_adjustment": manual_adjustments[body.candidate_id],
            "dimension": dimension,
            "decision": decision,
            "lengths_after": review["lengths"],
            "prompt": trace.get("prompt"),
            "payload": trace.get("payload"),
            "response_raw": trace.get("response_raw"),
        },
    })
    persisted_state = STATE["runs"].get(body.run_id)
    if persisted_state:
        target_line = persisted_state.lines.get(body.line_id)
        target_page = target_line.page_results.get(body.page_number) if target_line else None
        if target_page:
            target_page.analysis = analysis
            _persist(persisted_state)
            return run_dict(persisted_state)
    run_db.save_run(run)
    return run


@app.post("/api/pipeline-length/confirm")
def confirm_pipeline_length_proposals(body: PipelineLengthConfirmationBody) -> dict[str, Any]:
    """Record only explicitly accepted provider proposals; local decisions stay intact."""
    persisted_state = STATE["runs"].get(body.run_id)
    if persisted_state:
        run_dict_value = run_dict(persisted_state)
    else:
        run_dict_value = run_db.load_run(body.run_id)
    if not run_dict_value:
        raise HTTPException(404, "Прогон не найден")
    line = next((item for item in run_dict_value.get("lines", []) if item.get("line_id") == body.line_id), None)
    analysis = (line or {}).get("analysis") or {}
    pipeline = analysis.get("pipeline_length") or {}
    provider = pipeline.get("provider_result") or {}
    if not pipeline:
        raise HTTPException(400, "Расчет длины для линии еще не готов")
    assessments = {str(item.get("candidate_id")): item for item in provider.get("candidate_assessments") or []}
    distances = provider.get("intermediate_distances") or []
    routes = {str(item.get("edge_id")): item for item in provider.get("edge_routes") or []}
    unknown_candidates = [item for item in body.candidate_ids if item not in assessments]
    unknown_local_candidates = [item for item in body.keep_local_candidate_ids if item not in assessments]
    unknown_distances = [item for item in body.intermediate_distance_indices if item < 0 or item >= len(distances)]
    unknown_edges = [item for item in body.edge_ids if item not in routes]
    if unknown_candidates or unknown_local_candidates or unknown_distances or unknown_edges:
        raise HTTPException(400, {"unknown_candidate_ids": unknown_candidates, "unknown_local_candidate_ids": unknown_local_candidates, "unknown_distance_indices": unknown_distances, "unknown_edge_ids": unknown_edges})
    previous = pipeline.get("manual_confirmation") or {}
    accepted = set(str(item) for item in previous.get("candidate_ids") or [])
    kept_local = set(str(item) for item in previous.get("keep_local_candidate_ids") or [])
    accepted.update(str(item) for item in body.candidate_ids)
    kept_local.update(str(item) for item in body.keep_local_candidate_ids)
    # A later choice for the same candidate replaces the earlier choice.
    accepted -= kept_local
    kept_local -= accepted
    distance_indices = set(int(item) for item in previous.get("intermediate_distance_indices") or [])
    distance_indices.update(int(item) for item in body.intermediate_distance_indices)
    edge_ids = set(str(item) for item in previous.get("edge_ids") or [])
    edge_ids.update(str(item) for item in body.edge_ids)
    confirmation = {
        "candidate_ids": sorted(accepted),
        "keep_local_candidate_ids": sorted(kept_local),
        "intermediate_distance_indices": sorted(distance_indices),
        "edge_ids": sorted(edge_ids),
        "candidate_assessments": [assessments[item] for item in sorted(accepted)],
        "kept_local_assessments": [assessments[item] for item in sorted(kept_local)],
        "intermediate_distances": [distances[item] for item in sorted(distance_indices)],
        "edge_routes": [routes[item] for item in sorted(edge_ids)],
        "confirmed_at": now(),
        "source": "user_manual_confirmation",
    }
    pipeline["manual_confirmation"] = confirmation
    payload = ((line or {}).get("provider_trace") or {}).get("payload")
    if payload:
        provider["calculated_lengths"] = calculate_provider_length_summary(payload, provider, confirmation)
        pipeline["provider_result"] = provider
    pipeline["applied_provider_changes"] = bool(
        confirmation["candidate_ids"] or confirmation["keep_local_candidate_ids"] or confirmation["intermediate_distance_indices"] or confirmation["edge_ids"]
    )
    analysis["pipeline_length"] = pipeline
    line["analysis"] = analysis
    if persisted_state:
        target_line = persisted_state.lines.get(body.line_id)
        if target_line:
            target_line.analysis = analysis
            _persist(persisted_state)
            return run_dict(persisted_state)
    run_dict_value["lines"] = [line if item.get("line_id") == body.line_id else item for item in run_dict_value.get("lines", [])]
    run_db.save_run(run_dict_value)
    return run_dict_value


@app.get("/api/prompts/{name}")
def get_prompt(name: str) -> dict[str, Any]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    revision = prompts.load_prompt_revision(name)
    feedback_rows = _versioned_prompt_feedback_summary()
    active_feedback = _prompt_feedback_counts(feedback_rows, name, revision)
    return {
        "name": name,
        "title": prompts.PROMPT_REGISTRY[name].get("title", name),
        "source": revision["source"],
        "version": revision["version"],
        "sha256": revision["sha256"],
        "text": revision["text"],
        "feedback": active_feedback,
        "active_feedback": active_feedback,
        "prompt_feedback": _prompt_feedback_counts(feedback_rows, name),
        "unversioned_feedback": _prompt_feedback_counts(feedback_rows, name, unversioned=True),
        "versions": _prompt_version_rows(name, revision, feedback_rows),
    }


@app.post("/api/prompts/{name}")
def save_prompt(name: str, body: PromptBody) -> dict[str, Any]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    prompts.save_prompt(name, body.text)
    return get_prompt(name)


@app.post("/api/prompts/{name}/reset")
def reset_prompt(name: str) -> dict[str, str]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    prompts.reset_prompt(name)
    return {"name": name, "source": "default"}


@app.get("/api/prompts/{name}/versions")
def list_prompt_versions(name: str) -> dict[str, Any]:
    snapshot = get_prompt(name)
    return {key: snapshot[key] for key in ("name", "versions", "unversioned_feedback")}


def _prompt_version_rows(
    name: str, active_revision: dict[str, Any], feedback_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from src import prompts

    stored_versions = prompts.list_versions(name)
    active_key = (str(active_revision.get("version", "default")), active_revision.get("sha256"))

    versions = []
    if not any(
        (str(version.get("version")), version.get("sha256")) == active_key
        for version in stored_versions
    ):
        active_feedback = _prompt_feedback_counts(feedback_rows, name, active_revision)
        active_text = str(active_revision.get("text") or "")
        versions.append({
            "version": active_revision.get("version", "default"),
            "created_at": "",
            "preview": active_text[:80],
            "length": len(active_text),
            "sha256": active_revision.get("sha256", ""),
            "source": active_revision.get("source", "default"),
            "is_active": True,
            "feedback": active_feedback,
        })

    for version in stored_versions:
        versions.append({
            **version,
            "is_active": (
                str(version.get("version")) == str(active_revision.get("version"))
                and version.get("sha256") == active_revision.get("sha256")
            ),
            "feedback": _prompt_feedback_counts(feedback_rows, name, version),
        })
    return versions


class RestoreBody(BaseModel):
    version: int


@app.post("/api/prompts/{name}/restore")
def restore_prompt_version(name: str, body: RestoreBody) -> dict[str, Any]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    try:
        prompts.restore_version(name, body.version)
    except IndexError as error:
        raise HTTPException(400, str(error)) from error
    return get_prompt(name)


@app.post("/api/runs")
def create_run(body: AnalyzeBody) -> dict[str, Any]:
    document = STATE["documents"].get(body.document_id)
    if not document:
        raise HTTPException(404, "Документ не найден")
    stop_stage = (body.stop_stage or "dimension_review").strip().lower()
    if stop_stage not in {"local_processing", "prepare", "dimensions", "lead_detection", "pipeline_length", "dimension_review"}:
        raise HTTPException(400, "stop_stage: local_processing|lead_detection|pipeline_length|dimension_review")
    if stop_stage in {"prepare", "dimensions"}:
        stop_stage = "local_processing"
    provider = (body.provider or ENV.get("AI_PROVIDER", "deepseek") or "deepseek").strip().lower()
    if provider not in {"deepseek", "codex_cli"}:
        raise HTTPException(400, "provider: deepseek|codex_cli")
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    kind, kind_label = RUN_KIND_BY_STAGE.get(stop_stage, (stop_stage, stop_stage))
    excluded_pages = set()
    for page_number in body.excluded_pages or []:
        try:
            value = int(page_number)
        except (TypeError, ValueError):
            continue
        if value > 0:
            excluded_pages.add(value)
    run = RunState(
        run_id=run_id,
        document_id=body.document_id,
        source_name=document["source_name"],
        pdf_path=document["pdf_path"],
        line_ids=list(body.line_ids),
        excluded_pages=sorted(excluded_pages),
        stop_stage=stop_stage,
        created_at=now(),
        kind=kind,
        kind_label=kind_label,
        model=ENV.get("CODEX_CLI_MODEL", "codex_cli_default") if provider == "codex_cli" else ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
        provider=provider,
    )
    for line_id in body.line_ids:
        group = _group(document, line_id)
        pages = [int(page_number) for page_number in (group.get("pages") if group else []) if int(page_number) not in excluded_pages]
        if not pages:
            continue
        run.lines[line_id] = LineRun(line_id=line_id, pages=pages)
    if not run.lines:
        raise HTTPException(400, "После исключения листов не осталось страниц для анализа")
    STATE["runs"][run_id] = run
    run_db.save_run(run_dict(run))
    context = {
        "api_key": ENV.get("DEEPSEEK_API_KEY", ""),
        "model": run.model,
        "provider": provider,
        "document": document,
        "run": run,
    }
    threading.Thread(target=_run_pipeline, args=(run_id, context), daemon=True).start()
    return {"run_id": run_id, "status": "running", "stop_stage": stop_stage, "line_ids": body.line_ids, "provider": provider}


def _persist(run: RunState) -> None:
    run_db.save_run(run_dict(run))


def _run_pipeline(run_id: str, context: dict[str, Any]) -> None:
    run: RunState = context["run"]
    document = context["document"]
    api_key = context["api_key"]
    model = context["model"]
    provider = context.get("provider", "deepseek")
    stop_stage = run.stop_stage
    pdf_path = document["pdf_path"]
    max_workers = max(1, int(os.getenv("PIPELINE_WORKERS", "4")))

    def process_page(line: LineRun, page_run: PageRun) -> None:
        page_run.status = "running"
        run_dir = ARTIFACTS_DIR / run.run_id / line.line_id
        try:
            page_run.events.append({"time": now(), "stage": "prepare", "message": "формирование чисел и вершин"})
            _persist(run)
            prepare = run_prepare(pdf_path, page_run.page_number, run_dir)
            page_run.numbers = prepare.numbers
            page_run.vertices = prepare.vertices
            page_run.coordinates = prepare.coordinates
            page_run.skipped_numbers = prepare.skipped_numbers
            page_run.files = {
                "numbers_pdf": prepare.numbers_pdf.name,
                "vertices_pdf": prepare.vertices_pdf.name,
                "coordinates_pdf": prepare.coordinates_pdf.name,
                "numbers_txt": prepare.numbers_txt.name,
            }
            components = prepare.components or []
            kept = [c for c in components if c.get("kept")]
            dropped = [c for c in components if not c.get("kept")]
            page_run.events.append({"time": now(), "stage": "prepare", "message": f"компонент оси: {len(kept)} принято, {len(dropped)} отброшено"})
            for c in kept:
                page_run.events.append({"time": now(), "stage": "prepare", "message": f"  компонент #{c['index']}: {c['nodes']} узлов, bbox {c['bbox']}"})
            for c in dropped:
                page_run.events.append({"time": now(), "stage": "prepare", "message": f"  отброшено #{c['index']}: {c['nodes']} узлов, диаг. {c['diag_px']}px"})
            page_run.stage = "prepare"
            page_run.status = "prepare_done"
            page_run.events.append({"time": now(), "stage": "prepare", "message": f"чисел {len(prepare.numbers)}, координат {len(prepare.coordinates)}, вершин {len(prepare.vertices)}"})
            _persist(run)
            if stop_stage in {"prepare", "local_processing"}:
                page_run.stage = "prepare"
                page_run.status = "prepare_done"
                _persist(run)
                if stop_stage == "prepare":
                    page_run.status = "complete"
                    _persist(run)
                    return

            if stop_stage in {"local_processing", "dimensions", "lead_detection", "pipeline_length", "dimension_review"}:
                page_run.stage = "dimensions"
                page_run.status = "running"
                page_run.events.append({"time": now(), "stage": "dimensions", "message": "локальная обработка: разметка и привязка размеров"})
                pdf_stem = Path(pdf_path).stem
                dimensions_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimensions_marked.pdf"
                dimensions_json = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimensions.json"
                pipeline_length_mode = stop_stage == "pipeline_length"
                lead_detection_mode = stop_stage == "lead_detection"
                # pipeline_length must run on the finalized local contour (VE/HG/F-*),
                # so only lead_detection keeps the pre-final snapshot.
                mapping = run_dimension_mapping(
                    pdf_path,
                    page_run.page_number,
                    dimensions_pdf,
                    dimensions_json,
                    finalize=not lead_detection_mode,
                )
                mapping["coordinates"] = list(page_run.coordinates or [])
                if lead_detection_mode:
                    lead_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_lead_detection.pdf"
                    save_dimension_lead_detection_pdf(pdf_path, page_run.page_number, lead_pdf, mapping)
                    page_run.analysis = {"lead_detection": mapping}
                    page_run.files["dimensions_pdf"] = dimensions_pdf.name
                    page_run.files["dimensions_json"] = dimensions_json.name
                    page_run.files["lead_detection_pdf"] = lead_pdf.name
                    page_run.stage = "lead_detection"
                    page_run.status = "complete"
                    page_run.events.append({"time": now(), "stage": "lead_detection", "message": "PDF диагностики lead-стрелок готов"})
                    _persist(run)
                    return
                if pipeline_length_mode:
                    with fitz.open(str(pdf_path)) as source_document:
                        attach_coordinate_leads(
                            source_document[page_run.page_number - 1],
                            mapping,
                        )
                    diagnostic_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_pipeline_length_diagnostic.pdf"
                    save_pipeline_length_diagnostic_pdf(pdf_path, page_run.page_number, diagnostic_pdf, mapping)
                    overview_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_local_markup_overview.pdf"
                    save_local_markup_overview_pdf(pdf_path, page_run.page_number, overview_pdf, mapping)
                    page_run.analysis = {"pipeline_length_local": mapping}
                    page_run.files["dimensions_pdf"] = dimensions_pdf.name
                    page_run.files["dimensions_json"] = dimensions_json.name
                    page_run.files["pipeline_length_diagnostic_pdf"] = diagnostic_pdf.name
                    page_run.files["local_markup_overview_pdf"] = overview_pdf.name
                    page_run.stage = "pipeline_length"
                    page_run.status = "local_complete"
                    page_run.events.append({"time": now(), "stage": "pipeline_length", "message": "локальный снимок готов, ожидается общий расчет по линии"})
                    _persist(run)
                    return
                with fitz.open(str(pdf_path)) as source_document:
                    mapping["handwheels"] = _handwheel_details(
                        source_document[page_run.page_number - 1],
                        mapping,
                    )
                mapping["handwheel_edges"] = [
                    {
                        "id": item["edge_id"],
                        "edge_kind": item.get("edge_kind"),
                        "is_pipe_edge": item.get("is_pipe_edge", False),
                        "handwheel_id": item["id"],
                        "arrow_start": item.get("arrow_start"),
                        "arrow_end": item.get("arrow_end"),
                        "reason": item.get("edge_reason", "Привязка штурвала к существующему ребру."),
                    }
                    for item in mapping["handwheels"]
                    if item.get("edge_id")
                ]
                preprocess_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_preprocess_annotations.pdf"
                save_preprocess_annotation_pdf(pdf_path, page_run.page_number, preprocess_pdf, mapping)
                clean_markup_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_clean_local_markup.pdf"
                save_clean_local_markup_pdf(pdf_path, page_run.page_number, clean_markup_pdf, mapping)
                local_filter_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_local_dimension_filter.pdf"
                save_local_dimension_filter_pdf(pdf_path, page_run.page_number, local_filter_pdf, mapping)
                final_contour_rays_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_final_contour_rays.pdf"
                save_final_contour_rays_pdf(pdf_path, page_run.page_number, final_contour_rays_pdf, mapping)
                clean_graph_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_graph.pdf"
                save_clean_graph_pdf(pdf_path, page_run.page_number, clean_graph_pdf, mapping)
                skeleton_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_skeleton.pdf"
                save_skeleton_pdf(pdf_path, page_run.page_number, skeleton_pdf, mapping)
                dimension_map_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_map.pdf"
                dimension_map_json = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_map.json"
                dimension_map = build_dimension_map(Path(pdf_path), page_run.page_number)
                # build_dimension_map is the final source of truth for the
                # provider payload. It already contains remapped handwheel
                # edges and connections; do not overwrite them with the
                # pre-split local mapping here.
                for key in (
                    "final_edge_candidate_groups",
                    "unresolved_final_candidates",
                    "final_assignment_summary",
                ):
                    if mapping.get(key) is not None:
                        dimension_map[key] = mapping.get(key)
                dimension_map_json.write_text(json.dumps(dimension_map, ensure_ascii=False, indent=2), encoding="utf-8")
                render_dimension_map(Path(pdf_path), page_run.page_number, dimension_map_pdf, dimension_map)
                page_run.analysis = {"dimension_mapping": mapping, "dimension_map": dimension_map}
                page_run.files["dimensions_pdf"] = dimensions_pdf.name
                page_run.files["dimensions_json"] = dimensions_json.name
                page_run.files["preprocess_annotations_pdf"] = preprocess_pdf.name
                page_run.files["clean_local_markup_pdf"] = clean_markup_pdf.name
                page_run.files["local_dimension_filter_pdf"] = local_filter_pdf.name
                page_run.files["final_contour_rays_pdf"] = final_contour_rays_pdf.name
                page_run.files["dimension_graph_pdf"] = clean_graph_pdf.name
                page_run.files["dimension_skeleton_pdf"] = skeleton_pdf.name
                page_run.files["dimension_map_pdf"] = dimension_map_pdf.name
                page_run.files["dimension_map_json"] = dimension_map_json.name
                if stop_stage == "dimension_review":
                    map_text = build_map_text(dimension_map)
                    dimension_map_txt = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_map.txt"
                    review_json = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_review.json"
                    dimension_map_txt.write_text(map_text, encoding="utf-8")
                    review_pdf_path = run_dir / f"{pdf_stem}_page{page_run.page_number}_clean_local_markup.pdf"
                    if not review_pdf_path.exists():
                        review_pdf_path = Path(pdf_path)
                    review_trace = review_map_with_provider(
                        dimension_map,
                        map_text,
                        api_key,
                        model,
                        pdf_path=review_pdf_path,
                        page_number=page_run.page_number,
                        provider=provider,
                    )
                    review = review_trace["answer"]
                    page_run.analysis["dimension_review"] = {
                        "answer": review,
                        "lengths": calculate_lengths(dimension_map, review),
                        "model": review_trace["model"],
                    }
                    review_json.write_text(json.dumps(page_run.analysis["dimension_review"], ensure_ascii=False, indent=2), encoding="utf-8")
                    page_run.provider_trace = {
                        "prompt": review_trace["prompt"],
                        "payload": review_trace["payload"],
                        "response_raw": review_trace["response_raw"],
                        "answer": review,
                        "model": review_trace["model"],
                        "provider": review_trace.get("provider", provider),
                        "prompt_name": "dimension_review",
                        "prompt_version": review_trace["prompt_version"],
                        "prompt_sha256": review_trace["prompt_sha256"],
                        "prompt_source": review_trace["prompt_source"],
                    }
                    page_run.prompt_revision = {
                        "name": "dimension_review",
                        "version": review_trace["prompt_version"],
                        "sha256": review_trace["prompt_sha256"],
                        "source": review_trace["prompt_source"],
                    }
                    eval_result = evaluate_line_for_prompt(line.line_id, "dimension_review", review, sheet=page_run.page_number)
                    if eval_result:
                        page_run.analysis["dimension_review"]["eval"] = aggregate_eval_results([
                            result for group_result in eval_result.values() for result in [group_result]
                        ])
                    page_run.files["dimension_map_txt"] = dimension_map_txt.name
                    page_run.files["dimension_review_json"] = review_json.name
                page_run.stage = "done"
                page_run.status = "complete"
                page_run.events.append({"time": now(), "stage": "done", "message": f"размеров: {len(mapping['dimensions'])}, рёбер: {len(mapping['edges'])}"})
                _persist(run)
                return

            raise RuntimeError(f"Неизвестный этап анализа: {stop_stage}")
        except Exception as error:  # noqa: BLE001
            page_run.status = "error"
            page_run.error = str(error)
            page_run.events.append({"time": now(), "stage": "error", "message": str(error)})
            _persist(run)

    futures = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for line in run.lines.values():
            for page_number in line.pages:
                page_run = PageRun(page_number=page_number)
                line.page_results[page_number] = page_run
                futures.append(executor.submit(process_page, line, page_run))
            _persist(run)
        for future in futures:
            future.result()

    if stop_stage == "pipeline_length":
        for line in run.lines.values():
            local_pages = [
                (page_run.page_number, (page_run.analysis or {}).get("pipeline_length_local"))
                for page_run in line.page_results.values()
                if (page_run.analysis or {}).get("pipeline_length_local")
            ]
            if not local_pages or any(page_run.status == "error" for page_run in line.page_results.values()):
                line.status = "error"
                line.error = "Не удалось собрать локальные данные для расчета длины"
                continue
            try:
                line.events.append({"time": now(), "stage": "pipeline_length", "message": "сбор payload по всем листам линии"})
                payload = build_pipeline_length_payload(line.line_id, run.source_name, local_pages)
                provider_payload = build_pipeline_length_provider_payload(payload)
                line.analysis = {
                    "pipeline_length": {
                        "payload_summary": {
                            "analysis_type": payload["analysis_type"],
                            "line_id": payload["line_id"],
                            "pages": payload["cross_page_context"]["page_order"],
                        },
                    }
                }
                line_dir = ARTIFACTS_DIR / run.run_id / line.line_id
                payload_path = line_dir / f"{Path(run.source_name).stem}_{line.line_id}_pipeline_length_payload.json"
                text_path = line_dir / f"{Path(run.source_name).stem}_{line.line_id}_pipeline_length.txt"
                response_path = line_dir / f"{Path(run.source_name).stem}_{line.line_id}_pipeline_length_response.json"
                axis_log_path = line_dir / f"{Path(run.source_name).stem}_{line.line_id}_axis_sign.jsonl"
                axis_log_rows = []
                for page in payload.get("pages") or []:
                    reconstruction = page.get("coordinate_reconstruction") or {}
                    diagnostics = reconstruction.get("_axis_sign_diagnostics") or []
                    for item in diagnostics:
                        axis_log_rows.append(json.dumps({"run_id": run.run_id, "page": page.get("page"), **item}, ensure_ascii=False))
                    axis_log_rows.append(json.dumps({
                        "run_id": run.run_id,
                        "page": page.get("page"),
                        "rule": "summary",
                        "edges_total": len(diagnostics),
                        "axis_filled": sum(1 for item in diagnostics if item.get("axis") is not None),
                        "axis_null": sum(1 for item in diagnostics if item.get("axis") is None),
                        "inconsistent": sum(1 for item in diagnostics if item.get("inconsistent")),
                        "ambiguous": sum(1 for item in diagnostics if item.get("ambiguous")),
                    }, ensure_ascii=False))
                    axis_log_rows.append(json.dumps({
                        "run_id": run.run_id,
                        "page": page.get("page"),
                        "axis_map": reconstruction.get("_axis_map") or {},
                    }, ensure_ascii=False))
                axis_log_path.write_text("\n".join(axis_log_rows) + ("\n" if axis_log_rows else ""), encoding="utf-8")
                payload_path.write_text(json.dumps(provider_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                text_path.write_text(build_pipeline_length_text(provider_payload), encoding="utf-8")
                line.events.append({"time": now(), "stage": "pipeline_length", "message": f"отправка payload провайдеру по листам ({provider})"})
                _persist(run)
                page_traces = []
                for page in payload.get("pages") or []:
                    page_payload = build_pipeline_length_page_payload(payload, page)
                    page_number = page.get("page")
                    page_run = line.page_results.get(int(page_number)) if str(page_number).isdigit() else None
                    page_prefix = f"{Path(run.source_name).stem}_{line.line_id}_лист{page_number}_pipeline_length"
                    page_payload_path = line_dir / f"{page_prefix}_payload.json"
                    page_prompt_path = line_dir / f"{page_prefix}_prompt.txt"
                    page_response_path = line_dir / f"{page_prefix}_response.json"
                    page_payload_path.write_text(json.dumps(page_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    prompt_revision = prompts.load_prompt_revision("pipeline_length")
                    page_prompt_path.write_text(prompt_revision["text"], encoding="utf-8")
                    line.events.append({"time": now(), "stage": "pipeline_length", "message": f"лист {page.get('page')}: запрос провайдеру"})
                    _persist(run)
                    try:
                        page_trace = run_pipeline_length_provider(
                            page_payload,
                            api_key,
                            model,
                            provider=provider,
                            pdf_path=pdf_path,
                            page_number=page.get("page"),
                        )
                    except Exception as error:  # noqa: BLE001
                        error_text = str(error)
                        page_trace = {
                            "answer": {"error": error_text},
                            "error": error_text,
                            "status": "error",
                            "provider": provider,
                            "model": model,
                            "payload": page_payload,
                            "prompt": prompt_revision["text"],
                            "prompt_name": prompt_revision["name"],
                            "prompt_version": prompt_revision["version"],
                            "prompt_sha256": prompt_revision["sha256"],
                            "prompt_source": prompt_revision["source"],
                        }
                        line.events.append({"time": now(), "stage": "pipeline_length", "message": f"лист {page_number}: ошибка провайдера: {error_text}"})
                    page_response_path.write_text(json.dumps(page_trace, ensure_ascii=False, indent=2), encoding="utf-8")
                    if page_run is not None:
                        page_run.provider_trace = page_trace
                        page_run.prompt_revision = {
                            "name": prompt_revision["name"],
                            "version": prompt_revision["version"],
                            "sha256": prompt_revision["sha256"],
                            "source": prompt_revision["source"],
                        }
                        page_run.files.update({
                            "pipeline_length_payload_json": page_payload_path.name,
                            "pipeline_length_prompt_txt": page_prompt_path.name,
                            "pipeline_length_response_json": page_response_path.name,
                        })
                        if page_trace.get("error"):
                            page_run.status = "error"
                            page_run.error = page_trace["error"]
                    page_traces.append(page_trace)
                    line.events.append({
                        "time": now(),
                        "stage": "pipeline_length",
                        "message": f"лист {page.get('page')}: ответ получен за {page_trace.get('elapsed_seconds')} с",
                    })
                    _persist(run)
                trace = merge_pipeline_length_provider_traces(provider_payload, page_traces)
                line.events.append({"time": now(), "stage": "pipeline_length", "message": "ответы провайдера по листам собраны, разбор результата"})
                result = build_pipeline_length_result(payload, trace["answer"])
                response_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                line.analysis["pipeline_length"] = result
                line.provider_trace = trace
                line.files = {
                    "pipeline_length_payload_json": payload_path.name,
                    "pipeline_length_text": text_path.name,
                    "pipeline_length_response_json": response_path.name,
                    "pipeline_length_axis_sign_jsonl": axis_log_path.name,
                }
                for page_run in line.page_results.values():
                    page_run.stage = "pipeline_length"
                    if page_run.error:
                        page_run.status = "error"
                        page_run.events.append({"time": now(), "stage": "pipeline_length", "message": f"ошибка провайдера сохранена вместе с payload и промптом: {page_run.error}"})
                    else:
                        page_run.status = "complete"
                        page_run.events.append({"time": now(), "stage": "pipeline_length", "message": "расчет линии готов, предложения требуют ручного подтверждения"})
                page_errors = [page_run.error for page_run in line.page_results.values() if page_run.error]
                line.status = "error" if page_errors else "complete"
                if page_errors:
                    line.error = "; ".join(page_errors)
            except Exception as error:  # noqa: BLE001
                line.status = "error"
                line.error = str(error)
                line.events.append({"time": now(), "stage": "pipeline_length", "message": f"ошибка провайдера: {error}"})
                for page_run in line.page_results.values():
                    page_run.status = "error"
                    page_run.error = str(error)
            _persist(run)
        statuses = {line.status for line in run.lines.values()}
        run.status = "error" if "error" in statuses else "complete"
        _persist(run)
        return

    for line in run.lines.values():
        statuses = {page_run.status for page_run in line.page_results.values()}
        if not statuses:
            line.status = "error"
        elif statuses <= {"complete"}:
            line.status = "complete"
        elif "error" in statuses:
            line.status = "error"
        else:
            line.status = "partial"
    statuses = {line.status for line in run.lines.values()}
    run.status = "error" if "error" in statuses else "complete"
    _persist(run)


def _eval_history_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in run_db.list_runs():
        for line in run.get("lines", []):
            line_id = str(line.get("line_id") or "")
            for page in line.get("page_results", []):
                review = (page.get("analysis") or {}).get("dimension_review")
                if not isinstance(review, dict):
                    continue
                answer = review.get("answer")
                if not isinstance(answer, dict):
                    continue
                eval_result = evaluate_line_for_prompt(
                    line_id,
                    "dimension_review",
                    answer,
                    sheet=page.get("page_number"),
                )
                rows.extend(eval_result.values())
    return rows


@app.get("/api/eval")
def list_eval_summary() -> dict[str, Any]:
    from src import prompts

    prompt_versions: list[dict[str, Any]] = []
    for prompt_name in sorted(prompts.PROMPT_REGISTRY):
        history_rows = [row for row in _eval_history_rows() if row.get("prompt_name") == prompt_name]
        if not history_rows:
            continue

        versions = prompts.list_versions(prompt_name)
        if not versions:
            revision = prompts.load_prompt_revision(prompt_name)
            versions = [{
                "version": revision.get("version", "default"),
                "sha256": revision.get("sha256", ""),
                "source": revision.get("source", "default"),
                "created_at": "",
                "length": len(str(revision.get("text", ""))),
                "preview": str(revision.get("text", ""))[:80],
            }]

        for version in versions:
            stats = summarize_prompt_eval_rows(history_rows, prompt_name)
            if not stats or int(stats.get("total_candidates") or 0) <= 0:
                continue
            prompt_versions.append({
                "prompt_name": prompt_name,
                "title": prompts.PROMPT_REGISTRY[prompt_name].get("title", prompt_name),
                "version": version.get("version", "default"),
                "sha256": version.get("sha256", ""),
                "source": version.get("source", "default"),
                "created_at": version.get("created_at", ""),
                "length": version.get("length", 0),
                "stats": stats,
            })

    return {
        "groups": sorted(list_eval_groups(), key=lambda value: str(value)),
        "prompt_versions": prompt_versions,
        "total_versions": len(prompt_versions),
    }


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    return run_db.list_runs()


def _run_progress_from_state(run: RunState) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "source_name": run.source_name,
        "stop_stage": run.stop_stage,
        "provider": run.provider,
        "model": run.model,
        "kind": run.kind,
        "kind_label": run.kind_label,
        "line_ids": run.line_ids,
        "excluded_pages": run.excluded_pages,
        "lines": [
            {
                "line_id": line.line_id,
                "pages": line.pages,
                "status": line.status,
                "error": line.error,
                "events": line.events[-20:],
                "page_results": [
                    {
                        "page_number": page_run.page_number,
                        "status": page_run.status,
                        "stage": page_run.stage,
                        "error": page_run.error,
                        "events": page_run.events[-12:],
                    }
                    for page_run in sorted(line.page_results.values(), key=lambda item: item.page_number)
                ],
            }
            for line in run.lines.values()
        ],
    }


def _run_progress_from_dict(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "source_name": run.get("source_name"),
        "stop_stage": run.get("stop_stage"),
        "provider": run.get("provider"),
        "model": run.get("model"),
        "kind": run.get("kind"),
        "kind_label": run.get("kind_label"),
        "line_ids": run.get("line_ids") or [],
        "excluded_pages": run.get("excluded_pages") or [],
        "lines": [
            {
                "line_id": line.get("line_id"),
                "pages": line.get("pages") or [],
                "status": line.get("status"),
                "error": line.get("error", ""),
                "events": (line.get("events") or [])[-20:],
                "page_results": [
                    {
                        "page_number": page.get("page_number"),
                        "status": page.get("status"),
                        "stage": page.get("stage"),
                        "error": page.get("error", ""),
                        "events": (page.get("events") or [])[-12:],
                    }
                    for page in line.get("page_results", [])
                ],
            }
            for line in run.get("lines", [])
        ],
    }


@app.get("/api/runs/{run_id}/progress")
def get_run_progress(run_id: str) -> dict[str, Any]:
    run = STATE["runs"].get(run_id)
    if run:
        return _run_progress_from_state(run)
    persisted = run_db.load_run(run_id)
    if not persisted:
        raise HTTPException(404, "Прогон не найден")
    return _run_progress_from_dict(persisted)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = STATE["runs"].get(run_id)
    if run:
        return _with_current_eval(run_dict(run))
    persisted = run_db.load_run(run_id)
    if not persisted:
        raise HTTPException(404, "Прогон не найден")
    return _with_current_eval(persisted)


def _with_current_eval(run: dict[str, Any]) -> dict[str, Any]:
    for line in run.get("lines", []):
        line_id = str(line.get("line_id") or "")
        for page in line.get("page_results", []):
            review = ((page.get("analysis") or {}).get("dimension_review") or {})
            answer = review.get("answer")
            if not isinstance(answer, dict):
                continue
            eval_result = evaluate_line_for_prompt(
                line_id,
                "dimension_review",
                answer,
                sheet=page.get("page_number"),
            )
            if eval_result:
                review["eval"] = aggregate_eval_results(list(eval_result.values()))
    return run


def _run_export_rows(run: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    pages = []
    events = []
    errors = []
    numbers = []
    vertices = []
    coordinates = []
    analyses = []
    pipeline_totals = []
    branch_rows = []
    local_decisions = []
    provider_reviews = []
    page_length_rows = []
    handwheels = []
    cross_sheet = []
    line_rows = []
    point_rows = []
    segment_rows = []
    element_rows = []
    uncertainty_rows = []
    for line in run.get("lines", []):
        line_pipeline = (line.get("analysis") or {}).get("pipeline_length") or {}
        line_local = line_pipeline.get("local_result") or {}
        line_provider = line_pipeline.get("provider_result") or {}
        line_lengths = line_provider.get("calculated_lengths") or line_provider.get("lengths") or {}
        local_lengths = line_local.get("main") or {}
        local_branch = line_local.get("branch") or {}
        line_pages = [page.get("page_number") for page in line.get("page_results", [])]
        line_files = line.get("files") or {}
        provider_file = line_files.get("pipeline_length_response_json", "")
        support_count = 0
        valve_count = 0
        for page in line.get("page_results", []):
            page_analysis = page.get("analysis") or {}
            page_elements = page_analysis.get("elements") or []
            support_count += sum(1 for item in page_elements if str(item.get("element_type") or item.get("type") or "").lower() == "support")
            valve_count += sum(1 for item in page_elements if str(item.get("element_type") or item.get("type") or "").lower() in {"valve", "handwheel", "valve / handwheel"})
            local_page = page_analysis.get("pipeline_length_local") or {}
            valve_count += len((local_page.get("handwheel_annotations") or {}).get("handwheels") or [])
        line_rows.append({
            "line_id": line.get("line_id"),
            "pages": ", ".join(str(page) for page in line_pages),
            "local_main_length_mm": local_lengths.get("clean_length_mm") or line_local.get("clean_length_mm"),
            "local_branch_length_mm": local_branch.get("clean_length_mm", 0.0),
            "local_total_length_mm": line_local.get("clean_length_mm"),
            "main_length_mm": (line_lengths.get("main") or {}).get("clean_length_mm"),
            "branch_length_mm": (line_lengths.get("branch") or {}).get("clean_length_mm"),
            "total_length_mm": line_lengths.get("total_clean_length_mm"),
            "provider_minus_local_mm": ((line_lengths.get("total_clean_length_mm") or 0) - (line_local.get("clean_length_mm") or 0)) if line_pipeline else None,
            "supports_count": support_count,
            "valves_count": valve_count,
            "completeness_status": line.get("status"),
            "error": line.get("error", ""),
            "pipeline_payload": line_files.get("pipeline_length_payload_json", ""),
            "provider_response": provider_file,
            "provider_elapsed_seconds": (line.get("provider_trace") or {}).get("elapsed_seconds"),
            "provider_total_tokens": (line.get("provider_trace") or {}).get("total_tokens"),
            "provider_fixes": len((line_pipeline.get("manual_confirmation") or {}).get("candidate_ids") or []),
            "local_kept": len((line_pipeline.get("manual_confirmation") or {}).get("keep_local_candidate_ids") or []),
        })
        for page in line.get("page_results", []):
            base = {
                "line_id": line.get("line_id"),
                "page_number": page.get("page_number"),
                "status": page.get("status"),
                "stage": page.get("stage"),
                "error": page.get("error", ""),
                "numbers_count": len(page.get("numbers", [])),
                "vertices_count": len(page.get("vertices", [])),
                "diagnostic_pdf": (page.get("files") or {}).get("pipeline_length_diagnostic_pdf", ""),
            }
            pages.append(base)
            for item in page.get("numbers", []):
                numbers.append({"line_id": line.get("line_id"), "page_number": page.get("page_number"), **item})
            for item in page.get("vertices", []):
                vertices.append({"line_id": line.get("line_id"), "page_number": page.get("page_number"), **item})
                point_rows.append({
                    "point_id": item.get("id"),
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "purpose": item.get("role"),
                    "x": item.get("x"),
                    "y": item.get("y"),
                    "z": item.get("z"),
                    "coordinate_source": item.get("source") or "base vertex geometry",
                    "source_area": json.dumps(item.get("bbox"), ensure_ascii=False) if item.get("bbox") else "",
                    "diagnostic_pdf": base["diagnostic_pdf"],
                })
            for item in page.get("coordinates", []):
                coordinates.append({"line_id": line.get("line_id"), "page_number": page.get("page_number"), **item})
            if page.get("analysis"):
                analyses.append({
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "analysis_json": json.dumps(page["analysis"], ensure_ascii=False),
                })
            if page.get("error"):
                errors.append(base)
            for event in page.get("events", []):
                events.append({
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "time": event.get("time", ""),
                    "stage": event.get("stage", ""),
                    "message": event.get("message", ""),
                })
            local_snapshot = (page.get("analysis") or {}).get("pipeline_length_local") or {}
            pipeline_page_result = (line_pipeline.get("local_result") or {}).get("page_summaries") or []
            page_summary = next((item for item in pipeline_page_result if item.get("page") == page.get("page_number")), None)
            if page_summary:
                page_length_rows.append({
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "local_clean_mm": page_summary.get("clean_length_mm"),
                    "local_dirty_mm": page_summary.get("dirty_length_mm"),
                    "local_ambiguous_mm": page_summary.get("ambiguous_length_mm"),
                    "diagnostic_pdf": base["diagnostic_pdf"],
                })
            base_edges = local_snapshot.get("base_edges") or local_snapshot.get("edges") or []
            dimensions_by_edge = {}
            for dimension in local_snapshot.get("dimensions") or []:
                if dimension.get("edge_id"):
                    dimensions_by_edge.setdefault(dimension.get("edge_id"), []).append(dimension)
            for edge in base_edges:
                edge_id = edge.get("id")
                source_dimensions = dimensions_by_edge.get(edge_id) or []
                segment_rows.append({
                    "segment_id": f"{line.get('line_id')}:{page.get('page_number')}:{edge_id}",
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "from_point": edge.get("from_vertex") or edge.get("from_node_id"),
                    "to_point": edge.get("to_vertex") or edge.get("to_node_id"),
                    "dn": edge.get("dn"),
                    "length_mm": round(sum(float(item.get("value_mm") or item.get("value") or 0.0) for item in source_dimensions if (item.get("local_decision") or {}).get("decision", item.get("local_filter_decision")) == "include"), 2) or None,
                    "pixel_length": edge.get("pixel_length"),
                    "source_dimensions": ", ".join(str(item.get("id")) for item in source_dimensions),
                    "source_dimension_values": ", ".join(str(item.get("value")) for item in source_dimensions),
                    "source_page": page.get("page_number"),
                    "diagnostic_pdf": base["diagnostic_pdf"],
                })
            for item in (local_snapshot.get("handwheel_annotations") or {}).get("handwheels") or []:
                handwheels.append({
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "handwheel_id": item.get("id"),
                    "label": item.get("label"),
                    "arrow_found": item.get("arrow_found"),
                    "edge_id": item.get("edge_id"),
                    "arrow_end": json.dumps(item.get("arrow_end"), ensure_ascii=False),
                })
                element_rows.append({
                    "element_id": item.get("id"),
                    "element_type": "valve / handwheel",
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "attached_edge": item.get("edge_id"),
                    "attached_point": "",
                    "x": (item.get("arrow_end") or [None, None])[0],
                    "y": (item.get("arrow_end") or [None, None])[1],
                    "z": None,
                    "source_area": json.dumps(item.get("bbox"), ensure_ascii=False) if item.get("bbox") else "",
                    "diagnostic_pdf": base["diagnostic_pdf"],
                })
            for dimension in local_snapshot.get("dimensions") or []:
                local_decisions.append({
                    "line_id": line.get("line_id"),
                    "page_number": page.get("page_number"),
                    "candidate_id": dimension.get("id"),
                    "value_mm": dimension.get("value"),
                    "decision": dimension.get("local_filter_decision"),
                    "reason": dimension.get("local_filter_reason"),
                    "conflict_with": dimension.get("local_filter_conflict_with"),
                    "source_edge": dimension.get("edge_id"),
                    "bbox": json.dumps(dimension.get("bbox"), ensure_ascii=False) if dimension.get("bbox") else "",
                    "diagnostic_pdf": base["diagnostic_pdf"],
                })
                if dimension.get("local_filter_decision") == "ambiguous":
                    uncertainty_rows.append({
                        "line_id": line.get("line_id"),
                        "page_number": page.get("page_number"),
                        "object_id": dimension.get("id"),
                        "object_type": "dimension candidate",
                        "reason": dimension.get("local_filter_reason") or "ambiguous local decision",
                        "missing_information": "manual review",
                    })
        pipeline = (line.get("analysis") or {}).get("pipeline_length") or {}
        if pipeline:
            local = pipeline.get("local_result") or {}
            provider = pipeline.get("provider_result") or {}
            calculated = provider.get("calculated_lengths") or provider.get("lengths") or {}
            pipeline_totals.append({
                "line_id": line.get("line_id"),
                "local_clean_mm": local.get("clean_length_mm"),
                "local_dirty_mm": local.get("dirty_length_mm"),
                "local_ambiguous_mm": local.get("ambiguous_length_mm"),
                "provider_clean_mm": calculated.get("total_clean_length_mm"),
                "provider_dirty_mm": calculated.get("total_dirty_length_mm"),
                "provider_ambiguous_mm": calculated.get("total_ambiguous_length_mm"),
                "manual_confirmation": json.dumps(pipeline.get("manual_confirmation") or {}, ensure_ascii=False),
                "payload_file": (line.get("files") or {}).get("pipeline_length_payload_json", ""),
                "response_file": (line.get("files") or {}).get("pipeline_length_response_json", ""),
            })
            for branch in calculated.get("branches") or []:
                branch_rows.append({
                    "line_id": line.get("line_id"),
                    "branch_id": branch.get("branch_id"),
                    "name": branch.get("name"),
                    "junction_vertex_id": branch.get("junction_vertex_id"),
                    "endpoint_vertex_id": branch.get("endpoint_vertex_id"),
                    "edge_ids": ", ".join(str(item) for item in branch.get("edge_ids") or []),
                    "candidate_ids": ", ".join(str(item) for item in branch.get("candidate_ids") or []),
                    "clean_length_mm": branch.get("clean_length_mm"),
                    "dirty_length_mm": branch.get("dirty_length_mm"),
                    "ambiguous_length_mm": branch.get("ambiguous_length_mm"),
                })
            for item in provider.get("vertex_coordinates") or []:
                point_rows.append({
                    "point_id": item.get("vertex_id"),
                    "line_id": line.get("line_id"),
                    "page_number": item.get("page"),
                    "purpose": "provider_vertex_coordinate",
                    "x": item.get("x"),
                    "y": item.get("y"),
                    "z": item.get("z"),
                    "coordinate_source": item.get("method") or "provider",
                    "source_area": json.dumps(item.get("source_coordinate_labels") or [], ensure_ascii=False),
                    "diagnostic_pdf": "",
                    "confidence": item.get("confidence"),
                    "reason": item.get("reason"),
                })
            estimates = local.get("candidate_estimates") or []
            if not estimates:
                payload_pages = ((line.get("provider_trace") or {}).get("payload") or {}).get("pages") or []
                estimates = [
                    {
                        "candidate_id": dimension.get("id"),
                        "candidate_key": dimension.get("candidate_key"),
                        "page": page_data.get("page"),
                        "value_mm": dimension.get("value_mm"),
                        "local_decision": (dimension.get("local_decision") or {}).get("decision"),
                        "reason": (dimension.get("local_decision") or {}).get("reason"),
                    }
                    for page_data in payload_pages
                    for dimension in page_data.get("dimensions") or []
                ]
            reviews = {str(item.get("candidate_id")): item for item in provider.get("candidate_assessments") or []}
            manual = pipeline.get("manual_confirmation") or {}
            selected_provider = {str(item) for item in manual.get("candidate_ids") or []}
            selected_local = {str(item) for item in manual.get("keep_local_candidate_ids") or []}
            for estimate in estimates:
                key = str(estimate.get("candidate_key") or estimate.get("candidate_id"))
                review = reviews.get(key) or next((item for candidate_id, item in reviews.items() if candidate_id.endswith(":" + str(estimate.get("candidate_id")))), {})
                manual_choice = "provider" if key in selected_provider else ("local" if key in selected_local else "")
                provider_decision = estimate.get("local_decision")
                if review.get("proposed_decision"):
                    provider_decision = review.get("proposed_decision")
                elif review.get("assessment") in {"disputed", "insufficient"}:
                    provider_decision = "ambiguous"
                provider_reviews.append({
                    "line_id": line.get("line_id"),
                    "page_number": estimate.get("page"),
                    "candidate_id": key,
                    "value_mm": estimate.get("value_mm"),
                    "local_decision": estimate.get("local_decision"),
                    "local_reason": estimate.get("reason"),
                    "provider_assessment": review.get("assessment"),
                    "provider_proposed_decision": review.get("proposed_decision"),
                    "provider_reason": review.get("reason"),
                    "provider_effective_decision": provider_decision,
                    "manual_choice": manual_choice,
                    "counted_locally_mm": estimate.get("value_mm") if estimate.get("local_decision") == "include" else 0,
                    "counted_provider_mm": estimate.get("value_mm") if provider_decision == "include" else 0,
                })
            cross_sheet_items = list(
                provider.get("cross_sheet_measurements")
                or provider.get("intermediate_distances")
                or []
            )
            for item in provider.get("cross_sheet_links") or []:
                cross_sheet_items.append({"kind": "cross_sheet_link", **item})
            for item in cross_sheet_items:
                cross_sheet.append({"line_id": line.get("line_id"), **item})
            for item in provider.get("candidate_assessments") or []:
                if item.get("assessment") in {"disputed", "insufficient"}:
                    uncertainty_rows.append({
                        "line_id": line.get("line_id"),
                        "page_number": str(item.get("candidate_id", "")).split(":", 1)[0],
                        "object_id": item.get("candidate_id"),
                        "object_type": "provider review",
                        "reason": item.get("reason"),
                        "missing_information": item.get("proposed_decision") or item.get("assessment"),
                    })
            trace_answer = (line.get("provider_trace") or {}).get("answer")
            if trace_answer:
                analyses.append({
                    "line_id": line.get("line_id"),
                    "page_number": "line",
                    "analysis_json": json.dumps(trace_answer, ensure_ascii=False),
                })
    return {
        "Страницы": pages,
        "Числа": numbers,
        "Вершины": vertices,
        "Координаты": coordinates,
        "Анализ": analyses,
        "События": events,
        "Ошибки": errors,
        "Расчет длины": pipeline_totals,
        "Ответвления": branch_rows,
        "Расчет по листам": page_length_rows,
        "Локальные решения": local_decisions,
        "Кандидаты длины": provider_reviews,
        "Сравнение": provider_reviews,
        "Штурвалы": handwheels,
        "Межлистовые": cross_sheet,
        "Линии": line_rows,
        "Итоги": line_rows,
        "Точки": point_rows,
        "Участки": segment_rows,
        "Элементы": element_rows,
        "Неопределенности": uncertainty_rows,
    }


@app.get("/api/runs/{run_id}/export/json")
def export_run_json(run_id: str) -> Response:
    run = get_run(run_id)
    content = json.dumps(run, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(content, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})


@app.get("/api/runs/{run_id}/export/excel")
def export_run_excel(run_id: str) -> StreamingResponse:
    import pandas as pd

    run = get_run(run_id)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([{
            "run_id": run.get("run_id"),
            "source_name": run.get("source_name"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "provider": run.get("provider"),
            "model": run.get("model"),
            "stop_stage": run.get("stop_stage"),
        }]).to_excel(writer, sheet_name="Сводка", index=False)
        for sheet, rows in _run_export_rows(run).items():
            pd.DataFrame(rows or [{"status": "Нет данных"}]).to_excel(writer, sheet_name=sheet, index=False)
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'})


@app.get("/api/runs/{run_id}/export/review-data")
def export_review_data(run_id: str) -> StreamingResponse:
    run = get_run(run_id)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for line in run.get("lines", []):
            line_id = str(line.get("line_id") or "line")
            for page in line.get("page_results", []):
                page_number = page.get("page_number") or "page"
                prefix = "_".join([
                    _safe_export_part(line_id),
                    f"лист{_safe_export_part(page_number)}",
                ])
                files = page.get("files") or {}
                # Review-data must contain the complete local annotation layer,
                # including all diagnostic overlays, not the reduced clean view.
                markup_name = files.get("local_markup_overview_pdf") or files.get("clean_local_markup_pdf")
                markup_path = ARTIFACTS_DIR / run_id / line_id / markup_name if markup_name else None
                if markup_path and markup_path.exists():
                    images = pdf_pages_to_png(markup_path, max_pages=1, zoom=1.6)
                    if images:
                        archive.writestr(f"{prefix}_вся-локальная-разметка_{timestamp}.png", images[0])
                    else:
                        archive.writestr(f"{prefix}_вся-локальная-разметка-missing_{timestamp}.txt", "Не удалось отрисовать PDF разметки в PNG.")
                else:
                    archive.writestr(f"{prefix}_вся-локальная-разметка-missing_{timestamp}.txt", "Файл полной локальной разметки не найден.")

                review = ((page.get("analysis") or {}).get("dimension_review") or {})
                trace = page.get("provider_trace") or {}
                answer_payload = {
                    "run_id": run_id,
                    "group": line_id,
                    "page_number": page_number,
                    "prompt_revision": page.get("prompt_revision"),
                    "model": trace.get("model") or review.get("model"),
                    "answer": review.get("answer"),
                    "lengths": review.get("lengths"),
                    "eval": review.get("eval"),
                    "response_raw": trace.get("response_raw"),
                }
                archive.writestr(
                    f"{prefix}_ответ-провайдера_{timestamp}.json",
                    json.dumps(answer_payload, ensure_ascii=False, indent=2),
                )
                payload_data = {
                    "run_id": run_id,
                    "group": line_id,
                    "page_number": page_number,
                    "payload": trace.get("payload"),
                }
                archive.writestr(
                    f"{prefix}_payload_{timestamp}.json",
                    json.dumps(payload_data, ensure_ascii=False, indent=2),
                )
    output.seek(0)
    filename = f"{run_id}_review_data_{timestamp}.zip"
    return StreamingResponse(
        output,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _safe_export_part(value: Any) -> str:
    text = str(value or "").strip()
    safe = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in text)
    return safe.strip("_") or "item"


def run_dict(run: RunState) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "created_at": run.created_at,
        "stop_stage": run.stop_stage,
        "kind": run.kind,
        "kind_label": run.kind_label,
        "model": run.model,
        "provider": run.provider,
        "status": run.status,
        "source_name": run.source_name,
        "line_ids": run.line_ids,
        "excluded_pages": run.excluded_pages,
        "lines": [
            {
                "line_id": line.line_id,
                "pages": line.pages,
                "status": line.status,
                "error": line.error,
                "files": line.files,
                "analysis": line.analysis,
                "provider_trace": line.provider_trace,
                "events": line.events,
                "page_results": [
                    {
                        "page_number": page_run.page_number,
                        "status": page_run.status,
                        "stage": page_run.stage,
                        "error": page_run.error,
                        "numbers": page_run.numbers,
                        "vertices": page_run.vertices,
                        "coordinates": page_run.coordinates,
                        "skipped_numbers": page_run.skipped_numbers,
                        "files": page_run.files,
                        "analysis": page_run.analysis,
                        "provider_trace": page_run.provider_trace,
                        "prompt_revision": page_run.prompt_revision,
                        "events": page_run.events,
                    }
                    for page_run in sorted(line.page_results.values(), key=lambda item: item.page_number)
                ],
            }
            for line in run.lines.values()
        ],
    }


@app.get("/api/runs/{run_id}/artifacts/{line_id}/{filename}")
def download_artifact(run_id: str, line_id: str, filename: str) -> FileResponse:
    path = ARTIFACTS_DIR / run_id / line_id / filename
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(
        path,
        media_type="application/pdf" if filename.endswith(".pdf") else "text/plain; charset=utf-8",
        filename=filename,
    )


@app.get("/api/runs/{run_id}/artifacts/{line_id}/{filename}/preview")
def artifact_preview(run_id: str, line_id: str, filename: str) -> Response:
    path = ARTIFACTS_DIR / run_id / line_id / filename
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    if path.suffix.lower() == ".pdf":
        images = pdf_pages_to_png(path, max_pages=1, zoom=1.6)
        if not images:
            raise HTTPException(500, "Страниц не найдено")
        return Response(images[0], media_type="image/png")
    return Response(path.read_text(encoding="utf-8", errors="ignore"), media_type="text/plain; charset=utf-8")


def pdf_pages_to_png(pdf_path, max_pages: int = 1, zoom: float = 1.6) -> list[bytes]:
    import pymupdf

    images: list[bytes] = []
    with pymupdf.open(str(pdf_path)) as document:
        for page_index in range(min(max_pages, document.page_count)):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            images.append(pixmap.tobytes("png"))
    return images


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
