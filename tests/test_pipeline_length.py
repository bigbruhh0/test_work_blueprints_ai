import json
from pathlib import Path

from jsonschema import Draft202012Validator

from src.dimension_mapping import map_dimensions, save_pipeline_length_diagnostic_pdf
from src.pipeline_length import (
    build_pipeline_length_payload,
    build_pipeline_length_result,
    calculate_local_length_summary,
)


def test_excel_export_rows_include_human_review_tables_and_source_links():
    from server import _run_export_rows

    run = {
        "lines": [{
            "line_id": "CO_0031",
            "status": "complete",
            "page_results": [{
                "page_number": 216,
                "status": "complete",
                "stage": "pipeline_length",
                "files": {"pipeline_length_diagnostic_pdf": "page216.pdf"},
                "vertices": [{"id": "V01", "role": "endpoint", "x": 10, "y": 20, "source": "base"}],
                "numbers": [],
                "coordinates": [],
                "analysis": {"pipeline_length_local": {
                    "base_vertices": [{"id": "V01"}],
                    "base_edges": [{"id": "E001", "from_vertex": "V01", "to_vertex": "V02", "pixel_length": 12}],
                    "dimensions": [{
                        "id": "D001", "value": 100, "edge_id": "E001",
                        "local_filter_decision": "include", "local_filter_reason": "accepted",
                    }],
                    "handwheel_annotations": {"handwheels": [{"id": "HW-01", "label": "HW"}]},
                }},
                "events": [],
            }],
            "files": {"pipeline_length_payload_json": "payload.json", "pipeline_length_response_json": "response.json"},
            "analysis": {"pipeline_length": {
                "local_result": {"clean_length_mm": 100, "main": {"clean_length_mm": 100}, "page_summaries": [{"page": 216, "clean_length_mm": 100, "dirty_length_mm": 100, "ambiguous_length_mm": 0}]},
                "provider_result": {"calculated_lengths": {"main": {"clean_length_mm": 100}, "branch": {"clean_length_mm": 0}, "total_clean_length_mm": 100}, "candidate_assessments": []},
            }},
            "provider_trace": {"payload": {"pages": []}},
        }]
    }
    rows = _run_export_rows(run)
    for sheet in ("Линии", "Итоги", "Сравнение", "Точки", "Участки", "Элементы", "Неопределенности", "Расчет по листам"):
        assert sheet in rows
    assert rows["Линии"][0]["local_total_length_mm"] == 100
    assert rows["Линии"][0]["provider_response"] == "response.json"
    assert rows["Точки"][0]["diagnostic_pdf"] == "page216.pdf"
    assert rows["Участки"][0]["source_dimensions"] == "D001"
    assert rows["Участки"][0]["length_mm"] == 100
    assert rows["Элементы"][0]["element_type"] == "valve / handwheel"


