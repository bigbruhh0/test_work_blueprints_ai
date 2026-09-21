from __future__ import annotations

import base64
import io
import json
import re
import time
from dataclasses import fields
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

import pymupdf
import requests

from .models import (
    AnalysisResult,
    Annotation,
    Candidate,
    CandidateClassification,
    ElementResult,
    GraphData,
    GraphEdge,
    GraphNode,
    DimensionBinding,
    UnresolvedEdge,
    LineGroup,
    LineResult,
    PointResult,
    ProviderTrace,
    SegmentResult,
    SourceRef,
    Uncertainty,
    VertexMark,
)
from .dimension_mapping import _connection_rows_from_page, _handwheel_details
from .route_reconstruction import extract_local_vertices


EventCallback = Callable[[str, dict[str, Any]], None]

# Расширенный список ключевых слов для поиска штурвалов/арматуры.
# Включает как русские, так и английские варианты, а также "вентиль" и "valve".
VALVE_KEYWORDS = (
    "штурвал",
    "рукоятка",
    "рукоять",
    "ручка",
    "маховик",
    "колесо",
    "рычаг",
    "трос",
    "вентиль",
    "задвижка",
    "клапан",
    "handwheel",
    "wheel",
    "handle",
    "lever",
    "crank",
    "valve",
)


def _looks_like_valve_label(text: str) -> bool:
    normalized = (text or "").lower()
    return any(keyword in normalized for keyword in VALVE_KEYWORDS)


def _candidate_center(candidate: Candidate) -> tuple[float, float]:
    return (
        (candidate.bbox[0] + candidate.bbox[2]) / 2.0,
        (candidate.bbox[1] + candidate.bbox[3]) / 2.0,
    )


