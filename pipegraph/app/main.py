from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai as deepseek
from .engine import _process_line, LineRunState
from .pre import read_document
from .runtime import RunState

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(__file__).resolve().parents[1]


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip())
    for key, value in os.environ.items():
        if key.startswith("DEEPSEEK"):
            values[key] = value
    return values


ENV = _load_env()
app = FastAPI(title="PipeGraph")
STATE: dict[str, Any] = {"documents": {}, "runs": {}}


@app.get("/favicon.ico")
def favicon() -> Any:
    from fastapi import Response

    return Response(status_code=204)


DEFAULT_PDF_NAME = "Изометрии.pdf"
DEFAULT_PDF_PATH = ROOT / DEFAULT_PDF_NAME


@app.post("/api/pdf/load-default")
def load_default_pdf() -> dict[str, Any]:
    if not DEFAULT_PDF_PATH.exists():
        raise HTTPException(404, f"Файл {DEFAULT_PDF_NAME} не найден в корне проекта")
    return build_document_response(DEFAULT_PDF_PATH)


def build_document_response(path: Path) -> dict[str, Any]:
    try:
        pages, groups = read_document(path)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(400, f"Не удалось прочитать PDF: {error}") from error
    document_id = uuid.uuid4().hex[:12]
    STATE["documents"][document_id] = {
        "document_id": document_id,
        "pdf_path": str(path),
        "source_name": path.name,
        "pages_count": len(pages),
        "groups": groups,
    }
    return {
        "document_id": document_id,
        "source_name": path.name,
        "pages_count": len(pages),
        "groups": [{"line_id": group.line_id, "pages": [page.page_number for page in group.pages]} for group in groups],
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "deepseek": bool(ENV.get("DEEPSEEK_API_KEY")),
        "model": ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "default_pdf": DEFAULT_PDF_PATH.name if DEFAULT_PDF_PATH.exists() else "",
    }


@app.post("/api/pdf/upload")
async def upload_pdf(file: UploadFile) -> dict[str, Any]:
    import shutil

    temp_dir = ROOT / ".cache" / "pipegraph_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / (file.filename or f"upload-{uuid.uuid4().hex}.pdf")
    with target.open("wb") as sink:
        shutil.copyfileobj(file.file, sink)
    return build_document_response(target)


class LoadPdfBody(BaseModel):
    file_path: str


@app.post("/api/pdf/load")
def load_pdf(body: LoadPdfBody) -> dict[str, Any]:
    path = Path(body.file_path)
    if not path.exists():
        raise HTTPException(404, f"Файл не найден: {body.file_path}")
    try:
        pages, groups = read_document(path)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(400, f"Не удалось прочитать PDF: {error}") from error
    document_id = uuid.uuid4().hex[:12]
    STATE["documents"][document_id] = {
        "document_id": document_id,
        "pdf_path": str(path),
        "source_name": path.name,
        "pages_count": len(pages),
        "groups": groups,
    }
    return {
        "document_id": document_id,
        "source_name": path.name,
        "pages_count": len(pages),
        "groups": [{"line_id": group.line_id, "pages": [page.page_number for page in group.pages]} for group in groups],
    }


class AnalyzeBody(BaseModel):
    document_id: str
    line_ids: list[str]


@app.post("/api/analyze")
def analyze(body: AnalyzeBody) -> dict[str, Any]:
    document = STATE["documents"].get(body.document_id)
    if not document:
        raise HTTPException(404, "Документ не найден")
    available = {group.line_id for group in document["groups"]}
    unknown = [line_id for line_id in body.line_ids if line_id not in available]
    if not body.line_ids or unknown:
        raise HTTPException(400, f"Некорректные линии: {unknown or 'выберите хотя бы одну'}")

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = RunState(run_id=run_id, document_id=body.document_id, source_name=document["source_name"], line_ids=list(body.line_ids))
    for line_id in body.line_ids:
        group = next((group for group in document["groups"] if group.line_id == line_id), None)
        run.lines[line_id] = LineRunState(
            line_id=line_id, pages=[page.page_number for page in (group.pages if group else [])]
        )
    STATE["runs"][run_id] = run
    context = {
        "pdf_path": document["pdf_path"],
        "api_key": ENV.get("DEEPSEEK_API_KEY", ""),
        "model": ENV.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "deepseek": deepseek,
    }
    threading.Thread(target=_run_analysis, args=(run_id, context), daemon=True).start()
    return {"run_id": run_id, "status": "running", "line_ids": body.line_ids}


def _run_analysis(run_id: str, context: dict[str, Any]) -> None:
    run = STATE["runs"].get(run_id)
    if run is None:
        return
    for line_state in run.lines.values():
        _process_line(line_state, context)
    if all(line.status in {"complete", "error"} for line in run.lines.values()):
        run.status = "complete"


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = STATE["runs"].get(run_id)
    if not run:
        raise HTTPException(404, "Прогон не найден")
    return run_dict(run)


def run_dict(run: RunState) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "source_name": run.source_name,
        "line_ids": run.line_ids,
        "lines": [
            {
                "line_id": line.line_id,
                "pages": line.pages,
                "stage": line.stage,
                "status": line.status,
                "error": line.error,
                "result": line.result,
                "events": line.events,
            }
            for line in run.lines.values()
        ],
    }


@app.get("/api/runs")
def list_runs() -> list[dict[str, Any]]:
    return [run_dict(run) for run in reversed(list(STATE["runs"].values()))]


app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "templates" / "index.html")
