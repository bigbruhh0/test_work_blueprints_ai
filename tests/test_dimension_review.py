from src.dimension_review import calculate_lengths


def test_non_pipe_kinds_are_excluded_from_pipe_totals():
    mapping = {
        "dimensions": [
            {"id": "D001", "value_mm": 100.0},
            {"id": "D002", "value_mm": 50.0},
            {"id": "D003", "value_mm": 25.0},
            {"id": "D004", "value_mm": 200.0},
        ]
    }
    review = {
        "candidate_decisions": [
            {"candidate_id": "D001", "decision": "include", "kind": "pipe_length", "edge_id": "E1", "covered_edge_ids": ["E1"]},
            {"candidate_id": "D002", "decision": "include", "kind": "valve_dimension", "edge_id": "E1", "covered_edge_ids": ["E1"]},
            {"candidate_id": "D003", "decision": "ambiguous", "kind": "cross_sheet_dimension", "edge_id": "E2", "covered_edge_ids": ["E2"]},
            {"candidate_id": "D004", "decision": "include", "kind": "pipe_length", "edge_id": "E3", "covered_edge_ids": ["E3"]},
        ],
        "edge_decisions": [],
        "main_route": {},
        "branch_routes": [],
        "cross_sheet_connections": [],
        "valve_dimensions": [],
        "cross_sheet_dimensions": [],
    }

    result = calculate_lengths(mapping, review)

    assert result["clean_length_mm"] == 300.0
    assert result["dirty_length_mm"] == 300.0
    assert result["ambiguous_length_mm"] == 0.0
    assert result["counted_candidate_ids"] == ["D004", "D001"]


def test_deterministically_invalid_candidates_are_flagged():
    mapping = {
        "dimensions": [
            {"id": "D001", "value_mm": 120.0},
            {"id": "D002", "value_mm": 80.0},
            {"id": "D003", "value_mm": 90.0},
        ]
    }
    review = {
        "candidate_decisions": [
            {"candidate_id": "D001", "decision": "exclude", "kind": "valve_dimension", "edge_id": "E1", "covered_edge_ids": ["E1"], "reason": "handwheel on same axis"},
            {"candidate_id": "D002", "decision": "exclude", "kind": "cross_sheet_dimension", "edge_id": "E2", "covered_edge_ids": ["E2"], "reason": "cross_sheet_reference: see sheet 217"},
            {"candidate_id": "D003", "decision": "include", "kind": "pipe_length", "edge_id": "E3", "covered_edge_ids": ["E3"], "reason": "valid pipe length"},
        ],
        "edge_decisions": [],
        "main_route": {},
        "branch_routes": [],
        "cross_sheet_connections": [],
        "valve_dimensions": [],
        "cross_sheet_dimensions": [],
    }

    result = calculate_lengths(mapping, review)

    assert result["deterministically_invalid_candidate_ids"] == ["D001", "D002"]
    assert result["prevalidated_invalid_candidate_ids"] == ["D001", "D002"]