def _extract_sheet_number(text: str) -> str | None:
    if not text:
        return None
    patterns = (
        r"(?:ЛИСТ|SHEET)\s*[:№]?\s*(\d+)",
        r"(?:SEE\s+SHEET|CONTINUATION)\s*(?:[A-Z0-9\-_/]+\s+)?(\d+)",
        r"(?:ЛИСТ|SHEET)\s*[:№]?\s*(?:№\s*)?(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE)
        if match:
            return match.group(1)
    return None


def _connection_classify(text: str) -> tuple[str, str | None, bool]:
    normalized = (text or "").strip()
    if not normalized:
        return "other", None, False
    upper = normalized.upper()
    tie_in_keywords = (
        "ПОДКЛЮЧЕНИЕ",
        "ПОДСОЕДИНЕНИЕ",
        "ВРЕЗКА",
        "TIE-IN",
        "TIE IN",
        "CONNECTION",
    )
    if any(keyword in upper for keyword in tie_in_keywords):
        return "tie_in", None, False
    sheet_number = _extract_sheet_number(normalized)
    if sheet_number is not None:
        return "continuation", sheet_number, True
    continuation_keywords = (
        "СМ.",
        "СМ ",
        "SEE SHEET",
        "SEE SHEET ",
        "CONTINUATION",
        "ЛИСТ",
        "SHEET",
    )
    has_sheet_hint = any(keyword in upper for keyword in continuation_keywords)
    if has_sheet_hint:
        return "other", None, True
    return "other", None, False


def _find_connection_rows(
    candidates: list[Candidate],
    vertices: list[VertexMark],
) -> list[dict[str, Any]]:
    """Найти метки вида 'ПОДКЛЮЧЕНИЕ ...' и 'СМ. ... ЛИСТ N'."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.zone != "drawing":
            continue
        label = (candidate.text or "").strip()
        if not label:
            continue
        connection_type, target_sheet, has_sheet_hint = _connection_classify(label)
        if connection_type == "other" and not has_sheet_hint and not any(
            keyword in (label or "").upper() for keyword in ("ПОДКЛЮЧЕНИЕ", "ВРЕЗКА", "TIE-IN", "TIE IN", "СМ.", "SEE SHEET", "CONTINUATION", "ЛИСТ", "SHEET")
        ):
            continue
        cx, cy = _candidate_center(candidate)
        nearest_vertex = None
        nearest_distance = None
        for vertex in vertices:
            if vertex.page != candidate.page:
                continue
            dist = ((vertex.x - cx) ** 2 + (vertex.y - cy) ** 2) ** 0.5
            if nearest_distance is None or dist < nearest_distance:
                nearest_distance = dist
                nearest_vertex = vertex
        row = {
            "id": f"CN-{candidate.id}",
            "page": candidate.page,
            "label": label,
            "bbox": [round(value, 2) for value in candidate.bbox],
            "center": [round(cx, 2), round(cy, 2)],
            "connection_type": connection_type,
            "vertex_id": nearest_vertex.id if nearest_vertex is not None else None,
            "target_sheet": target_sheet,
            "text_has_sheet_ref": has_sheet_hint,
        }
        row_id = f"{candidate.page}:{candidate.id}"
        if row_id in seen:
            continue
        seen.add(row_id)
        rows.append(row)
    return rows


def _handwheel_row_from_candidate(
    candidate: Candidate,
    vertices: list[VertexMark],
    confidence: float,
    reason: str,
) -> dict[str, Any]:
    cx, cy = _candidate_center(candidate)
    nearest_vertex = None
    nearest_distance = None
    for vertex in vertices:
        if vertex.page != candidate.page:
            continue
        dist = ((vertex.x - cx) ** 2 + (vertex.y - cy) ** 2) ** 0.5
        if nearest_distance is None or dist < nearest_distance:
            nearest_distance = dist
            nearest_vertex = vertex

    target_vertex_id = nearest_vertex.id if nearest_vertex else None
    target_point = None
    if nearest_vertex is not None:
        target_point = [round(nearest_vertex.x, 2), round(nearest_vertex.y, 2)]

    direction = "unknown"
    if nearest_vertex is not None:
        dx = nearest_vertex.x - cx
        dy = nearest_vertex.y - cy
        if abs(dx) > abs(dy):
            direction = "horizontal"
        elif abs(dy) > abs(dx):
            direction = "vertical"

    return {
        "id": f"HW-{candidate.id}",
        "page": candidate.page,
        "label": (candidate.text or "").strip() or "штурвал",
        "bbox": [round(value, 2) for value in candidate.bbox],
        "target_vertex_id": target_vertex_id,
        "target_point": target_point,
        "arrow_direction": direction,
        "confidence": confidence,
        "reason": reason,
    }


def _find_handwheel_rows(
    candidates: list[Candidate],
    vertices: list[VertexMark],
) -> list[dict[str, Any]]:
    """Найти штурвалы/рукоятки/маховики.

    Три прохода, от самого строгого к самому мягкому:
    1. Прямой: candidates с kind in {text, numeric, dimension}, text похож на штурвал.
    2. Запасной: ЛЮБЫЕ candidates (без фильтра по kind), text похож на штурвал.
    3. Геометрический: кандидаты рядом с DN-подписями в зоне drawing,
       если ни прямой, ни запасной путь не дал результата.
    """
    rows: list[dict[str, Any]] = []
    if not candidates:
        return rows

    seen_ids: set[str] = set()

    # --- Проход 1: прямой, как было ---
    for candidate in candidates:
        if candidate.id in seen_ids:
            continue
        if candidate.kind not in {"text", "numeric", "dimension"}:
            continue
        if not _looks_like_valve_label(candidate.text):
            continue
        rows.append(
            _handwheel_row_from_candidate(
                candidate,
                vertices,
                confidence=0.75,
                reason="Найдено по ключевому слову штурвал/рукоятка, привязка проверена по ближайшей вершине/оси.",
            )
        )
        seen_ids.add(candidate.id)

    if rows:
        return rows

    # --- Проход 2: запасной, без фильтра по kind ---
    # Иногда extractor помечает подпись «ШТУРВАЛ» как kind="unknown" или
    # вообще не относит её к dimension. Здесь принимаем ЛЮБОЙ kind.
    for candidate in candidates:
        if candidate.id in seen_ids:
            continue
        if candidate.zone != "drawing":
            continue
        if not _looks_like_valve_label(candidate.text):
            continue
        rows.append(
            _handwheel_row_from_candidate(
                candidate,
                vertices,
                confidence=0.6,
                reason="Запасной путь: подпись найдена среди всех кандидатов страницы (kind не важен).",
            )
        )
        seen_ids.add(candidate.id)

    if rows:
        return rows

    # --- Проход 3: геометрический, по близости к DN-подписям ---
    # На листах вроде CO_0031 (стр. 216) штурвалы физически стоят рядом
    # с подписями DN100X25 / DN100. Если текстовая подпись «ШТУРВАЛ» не
    # извлеклась как кандидат, но есть DN-подпись и рядом с ней есть
    # любой текстовый кандидат — помечаем его как штурвал с низкой уверенностью.
    dn_candidates = [
        candidate
        for candidate in candidates
        if candidate.zone == "drawing"
        and candidate.kind == "dn"
        and candidate.text
    ]
    if dn_candidates:
        used_dn: set[str] = set()
        for dn in dn_candidates:
            dn_cx, dn_cy = _candidate_center(dn)
            # Ищем любого текстового кандидата в радиусе 120 px от DN-подписи,
            # который ещё не помечен как штурвал.
            for candidate in candidates:
                if candidate.id in seen_ids or candidate.id == dn.id:
                    continue
                if candidate.zone != "drawing":
                    continue
                if candidate.kind not in {"text", "numeric", "dimension", "unknown", "label"}:
                    continue
                if not (candidate.text or "").strip():
                    continue
                cx, cy = _candidate_center(candidate)
                if ((cx - dn_cx) ** 2 + (cy - dn_cy) ** 2) ** 0.5 > 120.0:
                    continue
                rows.append(
                    _handwheel_row_from_candidate(
                        candidate,
                        vertices,
                        confidence=0.4,
                        reason=(
                            f"Геометрический запасной путь: кандидат рядом с DN-подписью "
                            f"{dn.text} (в радиусе 120 px)."
                        ),
                    )
                )
                seen_ids.add(candidate.id)
                used_dn.add(dn.id)
                break  # один штурвал на одну DN-подпись

    return rows


class AIClient(ABC):
    mode: str

    def set_candidates(self, candidates: list[Candidate]) -> None:
        """Receive PDF-extracted candidates for the current run."""

    def set_candidate_classifications(self, classifications: list[CandidateClassification]) -> None:
        """Receive AI classifications for PDF candidates."""

    def drain_provider_traces(self) -> list[ProviderTrace]:
        return []

    def preview_prompt(self, group: LineGroup, stage: str) -> str:
        return ""

    def classify_candidates(self, group: LineGroup) -> list[CandidateClassification]:
        return []

    def reconstruct_graph(self, group: LineGroup) -> GraphData:
        """Build a pipeline graph proposal (topology + dimension bindings)."""
        return GraphData()

    def recognize_vertices(self, group: LineGroup) -> list[VertexMark]:
        """Return page-coordinate marks without affecting the engineering result."""
        return []

    @abstractmethod
    def analyze_page_or_line(self, group: LineGroup) -> AnalysisResult:
        """Analyze one grouped pipeline line."""


class StubAIClient(AIClient):
    mode = "stub"

    def set_candidates(self, candidates: list[Candidate]) -> None:
        self.candidates_by_line: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            self.candidates_by_line.setdefault(candidate.line_id, []).append(candidate)

    def classify_candidates(self, group: LineGroup) -> list[CandidateClassification]:
        classifications: list[CandidateClassification] = []
        for index, candidate in enumerate(getattr(self, "candidates_by_line", {}).get(group.line_id, []), start=1):
            classification = "unknown"
            route_type = "unknown"
            if candidate.zone != "drawing":
                classification = "excluded_table"
            elif candidate.kind in {"coordinate_label", "coordinate_value"}:
                classification = "coordinate"
            elif candidate.kind == "coordinate_delta":
                classification = "route_segment"
                route_type = "unknown"
            elif candidate.kind == "dn":
                classification = "dn"
            elif candidate.kind == "line_ref":
                classification = "adjacent_line"
            elif candidate.kind == "dimension" and candidate.zone == "drawing":
                classification = "route_segment"
            classifications.append(
                CandidateClassification(
                    id=f"CC-{group.line_id}-{index:04d}",
                    line_id=group.line_id,
                    page=candidate.page,
                    candidate_id=candidate.id,
                    text=candidate.text,
                    classification=classification,  # type: ignore[arg-type]
                    route_type=route_type,  # type: ignore[arg-type]
                    reason="Heuristic stub classification.",
                    confidence=0.4,
                )
            )
        return classifications

    def analyze_page_or_line(self, group: LineGroup) -> AnalysisResult:
        return self._needs_review(group)

    def _needs_review(self, group: LineGroup) -> AnalysisResult:
        pages = [page.page_number for page in group.pages]
        line_id = group.line_id
        first_page = pages[0] if pages else None
        return AnalysisResult(
            lines=[
                LineResult(
                    id=line_id,
                    pages=pages,
                    status="needs_review",
                    supports_count=0,
                    valves_count=0,
                    completeness_note="Линия сгруппирована автоматически. Детальный анализ будет добавлен после подключения DeepSeek.",
                )
            ],
            uncertainties=[
                Uncertainty(
                    id=f"U-{line_id}-AI",
                    line_id=line_id,
                    page=first_page,
                    target=line_id,
                    reason="Stub-анализатор не восстанавливает геометрию этой линии.",
                    severity="warning",
                )
            ],
            annotations=[
                Annotation(
                    id=f"A-{line_id}-EXCLUDED-ZONES",
                    page=first_page or 1,
                    label="Запрещенные таблицы исключаются из расчетов",
                    kind="excluded_zone",
                    bbox=(810, 20, 1180, 690),
                    color="#dc2626",
                )
            ],
        )


class DeepSeekAIClient(AIClient):
    mode = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com/chat/completions",
        pdf_path: str | Path | None = None,
        include_images: bool = True,
        max_image_pages: int = 1,
        timeout_seconds: int = 120,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "deepseek-flash"
        self.base_url = base_url
        self.pdf_path = Path(pdf_path) if pdf_path else None
        self.include_images = include_images
        self.max_image_pages = max(0, max_image_pages)
        self.timeout_seconds = timeout_seconds
        self.event_callback = event_callback
        self.candidates_by_line: dict[str, list[Candidate]] = {}
        self.classifications_by_line: dict[str, list[CandidateClassification]] = {}
        self._graph_retry_count: dict[str, int] = {}
        self.provider_traces: list[ProviderTrace] = []

    def drain_provider_traces(self) -> list[ProviderTrace]:
        traces = self.provider_traces
        self.provider_traces = []
        return traces

    def preview_prompt(self, group: LineGroup, stage: str) -> str:
        candidates = self.candidates_by_line.get(group.line_id, [])
        payload = (
            self._build_classification_payload(group, candidates)
            if stage == "classify"
            else self._build_payload(group)
        )
        user_content = payload["messages"][1]["content"]
        if isinstance(user_content, list):
            return "\n\n".join(item.get("text", "") for item in user_content if item.get("type") == "text")
        return str(user_content)

    def _record_provider_trace(
        self,
        stage: str,
        group: LineGroup,
        payload: dict[str, Any],
        response: str = "",
        status: str = "ok",
        error: str = "",
    ) -> None:
        user_content = payload.get("messages", [{}, {}])[-1].get("content", "")
        prompt_parts = []
        image_count = 0
        if isinstance(user_content, list):
            for item in user_content:
                if item.get("type") == "text":
                    prompt_parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    image_count += 1
        else:
            prompt_parts.append(str(user_content))
        self.provider_traces.append(
            ProviderTrace(
                id=f"AI-{group.line_id}-{stage}-{len(self.provider_traces) + 1:04d}",
                line_id=group.line_id,
                stage=stage,
                model=self.model,
                prompt="\n\n".join(prompt_parts),
                response=response,
                status=status,
                image_count=image_count,
                error=error,
            )
        )

    def set_candidates(self, candidates: list[Candidate]) -> None:
        grouped: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.line_id, []).append(candidate)
        self.candidates_by_line = grouped

    def set_candidate_classifications(self, classifications: list[CandidateClassification]) -> None:
        grouped: dict[str, list[CandidateClassification]] = {}
        for classification in classifications:
            grouped.setdefault(classification.line_id, []).append(classification)
        self.classifications_by_line = grouped

    def classify_candidates(self, group: LineGroup) -> list[CandidateClassification]:
        if not self.api_key:
            return []
        candidates = self.candidates_by_line.get(group.line_id, [])
        if not candidates:
            return []

        try:
            started_at = time.perf_counter()
            payload = self._build_classification_payload(group, candidates)
            self._emit(
                "deepseek.classify.request.start",
                {
                    "line_id": group.line_id,
                    "candidates": len(candidates),
                    "payload_kb": round(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) / 1024, 1),
                },
            )
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            self._emit(
                "deepseek.classify.response.received",
                {
                    "line_id": group.line_id,
                    "status_code": response.status_code,
                    "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    "response_kb": round(len(response.content) / 1024, 1),
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            self._record_provider_trace("classify", group, payload, response=content)
            data = self._parse_json_lenient(content)
            result = [
                _coerce(CandidateClassification, item)
                for item in data.get("classifications", [])
            ]
            self._emit(
                "deepseek.classify.parse.done",
                {
                    "line_id": group.line_id,
                    "classifications": len(result),
                },
            )
            return result
        except Exception as error:
            self._emit("deepseek.classify.error", {"line_id": group.line_id, "error": str(error)})
            return []

    def recognize_vertices(self, group: LineGroup) -> list[VertexMark]:
        if not self.api_key or not self.pdf_path:
            return []
        try:
            payload = self._build_vertices_payload(group)
            started_at = time.perf_counter()
            self._emit("deepseek.vertices.request.start", {"line_id": group.line_id})
            response = requests.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            self._record_provider_trace("vertices", group, payload, response=content)
            data = self._parse_json_lenient(content)
            marks = [_coerce(VertexMark, item) for item in data.get("vertices", []) if isinstance(item, dict)]
            self._emit("deepseek.vertices.parse.done", {
                "line_id": group.line_id,
                "vertices": len(marks),
                "elapsed_seconds": round(time.perf_counter() - started_at, 2),
            })
            return [mark for mark in marks if mark.line_id == group.line_id and mark.page > 0]
        except Exception as error:
            self._emit("deepseek.vertices.error", {"line_id": group.line_id, "error": str(error)})
            return []

    def _build_vertices_payload(self, group: LineGroup) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": f"""
Ты размечаешь вершины трубопровода на странице PDF для линии {group.line_id}.
Верни только JSON. Нужны точки, которые можно нанести обратно на PDF.
Координаты x/y должны быть в координатах PDF: начало в левом верхнем углу страницы,
единица измерения — PDF point. Изображение страницы передано в масштабе 1:1,
поэтому координата пикселя изображения совпадает с координатой PDF point.
Не возвращай инженерные координаты x/y/z.
Отмечай только видимые геометрические вершины трассы: начало, конец, поворот,
тройник, переход или продолжение. Не отмечай размеры, текст, опоры и таблицы.
Для каждой точки дай короткую подпись (например N01, Поворот 1) и роль.

Страницы и их размеры: {json.dumps([{"page": p.page_number, "width": p.width, "height": p.height} for p in group.pages], ensure_ascii=False)}

Формат ответа:
{{"vertices": [{{"id": "V01", "line_id": "{group.line_id}", "page": 1,
"label": "N01", "role": "поворот", "x": 100.0, "y": 200.0,
"confidence": 0.9, "reason": "видимый стык осевых линий"}}]}}
""".strip()}]
        for page in group.pages[: self.max_image_pages]:
            image_data = self._render_page_data_url(page.page_number, zoom=1.0)
            if image_data:
                content.append({"type": "image_url", "image_url": {"url": image_data}})
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Ты точно размечаешь координаты вершин инженерного чертежа. Возвращай только JSON."},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

    def reconstruct_graph(self, group: LineGroup) -> GraphData:
        if not self.api_key:
            return GraphData()
        candidates = self.candidates_by_line.get(group.line_id, [])
        classifications = self.classifications_by_line.get(group.line_id, [])
        if not candidates:
            return GraphData()
        try:
            started_at = time.perf_counter()
            self._emit(
                "deepseek.graph.payload.start",
                {
                    "line_id": group.line_id,
                    "pages": [page.page_number for page in group.pages],
                    "candidates": len(candidates),
                },
            )
            payload = self._build_graph_payload(group, candidates, classifications)
            self._emit(
                "deepseek.graph.request.start",
                {
                    "line_id": group.line_id,
                    "model": self.model,
                    "payload_kb": round(
                        len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) / 1024, 1
                    ),
                    "timeout_seconds": self.timeout_seconds,
                },
            )
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            self._emit(
                "deepseek.graph.response.received",
                {
                    "line_id": group.line_id,
                    "status_code": response.status_code,
                    "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                    "response_kb": round(len(response.content) / 1024, 1),
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            self._record_provider_trace("graph", group, payload, response=content)
            data = self._parse_json_lenient(content)
            result = self._graph_data_from_payload(data)
            if result.is_empty() and self._graph_retry_count.get(group.line_id, 0) < 1:
                self._graph_retry_count[group.line_id] = self._graph_retry_count.get(group.line_id, 0) + 1
                self._emit(
                    "deepseek.graph.retry",
                    {
                        "line_id": group.line_id,
                        "payload_keys": sorted(data.keys()) if isinstance(data, dict) else str(type(data)),
                    },
                )
                retry_payload = self._build_graph_payload(group, candidates, classifications)
                retry_response = requests.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=retry_payload,
                    timeout=self.timeout_seconds,
                )
                retry_response.raise_for_status()
                retry_content = retry_response.json()["choices"][0]["message"]["content"]
                self._record_provider_trace("graph_retry", group, retry_payload, response=retry_content)
                data = self._parse_json_lenient(retry_content)
                result = self._graph_data_from_payload(data)
            self._emit(
                "deepseek.graph.parse.done",
                {
                    "line_id": group.line_id,
                    "nodes": len(result.nodes),
                    "edges": len(result.edges),
                    "bindings": len(result.dimension_bindings),
                    "unresolved": len(result.unresolved_edges),
                    "payload_keys": sorted(data.keys()) if isinstance(data, dict) else str(type(data)),
                },
            )
            return result
        except Exception as error:
            self._emit("deepseek.graph.error", {"line_id": group.line_id, "error": str(error)})
            return GraphData()

    def _graph_data_from_payload(self, data: Any) -> "GraphData":
        if not isinstance(data, dict):
            return GraphData()
        if "graph" in data and isinstance(data["graph"], dict) and not data.get("nodes"):
            data = data["graph"]
        nodes = data.get("nodes") or data.get("vertices") or []
        edges = data.get("edges") or data.get("links") or []
        bindings = data.get("dimension_bindings") or data.get("bindings") or []
        unresolved = data.get("unresolved_edges") or data.get("unresolved") or []
        ignored = data.get("ignored_candidates") or data.get("ignored") or []
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
        if isinstance(edges, dict):
            edges = list(edges.values())
        return GraphData(
            nodes=[_coerce(GraphNode, item) for item in nodes if isinstance(item, dict)],
            edges=[_coerce(GraphEdge, item) for item in edges if isinstance(item, dict)],
            dimension_bindings=[
                _coerce(DimensionBinding, item) for item in bindings if isinstance(item, dict)
            ],
            unresolved_edges=[_coerce(UnresolvedEdge, item) for item in unresolved if isinstance(item, dict)],
            ignored_candidates=[item for item in ignored if isinstance(item, dict)],
        )

    def _build_graph_payload(
        self,
        group: LineGroup,
        candidates: list[Candidate],
        classifications: list[CandidateClassification],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": self._graph_prompt_for_group(group, candidates, classifications)}
        ]
        if self.include_images and self.pdf_path:
            for page_number in [page.page_number for page in group.pages[: self.max_image_pages]]:
                local_vertices = extract_local_vertices(
                    self.pdf_path,
                    page_number,
                    group.line_id,
                    self.candidates_by_line.get(group.line_id, []),
                )
                image_data = self._render_page_data_url(page_number, vertices=local_vertices)
                if image_data:
                    self._emit(
                        "deepseek.graph.image.ready",
                        {
                            "page": page_number,
                            "image_kb": round(len(image_data.encode("utf-8")) / 1024, 1),
                        },
                    )
                    content.append({"type": "image_url", "image_url": {"url": image_data}})
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты инженерный ассистент, который размечает изометрический чертеж трубопровода "
                        "как граф узлов и ребер. Не считай итоговые длины. Возвращай только валидный JSON "
                        "без Markdown."
                    ),
                },
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

    def _known_vertex_rows_for_group(self, group: LineGroup, candidates: list[Candidate]) -> list[dict[str, Any]]:
        if not self.pdf_path:
            return []
        rows: list[dict[str, Any]] = []
        seen: set[tuple[int, str, float, float]] = set()
        for page in group.pages:
            for vertex in extract_local_vertices(self.pdf_path, page.page_number, group.line_id, candidates):
                key = (vertex.page, vertex.label, round(vertex.x, 2), round(vertex.y, 2))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "id": vertex.id,
                        "page": vertex.page,
                        "label": vertex.label,
                        "role": vertex.role,
                        "x": round(vertex.x, 2),
                        "y": round(vertex.y, 2),
                    }
                )
        return rows

    def _handwheel_rows_for_group(self, group: LineGroup, candidates: list[Candidate]) -> list[dict[str, Any]]:
        """Собрать штурвалы для всех страниц группы.

        Раньше здесь были только вершины текущей страницы, теперь — вершины
        всех страниц группы, а поиск идёт по расширенному алгоритму
        `_find_handwheel_rows` (3 прохода).
        """
        vertices: list[VertexMark] = []
        if self.pdf_path:
            for page in group.pages:
                vertices.extend(
                    extract_local_vertices(
                        self.pdf_path,
                        page.page_number,
                        group.line_id,
                        candidates,
                    )
                )
        return _find_handwheel_rows(candidates, vertices)

    def _handwheel_payload_rows(self, group: LineGroup, candidates: list[Candidate]) -> list[dict[str, Any]]:
        rows = self._handwheel_rows_for_group(group, candidates)
        if not self.pdf_path:
            return rows
        by_page: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            by_page.setdefault(int(row.get("page") or 0), []).append(row)
        try:
            with pymupdf.open(str(self.pdf_path)) as document:
                for page_info in group.pages:
                    details = _handwheel_details(document[page_info.page_number - 1], {"edges": []})
                    for detail in details:
                        detail_page_rows = by_page.setdefault(page_info.page_number, [])
                        detail_center = (
                            (detail["bbox"][0] + detail["bbox"][2]) / 2,
                            (detail["bbox"][1] + detail["bbox"][3]) / 2,
                        )
                        matching = None
                        for row in detail_page_rows:
                            bbox = row.get("bbox") or []
                            if len(bbox) < 4:
                                continue
                            row_center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
                            if abs(row_center[0] - detail_center[0]) <= 8 and abs(row_center[1] - detail_center[1]) <= 8:
                                matching = row
                                break
                        if matching is None:
                            matching = {
                                "id": detail["id"],
                                "page": page_info.page_number,
                                "label": detail["label"],
                                "bbox": detail["bbox"],
                                "confidence": 0.7,
                                "reason": "Найдено напрямую по тексту PDF.",
                            }
                            detail_page_rows.append(matching)
                        matching.update({
                            "arrow_found": detail["arrow_found"],
                            "arrow_start": detail["arrow_start"],
                            "arrow_end": detail["arrow_end"],
                            "edge_id": detail["edge_id"],
                            "edge_distance_px": detail["edge_distance_px"],
                            "edge_created": detail.get("edge_created", False),
                            "edge_kind": detail.get("edge_kind"),
                            "is_pipe_edge": detail.get("is_pipe_edge", False),
                            "edge_reason": detail.get("edge_reason"),
                        })
        except (OSError, IndexError, ValueError):
            return rows
        return [row for page_rows in by_page.values() for row in page_rows]

    def _graph_prompt_for_group(
        self,
        group: LineGroup,
        candidates: list[Candidate],
        classifications: list[CandidateClassification],
    ) -> str:
        pages_text = []
        for page in group.pages:
            pages_text.append(f"--- PAGE {page.page_number} ---\n{page.text[:9000]}")
        candidate_rows = [
            {
                "id": candidate.id,
                "page": candidate.page,
                "kind": candidate.kind,
                "text": candidate.text,
                "zone": candidate.zone,
                "bbox": [round(value, 1) for value in candidate.bbox],
            }
            for candidate in candidates[:220]
        ]
        classification_rows = [
            {
                "candidate_id": item.candidate_id,
                "classification": item.classification,
                "route_type": item.route_type,
                "confidence": item.confidence,
                "reason": item.reason,
            }
            for item in classifications[:220]
        ]
        known_vertex_rows = self._known_vertex_rows_for_group(group, candidates)
        handwheel_rows = self._handwheel_payload_rows(group, candidates)
        connection_rows = []
        if self.pdf_path:
            try:
                vertices_by_page: dict[int, list[dict[str, Any]]] = {}
                for vertex in known_vertex_rows:
                    vertices_by_page.setdefault(int(vertex["page"]), []).append(vertex)
                with pymupdf.open(str(self.pdf_path)) as document:
                    for page_info in group.pages:
                        connection_rows.extend(
                            _connection_rows_from_page(
                                document[page_info.page_number - 1],
                                page_info.page_number,
                                vertices_by_page.get(page_info.page_number, []),
                            )
                        )
            except (OSError, IndexError, ValueError):
                connection_rows = []
        if not connection_rows:
            connection_rows = _find_connection_rows(candidates, [
                VertexMark(
                    id=item["id"],
                    line_id=group.line_id,
                    page=item["page"],
                    label=item["label"],
                    role="unknown",
                    x=item["x"],
                    y=item["y"],
                )
                for item in known_vertex_rows
            ])
        return f"""
Ты анализируешь один или несколько листов изометрического чертежа трубопровода.

Цель: построить граф трубопровода и привязать найденные размерные обозначения к ребрам графа.
Не считай итоговую длину и не агрегируй суммы.

Входные данные:
- идентификатор анализируемой линии: {group.line_id}
- изображение листа (при наличии);
- список текстовых/числовых кандидатов из PDF (id, kind, text, zone, bbox) и их классификация.

Общие правила:
1. Используй графическую часть чертежа как основной источник геометрии.
2. Размерные обозначения с размерными линиями, стрелками или выносками — основной источник длины участка.
3. Текстовые координаты точки задают положение точки, но не длину участка сами по себе.
4. Обозначения диаметра, типоразмера, перехода или арматуры не являются длиной участка.
5. Номера позиций, деталей, опор, выносок и листов не являются длиной участка.
6. Таблицы, спецификации, штампы и легенды не использовать как источник длины.
7. Если у участка есть явный размер и вычислимая координатная разница, приоритет у явного размера.
8. Не дублируй длины: один candidate_id может быть привязан только к одному ребру.
9. Если принадлежность размера к участку неясна, не придумывай связь.
9а. Выношенный размер (число, от которого идёт стрелка/линия к размерной стрелке участка) относится
    к тому участку, на размеры которого указывает цепочка: число -> выноска -> размерная стрелка.
    Проследи цепочку до конца участка; не привязывай число к участку лишь из-за близости.
9б. Короткий размер, параллельный более длинной размерной линии того же направления и в 10-40 px
    от неё — привязка опоры/детали (support_offset): используй его для положения опоры на ребре,
    но НЕ как длину ребра.
9в. Число, значение которого совпадает с координатным перепадом (coordinate_delta того же числа),
    подтверждает длину ребра; их нельзя рассматривать как «координатную проекцию».
9г. Число у DN-подписи или координатного блока подключения без собственной размерной линии со
    стрелками (обычно равно DN в мм) — это проходной размер привязки, а не длина ребра.
10. Если невозможно определить main/branch, поставь path_type "unknown".
11. Если участок виден, но длина не найдена — создай ребро с length_mm=null и добавь его в unresolved_edges.
12. Если часть линии продолжается на другом листе, помечь ребро как continuation в length_source.

Верни строго JSON:
{{
  "nodes": [
    {{"id": "N01", "line_id": "{group.line_id}", "page": 1, "role": "начало|поворот|тройник|переход|конец|продолжение",
      "bbox": [10, 20, 30, 40], "x": 0, "y": 0, "z": 0, "source_candidate_ids": ["C-..."], "confidence": 0.9, "reason": "..."}}
  ],
  "edges": [
    {{"id": "E01", "line_id": "{group.line_id}", "page": 1, "from_node_id": "N01", "to_node_id": "N02",
      "path_type": "main|branch|unknown", "nominal_size": "40", "length_mm": 0,
      "length_source": "dimension|coordinate|continuation|unknown",
      "source_candidate_ids": ["C-..."], "bbox": [10, 20, 40, 60], "confidence": 0.9, "reason": "..."}}
  ],
  "dimension_bindings": [
    {{"candidate_id": "C-...", "edge_id": "E01", "binding_type": "dimension|coordinate|support|ignored", "reason": "..."}}
  ],
  "unresolved_edges": [
    {{"edge_id": "E03", "reason": "нет длины", "suggested_action": "найти свободный размер рядом"}}
  ],
  "ignored_candidates": [
    {{"candidate_id": "C-...", "reason": "..."}}
  ]
}}

Кандидаты:
{json.dumps(candidate_rows, ensure_ascii=False, indent=2)}

Классификация кандидатов:
{json.dumps(classification_rows, ensure_ascii=False, indent=2)}

Известные координаты вершин (PDF point, уже полученные из геометрии чертежа):
{json.dumps(known_vertex_rows, ensure_ascii=False, indent=2)}

Подтвержденные штурвалы/рукоятки/маховики, найденные по ключевым словам и привязке к ближайшей вершине/оси:
{json.dumps(handwheel_rows, ensure_ascii=False, indent=2)}

Подтвержденные метки подключения/продолжения, найденные на рендере и привязанные к ближайшей вершине:
{json.dumps(connection_rows, ensure_ascii=False, indent=2)}

Для каждого штурвала отдельно учитывай поля стрелки:
- arrow_found=true означает, что найден векторный сегмент, один конец которого ближайший к bbox надписи;
- arrow_start и arrow_end задают направление от надписи наружу;
- edge_id используй только если он не null и подтверждается геометрией; null означает, что локально ребро не определено;
- если edge_created=true, это synthetic edge только для привязки штурвала, а не трубное ребро;
- is_pipe_edge=false означает, что этот edge нельзя использовать для длины трубы;
- edge_distance_px является расстоянием до предполагаемого ребра в PDF-пикселях и не является длиной трубы.

Извлеченный текст страниц:
{chr(10).join(pages_text)}
""".strip()

    def analyze_page_or_line(self, group: LineGroup) -> AnalysisResult:
        if not self.api_key:
            return self._failed(group, "Не задан DEEPSEEK_API_KEY.")

        try:
            started_at = time.perf_counter()
            self._emit(
                "deepseek.payload.start",
                {
                    "line_id": group.line_id,
                    "pages": [page.page_number for page in group.pages],
                    "model": self.model,
                    "include_images": self.include_images,
                },
            )
            payload = self._build_payload(group)
            payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            user_content = payload["messages"][1]["content"]
            text_chars = sum(len(item.get("text", "")) for item in user_content if item.get("type") == "text")
            image_count = sum(1 for item in user_content if item.get("type") == "image_url")
            self._emit(
                "deepseek.request.start",
                {
                    "line_id": group.line_id,
                    "model": self.model,
                    "text_chars": text_chars,
                    "image_count": image_count,
                    "payload_kb": round(payload_bytes / 1024, 1),
                    "timeout_seconds": self.timeout_seconds,
                },
            )
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
            elapsed = time.perf_counter() - started_at
            self._emit(
                "deepseek.response.received",
                {
                    "line_id": group.line_id,
                    "status_code": response.status_code,
                    "elapsed_seconds": round(elapsed, 2),
                    "response_kb": round(len(response.content) / 1024, 1),
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            self._record_provider_trace("analyze", group, payload, response=content)
            self._emit(
                "deepseek.response.parse",
                {
                    "line_id": group.line_id,
                    "content_chars": len(content),
                    "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                },
            )
            result = self._parse_result(group, content)
            # Добавляем найденные штурвалы в аннотации результата, чтобы UI
            # на вкладке «Разметка» их отрисовал (render_page_with_annotations
            # уже умеет рисовать kind != "vertex_*" как прямоугольники).
            handwheel_rows = self._handwheel_rows_for_group(
                group, self.candidates_by_line.get(group.line_id, [])
            )
            for row in handwheel_rows:
                bbox = row.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                result.annotations.append(
                    Annotation(
                        id=row.get("id") or f"HW-{len(result.annotations) + 1}",
                        page=int(row.get("page") or 1),
                        label=str(row.get("label") or "штурвал"),
                        kind="valve",
                        bbox=tuple(float(value) for value in bbox[:4]),
                        color="#2563eb",
                    )
                )
            return result
        except Exception as error:
            self._emit(
                "deepseek.error",
                {
                    "line_id": group.line_id,
                    "error": str(error),
                },
            )
            return self._failed(group, f"DeepSeek API error: {error}")

    def _build_payload(self, group: LineGroup) -> dict[str, Any]:
        candidates = self.candidates_by_line.get(group.line_id, [])
        content: list[dict[str, Any]] = [{"type": "text", "text": self._prompt_for_group(group)}]

        # Заранее считаем штурвалы для всей группы — они понадобятся и для картинки,
        # и для события в UI, и для аннотаций результата.
        handwheel_rows = self._handwheel_payload_rows(group, candidates)
        if handwheel_rows:
            self._emit(
                "deepseek.handwheels.found",
                {
                    "line_id": group.line_id,
                    "count": len(handwheel_rows),
                    "handwheels": [
                        {
                            "id": row.get("id"),
                            "page": row.get("page"),
                            "label": row.get("label"),
                            "bbox": row.get("bbox"),
                            "target_vertex_id": row.get("target_vertex_id"),
                            "confidence": row.get("confidence"),
                            "reason": row.get("reason"),
                        }
                        for row in handwheel_rows
                    ],
                },
            )

        if self.include_images and self.pdf_path:
            for page_number in [page.page_number for page in group.pages[: self.max_image_pages]]:
                local_vertices = extract_local_vertices(
                    self.pdf_path,
                    page_number,
                    group.line_id,
                    candidates,
                )
                handwheel_boxes = []
                for row in handwheel_rows:
                    if row.get("page") != page_number:
                        continue
                    bbox = row.get("bbox")
                    if not bbox or len(bbox) < 4:
                        continue
                    try:
                        handwheel_boxes.append({
                            "bbox": [float(value) for value in bbox[:4]],
                            "label": row.get("label"),
                            "target_vertex_id": row.get("target_vertex_id"),
                            "arrow_found": row.get("arrow_found", False),
                            "arrow_start": row.get("arrow_start"),
                            "arrow_end": row.get("arrow_end"),
                            "edge_id": row.get("edge_id"),
                            "edge_distance_px": row.get("edge_distance_px"),
                        })
                    except (TypeError, ValueError):
                        continue
                image_data = self._render_page_data_url(
                    page_number,
                    vertices=local_vertices,
                    handwheels=handwheel_boxes,
                )
                if image_data:
                    self._emit(
                        "deepseek.image.ready",
                        {
                            "page": page_number,
                            "image_kb": round(len(image_data.encode("utf-8")) / 1024, 1),
                            "handwheel_count": len(handwheel_boxes),
                        },
                    )
                    content.append({"type": "image_url", "image_url": {"url": image_data}})

        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты инженерный ассистент для анализа изометрических чертежей трубопроводов. "
                        "Возвращай только валидный JSON без Markdown. Не используй спецификации материалов "
                        "и таблицы готовых длин как источник расчета."
                    ),
                },
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

    def _prompt_for_group(self, group: LineGroup) -> str:
        pages_text = []
        for page in group.pages:
            text = page.text[:9000]
            pages_text.append(f"--- PAGE {page.page_number} ---\n{text}")
        candidates = self.candidates_by_line.get(group.line_id, [])
        classifications = self.classifications_by_line.get(group.line_id, [])
        candidate_rows = [
            {
                "id": candidate.id,
                "page": candidate.page,
                "kind": candidate.kind,
                "text": candidate.text,
                "zone": candidate.zone,
                "bbox": [round(value, 1) for value in candidate.bbox],
            }
            for candidate in candidates[:220]
        ]
        classification_rows = [
            {
                "candidate_id": item.candidate_id,
                "text": item.text,
                "classification": item.classification,
                "route_type": item.route_type,
                "confidence": item.confidence,
                "reason": item.reason,
            }
            for item in classifications[:220]
        ]
        local_vertex_rows = self._known_vertex_rows_for_group(group, candidates)
        drawing_dimensions = [
            {
                "id": candidate.id,
                "page": candidate.page,
                "text": candidate.text,
                "bbox": [round(value, 1) for value in candidate.bbox],
            }
            for candidate in candidates
            if candidate.kind == "dimension" and candidate.zone == "drawing"
        ]

        return f"""
Проанализируй трубопроводную линию {group.line_id}.

Нужно вернуть JSON строго такой формы:
{{
  "lines": [
    {{
      "id": "{group.line_id}",
      "pages": [1],
      "status": "complete|partial|needs_review|failed",
      "main_length_mm": 0,
      "branches_length_mm": 0,
      "total_length_mm": 0,
      "supports_count": 0,
      "valves_count": 0,
      "completeness_note": "..."
    }}
  ],
  "points": [
    {{
      "id": "P01",
      "line_id": "{group.line_id}",
      "role": "начало/поворот/тройник/конец",
      "x": 0,
      "y": 0,
      "z": 0,
      "source": "drawing|calculated|unknown",
      "source_ref": {{"page": 1, "label": "подпись или расчет", "bbox": null}}
    }}
  ],
  "segments": [
    {{
      "id": "E01",
      "line_id": "{group.line_id}",
      "start_point_id": "P01",
      "end_point_id": "P02",
      "dn": "40",
      "length_mm": 0,
      "route_type": "main|branch|unknown",
      "source_size": "размер на чертеже",
      "source_ref": {{"page": 1, "label": "candidate_id + источник размера", "bbox": null}}
    }}
  ],
  "elements": [
    {{
      "id": "S01",
      "element_type": "support|valve|unknown",
      "line_id": "{group.line_id}",
      "bound_to": "E01 или P01",
      "x": null,
      "y": null,
      "z": null,
      "status": "complete|partial|needs_review|failed",
      "source_ref": {{"page": 1, "label": "источник", "bbox": null}}
    }}
  ],
  "uncertainties": [
    {{
      "id": "U01",
      "line_id": "{group.line_id}",
      "page": 1,
      "target": "участок или элемент",
      "reason": "что нельзя подтвердить",
      "severity": "info|warning|critical"
    }}
  ],
  "annotations": [
    {{
      "id": "A01",
      "page": 1,
      "label": "подпись",
      "kind": "segment|point|support|valve|excluded_zone",
      "bbox": [100, 100, 200, 200],
      "color": "#2563eb"
    }}
  ]
}}

Правила:
- Главный объект анализа — красные локальные вершины V01, V02, ... и участки трубы между ними.
- Красные точки и подписи V01, V02, ... уже нанесены на изображение программой. Считай их
    исходной разметкой геометрии и не переименовывай, не удаляй и не добавляй вершины.
- Числовая последовательность меток не задаёт топологию: V06 и V07 не считаются соседними
    автоматически только потому, что их номера идут подряд.
- Создавай segment только между двумя вершинами, если на изображении видна непрерывная ось трубы
    от одной точки до другой. Если между точками есть другая вершина, излом, разрыв или ответвление,
    прямой segment между ними запрещён.
- Размер участка может быть указан одной размерной стрелкой, равной длине участка, либо цепочкой
    непрерывных размерных отрезков, совпадающих с направлением участка от вершины до вершины.
- Для каждой физически соединённой пары вершин создай отдельный segment, но не соединяй точки
    только по близости координат или по порядку их идентификаторов.
- Для каждого segment ищи размерное число на размерной линии, ограниченной стрелками, засечками
    или выносными линиями около этого участка. Вынесенный размер тоже используй, если его линии
    явно указывают на данный участок.
- Привязывай число к участку по направлению размерной линии, стрелок и выносных линий, а не по
    величине числа и не по близости текста к другому числу.
- Любое число из зоны drawing допустимо как длина, включая короткие, длинные или редко
    встречающиеся значения. Не отбрасывай его из-за размера или низкой частоты.
- Если размерная линия читается, но текст числа неуверен, укажи наиболее вероятное значение,
    снизь confidence и добавь uncertainty.
- Категорически не используй длины из координатных блоков X/Y/Z, DN, арматуры, номеров позиций,
    опор, деталей, таблицы "Длины отрезков", спецификации материалов, штампа, легенды, рамки,
    основной надписи или любой области вне графического чертежа.
- Кандидаты с zone != "drawing" запрещены как источники длины.
- Предварительная классификация кандидатов может отсутствовать. Если список классификаций пуст,
    самостоятельно проверь каждый кандидат по изображению, bbox и геометрии.
- Для кандидата kind="dimension" с zone="drawing" выполни собственную проверку по геометрии
    размерной линии; не отбрасывай его из-за отсутствия classification.
- Размер может быть указан не рядом с самим участком. Для короткого или тесного участка число
    может находиться на вынесенной размерной линии, а связь задаваться наводящей линией/стрелкой
    к размерной стрелке или к концам участка. Прослеживай эту цепочку: число -> выноска ->
    размерная линия/стрелки -> участок между двумя локальными вершинами.
- Вынесенная линия считается подтверждением размера только если она примерно исходит от
    соответствующей локальной вершины или явно указывает на конец данного участка. Если выносная
    линия не связана геометрически с вершиной/концом участка, не принимай её число как длину этого
    участка; оставь длину неопределённой или проверь другой размер.
- Не считай близость числа к тексту достаточным доказательством. Нужна геометрическая связь
    через направление линии, стрелки или выноски.
- Если связи или размеры нельзя подтвердить, ставь status="needs_review" или "partial" и добавляй uncertainty.
- Не заменяй неизвестные координаты нулями: используй null.
- В первую очередь используй PDF-кандидаты ниже. Кандидаты из zone != "drawing" нельзя принимать как источник длины трассы.
- Для принятого размера обязательно укажи его candidate_id и bbox. Для размеров из drawing проверяй
    также кандидатов с classification="unknown", если они геометрически относятся к segment.
- Если segment построен из PDF-кандидата, обязательно укажи candidate_id в source_ref.label и bbox этого кандидата в source_ref.bbox.
- В source_ref.label укажи пару вершин, например "V01 -> V02; candidate_id: C-002-0015".
- Не создавай промежуточные вершины и не дели один участок на части только потому, что рядом видны
    несколько чисел; если связь неоднозначна, создай uncertainty.
- Если кандидат является координатой, DN или ссылкой на смежную линию, не превращай его в размер участка.
- Список drawing_dimensions ниже является полным списком числовых кандидатов из зоны чертежа;
    проверь каждый из них, включая короткие числа. Не ограничивайся первыми/самыми крупными.
- Кандидаты kind="coordinate_delta" являются программно вычисленными перепадами координат; их можно использовать как участок только если перепад объясняет реальный осевой/вертикальный отрезок трассы.
- Если число похоже на привязку опоры или позицию детали, пометь его как неопределенность, а не как участок.
- Размер, значение которого равно координатному перепаду (kind="coordinate_delta" с тем же числом),
    ПОДТВЕРЖДАЕТ осевой участок: это длина участка, а не «координатная проекция». Используйте его
    как длину участка, направление которого совпадает по оси с этим перепадом.
- Если к одному участку относятся несколько размеров, выберите тот, чья размерная линия коллинеарна
    (лежит на той же прямой/направлении, что и ось участка) или соединена с осью выноской.
    Короткие размеры, параллельные основной размерной линии этого же участка и смещённые от неё
    на 10-40 px, — это привязка опоры/детали (support_offset), а не длина участка: их можно
    использовать только для положения опоры, но не как длину ребра.
- Размер может применять разными стрелками: тиковый размер-выноска (короткая стрелка от числа,
    указывающая в размерную стрелку участка) — это полноценный размер участка. Прослеживайте
    цепочку: число -> Leader-стрелка -> размерная стрелка/концы участка.
- Метка DN или водаhall "40" возле координатного блока/подключения не является длиной: DN-подписи
    и проходные размеры (равные DN в мм) не считаются длиной участка.
- Возвращай только JSON.

PDF-кандидаты:
{json.dumps(candidate_rows, ensure_ascii=False, indent=2)}

Локальные вершины, предварительно найденные кодом по толстым осевым линиям:
{json.dumps(local_vertex_rows, ensure_ascii=False, indent=2)}

Все числовые размеры именно из зоны drawing:
{json.dumps(drawing_dimensions, ensure_ascii=False, indent=2)}

Классификация PDF-кандидатов:
{json.dumps(classification_rows, ensure_ascii=False, indent=2)}

Извлеченный текст страниц:
{chr(10).join(pages_text)}
""".strip()

    def _build_classification_payload(self, group: LineGroup, candidates: list[Candidate]) -> dict[str, Any]:
        rows = [
            {
                "id": candidate.id,
                "page": candidate.page,
                "kind": candidate.kind,
                "text": candidate.text,
                "zone": candidate.zone,
                "bbox": [round(value, 1) for value in candidate.bbox],
            }
            for candidate in candidates[:260]
        ]
        prompt = f"""
Классифицируй PDF-кандидаты для линии {group.line_id}.

Верни только JSON:
{{
  "classifications": [
    {{
      "id": "CC001",
      "line_id": "{group.line_id}",
      "page": 2,
      "candidate_id": "C-002-0001",
      "text": "1950",
      "classification": "route_segment|support_offset|adjacent_line|coordinate|dn|excluded_table|unknown",
      "route_type": "main|branch|unknown",
      "reason": "краткое проверяемое объяснение",
      "confidence": 0.0,
      "accepted": false,
      "gate_reason": ""
    }}
  ]
}}

Правила классификации:
- route_segment: размер из зоны drawing, который покрывает ось трубопровода ИЛИ направлен на неё
  выноской/стрелкой (вынесенный размер, в том числе короткий участок, соединяющий подключение
  с первым поворотом). Значение, совпадающее с координатным перепадом (kind="coordinate_delta"
  того же числа), подтверждает длину, а не опровергает её.
- support_offset: размер, который задаёт положение опоры/детали: короткий размер, параллельный
  более длинной размерной линии того же направления и смещённый от неё (сдвоенный размер у оси),
  либо размер у выноски опоры/листа позиции (О-метки, СМ.-ссылки).
- adjacent_line: ссылка, координата или размер, относящиеся к смежной линии/подключению, а не к
  длине текущей линии; также число рядом с "СМ." является ссылкой на смежный лист/линию.
- coordinate: X/Y/Z подписи и значения координат; kind="coordinate_delta" классифицируется как
  route_segment, если перепад подтверждает реальный осевой/вертикальный отрезок трассы, иначе
  coordinate.
- dn: DN-подписи и переходы диаметров; число, равное DN-подписи у подключения (например 40 рядом
  с DN40), НЕ является длиной участка — классифицируй как dn.
- excluded_table: все кандидаты из materials_spec, length_table или title_block.
- unknown: если нельзя доказать назначение даже после проверки выносок и стрелок.
- Не путай размер участка с DN: если рядом есть подпись DN40/DN40X25/DN100X40, число 35..100,
  стоящее вблизи выноски подключения/перехода, может быть значением "проходного размера",
  а не длиной. Проверь, есть ли у числа собственная размерная линия со стрелками — если нет,
  это dn/support_offset, а не длина.
- confidence >= 0.7 ставь только если кандидат точно является участком трассы и точно известен main/branch.
- route_type="unknown" ставь, если принадлежность main/branch нельзя доказать.
- Поля accepted и gate_reason оставляй как false/пусто: их заполнит программа.

Кандидаты:
{json.dumps(rows, ensure_ascii=False, indent=2)}
""".strip()
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты классифицируешь извлеченные из PDF кандидаты. Возвращай только валидный JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

    def _render_page_data_url(
        self,
        page_number: int,
        zoom: float = 1.0,
        vertices: list[VertexMark] | None = None,
        handwheels: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if not self.pdf_path:
            return None
        document = pymupdf.open(str(self.pdf_path))
        try:
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            image_bytes = pixmap.tobytes("jpeg", jpg_quality=75)
        finally:
            document.close()
        if vertices or handwheels:
            from PIL import Image, ImageDraw, ImageFont

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            draw = ImageDraw.Draw(image, "RGBA")
            try:
                font = ImageFont.truetype("arial.ttf", size=max(9, int(9 * zoom)))
            except Exception:
                font = ImageFont.load_default()
            if vertices:
                for vertex in vertices:
                    x = vertex.x * zoom
                    y = vertex.y * zoom
                    draw.ellipse(
                        (x - 6, y - 6, x + 6, y + 6),
                        fill=(220, 38, 38, 255),
                        outline=(255, 255, 255, 255),
                        width=2,
                    )
                    draw.text(
                        (x + 8, y - 14),
                        vertex.label,
                        fill=(220, 38, 38, 255),
                        font=font,
                        stroke_width=1,
                        stroke_fill=(255, 255, 255, 220),
                    )
            if handwheels:
                for item in handwheels:
                    bbox = item.get("bbox") or []
                    if len(bbox) < 4:
                        continue
                    try:
                        x0, y0, x1, y1 = [float(value) * zoom for value in bbox[:4]]
                    except (TypeError, ValueError):
                        continue
                    try:
                        draw.rectangle((x0, y0, x1, y1), outline=(37, 99, 235, 255), width=3)
                        label = str(item.get("label") or "штурвал")[:28]
                        # Небольшой фон под подписью, чтобы читалось поверх чертежа.
                        text_y = max(0.0, y0 - 16)
                        draw.rectangle(
                            (x0 + 2, text_y, x0 + 8 + len(label) * 7, text_y + 14),
                            fill=(255, 255, 255, 220),
                        )
                        draw.text(
                            (x0 + 4, text_y + 1),
                            label,
                            fill=(37, 99, 235, 255),
                            font=font,
                        )
                    except Exception:
                        # Никогда не падаем из-за одной битой аннотации.
                        continue
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85)
            image_bytes = output.getvalue()
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    def _parse_result(self, group: LineGroup, content: str) -> AnalysisResult:
        data = self._parse_json_lenient(content)
        return AnalysisResult(
            lines=[_coerce(LineResult, item) for item in data.get("lines", [])],
            points=[_coerce(PointResult, item) for item in data.get("points", [])],
            segments=[_coerce(SegmentResult, item) for item in data.get("segments", [])],
            elements=[_coerce(ElementResult, item) for item in data.get("elements", [])],
            uncertainties=[_coerce(Uncertainty, item) for item in data.get("uncertainties", [])],
            annotations=[_coerce(Annotation, item) for item in data.get("annotations", [])],
            vertices=[_coerce(VertexMark, item) for item in data.get("vertices", [])],
            candidate_classifications=[
                _coerce(CandidateClassification, item)
                for item in data.get("candidate_classifications", [])
            ],
        )

    def _strip_json(self, content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        return text

    def _parse_json_lenient(self, content: str) -> dict[str, Any]:
        text = self._strip_json(content)
        try:
            return json.loads(text)
        except ValueError:
            splitters = ["\n]\n", "}\n{", "}\n\""]
            for splitter in splitters:
                if splitter in text:
                    candidate_text = text.split(splitter)[0]
                    if splitter != "}\n{":
                        candidate_text = candidate_text + splitter.strip()
                    try:
                        return json.loads(candidate_text)
                    except ValueError:
                        continue
            decoder = json.JSONDecoder()
            for index, char in enumerate(text):
                if char == "{":
                    try:
                        obj, _end = decoder.raw_decode(text[index:])
                        return obj
                    except ValueError:
                        continue
            raise

    def _failed(self, group: LineGroup, reason: str) -> AnalysisResult:
        pages = [page.page_number for page in group.pages]
        return AnalysisResult(
            lines=[
                LineResult(
                    id=group.line_id,
                    pages=pages,
                    status="failed",
                    completeness_note=reason,
                )
            ],
            uncertainties=[
                Uncertainty(
                    id=f"U-{group.line_id}-DEEPSEEK",
                    line_id=group.line_id,
                    page=pages[0] if pages else None,
                    target=group.line_id,
                    reason=reason,
                    severity="critical",
                )
            ],
        )

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(event, payload)


def _coerce(model_type: type, raw: dict[str, Any]) -> Any:
    values = dict(raw)
    if "source_ref" in values and isinstance(values["source_ref"], dict):
        values["source_ref"] = _coerce(SourceRef, values["source_ref"])
    if "bbox" in values and isinstance(values["bbox"], list):
        values["bbox"] = tuple(values["bbox"])

    allowed = {field.name for field in fields(model_type)}
    filtered = {key: value for key, value in values.items() if key in allowed}
    return model_type(**filtered)