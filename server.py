from __future__ import annotations

import io
import json
import os
import shutil
import threading
import uuid
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

from src import db as run_db
from src.dimension_mapping import (
    _handwheel_details,
    run_dimension_mapping,
    save_clean_graph_pdf,
    save_clean_local_markup_pdf,
    save_preprocess_annotation_pdf,
    save_skeleton_pdf,
)
from src.distance_ai import call_distance_ai_trace
from src.dimension_review import build_map_text, calculate_lengths, review_map_with_provider
from src.eval_data import aggregate_eval_results, evaluate_line_for_prompt, list_eval_groups, summarize_prompt_eval_rows
from src.prepare_stage import run_prepare
from scripts.build_dimension_map import build_map as build_dimension_map, render_pdf as render_dimension_map


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_PDF = ROOT / "\u0418\u0437\u043e\u043c\u0435\u0442\u0440\u0438\u0438.pdf"
ARTIFACTS_DIR = ROOT / ".cache" / "mark_runs"
load_dotenv(ROOT / ".env")

PIPELINE_STEPS = [
    ("local_processing", "Локальная обработка"),
    ("dimension_review", "Карта размеров + проверка провайдером"),
    ("analyze", "Анализ у провайдера"),
]

# Расширяемый реестр конфигураций запуска. stop_stage -> ключ + человекочитаемая метка.
RUN_KIND_BY_STAGE = {
    "local_processing": ("local_processing", "Локальная обработка"),
    "prepare": ("local_prepare", "Локальная обработка"),
    "dimensions": ("dimension_mapping", "Локальная обработка"),
    "dimension_review": ("dimension_review", "Карта размеров + проверка провайдером"),
    "analyze": ("deepseek_analyze", "Анализ DeepSeek"),
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


@dataclass
class RunState:
    run_id: str
    document_id: str
    source_name: str
    pdf_path: str
    line_ids: list[str]
    stop_stage: str = "analyze"
    status: str = "running"
    created_at: str = ""
    kind: str = ""
    kind_label: str = ""
    model: str = ""
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "pipeline_steps": [{"id": key, "label": label} for key, label in PIPELINE_STEPS],
        "deepseek": bool(ENV.get("DEEPSEEK_API_KEY")),
        "model": ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "default_pdf": DEFAULT_PDF.name if DEFAULT_PDF.exists() else "",
    }


class LoadPdfBody(BaseModel):
    file_path: str = ""


@app.post("/api/pdf/load-default")
def load_default_pdf() -> dict[str, Any]:
    return load_pdf(LoadPdfBody(file_path=str(DEFAULT_PDF)))


