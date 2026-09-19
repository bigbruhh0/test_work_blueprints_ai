from __future__ import annotations

import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import db as run_db
from src.distance_ai import call_distance_ai_trace
from src.prepare_stage import run_prepare


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_PDF = ROOT / "\u0418\u0437\u043e\u043c\u0435\u0442\u0440\u0438\u0438.pdf"
ARTIFACTS_DIR = ROOT / ".cache" / "mark_runs"
load_dotenv(ROOT / ".env")

PIPELINE_STEPS = [
    ("prepare", "Подготовка файлов (числа + вершины)"),
    ("analyze", "Анализ у провайдера"),
]

# Расширяемый реестр конфигураций запуска. stop_stage -> ключ + человекочитаемая метка.
RUN_KIND_BY_STAGE = {
    "prepare": ("local_prepare", "Локальная подготовка (без ИИ)"),
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
    skipped_numbers: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    analysis: dict[str, Any] | None = None
    provider_trace: dict[str, Any] | None = None
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


@app.get("/api/prompts")
def list_prompts() -> list[dict[str, str]]:
    from src import prompts

    return prompts.list_prompts()


class PromptBody(BaseModel):
    text: str


@app.get("/api/prompts/{name}")
def get_prompt(name: str) -> dict[str, str]:
    from src import prompts

    if name not in prompts.PROMPT_REGISTRY:
        raise HTTPException(404, "Промпт не найден")
    return {
        "name": name,
        "title": prompts.PROMPT_REGISTRY[name].get("title", name),
        "source": "override" if (prompts.OVERRIDE_DIR / f"{name}.txt").exists() else "default",
        "text": prompts.load_prompt(name),
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


@app.post("/api/runs")
def create_run(body: AnalyzeBody) -> dict[str, Any]:
    document = STATE["documents"].get(body.document_id)
    if not document:
        raise HTTPException(404, "Документ не найден")
    stop_stage = (body.stop_stage or "analyze").strip().lower()
    if stop_stage not in {"prepare", "analyze"}:
        raise HTTPException(400, "stop_stage: prepare|analyze")
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    kind, kind_label = RUN_KIND_BY_STAGE.get(stop_stage, (stop_stage, stop_stage))
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
        run.lines[line_id] = LineRun(line_id=line_id, pages=[page.page_number for page in (group.pages if group else [])])
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
            prepare = run_prepare(pdf_path, page_run.page_number, run_dir)
            page_run.numbers = prepare.numbers
            page_run.vertices = prepare.vertices
            page_run.skipped_numbers = prepare.skipped_numbers
            page_run.files = {
                "numbers_pdf": prepare.numbers_pdf.name,
                "vertices_pdf": prepare.vertices_pdf.name,
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
            page_run.events.append({"time": now(), "stage": "prepare", "message": f"чисел {len(prepare.numbers)}, вершин {len(prepare.vertices)}"})
            if stop_stage == "prepare":
                page_run.status = "complete"
                return

            page_run.stage = "analyze"
            page_run.status = "running"
            page_run.events.append({"time": now(), "stage": "analyze", "message": "запрос к анализатору"})
            if not api_key:
                raise RuntimeError("DEEPSEEK_API_KEY не задан в .env")
            trace = call_distance_ai_trace(
                api_key=api_key,
                model=model,
                vertices=prepare.vertices,
                numbers=prepare.numbers,
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
            }
            page_run.stage = "done"
            page_run.status = "complete"
            page_run.events.append({"time": now(), "stage": "done", "message": f"main_chain: {(trace['answer'].get('main_chain') or {}).get('path')}"})
        except Exception as error:  # noqa: BLE001
            page_run.status = "error"
            page_run.error = str(error)
            page_run.events.append({"time": now(), "stage": "error", "message": str(error)})

    futures = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for line in run.lines.values():
            for page_number in line.pages:
                page_run = PageRun(page_number=page_number)
                line.page_results[page_number] = page_run
                futures.append(executor.submit(process_page, line, page_run))
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
    run.status = "complete"
    _persist(run)


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
                        "skipped_numbers": page_run.skipped_numbers,
                        "files": page_run.files,
                        "analysis": page_run.analysis,
                        "provider_trace": page_run.provider_trace,
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
