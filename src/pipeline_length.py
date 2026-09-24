from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from . import prompts


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 180
DEFAULT_CODEX_TIMEOUT_SECONDS = 300
DEFAULT_PIPELINE_LENGTH_MAX_TOKENS = 8192
MAX_PIPELINE_LENGTH_MAX_TOKENS = 16000
MAX_PROVIDER_TIMEOUT_SECONDS = 600
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024


def _bounded_timeout(env_name: str, default: int) -> int:
    try:
        value = int(os.getenv(env_name, str(default)))
    except ValueError:
        value = default
    return max(1, min(value, MAX_PROVIDER_TIMEOUT_SECONDS))


def _pipeline_length_max_tokens() -> int:
    try:
        value = int(os.getenv("PIPELINE_LENGTH_MAX_TOKENS", str(DEFAULT_PIPELINE_LENGTH_MAX_TOKENS)))
    except ValueError:
        value = DEFAULT_PIPELINE_LENGTH_MAX_TOKENS
    return max(1024, min(value, MAX_PIPELINE_LENGTH_MAX_TOKENS))


def _base_vertices(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = mapping.get("base_vertices") or mapping.get("vertices") or []
    return [dict(row) for row in rows if str(row.get("id", "")).startswith("V")]


def _base_edges(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = mapping.get("base_edges") or mapping.get("edges") or []
    return [
        dict(row)
        for row in rows
        if row.get("id") and not str(row.get("id")).startswith("F-")
    ]


def _dimension_row(dimension: dict[str, Any], page_number: int) -> dict[str, Any]:
    edge_id = dimension.get("edge_id")
    dimension_stroke = dimension.get("dimension_stroke") or {}
    leader_stroke = dimension.get("leader_stroke") or {}
    extension_strokes = dimension.get("extension_strokes") or []
    return {
        "id": dimension.get("id"),
        "candidate_key": f"{page_number}:{dimension.get('id')}",
        "page": page_number,
        "value_mm": float(dimension.get("value", 0.0)),
        "text": dimension.get("text", ""),
        "bbox": dimension.get("bbox"),
        "label_center": dimension.get("label_center"),
        "edge_id": edge_id,
        "edge_candidates": ([{"edge_id": edge_id, "source": "local_dimension_mapping"}] if edge_id else []),
        "status": dimension.get("status"),
        "hints": dimension.get("hints", []),
        "geometry": {
            "dimension_stroke": {
                "start": dimension_stroke.get("start"),
                "end": dimension_stroke.get("end"),
                "length_px": dimension_stroke.get("length_px"),
            } if dimension_stroke else None,
            "leader_stroke": {
                "start": leader_stroke.get("start"),
                "end": leader_stroke.get("end"),
                "length_px": leader_stroke.get("length_px"),
            } if leader_stroke else None,
            "extension_strokes": [
                {
                    "start": stroke.get("start"),
                    "end": stroke.get("end"),
                    "pipe_edge_id": stroke.get("pipe_edge_id"),
                    "target_endpoint": stroke.get("target_endpoint"),
                }
                for stroke in extension_strokes[:2]
            ],
            "attachment_kind": dimension.get("attachment_kind"),
            "attachment_source": dimension.get("attachment_source"),
        },
        "local_decision": {
            "decision": dimension.get("local_filter_decision"),
            "reason": dimension.get("local_filter_reason"),
            "conflict_with": dimension.get("local_filter_conflict_with"),
        },
    }


def _coordinate_rows(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, row in enumerate(mapping.get("coordinates") or [], start=1):
        rows.append(
            {
                "id": row.get("id") or f"C{index:03d}",
                "label": row.get("label"),
                "value": row.get("value"),
                "label_bbox": row.get("label_bbox"),
                "value_bbox": row.get("value_bbox"),
            }
        )
    return rows


def build_pipeline_length_page(mapping: dict[str, Any], page_number: int) -> dict[str, Any]:
    dimensions = [_dimension_row(row, page_number) for row in mapping.get("dimensions") or []]
    annotations = mapping.get("handwheel_annotations") or {}
    handwheels = [
        {
            "id": row.get("id"),
            "label": row.get("label"),
            "bbox": row.get("bbox"),
            "edge_id": row.get("edge_id"),
            "arrow_found": row.get("arrow_found"),
            "arrow_end": row.get("arrow_end"),
        }
        for row in annotations.get("handwheels") or []
    ]
    glyphs = [
        {
            "id": row.get("id"),
            "center": row.get("center"),
            "matched_source": row.get("matched_source"),
        }
        for row in annotations.get("glyphs") or []
    ]
    return {
        "page": page_number,
        "vertices": _base_vertices(mapping),
        "edges": _base_edges(mapping),
        "coordinates": _coordinate_rows(mapping),
        "dimensions": dimensions,
        "local_decisions": [
            dict(row)
            for row in mapping.get("local_decisions") or [
                {
                    "candidate_id": row.get("id"),
                    "candidate_key": f"{page_number}:{row.get('id')}",
                    "value_mm": row.get("value"),
                    "decision": row.get("local_filter_decision"),
                    "reason": row.get("local_filter_reason"),
                    "conflict_with": row.get("local_filter_conflict_with"),
                    "status": row.get("status"),
                }
                for row in mapping.get("dimensions") or []
            ]
        ],
        "connections": list(mapping.get("connections") or []),
        "discarded_numbers": [],
        "handwheels": handwheels,
        "handwheel_glyphs": glyphs,
    }


def build_pipeline_length_payload(
    line_id: str,
    source_name: str,
    page_mappings: list[tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    pages = [build_pipeline_length_page(mapping, page) for page, mapping in sorted(page_mappings, key=lambda item: item[0])]
    return {
        "analysis_type": "pipeline_length",
        "line_id": line_id,
        "source_name": source_name,
        "pages": pages,
        "cross_page_context": {
            "line_id": line_id,
            "page_order": [page["page"] for page in pages],
            "available_connections": [connection for page in pages for connection in page["connections"]],
        },
    }


def build_pipeline_length_page_payload(payload: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    """Return a provider-sized payload for one page with line context kept compact."""
    pages = payload.get("pages") or []
    return {
        "analysis_type": payload.get("analysis_type", "pipeline_length"),
        "line_id": payload.get("line_id"),
        "source_name": payload.get("source_name"),
        "pages": [page],
        "cross_page_context": {
            "line_id": payload.get("line_id"),
            "page_order": [item.get("page") for item in pages],
            "current_page": page.get("page"),
            "available_connections": [connection for item in pages for connection in item.get("connections") or []],
        },
    }


def calculate_local_length_summary(payload: dict[str, Any]) -> dict[str, Any]:
    included: list[str] = []
    excluded: list[str] = []
    ambiguous: list[str] = []
    candidate_estimates: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    clean = dirty = uncertain = 0.0
    for page in payload.get("pages") or []:
        page_clean = page_dirty = page_uncertain = 0.0
        for row in page.get("dimensions") or []:
            candidate_id = row.get("id")
            value = float(row.get("value_mm") or 0.0)
            decision = (row.get("local_decision") or {}).get("decision")
            candidate_estimates.append({
                "candidate_id": candidate_id,
                "candidate_key": row.get("candidate_key") or f"{page.get('page')}:{candidate_id}",
                "page": page.get("page"),
                "value_mm": value,
                "local_decision": decision,
                "edge_id": row.get("edge_id"),
                "reason": (row.get("local_decision") or {}).get("reason"),
            })
            if decision == "include":
                included.append(candidate_id)
                clean += value
                dirty += value
                page_clean += value
                page_dirty += value
            elif decision == "exclude":
                excluded.append(candidate_id)
            else:
                ambiguous.append(candidate_id)
                dirty += value
                uncertain += value
                page_dirty += value
                page_uncertain += value
        page_summaries.append({
            "page": page.get("page"),
            "clean_length_mm": round(page_clean, 2),
            "dirty_length_mm": round(page_dirty, 2),
            "ambiguous_length_mm": round(page_uncertain, 2),
        })
    return {
        "clean_length_mm": round(clean, 2),
        "dirty_length_mm": round(dirty, 2),
        "ambiguous_length_mm": round(uncertain, 2),
        "included_candidate_ids": included,
        "excluded_candidate_ids": excluded,
        "ambiguous_candidate_ids": ambiguous,
        "candidate_estimates": candidate_estimates,
        "page_summaries": page_summaries,
        "main": {"clean_length_mm": round(clean, 2), "dirty_length_mm": round(dirty, 2), "ambiguous_length_mm": round(uncertain, 2)},
        "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
    }


def build_pipeline_length_text(payload: dict[str, Any]) -> str:
    lines = [
        f"ANALYSIS: {payload.get('analysis_type')}",
        f"LINE: {payload.get('line_id')}",
        f"SOURCE: {payload.get('source_name')}",
    ]
    for page in payload.get("pages") or []:
        lines.append(f"PAGE {page.get('page')}:")
        lines.append("VERTICES: " + ", ".join(str(vertex.get("id")) for vertex in page.get("vertices") or []))
        for edge in page.get("edges") or []:
            lines.append(f"EDGE {edge.get('id')} {edge.get('from_vertex') or edge.get('from_node_id')} -> {edge.get('to_vertex') or edge.get('to_node_id')} pixel_length={edge.get('pixel_length')}")
        for dimension in page.get("dimensions") or []:
            local = dimension.get("local_decision") or {}
            lines.append(f"CANDIDATE {dimension.get('candidate_key')} local_id={dimension.get('id')} value_mm={dimension.get('value_mm')} edge={dimension.get('edge_id')} local={local.get('decision')} reason={local.get('reason')}")
        for coordinate in page.get("coordinates") or []:
            lines.append(f"COORDINATE {coordinate.get('id')} {coordinate.get('label')}={coordinate.get('value')} label_bbox={coordinate.get('label_bbox')} value_bbox={coordinate.get('value_bbox')}")
        for handwheel in page.get("handwheels") or []:
            lines.append(f"HANDWHEEL {handwheel.get('id')} label={handwheel.get('label')} edge={handwheel.get('edge_id')} arrow_end={handwheel.get('arrow_end')}")
        for glyph in page.get("handwheel_glyphs") or []:
            lines.append(f"HANDWHEEL_GLYPH {glyph.get('id')} center={glyph.get('center')} source={glyph.get('matched_source')}")
    return "\n".join(lines) + "\n"


def _parse_json(text: str) -> dict[str, Any]:
    original = text or ""
    text = original.strip()
    if not text:
        raise ValueError("Провайдер вернул пустой ответ вместо JSON")
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            preview = text[:300].replace("\n", " ")
            raise ValueError(f"Провайдер вернул не JSON. Начало ответа: {preview}") from None
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError as error:
            preview = text[:300].replace("\n", " ")
            raise ValueError(f"Провайдер вернул поврежденный JSON: {error.msg}. Начало ответа: {preview}") from None
    if not isinstance(result, dict):
        raise ValueError("pipeline_length provider response must be an object")
    return result


def _provider_failure_details(raw: dict[str, Any], choice: dict[str, Any]) -> str:
    usage = raw.get("usage") or {}
    message = choice.get("message") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    parts = [
        f"finish_reason={choice.get('finish_reason') or 'unknown'}",
        f"prompt_tokens={usage.get('prompt_tokens', 'unknown')}",
        f"completion_tokens={usage.get('completion_tokens', 'unknown')}",
        f"reasoning_tokens={completion_details.get('reasoning_tokens', 'unknown')}",
        f"content_chars={len(message.get('content') or '')}",
        f"reasoning_chars={len(message.get('reasoning_content') or '')}",
    ]
    return ", ".join(parts)


def run_pipeline_length_provider(
    payload: dict[str, Any],
    api_key: str,
    model: str,
    *,
    provider: str = "deepseek",
) -> dict[str, Any]:
    started = time.perf_counter()
    revision = prompts.load_prompt_revision("pipeline_length")
    prompt = revision["text"]
    provider_prompt = (
        "Верни только компактный JSON. Не пиши ход рассуждений, пояснения вне JSON или markdown. "
        "reason делай коротким: до 12 слов.\n\n"
        + prompt
    )
    request_text = json.dumps(payload, ensure_ascii=False)
    provider = (provider or "deepseek").strip().lower()
    if provider == "deepseek":
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY не задан в .env")
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        request_max_tokens = _pipeline_length_max_tokens()
        retry_max_tokens = max(request_max_tokens * 2, DEFAULT_PIPELINE_LENGTH_MAX_TOKENS)
        token_attempts = [request_max_tokens]
        if retry_max_tokens > request_max_tokens:
            token_attempts.append(min(retry_max_tokens, MAX_PIPELINE_LENGTH_MAX_TOKENS))
        last_error = None
        raw: dict[str, Any] = {}
        answer: dict[str, Any] | None = None
        for attempt_index, max_tokens in enumerate(token_attempts, start=1):
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": provider_prompt},
                        {"role": "user", "content": request_text},
                    ],
                },
                timeout=_bounded_timeout("PIPELINE_LENGTH_DEEPSEEK_TIMEOUT", DEFAULT_DEEPSEEK_TIMEOUT_SECONDS),
            )
            response.raise_for_status()
            if len(response.content) > MAX_PROVIDER_RESPONSE_BYTES:
                raise RuntimeError("Ответ провайдера превысил лимит 4 МБ")
            raw = response.json()
            choice = (raw.get("choices") or [{}])[0]
            content = ((choice.get("message") or {}).get("content") or "")
            try:
                answer = _parse_json(content)
                break
            except ValueError as error:
                details = _provider_failure_details(raw, choice)
                last_error = RuntimeError(f"{error}. {details}, attempt={attempt_index}, max_tokens={max_tokens}")
                if choice.get("finish_reason") != "length" or attempt_index == len(token_attempts):
                    raise last_error from None
        if answer is None:
            raise last_error or RuntimeError("Провайдер не вернул JSON")
        trace = {
            "response_raw": raw,
            "provider": provider,
            "request_max_tokens": token_attempts[0],
            "attempts": attempt_index,
            "used_max_tokens": max_tokens,
        }
    elif provider == "codex_cli":
        with tempfile.TemporaryDirectory(prefix="codex-pipeline-length-") as temp_dir:
            schema_path = Path(temp_dir) / "pipeline_length_response.schema.json"
            output_path = Path(temp_dir) / "last_message.json"
            schema_path.write_text(
                json.dumps(_response_schema(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            command = [
                "codex", "exec", "--ephemeral", "--ignore-rules", "--skip-git-repo-check",
                "--sandbox", "read-only", "--ask-for-approval", "never", "--cd", str(Path.cwd()),
                "--output-schema", str(schema_path), "--output-last-message", str(output_path), "-",
            ]
            response = subprocess.run(
                command,
                input=("SYSTEM_PROMPT:\n" + provider_prompt + "\n\nPAYLOAD_JSON:\n" + request_text),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_bounded_timeout("PIPELINE_LENGTH_CODEX_TIMEOUT", DEFAULT_CODEX_TIMEOUT_SECONDS),
            )
            output_text = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else response.stdout
            if len(output_text.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES:
                raise RuntimeError("Ответ провайдера превысил лимит 4 МБ")
            if response.returncode != 0:
                raise RuntimeError(f"codex CLI завершился с ошибкой {response.returncode}: {(response.stderr or output_text)[:1200]}")
            answer = _parse_json(output_text)
            trace = {"response_raw": {"stdout": response.stdout, "stderr": response.stderr, "returncode": response.returncode}, "provider": provider}
    else:
        raise RuntimeError(f"Неизвестный провайдер анализа: {provider}")
    usage = (trace.get("response_raw") or {}).get("usage") or {}
    return {
        "answer": answer,
        "payload": payload,
        "prompt": prompt,
        "prompt_name": revision["name"],
        "prompt_version": revision["version"],
        "prompt_sha256": revision["sha256"],
        "prompt_source": revision["source"],
        "model": model,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        **trace,
    }


def _prefix_page_id(page_number: Any, value: Any) -> Any:
    if value is None:
        return value
    text = str(value)
    if ":" in text or not text:
        return text
    return f"{page_number}:{text}"


def _normalize_page_answer(answer: dict[str, Any], page_number: Any) -> dict[str, Any]:
    normalized = dict(answer or {})
    assessments = []
    for item in normalized.get("candidate_assessments") or []:
        row = dict(item)
        row["candidate_id"] = _prefix_page_id(page_number, row.get("candidate_id"))
        assessments.append(row)
    normalized["candidate_assessments"] = assessments

    edge_routes = []
    for item in normalized.get("edge_routes") or []:
        row = dict(item)
        row["edge_id"] = _prefix_page_id(page_number, row.get("edge_id"))
        edge_routes.append(row)
    normalized["edge_routes"] = edge_routes

    branches = []
    for item in normalized.get("branch_routes") or normalized.get("branches") or []:
        row = dict(item)
        row["edge_ids"] = [_prefix_page_id(page_number, edge_id) for edge_id in row.get("edge_ids") or []]
        row["candidate_ids"] = [_prefix_page_id(page_number, candidate_id) for candidate_id in row.get("candidate_ids") or []]
        branches.append(row)
    normalized["branch_routes"] = branches

    segments = []
    for item in normalized.get("route_segments") or []:
        row = dict(item)
        row["candidate_ids"] = [_prefix_page_id(page_number, candidate_id) for candidate_id in row.get("candidate_ids") or []]
        row.setdefault("from_page", page_number)
        row.setdefault("to_page", page_number)
        segments.append(row)
    normalized["route_segments"] = segments

    cross_sheet_links = []
    for item in normalized.get("cross_sheet_links") or []:
        row = dict(item)
        row.setdefault("from_page", page_number)
        row.setdefault("page", page_number)
        cross_sheet_links.append(row)
    normalized["cross_sheet_links"] = cross_sheet_links

    intermediate_distances = []
    for item in normalized.get("intermediate_distances") or []:
        row = dict(item)
        row.setdefault("from_page", page_number)
        row.setdefault("page", page_number)
        if row.get("candidate_id"):
            row["candidate_id"] = _prefix_page_id(page_number, row.get("candidate_id"))
        intermediate_distances.append(row)
    normalized["intermediate_distances"] = intermediate_distances

    vertex_coordinates = []
    for item in normalized.get("vertex_coordinates") or normalized.get("points") or []:
        row = dict(item)
        vertex_id = row.get("vertex_id") or row.get("id")
        if vertex_id:
            row["vertex_id"] = _prefix_page_id(page_number, vertex_id)
            row.setdefault("local_vertex_id", vertex_id)
        row.setdefault("page", page_number)
        vertex_coordinates.append(row)
    normalized["vertex_coordinates"] = vertex_coordinates
    return normalized


def merge_pipeline_length_provider_traces(
    payload: dict[str, Any],
    page_traces: list[dict[str, Any]],
) -> dict[str, Any]:
    combined_answer: dict[str, Any] = {
        "candidate_assessments": [],
        "edge_routes": [],
        "branch_routes": [],
        "route_segments": [],
        "cross_sheet_links": [],
        "intermediate_distances": [],
        "vertex_coordinates": [],
        "lengths": {
            "main": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
            "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0},
            "total_clean_length_mm": 0.0,
            "total_dirty_length_mm": 0.0,
            "total_ambiguous_length_mm": 0.0,
        },
    }
    response_raw = {"page_responses": []}
    elapsed = 0.0
    prompt_tokens = completion_tokens = total_tokens = 0
    has_prompt_tokens = has_completion_tokens = has_total_tokens = False
    first_trace = page_traces[0] if page_traces else {}
    for trace in page_traces:
        page_payload = trace.get("payload") or {}
        page_number = ((page_payload.get("pages") or [{}])[0] or {}).get("page")
        answer = _normalize_page_answer(trace.get("answer") or {}, page_number)
        for key in ("candidate_assessments", "edge_routes", "branch_routes", "route_segments", "cross_sheet_links", "intermediate_distances", "vertex_coordinates"):
            combined_answer[key].extend(answer.get(key) or [])
        response_raw["page_responses"].append({
            "page": page_number,
            "provider": trace.get("provider"),
            "model": trace.get("model"),
            "elapsed_seconds": trace.get("elapsed_seconds"),
            "prompt_tokens": trace.get("prompt_tokens"),
            "completion_tokens": trace.get("completion_tokens"),
            "total_tokens": trace.get("total_tokens"),
            "response_raw": trace.get("response_raw"),
        })
        elapsed += float(trace.get("elapsed_seconds") or 0.0)
        if trace.get("prompt_tokens") is not None:
            prompt_tokens += int(trace.get("prompt_tokens") or 0)
            has_prompt_tokens = True
        if trace.get("completion_tokens") is not None:
            completion_tokens += int(trace.get("completion_tokens") or 0)
            has_completion_tokens = True
        if trace.get("total_tokens") is not None:
            total_tokens += int(trace.get("total_tokens") or 0)
            has_total_tokens = True
    return {
        "answer": combined_answer,
        "payload": payload,
        "page_traces": [
            {
                "page": ((trace.get("payload") or {}).get("pages") or [{}])[0].get("page"),
                "elapsed_seconds": trace.get("elapsed_seconds"),
                "prompt_tokens": trace.get("prompt_tokens"),
                "completion_tokens": trace.get("completion_tokens"),
                "total_tokens": trace.get("total_tokens"),
            }
            for trace in page_traces
        ],
        "prompt": first_trace.get("prompt", ""),
        "prompt_name": first_trace.get("prompt_name", "pipeline_length"),
        "prompt_version": first_trace.get("prompt_version"),
        "prompt_sha256": first_trace.get("prompt_sha256"),
        "prompt_source": first_trace.get("prompt_source"),
        "model": first_trace.get("model"),
        "provider": first_trace.get("provider"),
        "elapsed_seconds": round(elapsed, 3),
        "prompt_tokens": prompt_tokens if has_prompt_tokens else None,
        "completion_tokens": completion_tokens if has_completion_tokens else None,
        "total_tokens": total_tokens if has_total_tokens else None,
        "response_raw": response_raw,
    }


def _response_schema() -> dict[str, Any]:
    return json.loads((ROOT / "schemas" / "pipeline_length_response.schema.json").read_text(encoding="utf-8"))


def build_pipeline_length_result(payload: dict[str, Any], provider_answer: dict[str, Any]) -> dict[str, Any]:
    provider_result = dict(provider_answer or {})
    provider_result["calculated_lengths"] = calculate_provider_length_summary(payload, provider_result)
    return {
        "analysis_type": "pipeline_length",
        "local_result": calculate_local_length_summary(payload),
        "provider_result": provider_result,
        "manual_confirmation_required": True,
        "applied_provider_changes": False,
    }


def calculate_provider_length_summary(
    payload: dict[str, Any],
    provider: dict[str, Any],
    manual_confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate a separate provider-side estimate from its candidate reviews."""
    assessments = {
        str(item.get("candidate_id")): item
        for item in provider.get("candidate_assessments") or []
        if item.get("candidate_id")
    }
    routes = {}
    for item in provider.get("edge_routes") or []:
        edge_id = item.get("edge_id")
        if edge_id:
            routes[str(edge_id)] = item.get("route_type")
    branch_specs = provider.get("branch_routes") or provider.get("branches") or []
    branch_by_candidate: dict[str, str] = {}
    branch_by_edge: dict[str, str] = {}
    branches: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(branch_specs, start=1):
        branch_id = str(item.get("branch_id") or item.get("id") or f"branch-{index:02d}")
        branches[branch_id] = {
            "branch_id": branch_id,
            "name": item.get("name") or branch_id,
            "junction_vertex_id": item.get("junction_vertex_id"),
            "endpoint_vertex_id": item.get("endpoint_vertex_id"),
            "edge_ids": list(item.get("edge_ids") or []),
            "candidate_ids": list(item.get("candidate_ids") or []),
            "clean_length_mm": 0.0,
            "dirty_length_mm": 0.0,
            "ambiguous_length_mm": 0.0,
            "counted_candidate_ids": [],
        }
        for candidate_id in branches[branch_id]["candidate_ids"]:
            branch_by_candidate[str(candidate_id)] = branch_id
        for edge_id in branches[branch_id]["edge_ids"]:
            branch_by_edge[str(edge_id)] = branch_id
    manual_confirmation = manual_confirmation or {}
    accepted_by_user = {str(item) for item in manual_confirmation.get("candidate_ids") or []}
    kept_local_by_user = {str(item) for item in manual_confirmation.get("keep_local_candidate_ids") or []}
    accepted_intermediate_indices = {
        int(item)
        for item in manual_confirmation.get("intermediate_distance_indices") or []
        if isinstance(item, int) or str(item).isdigit()
    }
    seen_candidates: set[str] = set()
    totals = {
        "main": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0, "counted_candidate_ids": [], "counted_intermediate_distances": []},
        "branch": {"clean_length_mm": 0.0, "dirty_length_mm": 0.0, "ambiguous_length_mm": 0.0, "counted_candidate_ids": [], "counted_intermediate_distances": []},
    }
    for page in payload.get("pages") or []:
        page_number = page.get("page")
        for row in page.get("dimensions") or []:
            key = str(row.get("candidate_key") or f"{page_number}:{row.get('id')}")
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            review = assessments.get(key) or assessments.get(str(row.get("id")))
            decision = (row.get("local_decision") or {}).get("decision")
            if key in kept_local_by_user or str(row.get("id")) in kept_local_by_user:
                decision = (row.get("local_decision") or {}).get("decision")
            elif key in accepted_by_user or str(row.get("id")) in accepted_by_user:
                decision = (review or {}).get("proposed_decision") or decision
            elif review and review.get("proposed_decision"):
                decision = review["proposed_decision"]
            elif review and review.get("assessment") == "accepted":
                decision = review.get("local_decision") or decision
            elif review and review.get("assessment") in {"disputed", "insufficient"}:
                decision = "ambiguous"
            edge_key = str(row.get("edge_id") or "")
            route_type = routes.get(f"{page_number}:{edge_key}", routes.get(edge_key, "main"))
            branch_id = branch_by_candidate.get(key) or branch_by_candidate.get(str(row.get("id")))
            branch_id = branch_id or branch_by_edge.get(f"{page_number}:{edge_key}") or branch_by_edge.get(edge_key)
            if branch_id:
                bucket = branches[branch_id]
            else:
                bucket = totals["branch" if route_type == "branch" else "main"]
            value = float(row.get("value_mm") or 0.0)
            if decision == "include":
                bucket["clean_length_mm"] += value
                bucket["dirty_length_mm"] += value
                bucket["counted_candidate_ids"].append(key)
            elif decision not in {"exclude", None}:
                bucket["dirty_length_mm"] += value
                bucket["ambiguous_length_mm"] += value
    for branch in branches.values():
        for key in ("clean_length_mm", "dirty_length_mm", "ambiguous_length_mm"):
            totals["branch"][key] += branch[key]
        totals["branch"]["counted_candidate_ids"].extend(branch["counted_candidate_ids"])
    for index, item in enumerate(provider.get("intermediate_distances") or []):
        if index not in accepted_intermediate_indices:
            continue
        value = float(item.get("value_mm") or item.get("length_mm") or 0.0)
        if value <= 0:
            continue
        route_type = str(item.get("route_type") or item.get("route") or "main").lower()
        branch_id = str(item.get("branch_id") or "")
        bucket = branches.get(branch_id) if branch_id else None
        branch_bucket = bool(bucket)
        if not bucket:
            bucket = totals["branch" if route_type == "branch" else "main"]
        bucket["clean_length_mm"] += value
        bucket["dirty_length_mm"] += value
        bucket.setdefault("counted_intermediate_distances", []).append(index)
        if branch_bucket:
            totals["branch"]["clean_length_mm"] += value
            totals["branch"]["dirty_length_mm"] += value
            totals["branch"].setdefault("counted_intermediate_distances", []).append(index)
    for bucket in totals.values():
        for key in ("clean_length_mm", "dirty_length_mm", "ambiguous_length_mm"):
            bucket[key] = round(bucket[key], 2)
    for branch in branches.values():
        for key in ("clean_length_mm", "dirty_length_mm", "ambiguous_length_mm"):
            branch[key] = round(branch[key], 2)
    return {
        **totals,
        "branches": list(branches.values()),
        "total_clean_length_mm": round(sum(item["clean_length_mm"] for item in totals.values()), 2),
        "total_dirty_length_mm": round(sum(item["dirty_length_mm"] for item in totals.values()), 2),
        "total_ambiguous_length_mm": round(sum(item["ambiguous_length_mm"] for item in totals.values()), 2),
    }
