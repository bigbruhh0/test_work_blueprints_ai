from src import eval_data
from src.eval_data import evaluate_line_for_prompt


def test_eval_matches_sheet_suffix_against_group_truth():
    key = "LC_1031_S216"
    eval_data.EVAL_GROUPS[key] = {
        "dimension_review": {
            "candidate_decisions": [
                {"candidate_id": "D001", "sheet": "2", "value": 60, "decision": "include", "reason": "keep on sheet 216"},
                {"candidate_id": "D002", "sheet": "2", "value": 916, "decision": "exclude", "reason": "covered on sheet 216"},
            ]
        }
    }
    answer = {
        "candidate_decisions": [
            {"candidate_id": "D001", "sheet": "2", "value": 60, "decision": "include", "reason": "keep on sheet 216"},
            {"candidate_id": "D002", "sheet": "2", "value": 916, "decision": "exclude", "reason": "covered on sheet 216"},
        ]
    }
    stats = evaluate_line_for_prompt("LC_1031_S216", "dimension_review", answer)
    assert key in stats
    assert stats[key]["status"] == "evaluated"
    assert stats[key]["incorrect"] == 0


def test_eval_distinguishes_same_candidate_id_on_different_sheets():
    key = "LC_1031_S217"
    eval_data.EVAL_GROUPS[key] = {
        "dimension_review": {
            "candidate_decisions": [
                {"candidate_id": "D001", "sheet": "217", "value": 90, "decision": "exclude", "reason": "cross-sheet reference"},
            ]
        }
    }
    answer = {
        "candidate_decisions": [
            {"candidate_id": "D001", "sheet": "216", "value": 60, "decision": "include", "reason": "valid on sheet 216"},
        ]
    }
    stats = evaluate_line_for_prompt("LC_1031_S217", "dimension_review", answer)
    assert key in stats
    assert stats[key]["incorrect"] == 2
    assert {m["candidate_id"] for m in stats[key]["mismatches"]} == {"D001"}
    assert {m["expected"] for m in stats[key]["mismatches"]} == {"exclude", None}
    assert {m["actual"] for m in stats[key]["mismatches"]} == {"include", None}


def test_eval_matches_group_truth_for_known_group():
    answer = {
        "candidate_decisions": [
            {"candidate_id": "D001", "value": 60, "decision": "include"},
            {"candidate_id": "D002", "value": 916, "decision": "exclude"},
            {"candidate_id": "D003", "value": 1950, "decision": "include"},
            {"candidate_id": "D004", "value": 34, "decision": "include"},
            {"candidate_id": "D005", "value": 650, "decision": "include"},
            {"candidate_id": "D006", "value": 300, "decision": "exclude"},
            {"candidate_id": "D007", "value": 840, "decision": "include"},
            {"candidate_id": "D008", "value": 522, "decision": "include"},
            {"candidate_id": "D009", "value": 250, "decision": "include"},
        ]
    }
    stats = evaluate_line_for_prompt("LC_1031", "dimension_review", answer)
    assert "LC_1031" in stats
    assert stats["LC_1031"]["status"] == "evaluated"
    assert stats["LC_1031"]["accuracy"] == 100.0
    assert stats["LC_1031"]["incorrect"] == 0


def test_eval_ignores_unknown_group():
    answer = {"candidate_decisions": [{"candidate_id": "D001", "decision": "include"}]}
    stats = evaluate_line_for_prompt("UNKNOWN_GROUP", "dimension_review", answer)
    assert stats == {}


def test_eval_keeps_value_field_for_reference_without_adding_extra_score_check():
    answer = {
        "candidate_decisions": [
            {"candidate_id": "D001", "value": 60, "decision": "include"},
        ]
    }
    stats = evaluate_line_for_prompt("LC_1031", "dimension_review", answer)
    assert stats["LC_1031"]["status"] == "evaluated"
    assert stats["LC_1031"]["mismatches"][0]["candidate_id"] == "D002"
    assert stats["LC_1031"]["mismatches"][0]["expected_value"] == 916
