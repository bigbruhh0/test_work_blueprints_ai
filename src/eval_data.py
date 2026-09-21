from __future__ import annotations

from typing import Any

# Extensible evaluation catalog. Each group may contain several prompt names, and each prompt
# stores the gold decisions for candidate_ids used by the model. The structure is intentionally
# simple and can be extended later without touching the evaluator logic.
EVAL_GROUPS: dict[str, dict[str, Any]] = {
    "LC_1031": {
        "dimension_review": {
            "candidate_decisions": [
                {"candidate_id": "D001", "value": 60, "decision": "include", "kind": "pipe_length", "reason": "main pipe run: unique size on E001"},
                {"candidate_id": "D002", "value": 916, "decision": "exclude", "kind": "pipe_length", "reason": "covered by D003 on the same edge and direction"},
                {"candidate_id": "D003", "value": 1950, "decision": "include", "kind": "pipe_length", "reason": "dominant pipe run segment on E004"},
                {"candidate_id": "D004", "value": 34, "decision": "include", "kind": "pipe_length", "reason": "endpoint branch segment on E002"},
                {"candidate_id": "D005", "value": 650, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E006"},
                {"candidate_id": "D006", "value": 300, "decision": "exclude", "kind": "pipe_length", "reason": "covered by D007 on the same edge and direction"},
                {"candidate_id": "D007", "value": 840, "decision": "include", "kind": "pipe_length", "reason": "dominant segment on E005"},
                {"candidate_id": "D008", "value": 522, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E007"},
                {"candidate_id": "D009", "value": 250, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E003"},
            ]
        }
    },
    "CO_0031": {
        "dimension_review": {
            "candidate_decisions": [
                {"candidate_id": "D001", "sheet": "216", "value": 1057.0, "decision": "exclude", "kind": "pipe_length", "reason": "largest valid size on E001"},
                {"candidate_id": "D002", "sheet": "216", "value": 294.0, "decision": "exclude", "kind": "pipe_length", "reason": "covered by longer D001 on same edge"},
                {"candidate_id": "D003", "sheet": "216", "value": 203.0, "decision": "include", "kind": "pipe_length", "reason": "covered by longer D001 on same edge"},
                {"candidate_id": "D004", "sheet": "216", "value": 300.0, "decision": "include", "kind": "pipe_length", "reason": "valid branch segment on E004"},
                {"candidate_id": "D005", "sheet": "216", "value": 191.0, "decision": "include", "kind": "valve_dimension", "reason": "handwheel / valve dimension, not pipe length"},
                {"candidate_id": "D006", "sheet": "216", "value": 134.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid pipe length on E002"},
                {"candidate_id": "D007", "sheet": "216", "value": 300.0, "decision": "include", "kind": "pipe_length", "reason": "separate valid segment on E006"},
                {"candidate_id": "D008", "sheet": "216", "value": 800.0, "decision": "exclude", "kind": "pipe_length", "reason": "covered by longer D009 on same direction"},
                {"candidate_id": "D009", "sheet": "216", "value": 1704.0, "decision": "include", "kind": "pipe_length", "reason": "largest valid size on E005"},
                {"candidate_id": "D010", "sheet": "216", "value": 1194.0, "decision": "include", "kind": "pipe_length", "reason": "distinct valid segment on E003"},
                {"candidate_id": "D011", "sheet": "216", "value": 357.0, "decision": "exclude", "kind": "pipe_length", "reason": "covered by D012 on same contour"},
                {"candidate_id": "D012", "sheet": "216", "value": 3150.0, "decision": "include", "kind": "pipe_length", "reason": "dominant valid size on E003"},
                {"candidate_id": "D013", "sheet": "216", "value": 904.0, "decision": "exclude", "kind": "pipe_length", "reason": "covered by longer D014 on same contour"},
                {"candidate_id": "D014", "sheet": "216", "value": 3200.0, "decision": "include", "kind": "pipe_length", "reason": "largest valid segment on E003"},
                {"candidate_id": "D001", "sheet": "217", "value": 3000.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D002", "sheet": "217", "value": 704.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid separate segment on E002"},
                {"candidate_id": "D003", "sheet": "217", "value": 6000.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid main route segment on E002"},
                {"candidate_id": "D004", "sheet": "217", "value": 7157.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D005", "sheet": "217", "value": 5600.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D006", "sheet": "217", "value": 453.0, "decision": "exclude", "kind": "pipe_length", "reason": "covered by larger D007"},
                {"candidate_id": "D007", "sheet": "217", "value": 5200.0, "decision": "include", "kind": "pipe_length", "reason": "dominant valid size on E002"},
                {"candidate_id": "D008", "sheet": "217", "value": 748.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D009", "sheet": "217", "value": 4000.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D010", "sheet": "217", "value": 2748.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D011", "sheet": "217", "value": 4000.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D012", "sheet": "217", "value": 1104.0, "decision": "include", "kind": "pipe_length", "reason": "valid continuation on E001"},
                {"candidate_id": "D001", "sheet": "218", "value": 100.0, "decision": "include", "kind": "pipe_length", "reason": "cross-edge ambiguity, not deterministically valid"},
                {"candidate_id": "D002", "sheet": "218", "value": 428.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E002"},
                {"candidate_id": "D003", "sheet": "218", "value": 449.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E003"},
                {"candidate_id": "D004", "sheet": "218", "value": 294.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E004"},
                {"candidate_id": "D005", "sheet": "218", "value": 153.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E004"},
                {"candidate_id": "D006", "sheet": "218", "value": 303.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E004"},
                {"candidate_id": "D007", "sheet": "218", "value": 334.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E010"},
                {"candidate_id": "D008", "sheet": "218", "value": 194.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E005"},
                {"candidate_id": "D009", "sheet": "218", "value": 244.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E005"},
                {"candidate_id": "D010", "sheet": "218", "value": 203.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E006"},
                {"candidate_id": "D011", "sheet": "218", "value": 11.0, "decision": "exclude", "kind": "other", "reason": "not a pipe length"},
                {"candidate_id": "D012", "sheet": "218", "value": 294.0, "decision": "exclude", "kind": "pipe_length", "reason": "valid segment on E007"},
                {"candidate_id": "D013", "sheet": "218", "value": 303.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E007"},
                {"candidate_id": "D014", "sheet": "218", "value": 1046.0, "decision": "include", "kind": "pipe_length", "reason": "dominant valid size on E011"},
                {"candidate_id": "D015", "sheet": "218", "value": 428.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E011"},
                {"candidate_id": "D016", "sheet": "218", "value": 294.0, "decision": "exclude", "kind": "pipe_length", "reason": "covered by larger D014 on same edge"},
                {"candidate_id": "D017", "sheet": "218", "value": 3104.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E013"},
                {"candidate_id": "D018", "sheet": "218", "value": 1150.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E012"},
                {"candidate_id": "D019", "sheet": "218", "value": 2000.0, "decision": "include", "kind": "pipe_length", "reason": "valid segment on E009"},
            ]
        }
    },
}


def list_eval_groups() -> list[str]:
    return sorted(EVAL_GROUPS.keys())


def get_eval_truth(group_id: str, prompt_name: str) -> dict[str, Any] | None:
    group = EVAL_GROUPS.get(group_id)
    if not group:
        return None
    prompt_data = group.get(prompt_name)
    if not prompt_data:
        return None
    return prompt_data


def _candidate_key(item: dict[str, Any]) -> str:
    candidate_id = item.get("candidate_id")
    if candidate_id is None:
        candidate_id = item.get("id")
    if candidate_id is None:
        return ""

    sheet = item.get("sheet")
    if sheet is None:
        sheet = item.get("sheet_id")
    if sheet is None:
        sheet = item.get("sheet_number")

    candidate_key = str(candidate_id)
    if sheet is not None and str(sheet).strip():
        candidate_key = f"{candidate_key}@{sheet}"
    return candidate_key


def _normalize_truth_rows(rows: Any) -> dict[str, dict[str, Any]]:
    if isinstance(rows, dict):
        output: dict[str, dict[str, Any]] = {}
        for candidate_id, value in rows.items():
            if isinstance(value, dict):
                value = dict(value)
                value.setdefault("candidate_id", candidate_id)
                output[_candidate_key(value)] = value
        return output
    if isinstance(rows, list):
        output: dict[str, dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            candidate_key = _candidate_key(item)
            if not candidate_key:
                continue
            output[candidate_key] = dict(item)
        return output
    return {}


def evaluate_prompt_against_truth(group_id: str, prompt_name: str, model_answer: dict[str, Any]) -> dict[str, Any]:
    truth = get_eval_truth(group_id, prompt_name)
    if truth is None:
        return {
            "group_id": group_id,
            "prompt_name": prompt_name,
            "status": "not_configured",
            "total_candidates": 0,
            "correct": 0,
            "incorrect": 0,
            "accuracy": 0.0,
            "mismatches": [],
            "missing_candidates": [],
            "extra_candidates": [],
            "expected_decisions": {},
            "actual_decisions": {},
        }

    expected_rows = _normalize_truth_rows(truth.get("candidate_decisions", []))
    actual_rows = {}
    for item in model_answer.get("candidate_decisions", []):
        if not isinstance(item, dict):
            continue
        candidate_key = _candidate_key(item)
        if not candidate_key:
            continue
        actual_rows[candidate_key] = dict(item)

    all_candidate_ids = sorted(set(expected_rows) | set(actual_rows))
    correct = 0
    mismatches: list[dict[str, Any]] = []
    for candidate_key in all_candidate_ids:
        expected = expected_rows.get(candidate_key, {})
        actual = actual_rows.get(candidate_key, {})
        expected_decision = expected.get("decision")
        actual_decision = actual.get("decision")
        expected_value = expected.get("value")
        actual_value = actual.get("value")
        candidate_id = expected.get("candidate_id") or actual.get("candidate_id") or candidate_key
        if expected_decision == actual_decision:
            correct += 1
        else:
            mismatches.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_key": candidate_key,
                    "expected": expected_decision,
                    "actual": actual_decision,
                    "expected_value": expected_value,
                    "actual_value": actual_value,
                    "expected_reason": expected.get("reason"),
                    "actual_reason": actual.get("reason"),
                }
            )

    total = len(expected_rows)
    incorrect = len(mismatches)
    accuracy = round((correct / total) * 100.0, 2) if total else 0.0
    missing_candidates = sorted(set(expected_rows) - set(actual_rows))
    extra_candidates = sorted(set(actual_rows) - set(expected_rows))

    return {
        "group_id": group_id,
        "prompt_name": prompt_name,
        "status": "evaluated",
        "total_candidates": total,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": accuracy,
        "mismatches": mismatches,
        "missing_candidates": missing_candidates,
        "extra_candidates": extra_candidates,
        "expected_decisions": {candidate_id: row.get("decision") for candidate_id, row in expected_rows.items()},
        "actual_decisions": {candidate_id: row.get("decision") for candidate_id, row in actual_rows.items()},
    }


def detect_eval_group_ids(line_id: str) -> list[str]:
    matches: list[str] = []
    normalized = str(line_id or "").strip()
    if not normalized:
        return matches

    for group_id in list_eval_groups():
        if group_id == normalized or normalized.startswith(f"{group_id}_") or group_id.startswith(f"{normalized}_"):
            matches.append(group_id)

    if not matches:
        for group_id in list_eval_groups():
            if normalized.startswith(group_id) or group_id.startswith(normalized):
                matches.append(group_id)

    return sorted(set(matches), key=lambda value: str(value))


def evaluate_line_for_prompt(line_id: str, prompt_name: str, model_answer: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for group_id in detect_eval_group_ids(line_id):
        results[group_id] = evaluate_prompt_against_truth(group_id, prompt_name, model_answer)
    return results


def aggregate_eval_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = row.get("group_id")
        if not group_id:
            continue
        stats = grouped.setdefault(
            group_id,
            {
                "group_id": group_id,
                "prompt_name": row.get("prompt_name", ""),
                "total_candidates": 0,
                "correct": 0,
                "incorrect": 0,
                "mismatches": [],
                "missing_candidates": [],
                "extra_candidates": [],
                "statuses": [],
            },
        )
        stats["total_candidates"] += int(row.get("total_candidates") or 0)
        stats["correct"] += int(row.get("correct") or 0)
        stats["incorrect"] += int(row.get("incorrect") or 0)
        stats["mismatches"].extend(row.get("mismatches") or [])
        stats["missing_candidates"].extend(row.get("missing_candidates") or [])
        stats["extra_candidates"].extend(row.get("extra_candidates") or [])
        stats["statuses"].append(row.get("status", "not_configured"))

    summary: dict[str, Any] = {}
    for group_id, stats in grouped.items():
        total = max(1, stats["total_candidates"])
        summary[group_id] = {
            "group_id": group_id,
            "prompt_name": stats["prompt_name"],
            "total_candidates": stats["total_candidates"],
            "correct": stats["correct"],
            "incorrect": stats["incorrect"],
            "accuracy": round((stats["correct"] / total) * 100.0, 2),
            "mismatches": stats["mismatches"],
            "missing_candidates": sorted(set(stats["missing_candidates"])),
            "extra_candidates": sorted(set(stats["extra_candidates"])),
            "status": "evaluated" if "evaluated" in stats["statuses"] else ("not_configured" if stats["statuses"] else "not_configured"),
        }
    return summary


def summarize_prompt_eval_rows(rows: list[dict[str, Any]], prompt_name: str | None = None) -> dict[str, Any]:
    grouped = aggregate_eval_results(rows)
    if not grouped:
        return {
            "prompt_name": prompt_name or "",
            "total_candidates": 0,
            "correct": 0,
            "incorrect": 0,
            "accuracy": 0.0,
            "groups": [],
            "status": "not_configured",
        }

    total_candidates = sum(int(stats.get("total_candidates") or 0) for stats in grouped.values())
    correct = sum(int(stats.get("correct") or 0) for stats in grouped.values())
    incorrect = sum(int(stats.get("incorrect") or 0) for stats in grouped.values())
    accuracy = round((correct / max(total_candidates, 1)) * 100.0, 2) if total_candidates else 0.0
    return {
        "prompt_name": prompt_name or next(iter(grouped.values())).get("prompt_name", ""),
        "total_candidates": total_candidates,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": accuracy,
        "groups": [
            {
                "group_id": group_id,
                "accuracy": stats.get("accuracy", 0.0),
                "correct": stats.get("correct", 0),
                "incorrect": stats.get("incorrect", 0),
                "total_candidates": stats.get("total_candidates", 0),
                "status": stats.get("status", "not_configured"),
            }
            for group_id, stats in sorted(grouped.items())
        ],
        "status": "evaluated" if any(stats.get("status") == "evaluated" for stats in grouped.values()) else "not_configured",
    }
