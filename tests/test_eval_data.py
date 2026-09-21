from src import eval_data
from src.eval_data import evaluate_line_for_prompt


def test_eval_matches_status_by_group_sheet_and_candidate(monkeypatch):
    monkeypatch.setitem(
        eval_data.EVAL_GROUPS,
        "LC_1031",
        {
            "216": {
                "D001": {"value": 60, "status": "include"},
                "D002": {"value": 916, "status": "exclude"},
            }
        },
    )
    answer = {
        "candidate_decisions": [
            {"candidate_id": "D001", "value": 999, "decision": "include"},
            {"candidate_id": "D002", "value": 1, "decision": "exclude"},
        ]
    }

    stats = evaluate_line_for_prompt("LC_1031", "dimension_review", answer, sheet=216)

    assert stats["LC_1031"]["status"] == "evaluated"
    assert stats["LC_1031"]["accuracy"] == 100.0
    assert stats["LC_1031"]["incorrect"] == 0


def test_eval_distinguishes_same_candidate_id_on_different_sheets(monkeypatch):
    monkeypatch.setitem(
        eval_data.EVAL_GROUPS,
        "CO_0031",
        {
            "216": {"D001": {"value": 1057, "status": "include"}},
            "217": {"D001": {"value": 3000, "status": "exclude"}},
        },
    )

    page_216 = evaluate_line_for_prompt(
        "CO_0031",
        "dimension_review",
        {"candidate_decisions": [{"candidate_id": "D001", "decision": "include"}]},
        sheet=216,
    )
    page_217 = evaluate_line_for_prompt(
        "CO_0031",
        "dimension_review",
        {"candidate_decisions": [{"candidate_id": "D001", "decision": "include"}]},
        sheet=217,
    )

    assert page_216["CO_0031"]["incorrect"] == 0
    assert page_217["CO_0031"]["incorrect"] == 1
    assert page_217["CO_0031"]["mismatches"] == [
        {"candidate_id": "D001", "sheet": "217", "expected": "exclude", "actual": "include", "value": 3000}
    ]


def test_eval_marks_missing_and_extra_candidates(monkeypatch):
    monkeypatch.setitem(
        eval_data.EVAL_GROUPS,
        "LC_1031",
        {"216": {"D001": {"value": 60, "status": "include"}}},
    )

    stats = evaluate_line_for_prompt(
        "LC_1031",
        "dimension_review",
        {"candidate_decisions": [{"candidate_id": "D999", "decision": "exclude"}]},
        sheet=216,
    )

    assert stats["LC_1031"]["incorrect"] == 1
    assert stats["LC_1031"]["missing_candidates"] == ["216:D001"]
    assert stats["LC_1031"]["extra_candidates"] == ["216:D999"]


def test_eval_ignores_unknown_group():
    answer = {"candidate_decisions": [{"candidate_id": "D001", "decision": "include"}]}
    stats = evaluate_line_for_prompt("UNKNOWN_GROUP", "dimension_review", answer, sheet=216)
    assert stats == {}


def test_eval_known_lc_1031_truth_is_registered():
    answer = {
        "candidate_decisions": [
            {"candidate_id": "D001", "decision": "include"},
            {"candidate_id": "D002", "decision": "exclude"},
            {"candidate_id": "D003", "decision": "include"},
            {"candidate_id": "D004", "decision": "include"},
            {"candidate_id": "D005", "decision": "include"},
            {"candidate_id": "D006", "decision": "exclude"},
            {"candidate_id": "D007", "decision": "include"},
            {"candidate_id": "D008", "decision": "include"},
            {"candidate_id": "D009", "decision": "include"},
        ]
    }

    lc_stats = evaluate_line_for_prompt("LC_1031", "dimension_review", answer, sheet=2)

    assert lc_stats["LC_1031"]["status"] == "evaluated"
    assert lc_stats["LC_1031"]["accuracy"] == 100.0


def test_eval_known_co_0031_sheets_are_registered():
    for sheet, truth_rows in eval_data.EVAL_GROUPS["CO_0031"].items():
        answer = {
            "candidate_decisions": [
                {"candidate_id": candidate_id, "decision": row["status"]}
                for candidate_id, row in truth_rows.items()
            ]
        }

        stats = evaluate_line_for_prompt("CO_0031", "dimension_review", answer, sheet=sheet)

        assert stats["CO_0031"]["status"] == "evaluated"
        assert stats["CO_0031"]["accuracy"] == 100.0
        assert stats["CO_0031"]["incorrect"] == 0
