from __future__ import annotations

import base64
import io
import json
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
from .route_reconstruction import extract_local_vertices


EventCallback = Callable[[str, dict[str, Any]], None]


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
            return self._parse_result(group, content)
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
        content: list[dict[str, Any]] = [{"type": "text", "text": self._prompt_for_group(group)}]
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
                        "deepseek.image.ready",
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
        local_vertex_rows = []
        for page in group.pages:
            local_vertices = extract_local_vertices(
                self.pdf_path,
                page.page_number,
                group.line_id,
                candidates,
            ) if self.pdf_path else []
            local_vertex_rows.extend(
                {
                    "id": vertex.id,
                    "page": vertex.page,
                    "label": vertex.label,
                    "role": vertex.role,
                    "x": vertex.x,
                    "y": vertex.y,
                }
                for vertex in local_vertices
            )
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
        if vertices:
            from PIL import Image, ImageDraw, ImageFont

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            draw = ImageDraw.Draw(image, "RGBA")
            try:
                font = ImageFont.truetype("arial.ttf", size=max(9, int(9 * zoom)))
            except Exception:
                font = ImageFont.load_default()
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
