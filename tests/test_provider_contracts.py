import json
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.build_dimension_map import build_map
from src.dimension_review import build_map_text, build_review_payload


def load_schema(name: str) -> dict:
    return json.loads((Path("schemas") / name).read_text(encoding="utf-8"))


def test_provider_payload_schema_accepts_real_dimension_payload():
    schema = load_schema("provider_payload.schema.json")
    Draft202012Validator.check_schema(schema)

    page_number = 215
    dimension_map = build_map(Path("Изометрии.pdf"), page_number)
    payload = build_review_payload(dimension_map, build_map_text(dimension_map))

    errors = list(Draft202012Validator(schema).iter_errors(payload))

    assert errors == []
    assert "connections" in payload
    assert payload["preliminary_decisions"]
    assert payload["dimensions"][0]["preliminary_decision"]["source"] == "local_dimension_filter"
    assert all("parent_edge_id" not in edge for edge in payload["edges"])


def test_provider_payload_uses_final_segments_and_preserves_local_statuses():
    dimension_map = build_map(Path("Изометрии.pdf"), 218)
    payload = build_review_payload(dimension_map, build_map_text(dimension_map))

    assert payload["vertices"]
    assert any(vertex.get("source") == "handwheel" for vertex in payload["vertices"])
    assert any(edge.get("is_handwheel_segment") for edge in payload["edges"])
    assert all("parent_edge_id" not in edge for edge in payload["edges"])
    assert all(
        decision["decision"] == next(
            dimension["existing_mapping"]["local_filter_decision"]
            for dimension in payload["dimensions"]
            if dimension["id"] == decision["candidate_id"]
        )
        for decision in payload["preliminary_decisions"]
    )
    assert all(
        edge_id in {edge["id"] for edge in payload["edges"]}
        for dimension in payload["dimensions"]
        for edge_id in dimension.get("covered_edge_ids", [])
    )
    assert all(
        vertex["id"].startswith(("VE-", "HG-"))
        for vertex in payload["vertices"]
    )
    assert all(
        edge.get("from_vertex", "").startswith(("VE-", "HG-"))
        and edge.get("to_vertex", "").startswith(("VE-", "HG-"))
        for edge in payload["edges"]
    )
    by_dimension = {dimension["id"]: dimension for dimension in payload["dimensions"]}
    assert by_dimension["D015"]["existing_mapping"].get("leader_stroke") is None


def test_provider_response_schema_accepts_minimal_provider_answer():
    schema = load_schema("provider_response.schema.json")
    Draft202012Validator.check_schema(schema)
    answer = {
        "candidate_decisions": [
            {
                "candidate_id": "D001",
                "kind": "pipe_length",
                "decision": "include",
                "reason": "правило local_decision (подтверждено локальное решение)",
                "edge_id": "E001",
                "covered_edge_ids": ["E001"],
                "route_type": "main",
            }
        ],
        "edge_decisions": [],
        "main_route": {},
        "branch_routes": [],
        "cross_sheet_connections": [],
        "valve_dimensions": [],
        "cross_sheet_dimensions": [],
        "pipe_objects": [],
        "notes": [],
    }

    errors = list(Draft202012Validator(schema).iter_errors(answer))

    assert errors == []
