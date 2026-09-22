import json
from pathlib import Path

import fitz
from jsonschema import Draft202012Validator

from scripts.build_dimension_map import build_map
from src.dimension_mapping import _handwheel_details, map_dimensions
from src.dimension_review import build_map_text, build_review_payload


def load_schema(name: str) -> dict:
    return json.loads((Path("schemas") / name).read_text(encoding="utf-8"))


def test_provider_payload_schema_accepts_real_dimension_payload():
    schema = load_schema("provider_payload.schema.json")
    Draft202012Validator.check_schema(schema)

    page_number = 215
    local_mapping = map_dimensions("Изометрии.pdf", page_number)
    with fitz.open("Изометрии.pdf") as document:
        local_mapping["handwheels"] = _handwheel_details(document[page_number - 1], local_mapping)
    dimension_map = build_map(Path("Изометрии.pdf"), page_number)
    dimension_map["handwheels"] = local_mapping.get("handwheels", [])
    dimension_map["connections"] = local_mapping.get("connections", [])
    payload = build_review_payload(dimension_map, build_map_text(dimension_map))

    errors = list(Draft202012Validator(schema).iter_errors(payload))

    assert errors == []
    assert "connections" in payload
    assert payload["preliminary_decisions"]
    assert payload["dimensions"][0]["preliminary_decision"]["source"] == "local_dimension_filter"


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
