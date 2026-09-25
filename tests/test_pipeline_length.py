import json
from pathlib import Path

from jsonschema import Draft202012Validator

from src.dimension_mapping import map_dimensions, save_pipeline_length_diagnostic_pdf
from src.pipeline_length import (
    build_pipeline_length_payload,
    build_pipeline_length_result,
    calculate_local_length_summary,
    merge_pipeline_length_provider_traces,
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


def test_pipeline_length_payload_uses_final_vertices_and_edges():
    mapping = {
        "final_vertices": [
            {
                "id": "HG-01-A", "point": [10.0, 20.0], "vertex_source": "handwheel",
                "handwheel_id": "HW-001", "source": "handwheel_glyph_span",
            },
            {
                "id": "VE-07", "point": [30.0, 40.0], "vertex_source": "extension",
                "dimension_ids": ["D010"], "edge_id": "E007",
            },
        ],
        "final_contour": [
            {
                "id": "F-HG-01-A-HG-01-B", "from_vertex": "HG-01-A", "to_vertex": "HG-01-B",
                "start": [10.0, 20.0], "end": [10.0, 90.0], "pixel_length": 70.0,
                "element_type": "valve", "is_handwheel_segment": True, "handwheel_ids": ["HW-001"],
            },
            {
                "id": "F-VE-07-HG-03-A", "from_vertex": "VE-07", "to_vertex": "HG-03-A",
                "start": [30.0, 40.0], "end": [50.0, 40.0], "pixel_length": 20.0,
                "element_type": "pipe",
            },
        ],
        "final_edge_candidate_groups": [
            {
                "edge_id": "F-VE-07-HG-03-A",
                "candidates": [{"candidate_id": "D010", "value_mm": 203}],
            }
        ],
        "dimensions": [
            {
                "id": "D010", "value": 203, "text": "203", "local_filter_decision": "include",
                "final_interval_id": "F-VE-07-HG-03-A", "final_from_vertex": "VE-07",
                "final_to_vertex": "HG-03-A", "final_interval_status": "resolved",
                "dimension_stroke": {"start": [0, 0], "end": [1, 1]},
            },
            {
                "id": "D001", "value": 1057, "text": "1057", "local_filter_decision": "include",
                "final_interval_status": "unresolved", "final_interval_reason": "extension_projection_misses_pipe",
            },
        ],
        "connections": [
            {"id": "CN-1", "label": "СМ. CO-0031 ЛИСТ 2", "target_sheet": "2", "bbox": [1, 1, 2, 2], "center": [1.5, 1.5]},
        ],
        "coordinates": [],
        "handwheel_annotations": {"handwheels": [{"id": "HW-001", "label": "ШТУРВАЛ"}], "glyphs": []},
    }
    payload = build_pipeline_length_payload("L-1", "drawing.pdf", [(216, mapping)])
    schema = json.loads(Path("schemas/pipeline_length_payload.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(payload)) == []
    page = payload["pages"][0]
    assert [row["id"] for row in page["final_vertices"]] == ["HG-01-A", "VE-07"]
    assert page["vertices"] == page["final_vertices"]
    assert [row["id"] for row in page["final_edges"]] == ["F-HG-01-A-HG-01-B", "F-VE-07-HG-03-A"]
    assert page["final_edges"][1]["candidates"][0]["candidate_key"] == "216:D010"
    assert page["debug_source_vertices"] == []
    assert page["debug_source_edges"] == []
    dimension = next(row for row in page["dimensions"] if row["id"] == "D010")
    assert dimension["final_edge_id"] == "F-VE-07-HG-03-A"
    assert dimension["final_from_vertex"] == "VE-07"
    assert dimension["final_to_vertex"] == "HG-03-A"
    assert [row["candidate_id"] for row in page["unresolved_dimensions"]] == ["D001"]
    assert page["unresolved_dimensions"][0]["final_interval_reason"] == "extension_projection_misses_pipe"


def test_local_processing_snapshot_payload_keeps_base_only_as_debug():
    mapping = {
        "base_vertices": [{"id": "V01", "role": "endpoint", "x": 1, "y": 2}],
        "base_edges": [{"id": "E001", "start": [1, 2], "end": [4, 2], "pixel_length": 3}],
        "dimensions": [{
            "id": "D001", "value": 300, "text": "300", "label_center": [2, 2],
            "edge_id": "E001", "status": "projected", "local_filter_decision": "include",
            "local_filter_reason": "ok", "dimension_stroke": {"start": [0, 0], "end": [1, 1]},
        }],
        "connections": [],
        "coordinates": [],
        "discarded_numbers": [],
    }
    payload = build_pipeline_length_payload("L-1", "drawing.pdf", [(216, mapping)])
    page = payload["pages"][0]
    assert page["vertices"] == []
    assert page["final_vertices"] == []
    assert page["final_edges"] == []
    assert page["debug_source_vertices"][0]["id"] == "V01"
    assert page["debug_source_edges"][0]["id"] == "E001"
    assert page["dimensions"][0]["local_decision"]["decision"] == "include"
    assert page["unresolved_dimensions"][0]["candidate_id"] == "D001"


def test_cross_sheet_text_refs_require_list_and_number():
    mapping = {
        "dimensions": [
            {"id": "D001", "value": 100, "text": "СМ. CO-0031", "local_filter_decision": "include"},
        ],
        "connections": [
            {"id": "CN-OK", "label": "СМ. CO-0031 ЛИСТ 2", "target_sheet": "2", "bbox": [1, 1, 2, 2], "center": [1.5, 1.5]},
            {"id": "CN-MATERIAL", "label": "11 Лист 200X300X6, СТ3СП5, ГОСТ 19903-2015 - 100 2", "target_sheet": "200", "bbox": [3, 3, 4, 4], "center": [3.5, 3.5]},
            {"id": "CN-PLAIN", "label": "ПОДКЛЮЧЕНИЕ V-505", "bbox": [5, 5, 6, 6], "center": [5.5, 5.5]},
        ],
        "final_vertices": [],
        "final_contour": [],
        "coordinates": [],
    }
    payload = build_pipeline_length_payload("L-1", "drawing.pdf", [(218, mapping)])
    refs = payload["pages"][0]["cross_sheet_text_refs"]
    assert len(refs) == 1
    assert refs[0]["target_page"] == 2
    assert refs[0]["source_type"] == "connection"
    assert refs[0]["source_text"] == "СМ. CO-0031 ЛИСТ 2"


def test_pipeline_length_response_schema_accepts_final_review_contract():
    schema = json.loads(Path("schemas/pipeline_length_response.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    answer = {
        "candidate_assessments": [{
            "candidate_id": "216:D010",
            "candidate_key": "216:D010",
            "local_decision": "include",
            "assessment": "disputed",
            "proposed_decision": "exclude",
            "target_final_edge_id": "216:F-VE-07-HG-03-A",
            "reason": "не тот отрезок",
        }],
        "unresolved_reviews": [{
            "candidate_id": "216:D001",
            "candidate_key": "216:D001",
            "status": "needs_review",
            "reason": "не найдена выносная",
            "suggested_action": "уточнить отрезок вручную",
        }],
        "vertex_coordinates": [{
            "vertex_id": "216:HG-01-A",
            "vertex_key": "216:HG-01-A",
            "page": 216,
            "x": 1.0, "y": 2.0, "z": 3.0,
            "confidence": 0.9,
            "method": "по осям изометрии",
            "source_coordinate_labels": ["X", "Y", "Z"],
            "reason": "координаты из угловых штампов",
        }],
        "edge_routes": [],
        "cross_sheet_links": [],
        "intermediate_distances": [],
        "lengths": {"main": {}, "branch": {}},
    }
    errors = list(Draft202012Validator(schema).iter_errors(answer))
    assert errors == []


def test_pipeline_length_payload_exposes_coordinates_and_handwheel_edges():
    mapping = {
        "final_vertices": [
            {"id": "HG-01-A", "point": [5.0, 6.0], "vertex_source": "handwheel", "handwheel_id": "HW-001"},
            {"id": "HG-01-B", "point": [5.0, 36.0], "vertex_source": "handwheel", "handwheel_id": "HW-001"},
        ],
        "final_contour": [
            {
                "id": "F-HG-01-A-HG-01-B", "from_vertex": "HG-01-A", "to_vertex": "HG-01-B",
                "start": [5.0, 6.0], "end": [5.0, 36.0], "pixel_length": 30.0,
                "element_type": "valve", "is_handwheel_segment": True, "handwheel_ids": ["HW-001"],
            }
        ],
        "dimensions": [],
        "connections": [],
        "coordinates": [{"label": "X", "value": "226150", "label_bbox": [1, 1, 2, 2], "value_bbox": [3, 1, 8, 2]}],
        "handwheel_annotations": {
            "handwheels": [{"id": "HW-001", "label": "ШТУРВАЛ", "edge_id": "E001", "arrow_found": True}],
            "glyphs": [],
        },
    }
    payload = build_pipeline_length_payload("L-1", "drawing.pdf", [(216, mapping)])
    page = payload["pages"][0]
    assert page["coordinates"][0]["label"] == "X"
    assert page["handwheels"][0]["id"] == "HW-001"
    assert page["handwheels"][0]["endpoint_vertices"] == ["HG-01-A", "HG-01-B"]
    assert page["final_edges"][0]["is_handwheel_segment"] is True
    assert page["final_edges"][0]["element_type"] == "valve"


def test_final_local_regressions_on_real_pages():
    from src.dimension_mapping import map_dimensions

    page_216 = map_dimensions(Path("isometries.pdf"), 216, finalize=True)
    by_text = {row.get("text"): row for row in page_216["dimensions"]}
    assert by_text["1057"]["final_interval_status"] != "resolved"
    assert not by_text["1057"].get("final_interval_id")
    assert by_text["294"]["final_interval_id"] == "F-HG-01-A-HG-01-B"
    assert by_text["294"]["final_from_vertex"] == "HG-01-A"
    assert by_text["294"]["final_to_vertex"] == "HG-01-B"

    page_218 = map_dimensions(Path("isometries.pdf"), 218, finalize=True)
    by_text_218 = {row.get("text"): row for row in page_218["dimensions"]}
    assert by_text_218["203"]["final_interval_id"] == "F-VE-07-HG-03-A"
    assert by_text_218["203"]["final_from_vertex"] == "VE-07"
    assert by_text_218["203"]["final_to_vertex"] == "HG-03-A"



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


def test_pipeline_length_provider_vertex_coordinates_are_page_scoped():
    payload = {"pages": [{"page": 216}, {"page": 217}]}
    trace = {
        "payload": {"pages": [{"page": 216}]},
        "answer": {"vertex_coordinates": [{"vertex_id": "V01", "x": 1, "y": 2, "z": 3}]},
    }
    merged = merge_pipeline_length_provider_traces(payload, [trace])
    point = merged["answer"]["vertex_coordinates"][0]
    assert point["vertex_id"] == "216:V01"
    assert point["local_vertex_id"] == "V01"
    assert point["page"] == 216


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
    mapping = map_dimensions(Path("isometries.pdf"), 216, finalize=False)
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
    mapping = map_dimensions(Path("isometries.pdf"), 216, finalize=False)
    output = tmp_path / "pipeline_length_diagnostic.pdf"
    save_pipeline_length_diagnostic_pdf(Path("isometries.pdf"), 216, output, mapping)
    assert output.exists()
    assert output.stat().st_size > 0
