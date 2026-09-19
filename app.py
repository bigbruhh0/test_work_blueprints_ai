from __future__ import annotations

import tempfile
import os
import time
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.annotation import annotations_for_page, drawable_annotations, render_page_with_annotations
from src.ai_client import DeepSeekAIClient
from src.exporters import dataframe_for, to_excel_bytes, to_json_bytes
from src.models import AnalysisResult, LineGroup, ProjectResult
from src.pipeline import get_pdf_cache_status, prepare_pdf_groups, run_pipeline_from_groups


ROOT = Path(__file__).parent
DEFAULT_PDF = ROOT / "Изометрии.pdf"
load_dotenv(ROOT / ".env")


st.set_page_config(
    page_title="Анализатор изометрий",
    page_icon="📐",
    layout="wide",
    initial_sidebar_state="expanded",
)


st.markdown(
    """
    <style>
    .stApp {
        background: #0b1120;
        color: #e5e7eb;
    }
    [data-testid="stHeader"] {
        background: rgba(11, 17, 32, 0.82);
        backdrop-filter: blur(8px);
    }
    [data-testid="stSidebar"] {
        background: #111827;
        border-right: 1px solid #263244;
    }
    [data-testid="stSidebar"] * {
        color: #e5e7eb;
    }
    h1, h2, h3, h4, h5, h6, p, label, span {
        color: #e5e7eb;
    }
    .hero {
        padding: 28px 30px;
        border: 1px solid #263244;
        border-radius: 8px;
        background: #111827;
        box-shadow: 0 18px 45px rgba(0, 0, 0, 0.35);
        margin-bottom: 18px;
    }
    .hero h1 {
        margin: 0 0 8px 0;
        font-size: 34px;
        line-height: 1.12;
        color: #f8fafc;
        letter-spacing: 0;
    }
    .hero p {
        margin: 0;
        max-width: 980px;
        color: #cbd5e1;
        font-size: 16px;
        line-height: 1.55;
    }
    .metric-row {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 12px;
        margin: 14px 0 18px;
    }
    .metric-card {
        background: #111827;
        border: 1px solid #263244;
        border-radius: 8px;
        padding: 14px 16px;
    }
    .metric-card span {
        color: #94a3b8;
        font-size: 13px;
    }
    .metric-card strong {
        display: block;
        margin-top: 4px;
        color: #f8fafc;
        font-size: 24px;
        line-height: 1.1;
    }
    .status-pill {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
    }
    .status-complete { background: #dcfce7; color: #166534; }
    .status-review { background: #fef3c7; color: #92400e; }
    .small-note {
        color: #94a3b8;
        font-size: 13px;
    }
    .run-panel {
        background: #111827;
        border: 1px solid #263244;
        border-radius: 8px;
        padding: 16px;
        margin: 12px 0 18px;
    }
    .run-panel strong {
        color: #f8fafc;
    }
    .run-panel p {
        color: #cbd5e1;
        margin: 6px 0 0;
        line-height: 1.45;
    }
    .stCaption, [data-testid="stCaptionContainer"] {
        color: #cbd5e1 !important;
    }
    [data-testid="stAlert"] {
        color: #e5e7eb;
        background: #1f2937;
        border: 1px solid #334155;
    }
    .monitor-card {
        background: #111827;
        border: 1px solid #263244;
        border-radius: 8px;
        padding: 14px 16px;
        margin: 12px 0;
    }
    .monitor-card code {
        color: #f8fafc;
        background: #0f172a;
        padding: 2px 5px;
        border-radius: 5px;
    }
    .pipeline-flow {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        align-items: center;
        margin: 12px 0 18px;
    }
    .pipeline-step {
        border: 1px solid #334155;
        background: #111827;
        color: #cbd5e1;
        border-radius: 8px;
        padding: 9px 11px;
        font-size: 13px;
        line-height: 1.1;
    }
    .pipeline-step.done {
        border-color: #22c55e;
        color: #dcfce7;
        background: #10331f;
    }
    .pipeline-step.active {
        border-color: #60a5fa;
        color: #eff6ff;
        background: #1d4ed8;
    }
    .pipeline-arrow {
        color: #64748b;
        font-weight: 700;
    }
    .workspace-shell {
        margin: 0;
    }
    .workspace-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 0;
        align-items: flex-end;
        min-height: 34px;
        padding: 0 8px;
        border-bottom: 1px solid #334155;
        background: #0b1120;
    }
    .workspace-label {
        color: #94a3b8;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        margin: 0 10px 8px 0;
    }
    .st-key-workspace_tabstrip {
        margin-top: -34px;
        padding-left: 92px;
        border-bottom: 1px solid #334155;
    }
    .st-key-workspace_tabstrip [data-testid="stHorizontalBlock"] {
        gap: 3px;
        align-items: end;
    }
    .st-key-workspace_tabstrip button {
        min-height: 32px;
        height: 32px;
        padding: 0 18px 0 9px;
        border-radius: 7px 7px 0 0;
        font-size: 12px;
        line-height: 1;
        margin: 0 0 -1px 0;
        white-space: nowrap;
    }
    .st-key-workspace_tabstrip button[kind="primary"] {
        background: #0f172a;
        border-color: #60a5fa;
        border-bottom-color: #0f172a;
        box-shadow: inset 0 2px 0 #60a5fa;
    }
    .st-key-workspace_tabstrip button[kind="secondary"] {
        background: #111827;
        border-color: #334155;
        color: #cbd5e1;
    }
    .st-key-workspace_tabstrip [class*="st-key-workspace_close_"] {
        position: relative;
        z-index: 5;
        width: 0;
        overflow: visible;
    }
    .st-key-workspace_tabstrip [class*="st-key-workspace_close_"] button {
        position: relative;
        left: -18px;
        top: -8px;
        width: 18px;
        min-width: 18px;
        height: 18px;
        min-height: 18px;
        padding: 0;
        border-radius: 999px;
        border: 1px solid #475569;
        background: #020617;
        color: #cbd5e1;
        font-size: 12px;
        line-height: 1;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
        margin: 0;
    }
    .st-key-workspace_tabstrip [class*="st-key-workspace_close_"] button:hover {
        border-color: #f87171;
        background: #991b1b;
        color: #ffffff;
    }
    .workspace-active-note {
        color: #94a3b8;
        font-size: 12px;
        margin: 6px 2px 0;
    }
    .workspace-content {
        border: 1px solid #334155;
        border-top: 0;
        border-radius: 0 0 8px 8px;
        background: #0f172a;
        padding: 18px;
        margin-bottom: 18px;
    }
    div[data-testid="stDataFrame"] {
        border: 1px solid #263244;
        border-radius: 8px;
        overflow: hidden;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background: #111827;
        border: 1px solid #263244;
        border-radius: 8px;
        color: #cbd5e1;
        padding: 8px 14px;
    }
    .stTabs [aria-selected="true"] {
        background: #1d4ed8;
        color: #ffffff;
        border-color: #3b82f6;
    }
    .stButton > button, .stDownloadButton > button {
        border-radius: 8px;
        border: 1px solid #3b82f6;
        background: #1d4ed8;
        color: #ffffff;
        font-weight: 650;
    }
    .stButton > button:hover, .stDownloadButton > button:hover {
        border-color: #60a5fa;
        background: #2563eb;
        color: #ffffff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def save_uploaded_pdf(uploaded_file) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="iso_mvp_"))
    path = temp_dir / uploaded_file.name
    path.write_bytes(uploaded_file.getbuffer())
    return path


def render_metrics(project) -> None:
    complete = sum(1 for item in project.result.lines if item.status == "complete")
    review = sum(1 for item in project.result.lines if item.status == "needs_review")
    candidates = len(project.result.candidates)
    st.markdown(
        f"""
        <div class="metric-row">
            <div class="metric-card"><span>Страниц PDF</span><strong>{project.pages_count}</strong></div>
            <div class="metric-card"><span>Групп линий</span><strong>{project.line_groups_count}</strong></div>
            <div class="metric-card"><span>Полных разборов</span><strong>{complete}</strong></div>
            <div class="metric-card"><span>Кандидатов PDF</span><strong>{candidates}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_dataframe(title: str, data: pd.DataFrame) -> None:
    st.subheader(title)
    if data.empty:
        st.info("Данных пока нет.")
    else:
        st.dataframe(data, width="stretch", hide_index=True)


def result_dataframe(result, field_name: str) -> pd.DataFrame:
    return dataframe_for(getattr(result, field_name, []))


@st.cache_data(show_spinner=False)
def cached_prepare_pdf_groups(pdf_path_text: str):
    return prepare_pdf_groups(pdf_path_text)


def add_monitor_event(event: str, payload: dict) -> None:
    record = {
        "time": time.strftime("%H:%M:%S"),
        "run_id": st.session_state.get("active_run_id", ""),
        "event": event,
        **{key: flatten_monitor_value(value) for key, value in payload.items()},
    }
    run_id = record["run_id"]
    runs = st.session_state.setdefault("analysis_runs", {})
    if run_id and run_id in runs:
        events = runs[run_id].setdefault("events", [])
        runs[run_id]["updated_at"] = time.strftime("%H:%M:%S")
    else:
        events = st.session_state.setdefault("monitor_events", [])
    events.append(record)
    st.session_state["monitor_events"] = events
    history = st.session_state.setdefault("request_history_events", [])
    history.append(record)


def flatten_monitor_value(value):
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def monitor_dataframe(run_id: str | None = None) -> pd.DataFrame:
    if run_id:
        events = st.session_state.get("analysis_runs", {}).get(run_id, {}).get("events", [])
    else:
        events = st.session_state.get("monitor_events", [])
    frame = pd.DataFrame(events)
    for column in frame.columns:
        if frame[column].dtype == "object":
            frame[column] = frame[column].map(lambda value: "" if value is None else str(flatten_monitor_value(value)))
    return frame


def event_stage(event: str) -> str:
    if event.startswith("run."):
        return "Запуск"
    if event.startswith("cache."):
        return "Кеш"
    if event.startswith("pipeline."):
        return "Pipeline"
    if event.startswith("deepseek.classify"):
        return "Классификация"
    if event.startswith("deepseek.payload") or event.startswith("deepseek.image") or event.startswith("deepseek.request"):
        return "DeepSeek request"
    if event.startswith("deepseek.response"):
        return "DeepSeek response"
    if event.startswith("deepseek.error"):
        return "Ошибка провайдера"
    return "Событие"


def event_level(event: str) -> str:
    if event.endswith(".error") or ".error" in event:
        return "error"
    if event.startswith("cache.hit"):
        return "cache"
    if event.endswith(".done") or event.endswith(".received") or event.endswith(".store"):
        return "done"
    return "info"


def event_message(event: str, payload: dict) -> str:
    line_id = payload.get("line_id")
    if event == "run.start":
        selected = payload.get("selected_lines", "")
        return f"Старт анализа: {selected}"
    if event == "run.done":
        return f"Завершено за {payload.get('elapsed_seconds')} c; новых: {payload.get('new_runs')}, из кеша: {payload.get('cached')}"
    if event == "cache.hit":
        return f"Найдено в кеше групп: {payload.get('groups')}"
    if event == "cache.store":
        return f"{line_id}: результат сохранен в кеш"
    if event == "pipeline.group.start":
        return f"{line_id}: старт группы {payload.get('current')}/{payload.get('total')}"
    if event == "pipeline.geometry.done":
        return f"{line_id}: geometry solver завершен, участков: {payload.get('segments')}, точек: {payload.get('points')}"
    if event == "pipeline.validation.done":
        return f"{line_id}: валидация завершена, неопределённостей: {payload.get('uncertainties')}"
    if event == "deepseek.classify.request.start":
        return f"{line_id}: отправлена классификация кандидатов ({payload.get('candidates')} шт.)"
    if event == "deepseek.classify.response.received":
        return f"{line_id}: ответ классификации, HTTP {payload.get('status_code')}, {payload.get('elapsed_seconds')} c"
    if event == "deepseek.classify.parse.done":
        return f"{line_id}: классификаций разобрано {payload.get('classifications')}"
    if event == "deepseek.payload.start":
        return f"{line_id}: сбор payload, страницы {payload.get('pages')}"
    if event == "deepseek.image.ready":
        return f"{line_id}: подготовлено изображение страницы {payload.get('page')}"
    if event == "deepseek.request.start":
        return f"{line_id}: отправлен анализ ({payload.get('payload_kb')} KB, изображений: {payload.get('image_count')})"
    if event == "deepseek.response.received":
        return f"{line_id}: ответ анализа, HTTP {payload.get('status_code')}, {payload.get('elapsed_seconds')} c"
    if event == "deepseek.response.parse":
        return f"{line_id}: JSON ответа разобран ({payload.get('content_chars')} символов)"
    if event.startswith("deepseek.") and "error" in payload:
        return f"{line_id or 'provider'}: {payload.get('error')}"
    return event


def timeline_dataframe(run_id: str | None = None) -> pd.DataFrame:
    rows = []
    if run_id:
        events = st.session_state.get("analysis_runs", {}).get(run_id, {}).get("events", [])
    else:
        events = st.session_state.get("monitor_events", [])
    for item in events:
        event = item.get("event", "")
        payload = {key: value for key, value in item.items() if key not in {"time", "event"}}
        rows.append(
            {
                "time": item.get("time", ""),
                "stage": event_stage(event),
                "level": event_level(event),
                "event": event,
                "line_id": item.get("line_id", ""),
                "message": event_message(event, payload),
            }
        )
    return pd.DataFrame(rows)


def request_monitor_dataframe(events_key: str = "monitor_events", run_id: str | None = None) -> pd.DataFrame:
    requests: dict[tuple[str, str], dict] = {}
    if run_id:
        source_events = st.session_state.get("analysis_runs", {}).get(run_id, {}).get("events", [])
    else:
        source_events = st.session_state.get(events_key, [])
    for item in source_events:
        event = item.get("event", "")
        line_id = str(item.get("line_id", ""))
        if event.startswith("deepseek.classify"):
            request_type = "classification"
        elif event.startswith("deepseek.payload") or event.startswith("deepseek.image") or event.startswith("deepseek.request") or event.startswith("deepseek.response") or event.startswith("deepseek.error"):
            request_type = "analysis"
        else:
            continue

        key = (str(item.get("run_id", "")), line_id, request_type)
        row = requests.setdefault(
            key,
            {
                "run_id": item.get("run_id", ""),
                "line_id": line_id,
                "request_type": request_type,
                "status": "running",
                "started_at": "",
                "http_status": "",
                "elapsed_seconds": "",
                "payload_kb": "",
                "response_kb": "",
                "text_chars": "",
                "image_count": "",
                "error": "",
            },
        )

        if event.endswith(".request.start") or event == "deepseek.payload.start":
            row["started_at"] = item.get("time", "")
        if "payload_kb" in item:
            row["payload_kb"] = item.get("payload_kb", "")
        if "text_chars" in item:
            row["text_chars"] = item.get("text_chars", "")
        if "image_count" in item:
            row["image_count"] = item.get("image_count", "")
        if "status_code" in item:
            row["http_status"] = item.get("status_code", "")
        if "elapsed_seconds" in item:
            row["elapsed_seconds"] = item.get("elapsed_seconds", "")
        if "response_kb" in item:
            row["response_kb"] = item.get("response_kb", "")
        if event.endswith(".received") or event.endswith(".parse") or event.endswith(".done"):
            row["status"] = "done"
        if event.endswith(".error") or "error" in item:
            row["status"] = "error"
            row["error"] = item.get("error", "")

    return pd.DataFrame(requests.values())


PIPELINE_STEPS = [
    ("groups", "Группы PDF"),
    ("extract", "PDF candidates"),
    ("classify", "Классификация"),
    ("analyze", "DeepSeek"),
    ("geometry", "Geometry solver"),
    ("validate", "Валидация"),
    ("cache", "Кеш"),
]
PIPELINE_LABEL_BY_ID = dict(PIPELINE_STEPS)
PIPELINE_ID_BY_LABEL = {label: step_id for step_id, label in PIPELINE_STEPS}


def events_for_stage(events: pd.DataFrame, stage: str) -> pd.DataFrame:
    if events.empty or "event" not in events.columns:
        return events
    event_series = events["event"].astype(str)
    if stage == "groups":
        return events[event_series.str.startswith("run.") | event_series.str.startswith("pipeline.group")]
    if stage == "extract":
        return events[event_series.str.startswith("pipeline.group")]
    if stage == "classify":
        return events[event_series.str.startswith("deepseek.classify")]
    if stage == "analyze":
        return events[
            event_series.str.startswith("deepseek.payload")
            | event_series.str.startswith("deepseek.image")
            | event_series.str.startswith("deepseek.request")
            | event_series.str.startswith("deepseek.response")
            | event_series.str.startswith("deepseek.error")
        ]
    if stage == "geometry":
        return events[event_series.str.startswith("pipeline.geometry")]
    if stage == "validate":
        return events[event_series.str.startswith("pipeline.validation")]
    if stage == "cache":
        return events[event_series.str.startswith("cache.") | event_series.str.startswith("run.done")]
    return events


def render_stage_details(
    stage: str,
    groups_frame: pd.DataFrame,
    candidates_frame: pd.DataFrame | None = None,
    classifications_frame: pd.DataFrame | None = None,
    timeline_frame: pd.DataFrame | None = None,
    raw_events_frame: pd.DataFrame | None = None,
    uncertainties_frame: pd.DataFrame | None = None,
    cache_count: int = 0,
) -> None:
    st.markdown(f"#### {PIPELINE_LABEL_BY_ID.get(stage, stage)}")

    if stage == "groups":
        st.dataframe(groups_frame, width="stretch", hide_index=True)
        return

    if stage == "extract":
        if candidates_frame is not None and not candidates_frame.empty:
            st.dataframe(candidates_frame, width="stretch", hide_index=True)
        else:
            st.info("Кандидаты появятся после анализа выбранной линии.")
        return

    if stage == "classify":
        if classifications_frame is not None and not classifications_frame.empty:
            st.dataframe(classifications_frame, width="stretch", hide_index=True)
        else:
            st.info("Классификация появится после ответа провайдера.")
        return

    if stage == "validate":
        if uncertainties_frame is not None and not uncertainties_frame.empty:
            st.dataframe(uncertainties_frame, width="stretch", hide_index=True)
        else:
            st.info("Предупреждения валидации появятся после анализа.")
        return

    if stage == "cache":
        st.metric("Групп в кеше по этому PDF", cache_count)

    events_source = timeline_frame if timeline_frame is not None else pd.DataFrame()
    if events_source.empty:
        events_source = raw_events_frame if raw_events_frame is not None else pd.DataFrame()
    stage_events = events_for_stage(events_source, stage)
    if stage_events.empty:
        st.info("Для этого этапа пока нет событий.")
    else:
        st.dataframe(stage_events, width="stretch", hide_index=True)


def render_request_history_panel() -> None:
    st.subheader("История анализов")
    history_df = request_monitor_dataframe("request_history_events")
    raw_history = pd.DataFrame(st.session_state.get("request_history_events", []))
    if raw_history.empty:
        st.info("История анализов пока пустая.")
        return

    def filter_options(frame: pd.DataFrame, column: str) -> list[str]:
        if column not in frame:
            return ["Все"]
        values = []
        for value in frame[column].dropna().tolist():
            text = str(value).strip()
            if text and text.lower() not in {"nan", "none", "nat"}:
                values.append(text)
        return ["Все"] + sorted(set(values), key=str.casefold)

    filter_cols = st.columns(3)
    run_source = raw_history if not raw_history.empty else history_df
    run_options = filter_options(run_source, "run_id")
    line_options = filter_options(run_source, "line_id")
    status_options = filter_options(history_df, "status")
    with filter_cols[0]:
        selected_run = st.selectbox("Run ID", run_options, key="history_filter_run")
    with filter_cols[1]:
        selected_line = st.selectbox("Линия", line_options, key="history_filter_line")
    with filter_cols[2]:
        selected_status = st.selectbox("Статус", status_options, key="history_filter_status")

    if selected_run != "Все":
        history_df = history_df[history_df["run_id"].astype(str) == selected_run] if "run_id" in history_df else history_df
        raw_history = raw_history[raw_history["run_id"].astype(str) == selected_run] if "run_id" in raw_history else raw_history
    if selected_line != "Все":
        history_df = history_df[history_df["line_id"].astype(str) == selected_line] if "line_id" in history_df else history_df
        raw_history = raw_history[raw_history["line_id"].astype(str) == selected_line] if "line_id" in raw_history else raw_history
    if selected_status != "Все" and "status" in history_df:
        history_df = history_df[history_df["status"].astype(str) == selected_status]

    metric_cols = st.columns(4)
    done_count = int((history_df["status"] == "done").sum()) if "status" in history_df else 0
    error_count = int((history_df["status"] == "error").sum()) if "status" in history_df else 0
    elapsed_values = pd.to_numeric(history_df.get("elapsed_seconds", pd.Series(dtype=float)), errors="coerce").dropna()
    total_elapsed = round(float(elapsed_values.sum()), 2) if not elapsed_values.empty else 0
    with metric_cols[0]:
        st.metric("Запросов", len(history_df))
    with metric_cols[1]:
        st.metric("Завершено", done_count)
    with metric_cols[2]:
        st.metric("Ошибок", error_count)
    with metric_cols[3]:
        st.metric("Суммарно, сек", total_elapsed)

    st.markdown("#### Полные логи")
    st.dataframe(raw_history, width="stretch", hide_index=True)

    st.markdown("#### Запросы провайдера")
    if history_df.empty:
        st.info("Запросов провайдера по выбранному фильтру нет.")
    else:
        st.dataframe(history_df, width="stretch", hide_index=True)


def pdf_cache_key(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"


def group_options(groups: list[LineGroup]) -> list[str]:
    return [group.line_id for group in groups if not group.line_id.startswith("UNKNOWN_")]


def group_table(groups: list[LineGroup], cache: dict, cache_prefix: str) -> pd.DataFrame:
    rows = []
    for group in groups:
        pages = [page.page_number for page in group.pages]
        rows.append(
            {
                "line_id": group.line_id,
                "pages_count": len(pages),
                "pages": ", ".join(map(str, pages)),
                "status": "разобрано" if f"{cache_prefix}|{group.line_id}" in cache else "ожидает",
            }
        )
    return pd.DataFrame(rows)


def combine_cached_projects(
    source_name: str,
    pages_count: int,
    selected_line_ids: list[str],
    cache: dict,
    cache_prefix: str,
    provider: str,
) -> ProjectResult | None:
    aggregate = AnalysisResult()
    found = 0
    for line_id in selected_line_ids:
        cached = cache.get(f"{cache_prefix}|{line_id}")
        if not cached:
            continue
        aggregate.extend(cached.result)
        found += 1
    if not found:
        return None
    return ProjectResult.create(
        source_name=source_name,
        pages_count=pages_count,
        line_groups_count=found,
        model_mode=provider,
        result=aggregate,
    )


def render_pipeline(stage: str = "groups") -> None:
    active_index = next((index for index, item in enumerate(PIPELINE_STEPS) if item[0] == stage), 0)
    parts = ['<div class="pipeline-flow">']
    for index, (step_id, label) in enumerate(PIPELINE_STEPS):
        state = "active" if step_id == stage else "done" if index < active_index else ""
        parts.append(f'<div class="pipeline-step {state}">{label}</div>')
        if index < len(PIPELINE_STEPS) - 1:
            parts.append('<div class="pipeline-arrow">→</div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


HOME_TAB_ID = "welcome"
HISTORY_TAB_ID = "request_history"


def init_workspace() -> None:
    tabs = st.session_state.setdefault(
        "workspace_tabs",
        [
            {"id": HOME_TAB_ID, "type": "welcome", "title": "Welcome", "closable": False},
        ],
    )
    if not any(tab["id"] == HOME_TAB_ID for tab in tabs):
        tabs.insert(0, {"id": HOME_TAB_ID, "type": "welcome", "title": "Welcome", "closable": False})
    for tab in tabs:
        if tab["id"] == HOME_TAB_ID:
            tab["type"] = "welcome"
            tab["title"] = "Welcome"
    st.session_state.setdefault("active_workspace_tab", HOME_TAB_ID)
    st.session_state.setdefault("analysis_runs", {})


def set_active_tab(tab_id: str) -> None:
    if any(tab["id"] == tab_id for tab in st.session_state.get("workspace_tabs", [])):
        st.session_state["active_workspace_tab"] = tab_id


def open_history_tab() -> None:
    tabs = st.session_state.setdefault("workspace_tabs", [])
    if not any(tab["id"] == HISTORY_TAB_ID for tab in tabs):
        tabs.append({"id": HISTORY_TAB_ID, "type": "request_history", "title": "История запросов", "closable": True})
    st.session_state["active_workspace_tab"] = HISTORY_TAB_ID


def close_workspace_tab(tab_id: str) -> None:
    if tab_id == HOME_TAB_ID:
        return
    tabs = st.session_state.get("workspace_tabs", [])
    st.session_state["workspace_tabs"] = [tab for tab in tabs if tab["id"] != tab_id]
    if st.session_state.get("active_workspace_tab") == tab_id:
        st.session_state["active_workspace_tab"] = HOME_TAB_ID


def analysis_tab_title(line_ids: list[str]) -> str:
    if not line_ids:
        return "Анализ"
    if len(line_ids) == 1:
        return f"Анализ {line_ids[0]}"
    return f"Анализ {line_ids[0]} + {len(line_ids) - 1}"


def create_analysis_tab(line_ids: list[str]) -> str:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
    tab_id = f"analysis:{run_id}"
    st.session_state.setdefault("workspace_tabs", []).append(
        {
            "id": tab_id,
            "type": "analysis",
            "title": analysis_tab_title(line_ids),
            "run_id": run_id,
            "closable": True,
        }
    )
    st.session_state.setdefault("analysis_runs", {})[run_id] = {
        "run_id": run_id,
        "tab_id": tab_id,
        "title": analysis_tab_title(line_ids),
        "selected_line_ids": list(line_ids),
        "status": "pending",
        "stage": "groups",
        "progress": 0.0,
        "progress_text": "Ожидает старта анализа",
        "events": [],
        "result_line_ids": [],
        "error": "",
        "started_at": time.strftime("%H:%M:%S"),
        "updated_at": time.strftime("%H:%M:%S"),
    }
    st.session_state["active_workspace_tab"] = tab_id
    st.session_state["active_run_id"] = run_id
    st.session_state["monitor_events"] = st.session_state["analysis_runs"][run_id]["events"]
    return run_id


def active_workspace_tab() -> dict:
    active_id = st.session_state.get("active_workspace_tab", HOME_TAB_ID)
    for tab in st.session_state.get("workspace_tabs", []):
        if tab["id"] == active_id:
            return tab
    st.session_state["active_workspace_tab"] = HOME_TAB_ID
    return {"id": HOME_TAB_ID, "type": "welcome", "title": "Welcome", "closable": False}


def widget_key_fragment(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value)


def render_workspace_tabs() -> None:
    tabs = st.session_state.get("workspace_tabs", [])
    active_id = st.session_state.get("active_workspace_tab", HOME_TAB_ID)
    st.markdown('<div class="workspace-shell"><div class="workspace-bar"><span class="workspace-label">Workspace</span></div></div>', unsafe_allow_html=True)
    with st.container(key="workspace_tabstrip"):
        widths = []
        for tab in tabs:
            title_width = 1.55
            if tab.get("closable"):
                widths.extend([title_width, 0.05])
            else:
                widths.append(title_width)
        widths.append(max(12, 24 - sum(widths)))
        columns = st.columns(widths)
        column_index = 0
        for tab in tabs:
            key_fragment = widget_key_fragment(tab["id"])
            with columns[column_index]:
                label = tab["title"]
                button_type = "primary" if tab["id"] == active_id else "secondary"
                if st.button(label, key=f"workspace_open_{key_fragment}", type=button_type, width="stretch"):
                    set_active_tab(tab["id"])
                    st.rerun()
            column_index += 1
            if tab.get("closable"):
                with columns[column_index]:
                    if st.button("×", key=f"workspace_close_{key_fragment}", width="stretch"):
                        close_workspace_tab(tab["id"])
                        st.rerun()
                column_index += 1
    current = active_workspace_tab()
    st.markdown(f'<div class="workspace-active-note">Открыто: {current["title"]}</div>', unsafe_allow_html=True)


def open_workspace_content() -> None:
    st.markdown('<div class="workspace-content">', unsafe_allow_html=True)


def close_workspace_content() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


def render_welcome_page() -> None:
    st.markdown(
        """
        <div class="hero">
            <h1>Анализатор изометрий</h1>
            <p>
                Загрузите PDF, проверьте сгруппированные линии и запустите анализ нужных групп.
                Каждый запуск откроется в отдельной вкладке workspace.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_pdf_source_controls(default_pdf: Path) -> tuple[str, object | None, bool, bool]:
    st.subheader("Источник PDF")
    st.caption("Выберите PDF, загрузите его в приложение, затем отметьте нужные группы линий.")
    source_mode = st.radio(
        "Откуда взять PDF",
        ["Локальный Изометрии.pdf", "Загрузить новый PDF"],
        index=0 if default_pdf.exists() else 1,
        horizontal=True,
    )
    uploaded = None
    if source_mode == "Загрузить новый PDF":
        uploaded = st.file_uploader("PDF-файл", type=["pdf"], accept_multiple_files=False)
    else:
        if default_pdf.exists():
            st.caption(f"Будет использован файл: {default_pdf.name}")
        else:
            st.warning("Файл Изометрии.pdf не найден в папке проекта.")

    use_cache = st.checkbox("Использовать кеш", value=True)
    load_pdf_clicked = st.button("Загрузить PDF", type="primary", width="stretch")
    return source_mode, uploaded, use_cache, load_pdf_clicked


def set_run_stage(run_id: str, stage: str, progress: float | None = None, text: str | None = None) -> None:
    run = st.session_state.setdefault("analysis_runs", {}).get(run_id)
    if not run:
        return
    run["stage"] = stage
    if progress is not None:
        run["progress"] = max(0.0, min(float(progress), 1.0))
    if text is not None:
        run["progress_text"] = text
    run["updated_at"] = time.strftime("%H:%M:%S")


def render_run_status(run: dict) -> None:
    status = run.get("status", "waiting")
    if status == "running":
        st.info(run.get("progress_text", "Анализ выполняется..."))
    elif status == "complete":
        st.success(run.get("progress_text", "Анализ завершён."))
    elif status == "error":
        st.error(run.get("error", "Анализ завершился с ошибкой."))
    else:
        st.info(run.get("progress_text", "Ожидает запуска."))
    st.progress(float(run.get("progress", 0.0)), text=run.get("progress_text", ""))
    render_pipeline(run.get("stage", "groups"))


def render_request_monitor(run_id: str | None = None) -> None:
    requests_df = request_monitor_dataframe(run_id=run_id) if run_id else request_monitor_dataframe("request_history_events")
    events_df = monitor_dataframe(run_id) if run_id else pd.DataFrame(st.session_state.get("request_history_events", []))
    if requests_df.empty:
        st.info("Данные по запросам появятся после запуска анализа.")
        return

    metric_cols = st.columns(4)
    done_count = int((requests_df["status"] == "done").sum()) if "status" in requests_df else 0
    error_count = int((requests_df["status"] == "error").sum()) if "status" in requests_df else 0
    elapsed_values = pd.to_numeric(requests_df.get("elapsed_seconds", pd.Series(dtype=float)), errors="coerce").dropna()
    total_elapsed = round(float(elapsed_values.sum()), 2) if not elapsed_values.empty else 0
    with metric_cols[0]:
        st.metric("Запросов", len(requests_df))
    with metric_cols[1]:
        st.metric("Завершено", done_count)
    with metric_cols[2]:
        st.metric("Ошибок", error_count)
    with metric_cols[3]:
        st.metric("Суммарно, сек", total_elapsed)

    st.dataframe(requests_df, width="stretch", hide_index=True)
    provider_events = events_df[events_df["event"].astype(str).str.startswith("deepseek.")] if not events_df.empty and "event" in events_df else pd.DataFrame()
    with st.expander("Подробные события провайдера"):
        if provider_events.empty:
            st.info("Событий провайдера пока нет.")
        else:
            st.dataframe(provider_events, width="stretch", hide_index=True)


def render_project_result_tabs(
    project: ProjectResult,
    run_id: str,
    groups_df: pd.DataFrame,
    analysis_cache: dict,
    cache_prefix: str,
    loaded_use_cache: bool,
    provider: str,
    selected_line_ids: list[str],
    deepseek_key: str,
) -> None:
    render_metrics(project)

    lines_df = result_dataframe(project.result, "lines")
    points_df = result_dataframe(project.result, "points")
    segments_df = result_dataframe(project.result, "segments")
    elements_df = result_dataframe(project.result, "elements")
    uncertainties_df = result_dataframe(project.result, "uncertainties")
    annotations_df = result_dataframe(project.result, "annotations")
    candidates_df = result_dataframe(project.result, "candidates")
    classifications_df = result_dataframe(project.result, "candidate_classifications")

    tabs = st.tabs(["Линии", "Точки", "Участки", "Элементы", "Неопределённости", "Кандидаты PDF", "Классификация", "Разметка", "Запросы", "Журнал", "Экспорт"])

    with tabs[0]:
        show_dataframe("Линии", lines_df)
    with tabs[1]:
        show_dataframe("Точки", points_df)
    with tabs[2]:
        show_dataframe("Участки", segments_df)
    with tabs[3]:
        show_dataframe("Элементы", elements_df)
    with tabs[4]:
        show_dataframe("Неопределённости", uncertainties_df)
    with tabs[5]:
        show_dataframe("Кандидаты PDF", candidates_df)
    with tabs[6]:
        show_dataframe("Классификация кандидатов", classifications_df)
    with tabs[7]:
        st.subheader("Разметка")
        if annotations_df.empty:
            st.info("Разметка пока отсутствует.")
        else:
            available_pages = sorted({item.page for item in project.result.annotations})
            selected_page = st.selectbox("Страница", available_pages, key=f"annotation_page_{run_id}")
            page_annotations = annotations_for_page(project.result.annotations, selected_page)
            skipped_annotations = len(page_annotations) - len(drawable_annotations(page_annotations))
            if skipped_annotations:
                st.caption(f"Аннотаций без bbox не отрисовано: {skipped_annotations}")
            try:
                image_bytes = render_page_with_annotations(st.session_state["pdf_path"], selected_page, page_annotations)
                st.image(image_bytes, caption=f"Страница {selected_page}: MVP-разметка", width="stretch")
            except Exception as error:
                st.error(f"Не удалось отрисовать страницу: {error}")
            show_dataframe("Объекты разметки", annotations_df)
    with tabs[8]:
        st.subheader("Мониторинг запросов")
        render_request_monitor(run_id)
    with tabs[9]:
        st.subheader("Журнал анализа")
        events_df = monitor_dataframe(run_id)
        timeline_df = timeline_dataframe(run_id)
        if events_df.empty:
            st.info("Лог запуска появится после анализа.")
        else:
            summary_cols = st.columns(4)
            provider_events = events_df[events_df["event"].astype(str).str.startswith("deepseek.")] if "event" in events_df else pd.DataFrame()
            errors = timeline_df[timeline_df["level"] == "error"] if not timeline_df.empty else pd.DataFrame()
            with summary_cols[0]:
                st.metric("Событий", len(events_df))
            with summary_cols[1]:
                st.metric("Событий провайдера", len(provider_events))
            with summary_cols[2]:
                st.metric("Ошибок", len(errors))
            with summary_cols[3]:
                st.metric("Линий в результате", len(project.result.lines))

            selected_stage_label = st.radio(
                "Этап pipeline",
                [label for _, label in PIPELINE_STEPS],
                horizontal=True,
                key=f"journal_stage_{run_id}",
            )
            selected_stage = PIPELINE_ID_BY_LABEL[selected_stage_label]
            render_pipeline(selected_stage)
            render_stage_details(
                selected_stage,
                groups_df,
                candidates_frame=candidates_df,
                classifications_frame=classifications_df,
                timeline_frame=timeline_df,
                raw_events_frame=events_df,
                uncertainties_frame=uncertainties_df,
                cache_count=sum(1 for key in analysis_cache if key.startswith(cache_prefix)) if loaded_use_cache else 0,
            )

            with st.expander("События провайдера DeepSeek", expanded=True):
                if provider_events.empty:
                    st.info("Событий провайдера пока нет.")
                else:
                    st.dataframe(provider_events, width="stretch", hide_index=True)

            with st.expander("Сырые события pipeline"):
                st.dataframe(events_df, width="stretch", hide_index=True)

            with st.expander("Полный timeline"):
                st.dataframe(timeline_df, width="stretch", hide_index=True)

        st.markdown(
            f"""
            <div class="monitor-card">
                <strong>Текущая конфигурация</strong><br>
                Провайдер: <code>{provider}</code><br>
                Выбрано линий: <code>{len(selected_line_ids)}</code><br>
                Использование кеша: <code>{"включено" if loaded_use_cache else "выключено"}</code><br>
                В кеше по этому PDF: <code>{sum(1 for key in analysis_cache if key.startswith(cache_prefix)) if loaded_use_cache else 0}</code><br>
                Ключ DeepSeek: <code>{"найден" if deepseek_key else "не найден"}</code>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with tabs[10]:
        st.subheader("Экспорт")
        json_bytes = to_json_bytes(project)
        excel_bytes = to_excel_bytes(project)
        left, right = st.columns(2)
        with left:
            st.download_button(
                "Скачать JSON",
                data=json_bytes,
                file_name="isometry_analysis_result.json",
                mime="application/json",
                width="stretch",
                key=f"download_json_{run_id}",
            )
        with right:
            st.download_button(
                "Скачать Excel",
                data=excel_bytes,
                file_name="isometry_analysis_result.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
                key=f"download_excel_{run_id}",
            )


def render_analysis_workspace_page(
    run_id: str,
    source_name: str,
    pages_count: int,
    groups_df: pd.DataFrame,
    analysis_cache: dict,
    cache_prefix: str,
    provider: str,
    loaded_use_cache: bool,
    deepseek_key: str,
) -> None:
    run = st.session_state.get("analysis_runs", {}).get(run_id)
    if not run:
        st.error("Вкладка анализа не найдена.")
        return

    st.subheader(run.get("title", "Анализ"))
    st.caption(f"Run ID: {run_id}. Старт: {run.get('started_at', '')}. Обновлено: {run.get('updated_at', '')}.")
    render_run_status(run)

    selected_line_ids = list(run.get("selected_line_ids", []))
    result_line_ids = list(run.get("result_line_ids") or selected_line_ids)
    project = combine_cached_projects(
        source_name=source_name,
        pages_count=pages_count,
        selected_line_ids=result_line_ids,
        cache=analysis_cache,
        cache_prefix=cache_prefix,
        provider=provider,
    )

    selected_stage_label = st.radio(
        "Этап pipeline",
        [label for _, label in PIPELINE_STEPS],
        index=next(
            (index for index, item in enumerate(PIPELINE_STEPS) if item[0] == run.get("stage", "groups")),
            0,
        ),
        horizontal=True,
        key=f"active_stage_{run_id}",
    )
    selected_stage = PIPELINE_ID_BY_LABEL[selected_stage_label]

    if project:
        render_stage_details(
            selected_stage,
            groups_df,
            candidates_frame=result_dataframe(project.result, "candidates"),
            classifications_frame=result_dataframe(project.result, "candidate_classifications"),
            timeline_frame=timeline_dataframe(run_id),
            raw_events_frame=monitor_dataframe(run_id),
            uncertainties_frame=result_dataframe(project.result, "uncertainties"),
            cache_count=sum(1 for key in analysis_cache if key.startswith(cache_prefix)) if loaded_use_cache else 0,
        )
    else:
        render_stage_details(
            selected_stage,
            groups_df,
            timeline_frame=timeline_dataframe(run_id),
            raw_events_frame=monitor_dataframe(run_id),
            cache_count=sum(1 for key in analysis_cache if key.startswith(cache_prefix)) if loaded_use_cache else 0,
        )

    st.divider()
    if project:
        missing_results = [line_id for line_id in selected_line_ids if f"{cache_prefix}|{line_id}" not in analysis_cache]
        if missing_results:
            st.info(f"Еще не разобраны: {', '.join(missing_results)}")
        render_project_result_tabs(
            project,
            run_id,
            groups_df,
            analysis_cache,
            cache_prefix,
            loaded_use_cache,
            provider,
            selected_line_ids,
            deepseek_key,
        )
    else:
        render_request_monitor(run_id)


def execute_analysis_run(
    run_id: str,
    source_name: str,
    pages: list,
    groups: list[LineGroup],
    groups_df: pd.DataFrame,
    selected_line_ids: list[str],
    analysis_cache: dict,
    cache_prefix: str,
    provider: str,
    loaded_use_cache: bool,
    loaded_prepare_seconds: float,
    reanalyze: bool,
    pdf_path: Path,
    deepseek_key: str,
    deepseek_model: str,
    include_images: bool,
) -> None:
    run = st.session_state["analysis_runs"][run_id]
    run["status"] = "running"
    st.session_state["active_run_id"] = run_id
    st.session_state["monitor_events"] = run.setdefault("events", [])

    status_box = st.empty()
    progress_bar = st.empty()
    monitor_box = st.empty()

    def build_ai_client(event_callback) -> DeepSeekAIClient | None:
        if not deepseek_key.strip():
            st.error("Для анализа нужна переменная окружения DEEPSEEK_API_KEY или локальный .env.")
            return None
        return DeepSeekAIClient(
            api_key=deepseek_key,
            model=deepseek_model,
            pdf_path=pdf_path,
            include_images=include_images,
            max_image_pages=1,
            event_callback=event_callback,
        )

    def render_stage(stage: str) -> None:
        set_run_stage(run_id, stage)
        with monitor_box.container():
            render_stage_details(
                stage,
                groups_df,
                timeline_frame=timeline_dataframe(run_id),
                raw_events_frame=monitor_dataframe(run_id),
                cache_count=sum(1 for key in analysis_cache if key.startswith(cache_prefix)),
            )

    def on_ai_event(event: str, payload: dict) -> None:
        add_monitor_event(event, payload)
        if event.startswith("deepseek.classify"):
            set_run_stage(run_id, "classify")
            render_stage("classify")
        elif event.startswith("deepseek.payload") or event.startswith("deepseek.image") or event.startswith("deepseek.request"):
            set_run_stage(run_id, "analyze")
            render_stage("analyze")
        elif event.startswith("deepseek.response"):
            set_run_stage(run_id, "geometry")
            render_stage("geometry")

    add_monitor_event(
        "run.start",
        {
            "provider": provider,
            "source": source_name,
            "selected_lines": selected_line_ids,
            "cache_enabled": loaded_use_cache,
            "pdf_cache": get_pdf_cache_status(pdf_path) if loaded_use_cache else "disabled",
            "groups_prepare_seconds": loaded_prepare_seconds,
        },
    )

    line_by_id = {group.line_id: group for group in groups}
    groups_to_run = [
        line_by_id[line_id]
        for line_id in selected_line_ids
        if line_id in line_by_id and (not loaded_use_cache or reanalyze or f"{cache_prefix}|{line_id}" not in analysis_cache)
    ]
    cached_count = len(selected_line_ids) - len(groups_to_run) if loaded_use_cache else 0
    if cached_count:
        add_monitor_event("cache.hit", {"groups": cached_count})

    started_at = time.perf_counter()
    total_to_run = len(groups_to_run)
    try:
        if not total_to_run:
            set_run_stage(run_id, "cache", 1, "Все выбранные линии уже есть в кеше.")
            render_stage("cache")
            status_box.success("Все выбранные линии уже есть в кеше.")
        else:
            for index, group in enumerate(groups_to_run, start=1):
                progress_value = (index - 1) / max(total_to_run, 1)
                progress_text = f"{group.line_id}: подготовка"
                set_run_stage(run_id, "extract", progress_value, progress_text)
                status_box.info(f"Анализ {group.line_id}: {index}/{total_to_run}")
                progress_bar.progress(progress_value, text=progress_text)
                render_stage("extract")

                ai_client = build_ai_client(on_ai_event)
                if ai_client is None:
                    raise RuntimeError("Для анализа нужна переменная окружения DEEPSEEK_API_KEY или локальный .env.")

                def on_progress(current: int, total: int, line_id: str) -> None:
                    add_monitor_event("pipeline.group.start", {"line_id": line_id, "current": current, "total": total})
                    set_run_stage(run_id, "extract", (index - 1) / max(total_to_run, 1), f"{line_id}: извлечение кандидатов")
                    render_stage("extract")

                project_for_group = run_pipeline_from_groups(
                    source_name=source_name,
                    pages_count=len(pages),
                    groups=[group],
                    ai_client=ai_client,
                    progress=on_progress,
                    pdf_path=pdf_path,
                    extract_candidates=True,
                )
                add_monitor_event(
                    "pipeline.geometry.done",
                    {
                        "line_id": group.line_id,
                        "segments": len(project_for_group.result.segments),
                        "points": len(project_for_group.result.points),
                    },
                )
                render_stage("validate")
                add_monitor_event(
                    "pipeline.validation.done",
                    {
                        "line_id": group.line_id,
                        "lines": len(project_for_group.result.lines),
                        "uncertainties": len(project_for_group.result.uncertainties),
                    },
                )
                analysis_cache[f"{cache_prefix}|{group.line_id}"] = project_for_group
                run["result_line_ids"] = [*run.get("result_line_ids", []), group.line_id]
                add_monitor_event(
                    "cache.store",
                    {
                        "line_id": group.line_id,
                        "segments": len(project_for_group.result.segments),
                        "uncertainties": len(project_for_group.result.uncertainties),
                    },
                )
                done_progress = index / max(total_to_run, 1)
                done_text = f"{group.line_id}: готово"
                set_run_stage(run_id, "cache", done_progress, done_text)
                progress_bar.progress(done_progress, text=done_text)
                render_stage("cache")

            status_box.success(f"Готово. Новых разборов: {total_to_run}, из кеша: {cached_count}.")

        add_monitor_event(
            "run.done",
            {
                "elapsed_seconds": round(time.perf_counter() - started_at, 2),
                "selected_lines": len(selected_line_ids),
                "new_runs": total_to_run,
                "cached": cached_count,
            },
        )
        run["status"] = "complete"
        run["result_line_ids"] = selected_line_ids
        set_run_stage(run_id, "cache", 1, f"Готово. Новых разборов: {total_to_run}, из кеша: {cached_count}.")
        st.rerun()
    except Exception as error:
        run["status"] = "error"
        run["error"] = str(error)
        add_monitor_event("run.error", {"error": str(error), "selected_lines": selected_line_ids})
        set_run_stage(run_id, run.get("stage", "groups"), float(run.get("progress", 0.0)), str(error))
        st.rerun()


init_workspace()
render_workspace_tabs()

source_mode = ""
uploaded = None
use_cache = True
load_pdf_clicked = False
provider = "DeepSeek API"
deepseek_key = os.getenv("DEEPSEEK_API_KEY", "")

with st.sidebar:
    st.header("Тулбар")
    if st.button("Начать анализ", type="primary", width="stretch"):
        set_active_tab(HOME_TAB_ID)
        st.rerun()
    if st.button("История", width="stretch"):
        open_history_tab()
        st.rerun()
    st.divider()
    st.caption("Провайдер: DeepSeek API")
    if deepseek_key:
        st.success("DEEPSEEK_API_KEY найден.")
    else:
        st.warning("DEEPSEEK_API_KEY не найден.")

current_workspace = active_workspace_tab()
open_workspace_content()
if current_workspace["type"] == "request_history":
    render_request_history_panel()
    close_workspace_content()
    st.stop()

deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
include_images = os.getenv("DEEPSEEK_INCLUDE_IMAGES", "1") != "0"
analysis_cache = st.session_state.setdefault("analysis_cache", {})
ANALYSIS_CACHE_VERSION = "v9-coordinate-delta-skip-note"
if current_workspace["type"] == "analysis":
    if "loaded_pdf_path" not in st.session_state:
        st.error("Сначала загрузите PDF на вкладке Welcome.")
        close_workspace_content()
        st.stop()
    pdf_path = Path(st.session_state["loaded_pdf_path"])
    source_name = st.session_state["loaded_source_name"]
    pages = st.session_state["loaded_pages"]
    groups = st.session_state["loaded_groups"]
    loaded_prepare_seconds = float(st.session_state.get("loaded_prepare_seconds", 0))
    loaded_use_cache = bool(st.session_state.get("loaded_use_cache", True))
    cache_prefix = f"{ANALYSIS_CACHE_VERSION}|{pdf_cache_key(pdf_path)}|{provider}"
    st.session_state["pdf_path"] = str(pdf_path)
    current_run = st.session_state.get("analysis_runs", {}).get(current_workspace["run_id"], {})
    if current_run.get("status") == "pending":
        render_run_status(current_run)
        execute_analysis_run(
            run_id=current_workspace["run_id"],
            source_name=source_name,
            pages=pages,
            groups=groups,
            groups_df=group_table(groups, analysis_cache, cache_prefix),
            selected_line_ids=list(current_run.get("selected_line_ids", [])),
            analysis_cache=analysis_cache,
            cache_prefix=cache_prefix,
            provider=provider,
            loaded_use_cache=loaded_use_cache,
            loaded_prepare_seconds=loaded_prepare_seconds,
            reanalyze=bool(current_run.get("reanalyze", False)),
            pdf_path=pdf_path,
            deepseek_key=deepseek_key,
            deepseek_model=deepseek_model,
            include_images=include_images,
        )
        close_workspace_content()
        st.stop()
    render_analysis_workspace_page(
        run_id=current_workspace["run_id"],
        source_name=source_name,
        pages_count=len(pages),
        groups_df=group_table(groups, analysis_cache, cache_prefix),
        analysis_cache=analysis_cache,
        cache_prefix=cache_prefix,
        provider=provider,
        loaded_use_cache=loaded_use_cache,
        deepseek_key=deepseek_key,
    )
    close_workspace_content()
    st.stop()

if current_workspace["type"] != "welcome":
    st.error("Неизвестная вкладка workspace.")
    close_workspace_content()
    st.stop()

render_welcome_page()
source_mode, uploaded, use_cache, load_pdf_clicked = render_pdf_source_controls(DEFAULT_PDF)

if load_pdf_clicked:
    pdf_path_to_load: Path | None = None
    source_name_to_load = ""
    if source_mode == "Локальный Изометрии.pdf":
        if DEFAULT_PDF.exists():
            pdf_path_to_load = DEFAULT_PDF
            source_name_to_load = DEFAULT_PDF.name
        else:
            st.error("Локальный Изометрии.pdf не найден.")
            close_workspace_content()
            st.stop()
    else:
        if uploaded is None:
            st.error("Выберите PDF-файл для загрузки.")
            close_workspace_content()
            st.stop()
        pdf_path_to_load = save_uploaded_pdf(uploaded)
        source_name_to_load = uploaded.name

    prepare_started_at = time.perf_counter()
    with st.spinner("Читаю PDF и группирую линии..."):
        if use_cache:
            loaded_pages, loaded_groups = cached_prepare_pdf_groups(str(pdf_path_to_load))
        else:
            loaded_pages, loaded_groups = prepare_pdf_groups(pdf_path_to_load, use_disk_cache=False)

    st.session_state["loaded_pdf_path"] = str(pdf_path_to_load)
    st.session_state["loaded_source_name"] = source_name_to_load
    st.session_state["loaded_pages"] = loaded_pages
    st.session_state["loaded_groups"] = loaded_groups
    st.session_state["loaded_prepare_seconds"] = round(time.perf_counter() - prepare_started_at, 2)
    st.session_state["loaded_use_cache"] = use_cache
    st.session_state["monitor_events"] = []
    if not use_cache:
        prefix_to_clear = f"{ANALYSIS_CACHE_VERSION}|{pdf_cache_key(pdf_path_to_load)}|{provider}"
        for cache_key in [key for key in analysis_cache if key.startswith(prefix_to_clear)]:
            del analysis_cache[cache_key]

if "loaded_pdf_path" not in st.session_state:
    st.info("Выберите источник и нажмите «Загрузить PDF». После этого появится список сгруппированных линий.")
    close_workspace_content()
    st.stop()

pdf_path = Path(st.session_state["loaded_pdf_path"])
source_name = st.session_state["loaded_source_name"]
pages = st.session_state["loaded_pages"]
groups = st.session_state["loaded_groups"]
loaded_prepare_seconds = float(st.session_state.get("loaded_prepare_seconds", 0))
loaded_use_cache = bool(st.session_state.get("loaded_use_cache", True))
cache_prefix = f"{ANALYSIS_CACHE_VERSION}|{pdf_cache_key(pdf_path)}|{provider}"
st.session_state["pdf_path"] = str(pdf_path)

st.caption(
    f"Выбран файл: {source_name}. Страниц: {len(pages)}. Групп линий: {len(groups)}. "
    f"Кеш: {'включен' if loaded_use_cache else 'выключен'}."
)
render_pipeline("groups")

available_line_ids = group_options(groups)
default_selection = available_line_ids[:1]
selected_line_ids = st.multiselect(
    "Линии для анализа",
    options=available_line_ids,
    default=default_selection,
    placeholder="Выберите одну или несколько линий",
)

groups_df = group_table(groups, analysis_cache, cache_prefix)
st.subheader("Сгруппированные линии")
groups_table_box = st.empty()
groups_table_box.dataframe(groups_df, width="stretch", hide_index=True)

reanalyze = st.checkbox("Переанализировать выбранные, даже если они уже есть в кеше", value=False)
run_clicked = st.button("Анализировать выбранные линии", type="primary", width="stretch")

if run_clicked:
    if not selected_line_ids:
        st.warning("Выберите хотя бы одну линию.")
        st.stop()
    run_id = create_analysis_tab(selected_line_ids)
    st.session_state["analysis_runs"][run_id]["reanalyze"] = reanalyze
    st.rerun()

st.info("Выберите линии и нажмите «Анализировать выбранные линии». Новый запуск откроется в отдельной вкладке workspace.")
close_workspace_content()
st.stop()


def build_ai_client(event_callback) -> DeepSeekAIClient | None:
    if not deepseek_key.strip():
        st.error("Для анализа нужна переменная окружения DEEPSEEK_API_KEY или локальный .env.")
        return None
    return DeepSeekAIClient(
        api_key=deepseek_key,
        model=deepseek_model,
        pdf_path=pdf_path,
        include_images=include_images,
        max_image_pages=1,
        event_callback=event_callback,
    )


pipeline_box = st.empty()
status_box = st.empty()
progress_bar = st.empty()
monitor_box = st.empty()

if run_clicked:
    if not selected_line_ids:
        st.warning("Выберите хотя бы одну линию.")
        st.stop()

    run_id = create_analysis_tab(selected_line_ids)
    add_monitor_event(
        "run.start",
        {
            "provider": provider,
            "source": source_name,
            "selected_lines": selected_line_ids,
            "cache_enabled": loaded_use_cache,
            "pdf_cache": get_pdf_cache_status(pdf_path) if loaded_use_cache else "disabled",
            "groups_prepare_seconds": loaded_prepare_seconds,
        },
    )

    def render_stage(stage: str) -> None:
        set_run_stage(run_id, stage)
        with pipeline_box.container():
            render_pipeline(stage)
        with monitor_box.container():
            render_stage_details(
                stage,
                groups_df,
                timeline_frame=timeline_dataframe(run_id),
                raw_events_frame=monitor_dataframe(run_id),
                cache_count=sum(1 for key in analysis_cache if key.startswith(cache_prefix)),
            )

    def on_ai_event(event: str, payload: dict) -> None:
        add_monitor_event(event, payload)
        if event.startswith("deepseek.classify"):
            render_stage("classify")
        elif event.startswith("deepseek.payload") or event.startswith("deepseek.image") or event.startswith("deepseek.request"):
            render_stage("analyze")
        elif event.startswith("deepseek.response"):
            render_stage("geometry")

    line_by_id = {group.line_id: group for group in groups}
    groups_to_run = [
        line_by_id[line_id]
        for line_id in selected_line_ids
        if line_id in line_by_id and (not loaded_use_cache or reanalyze or f"{cache_prefix}|{line_id}" not in analysis_cache)
    ]
    cached_count = len(selected_line_ids) - len(groups_to_run) if loaded_use_cache else 0

    if cached_count:
        add_monitor_event("cache.hit", {"groups": cached_count})

    started_at = time.perf_counter()
    total_to_run = len(groups_to_run)
    if not total_to_run:
        render_stage("cache")
        set_run_stage(run_id, "cache", 1, "Все выбранные линии уже есть в кеше.")
        status_box.success("Все выбранные линии уже есть в кеше.")
    else:
        for index, group in enumerate(groups_to_run, start=1):
            progress_value = (index - 1) / max(total_to_run, 1)
            progress_text = f"{group.line_id}: подготовка"
            set_run_stage(run_id, "extract", progress_value, progress_text)
            status_box.info(f"Анализ {group.line_id}: {index}/{total_to_run}")
            progress_bar.progress(progress_value, text=progress_text)
            render_stage("extract")

            ai_client = build_ai_client(on_ai_event)
            if ai_client is None:
                run = st.session_state["analysis_runs"][run_id]
                run["status"] = "error"
                run["error"] = "Для анализа нужна переменная окружения DEEPSEEK_API_KEY или локальный .env."
                set_run_stage(run_id, run.get("stage", "groups"), progress_value, run["error"])
                st.stop()

            def on_progress(current: int, total: int, line_id: str) -> None:
                add_monitor_event("pipeline.group.start", {"line_id": line_id, "current": current, "total": total})
                set_run_stage(run_id, "extract", (index - 1) / max(total_to_run, 1), f"{line_id}: извлечение кандидатов")
                render_stage("extract")

            project_for_group = run_pipeline_from_groups(
                source_name=source_name,
                pages_count=len(pages),
                groups=[group],
                ai_client=ai_client,
                progress=on_progress,
                pdf_path=pdf_path,
                extract_candidates=True,
            )
            add_monitor_event(
                "pipeline.geometry.done",
                {
                    "line_id": group.line_id,
                    "segments": len(project_for_group.result.segments),
                    "points": len(project_for_group.result.points),
                },
            )
            render_stage("validate")
            add_monitor_event(
                "pipeline.validation.done",
                {
                    "line_id": group.line_id,
                    "lines": len(project_for_group.result.lines),
                    "uncertainties": len(project_for_group.result.uncertainties),
                },
            )
            analysis_cache[f"{cache_prefix}|{group.line_id}"] = project_for_group
            st.session_state["analysis_runs"][run_id]["result_line_ids"] = [
                *st.session_state["analysis_runs"][run_id].get("result_line_ids", []),
                group.line_id,
            ]
            groups_table_box.dataframe(
                group_table(groups, analysis_cache, cache_prefix),
                width="stretch",
                hide_index=True,
            )
            add_monitor_event(
                "cache.store",
                {
                    "line_id": group.line_id,
                    "segments": len(project_for_group.result.segments),
                    "uncertainties": len(project_for_group.result.uncertainties),
                },
            )
            render_stage("cache")
            done_progress = index / max(total_to_run, 1)
            done_text = f"{group.line_id}: готово"
            set_run_stage(run_id, "cache", done_progress, done_text)
            progress_bar.progress(done_progress, text=done_text)

        status_box.success(f"Готово. Новых разборов: {total_to_run}, из кеша: {cached_count}.")

    add_monitor_event(
        "run.done",
        {
            "elapsed_seconds": round(time.perf_counter() - started_at, 2),
            "selected_lines": len(selected_line_ids),
            "new_runs": total_to_run,
            "cached": cached_count,
        },
    )
    render_stage("cache")
    run = st.session_state["analysis_runs"][run_id]
    run["status"] = "complete"
    run["result_line_ids"] = selected_line_ids
    set_run_stage(run_id, "cache", 1, f"Готово. Новых разборов: {total_to_run}, из кеша: {cached_count}.")
    st.rerun()

project = combine_cached_projects(
    source_name=source_name,
    pages_count=len(pages),
    selected_line_ids=selected_line_ids,
    cache=analysis_cache,
    cache_prefix=cache_prefix,
    provider=provider,
)

if not project:
    st.info("Выберите линии и нажмите «Анализировать выбранные линии». После анализа результаты появятся ниже.")
    st.stop()

missing_results = [line_id for line_id in selected_line_ids if f"{cache_prefix}|{line_id}" not in analysis_cache]
if missing_results:
    st.info(f"Еще не разобраны: {', '.join(missing_results)}")

render_metrics(project)

lines_df = result_dataframe(project.result, "lines")
points_df = result_dataframe(project.result, "points")
segments_df = result_dataframe(project.result, "segments")
elements_df = result_dataframe(project.result, "elements")
uncertainties_df = result_dataframe(project.result, "uncertainties")
annotations_df = result_dataframe(project.result, "annotations")
candidates_df = result_dataframe(project.result, "candidates")
classifications_df = result_dataframe(project.result, "candidate_classifications")

tabs = st.tabs(["Линии", "Точки", "Участки", "Элементы", "Неопределённости", "Кандидаты PDF", "Классификация", "Разметка", "Запросы", "Журнал", "Экспорт"])

with tabs[0]:
    show_dataframe("Линии", lines_df)

with tabs[1]:
    show_dataframe("Точки", points_df)

with tabs[2]:
    show_dataframe("Участки", segments_df)

with tabs[3]:
    show_dataframe("Элементы", elements_df)

with tabs[4]:
    show_dataframe("Неопределённости", uncertainties_df)

with tabs[5]:
    show_dataframe("Кандидаты PDF", candidates_df)

with tabs[6]:
    show_dataframe("Классификация кандидатов", classifications_df)

with tabs[7]:
    st.subheader("Разметка")
    if annotations_df.empty:
        st.info("Разметка пока отсутствует.")
    else:
        available_pages = sorted({item.page for item in project.result.annotations})
        selected_page = st.selectbox("Страница", available_pages)
        page_annotations = annotations_for_page(project.result.annotations, selected_page)
        skipped_annotations = len(page_annotations) - len(drawable_annotations(page_annotations))
        if skipped_annotations:
            st.caption(f"Аннотаций без bbox не отрисовано: {skipped_annotations}")
        try:
            image_bytes = render_page_with_annotations(st.session_state["pdf_path"], selected_page, page_annotations)
            st.image(image_bytes, caption=f"Страница {selected_page}: MVP-разметка", width="stretch")
        except Exception as error:
            st.error(f"Не удалось отрисовать страницу: {error}")
        show_dataframe("Объекты разметки", annotations_df)

with tabs[8]:
    st.subheader("Мониторинг запросов")
    requests_df = request_monitor_dataframe()
    events_df = monitor_dataframe()
    if requests_df.empty:
        st.info("Данные по запросам появятся после запуска анализа.")
    else:
        metric_cols = st.columns(4)
        done_count = int((requests_df["status"] == "done").sum()) if "status" in requests_df else 0
        error_count = int((requests_df["status"] == "error").sum()) if "status" in requests_df else 0
        elapsed_values = pd.to_numeric(requests_df.get("elapsed_seconds", pd.Series(dtype=float)), errors="coerce").dropna()
        total_elapsed = round(float(elapsed_values.sum()), 2) if not elapsed_values.empty else 0
        with metric_cols[0]:
            st.metric("Запросов", len(requests_df))
        with metric_cols[1]:
            st.metric("Завершено", done_count)
        with metric_cols[2]:
            st.metric("Ошибок", error_count)
        with metric_cols[3]:
            st.metric("Суммарно, сек", total_elapsed)

        st.dataframe(requests_df, width="stretch", hide_index=True)

        provider_events = events_df[events_df["event"].astype(str).str.startswith("deepseek.")] if not events_df.empty else pd.DataFrame()
        with st.expander("Подробные события провайдера"):
            if provider_events.empty:
                st.info("Событий провайдера пока нет.")
            else:
                st.dataframe(provider_events, width="stretch", hide_index=True)

        with st.expander("Ошибки провайдера"):
            if provider_events.empty or "event" not in provider_events:
                st.info("Ошибок нет.")
            else:
                error_events = provider_events[provider_events["event"].astype(str).str.contains("error", case=False, na=False)]
                if error_events.empty:
                    st.info("Ошибок нет.")
                else:
                    st.dataframe(error_events, width="stretch", hide_index=True)

with tabs[9]:
    st.subheader("Журнал анализа")
    events_df = monitor_dataframe()
    timeline_df = timeline_dataframe()
    if events_df.empty:
        st.info("Лог запуска появится после анализа.")
    else:
        summary_cols = st.columns(4)
        provider_events = events_df[events_df["event"].astype(str).str.startswith("deepseek.")]
        errors = timeline_df[timeline_df["level"] == "error"] if not timeline_df.empty else pd.DataFrame()
        with summary_cols[0]:
            st.metric("Событий", len(events_df))
        with summary_cols[1]:
            st.metric("Событий провайдера", len(provider_events))
        with summary_cols[2]:
            st.metric("Ошибок", len(errors))
        with summary_cols[3]:
            st.metric("Линий в результате", len(project.result.lines))

        selected_stage_label = st.radio(
            "Этап pipeline",
            [label for _, label in PIPELINE_STEPS],
            horizontal=True,
            key="journal_stage",
        )
        selected_stage = PIPELINE_ID_BY_LABEL[selected_stage_label]
        render_pipeline(selected_stage)
        render_stage_details(
            selected_stage,
            groups_df,
            candidates_frame=candidates_df,
            classifications_frame=classifications_df,
            timeline_frame=timeline_df,
            raw_events_frame=events_df,
            uncertainties_frame=uncertainties_df,
            cache_count=sum(1 for key in analysis_cache if key.startswith(cache_prefix)) if loaded_use_cache else 0,
        )

        provider_df = events_df[events_df["event"].astype(str).str.startswith("deepseek.")]
        with st.expander("События провайдера DeepSeek", expanded=True):
            if provider_df.empty:
                st.info("Событий провайдера пока нет.")
            else:
                st.dataframe(provider_df, width="stretch", hide_index=True)

        with st.expander("Сырые события pipeline"):
            st.dataframe(events_df, width="stretch", hide_index=True)

        with st.expander("Полный timeline"):
            st.dataframe(timeline_df, width="stretch", hide_index=True)

        with st.expander("Итоговые предупреждения результата"):
            if uncertainties_df.empty:
                st.info("Неопределённостей нет.")
            else:
                st.dataframe(uncertainties_df, width="stretch", hide_index=True)

    st.markdown(
        f"""
        <div class="monitor-card">
            <strong>Текущая конфигурация</strong><br>
            Провайдер: <code>{provider}</code><br>
            Выбрано линий: <code>{len(selected_line_ids)}</code><br>
            Использование кеша: <code>{"включено" if loaded_use_cache else "выключено"}</code><br>
            В кеше по этому PDF: <code>{sum(1 for key in analysis_cache if key.startswith(cache_prefix)) if loaded_use_cache else 0}</code><br>
            Ключ DeepSeek: <code>{"найден" if deepseek_key else "не найден"}</code>
        </div>
        """,
        unsafe_allow_html=True,
    )

with tabs[10]:
    st.subheader("Экспорт")
    json_bytes = to_json_bytes(project)
    excel_bytes = to_excel_bytes(project)
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Скачать JSON",
            data=json_bytes,
            file_name="isometry_analysis_result.json",
            mime="application/json",
            width="stretch",
        )
    with right:
        st.download_button(
            "Скачать Excel",
            data=excel_bytes,
            file_name="isometry_analysis_result.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