@app.post("/api/pdf/load")
def load_pdf(body: LoadPdfBody) -> dict[str, Any]:
    from src.pipeline import get_pdf_cache_status, prepare_pdf_groups

    resolved = Path((body.file_path or "").strip() or str(DEFAULT_PDF))
    if not resolved.exists():
        resolved = DEFAULT_PDF if DEFAULT_PDF.exists() else resolved
    if not resolved.exists():
        raise HTTPException(404, f"Файл не найден: {body.file_path}")
    cache_status = get_pdf_cache_status(resolved)
    pages, groups = prepare_pdf_groups(resolved, use_disk_cache=True)
    document_id = uuid.uuid4().hex[:12]
    STATE["documents"][document_id] = {
        "document_id": document_id,
        "pdf_path": str(resolved),
        "source_name": resolved.name,
        "pages_count": len(pages),
        "groups": groups,
        "cache": cache_status,
    }
    return {
        "document_id": document_id,
        "source_name": resolved.name,
        "pages_count": len(pages),
        "cache": cache_status,
        "groups": [{"line_id": group.line_id, "pages": [page.page_number for page in group.pages]} for group in groups],
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
        if group.line_id == line_id:
            return group
    return None


class AnalyzeBody(BaseModel):
    document_id: str
    line_ids: list[str]
    stop_stage: str = "analyze"
    excluded_pages: list[int] = []


@app.get("/api/prompts")
def list_prompts() -> list[dict[str, Any]]:
    from src import prompts

    summary = {
        (item.get("prompt_name"), item.get("prompt_version"), item.get("prompt_sha256")): item
        for item in run_db.feedback_summary()
    }
    output = []
    for item in prompts.list_prompts():
        active = prompts.load_prompt_revision(item["name"])
        active_key = (item["name"], str(active.get("version")), active.get("sha256"))
        active_row = summary.get(active_key, {})
        legacy_row = summary.get((item["name"], None, None), {})
        positive = int(active_row.get("positive") or 0) + int(legacy_row.get("positive") or 0)
        negative = int(active_row.get("negative") or 0) + int(legacy_row.get("negative") or 0)
        output.append({
            **item,
            "feedback": {
                "positive": positive,
                "negative": negative,
                "total": positive + negative,
            },
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
    revision = page.get("prompt_revision") or {}
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
        "prompt_name": revision.get("name") or trace.get("prompt_name") or "dimension_review",
        "prompt_version": revision.get("version") or trace.get("prompt_version"),
        "prompt_sha256": revision.get("sha256") or trace.get("prompt_sha256"),
        "context": context,
    })


@app.get("/api/feedback")
def list_analysis_feedback(prompt_name: str | None = None) -> dict[str, Any]:
    return {"rows": run_db.feedback_rows(prompt_name), "summary": run_db.feedback_summary()}


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
    revision = page.get("prompt_revision") or {}
    run_db.save_feedback({
        "run_id": body.run_id,
        "line_id": body.line_id,
        "page_number": body.page_number,
        "candidate_id": body.candidate_id,
        "rating": "down",
        "prompt_name": revision.get("name") or trace.get("prompt_name") or "dimension_review",
        "prompt_version": revision.get("version") or trace.get("prompt_version"),
        "prompt_sha256": revision.get("sha256") or trace.get("prompt_sha256"),
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


@app.get("/api/prompts/{name}")
def get_prompt(name: str) -> dict[str, Any]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    revision = prompts.load_prompt_revision(name)
    feedback_rows = run_db.feedback_summary()
    active_row = next(
        (
            item for item in feedback_rows
            if item.get("prompt_name") == name
            and str(item.get("prompt_version")) == str(revision.get("version"))
            and item.get("prompt_sha256") == revision.get("sha256")
        ),
        {},
    )
    legacy_row = next(
        (
            item for item in feedback_rows
            if item.get("prompt_name") == name
            and not item.get("prompt_version")
            and not item.get("prompt_sha256")
        ),
        {},
    )
    positive = int(active_row.get("positive") or 0) + int(legacy_row.get("positive") or 0)
    negative = int(active_row.get("negative") or 0) + int(legacy_row.get("negative") or 0)
    return {
        "name": name,
        "title": prompts.PROMPT_REGISTRY[name].get("title", name),
        "source": revision["source"],
        "version": revision["version"],
        "sha256": revision["sha256"],
        "text": revision["text"],
        "feedback": {"positive": positive, "negative": negative, "total": positive + negative},
    }


@app.post("/api/prompts/{name}")
def save_prompt(name: str, body: PromptBody) -> dict[str, str]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    prompts.save_prompt(name, body.text)
    return {"name": name, "source": "override"}


@app.post("/api/prompts/{name}/reset")
def reset_prompt(name: str) -> dict[str, str]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    prompts.reset_prompt(name)
    return {"name": name, "source": "default"}


@app.get("/api/prompts/{name}/versions")
def list_prompt_versions(name: str) -> dict[str, Any]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    summaries = {
        (str(item.get("prompt_version")), item.get("prompt_sha256")): item
        for item in run_db.feedback_summary()
        if item.get("prompt_name") == name
    }
    active_revision = prompts.load_prompt_revision(name)
    legacy_summary = next(
        (
            item for item in run_db.feedback_summary()
            if item.get("prompt_name") == name
            and not item.get("prompt_version")
            and not item.get("prompt_sha256")
        ),
        None,
    )
    versions = []
    for version in prompts.list_versions(name):
        summary = summaries.get((str(version.get("version")), version.get("sha256")), {})
        if version.get("version") == active_revision.get("version") and legacy_summary:
            summary = {
                "positive": int(summary.get("positive") or 0) + int(legacy_summary.get("positive") or 0),
                "negative": int(summary.get("negative") or 0) + int(legacy_summary.get("negative") or 0),
                "total": int(summary.get("total") or 0) + int(legacy_summary.get("total") or 0),
            }
        versions.append({
            **version,
            "feedback": {
                "positive": int(summary.get("positive") or 0),
                "negative": int(summary.get("negative") or 0),
                "total": int(summary.get("total") or 0),
            },
        })
    return {"name": name, "versions": versions}


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
    revision = prompts.load_prompt_revision(name)
    return {
        "name": name,
        "version": revision["version"],
        "sha256": revision["sha256"],
        "source": "override",
    }


@app.post("/api/runs")
def create_run(body: AnalyzeBody) -> dict[str, Any]:
    document = STATE["documents"].get(body.document_id)
    if not document:
        raise HTTPException(404, "Документ не найден")
    stop_stage = (body.stop_stage or "analyze").strip().lower()
    if stop_stage not in {"local_processing", "prepare", "dimensions", "dimension_review", "analyze"}:
        raise HTTPException(400, "stop_stage: local_processing|dimension_review|analyze")
    if stop_stage in {"prepare", "dimensions"}:
        stop_stage = "local_processing"
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
        stop_stage=stop_stage,
        created_at=now(),
        kind=kind,
        kind_label=kind_label,
        model=ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
    )
    for line_id in body.line_ids:
        group = _group(document, line_id)
        pages = [page.page_number for page in (group.pages if group else []) if page.page_number not in excluded_pages]
        if not pages:
            continue
        run.lines[line_id] = LineRun(line_id=line_id, pages=pages)
    if not run.lines:
        raise HTTPException(400, "После исключения листов не осталось страниц для анализа")
    STATE["runs"][run_id] = run
    run_db.save_run(run_dict(run))
    context = {
        "api_key": ENV.get("DEEPSEEK_API_KEY", ""),
        "model": ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "document": document,
        "run": run,
    }
    threading.Thread(target=_run_pipeline, args=(run_id, context), daemon=True).start()
    return {"run_id": run_id, "status": "running", "stop_stage": stop_stage, "line_ids": body.line_ids}


def _persist(run: RunState) -> None:
    run_db.save_run(run_dict(run))


def _run_pipeline(run_id: str, context: dict[str, Any]) -> None:
    run: RunState = context["run"]
    document = context["document"]
    api_key = context["api_key"]
    model = context["model"]
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

            if stop_stage in {"local_processing", "dimensions", "dimension_review"}:
                page_run.stage = "dimensions"
                page_run.status = "running"
                page_run.events.append({"time": now(), "stage": "dimensions", "message": "локальная обработка: разметка и привязка размеров"})
                pdf_stem = Path(pdf_path).stem
                dimensions_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimensions_marked.pdf"
                dimensions_json = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimensions.json"
                mapping = run_dimension_mapping(pdf_path, page_run.page_number, dimensions_pdf, dimensions_json)
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
                clean_graph_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_graph.pdf"
                save_clean_graph_pdf(pdf_path, page_run.page_number, clean_graph_pdf, mapping)
                skeleton_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_skeleton.pdf"
                save_skeleton_pdf(pdf_path, page_run.page_number, skeleton_pdf, mapping)
                dimension_map_pdf = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_map.pdf"
                dimension_map_json = run_dir / f"{pdf_stem}_page{page_run.page_number}_dimension_map.json"
                dimension_map = build_dimension_map(Path(pdf_path), page_run.page_number)
                dimension_map["handwheels"] = mapping.get("handwheels", [])
                dimension_map_json.write_text(json.dumps(dimension_map, ensure_ascii=False, indent=2), encoding="utf-8")
                render_dimension_map(Path(pdf_path), page_run.page_number, dimension_map_pdf, dimension_map)
                page_run.analysis = {"dimension_mapping": mapping, "dimension_map": dimension_map}
                page_run.files["dimensions_pdf"] = dimensions_pdf.name
                page_run.files["dimensions_json"] = dimensions_json.name
                page_run.files["preprocess_annotations_pdf"] = preprocess_pdf.name
                page_run.files["clean_local_markup_pdf"] = clean_markup_pdf.name
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
                        "prompt_name": "dimension_review",
                    }
                    eval_result = evaluate_line_for_prompt(line.line_id, "dimension_review", review)
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

            page_run.stage = "analyze"
            page_run.status = "running"
            page_run.events.append({"time": now(), "stage": "analyze", "message": "запрос к анализатору"})
            _persist(run)
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY не задан в .env")
            trace = call_distance_ai_trace(
                api_key=api_key,
                model=model,
                vertices=prepare.vertices,
                numbers=prepare.numbers,
                coordinates=prepare.coordinates,
                event_callback=lambda name, payload: page_run.events.append({"time": now(), "stage": "analyze", "message": f"{name}: {payload}"}),
            )
            page_run.analysis = trace["answer"]
            page_run.provider_trace = {
                "prompt": trace["prompt"],
                "payload": trace["payload"],
                "response_raw": trace["response_raw"],
                "status_code": trace["status_code"],
                "elapsed_seconds": trace["elapsed_seconds"],
                "model": trace["model"],
                "prompt_name": trace.get("prompt_name", "analyze"),
                "prompt_version": trace.get("prompt_version", "default"),
                "prompt_source": trace.get("prompt_source", "default"),
                "prompt_sha256": trace.get("prompt_sha256", ""),
            }
            page_run.prompt_revision = {
                "name": trace.get("prompt_name", "analyze"),
                "version": trace.get("prompt_version", "default"),
                "source": trace.get("prompt_source", "default"),
                "sha256": trace.get("prompt_sha256", ""),
            }
            page_run.stage = "done"
            page_run.status = "complete"
            page_run.events.append({"time": now(), "stage": "done", "message": f"main_chain: {(trace['answer'].get('main_chain') or {}).get('path')}"})
            _persist(run)
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
            for page in line.get("page_results", []):
                review = (page.get("analysis") or {}).get("dimension_review")
                if not isinstance(review, dict):
                    continue
                eval_summary = review.get("eval")
                if not isinstance(eval_summary, dict):
                    continue
                for group_id, stats in eval_summary.items():
                    if not isinstance(stats, dict):
                        continue
                    row = {"group_id": group_id, "prompt_name": stats.get("prompt_name") or "dimension_review", **stats}
                    rows.append(row)
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


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = STATE["runs"].get(run_id)
    if run:
        return run_dict(run)
    persisted = run_db.load_run(run_id)
    if not persisted:
        raise HTTPException(404, "Прогон не найден")
    return persisted


def _run_export_rows(run: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    pages = []
    events = []
    errors = []
    numbers = []
    vertices = []
    coordinates = []
    analyses = []
    for line in run.get("lines", []):
        for page in line.get("page_results", []):
            base = {
                "line_id": line.get("line_id"),
                "page_number": page.get("page_number"),
                "status": page.get("status"),
                "stage": page.get("stage"),
                "error": page.get("error", ""),
                "numbers_count": len(page.get("numbers", [])),
                "vertices_count": len(page.get("vertices", [])),
            }
            pages.append(base)
            for item in page.get("numbers", []):
                numbers.append({"line_id": line.get("line_id"), "page_number": page.get("page_number"), **item})
            for item in page.get("vertices", []):
                vertices.append({"line_id": line.get("line_id"), "page_number": page.get("page_number"), **item})
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
    return {
        "Страницы": pages,
        "Числа": numbers,
        "Вершины": vertices,
        "Координаты": coordinates,
        "Анализ": analyses,
        "События": events,
        "Ошибки": errors,
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
            "model": run.get("model"),
            "stop_stage": run.get("stop_stage"),
        }]).to_excel(writer, sheet_name="Сводка", index=False)
        for sheet, rows in _run_export_rows(run).items():
            pd.DataFrame(rows or [{"status": "Нет данных"}]).to_excel(writer, sheet_name=sheet, index=False)
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{run_id}.xlsx"'})


def run_dict(run: RunState) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "created_at": run.created_at,
        "stop_stage": run.stop_stage,
        "kind": run.kind,
        "kind_label": run.kind_label,
        "model": run.model,
        "status": run.status,
        "source_name": run.source_name,
        "line_ids": run.line_ids,
        "lines": [
            {
                "line_id": line.line_id,
                "pages": line.pages,
                "status": line.status,
                "error": line.error,
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