def test_pipeline_length_payload_keeps_base_v_and_source_edges_only():
    mapping = {
        "base_vertices": [{"id": "V01", "role": "endpoint", "x": 1, "y": 2}],
        "base_edges": [{"id": "E001", "start": [1, 2], "end": [4, 2], "pixel_length": 3}],
        "dimensions": [{
            "id": "D001", "value": 300, "text": "300", "label_center": [2, 2],
            "edge_id": "E001", "status": "projected", "local_filter_decision": "include",
            "local_filter_reason": "ok", "dimension_stroke": {"start": [0, 0], "end": [1, 1]},
        }],
        "connections": [],
        "discarded_numbers": [],
    }
    payload = build_pipeline_length_payload("L-1", "drawing.pdf", [(216, mapping)])
    schema = json.loads(Path("schemas/pipeline_length_payload.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
    assert payload["pages"][0]["vertices"][0]["id"] == "V01"
    assert payload["pages"][0]["edges"][0]["id"] == "E001"
    assert all(not str(row["id"]).startswith(("HG-", "VE-", "F-")) for row in payload["pages"][0]["vertices"] + payload["pages"][0]["edges"])
    assert payload["pages"][0]["dimensions"][0]["local_decision"]["decision"] == "include"
    assert payload["pages"][0]["handwheels"] == []
    assert payload["pages"][0]["handwheel_glyphs"] == []


def test_pipeline_length_payload_combines_pages_and_local_summary():
    def page(value, decision):
        return {
            "base_vertices": [], "base_edges": [], "connections": [], "discarded_numbers": [],
            "dimensions": [{"id": f"D{value}", "value": value, "text": str(value), "local_filter_decision": decision}],
        }
    payload = build_pipeline_length_payload("L-1", "drawing.pdf", [(218, page(100, "include")), (216, page(50, "ambiguous"))])
    assert payload["cross_page_context"]["page_order"] == [216, 218]
    summary = calculate_local_length_summary(payload)
    assert summary["clean_length_mm"] == 100
    assert summary["ambiguous_length_mm"] == 50


def test_provider_suggestions_are_not_applied_automatically():
    payload = {"analysis_type": "pipeline_length", "pages": []}
    answer = {"candidate_assessments": [{"candidate_id": "D001", "assessment": "disputed", "proposed_decision": "exclude"}], "lengths": {}}
    result = build_pipeline_length_result(payload, answer)
    assert result["manual_confirmation_required"] is True
    assert result["applied_provider_changes"] is False
    assert result["provider_result"]["candidate_assessments"] == answer["candidate_assessments"]
    assert result["provider_result"]["lengths"] == answer["lengths"]
    assert result["provider_result"]["calculated_lengths"]["total_clean_length_mm"] == 0


def test_provider_length_summary_is_calculated_separately_from_provider_answer():
    payload = {
        "analysis_type": "pipeline_length",
        "pages": [{
            "page": 216,
            "dimensions": [
                {"id": "D001", "candidate_key": "216:D001", "value_mm": 100, "edge_id": "E001", "local_decision": {"decision": "include"}},
                {"id": "D002", "candidate_key": "216:D002", "value_mm": 40, "edge_id": "E002", "local_decision": {"decision": "exclude"}},
            ],
        }],
    }
    answer = {
        "candidate_assessments": [
            {"candidate_id": "216:D001", "assessment": "accepted", "local_decision": "include"},
            {"candidate_id": "216:D002", "assessment": "disputed", "proposed_decision": "include"},
        ],
        "edge_routes": [{"edge_id": "216:E002", "route_type": "branch"}],
    }
    result = build_pipeline_length_result(payload, answer)
    calculated = result["provider_result"]["calculated_lengths"]
    assert calculated["main"]["clean_length_mm"] == 100
    assert calculated["branch"]["clean_length_mm"] == 40
    assert calculated["total_clean_length_mm"] == 140


def test_manual_provider_choice_changes_calculated_length():
    payload = {
        "pages": [{"page": 216, "dimensions": [{
            "id": "D001", "candidate_key": "216:D001", "value_mm": 100, "edge_id": "E001",
            "local_decision": {"decision": "include"},
        }]}],
    }
    answer = {"candidate_assessments": [{
        "candidate_id": "216:D001", "local_decision": "include", "assessment": "disputed", "proposed_decision": "exclude",
    }]}
    result = build_pipeline_length_result(payload, answer)
    provider = result["provider_result"]
    from src.pipeline_length import calculate_provider_length_summary
    accepted = calculate_provider_length_summary(payload, provider, {"candidate_ids": ["216:D001"]})
    kept = calculate_provider_length_summary(payload, provider, {"keep_local_candidate_ids": ["216:D001"]})
    assert accepted["total_clean_length_mm"] == 0
    assert kept["total_clean_length_mm"] == 100


def test_provider_returns_each_branch_and_counts_explicit_candidates_once():
    from src.pipeline_length import calculate_provider_length_summary

    payload = {"pages": [{"page": 216, "dimensions": [
        {"id": "D001", "candidate_key": "216:D001", "value_mm": 100, "edge_id": "E001", "local_decision": {"decision": "include"}},
        {"id": "D002", "candidate_key": "216:D002", "value_mm": 50, "edge_id": "E002", "local_decision": {"decision": "include"}},
        {"id": "D002", "candidate_key": "216:D002", "value_mm": 50, "edge_id": "E002", "local_decision": {"decision": "include"}},
    ]}]}
    answer = {
        "edge_routes": [{"edge_id": "216:E002", "route_type": "branch"}],
        "branch_routes": [{"branch_id": "BR-01", "junction_vertex_id": "V01", "endpoint_vertex_id": "V02", "edge_ids": ["216:E002"], "candidate_ids": ["216:D002"]}],
    }
    result = calculate_provider_length_summary(payload, answer)
    assert result["main"]["clean_length_mm"] == 100
    assert result["branches"][0]["clean_length_mm"] == 50
    assert result["total_clean_length_mm"] == 150
    assert result["branch"]["counted_candidate_ids"] == ["216:D002"]


def test_local_snapshot_does_not_build_final_vertices():
    mapping = map_dimensions(Path("Изометрии.pdf"), 216, finalize=False)
    assert mapping["analysis_stage"] == "local_processing"
    assert mapping["vertices"]
    assert all(str(vertex["id"]).startswith("V") for vertex in mapping["vertices"])
    assert not mapping.get("final_vertices")
    assert not mapping.get("edge_segments")
    annotations = mapping["handwheel_annotations"]
    assert annotations["handwheels"]
    assert annotations["glyphs"]
    assert "handwheels" not in mapping
    assert "handwheel_glyphs" not in mapping


def test_pipeline_length_diagnostic_pdf_includes_handwheel_evidence(tmp_path):
    mapping = map_dimensions(Path("Изометрии.pdf"), 216, finalize=False)
    output = tmp_path / "pipeline_length_diagnostic.pdf"
    save_pipeline_length_diagnostic_pdf(Path("Изометрии.pdf"), 216, output, mapping)
    assert output.exists()
    assert output.stat().st_size > 0
