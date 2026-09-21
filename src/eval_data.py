from __future__ import annotations

from typing import Any

# Evaluation truth is intentionally small and mechanical:
# group -> sheet -> candidate_id -> {value, status}.
# Fill these placeholders with real checked data when it is ready.
EVAL_GROUPS: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
    "LC_1031": {
        "2": {
            "D001": {"value": 60, "edge_id": "E001", "status": "include"},
            "D002": {"value": 916, "edge_id": "E003", "status": "exclude"},
            "D003": {"value": 1950, "edge_id": "E004", "status": "include"},
            "D004": {"value": 34, "edge_id": "E007", "status": "include"},
            "D005": {"value": 650, "edge_id": "E009", "status": "include"},
            "D006": {"value": 300, "edge_id": "E012", "status": "exclude"},
            "D007": {"value": 840, "edge_id": "E013", "status": "include"},
            "D008": {"value": 522, "edge_id": "E016", "status": "include"},
            "D009": {"value": 250, "edge_id": "E019", "status": "include"},
        }
    },
    "CO_0031": {
        "216": {
            "D001": {"value": 1057, "edge_id": "E001", "status": "exclude"},
            "D002": {"value": 294, "edge_id": "E001", "status": "exclude"},
            "D003": {"value": 203, "edge_id": "E001", "status": "include"},
            "D004": {"value": 300, "edge_id": "E003", "status": "include"},
            "D005": {"value": 191, "edge_id": "E005", "status": "include"},
            "D006": {"value": 134, "edge_id": "E005", "status": "exclude"},
            "D007": {"value": 300, "edge_id": "E006", "status": "include"},
            "D008": {"value": 800, "edge_id": "E009", "status": "exclude"},
            "D009": {"value": 1704, "edge_id": "E010", "status": "include"},
            "D010": {"value": 1194, "edge_id": "E014", "status": "include"},
            "D011": {"value": 357, "edge_id": "E015", "status": "exclude"},
            "D012": {"value": 3150, "edge_id": "E016", "status": "include"},
            "D013": {"value": 904, "edge_id": "E017", "status": "exclude"},
            "D014": {"value": 3200, "edge_id": "E018", "status": "include"},
        },
        "217": {
            "D001": {"value": 3000, "edge_id": "E015", "status": "exclude"},
            "D002": {"value": 704, "edge_id": "E001", "status": "exclude"},
            "D003": {"value": 6000, "edge_id": "E003", "status": "exclude"},
            "D004": {"value": 7157, "edge_id": "E003", "status": "include"},
            "D005": {"value": 5600, "edge_id": "E005", "status": "include"},
            "D006": {"value": 453, "edge_id": "E006", "status": "exclude"},
            "D007": {"value": 5200, "edge_id": "E007", "status": "include"},
            "D008": {"value": 748, "edge_id": "E008", "status": "exclude"},
            "D009": {"value": 4000, "edge_id": "E009", "status": "include"},
            "D010": {"value": 2748, "edge_id": "E010", "status": "exclude"},
            "D011": {"value": 4000, "edge_id": "E011", "status": "include"},
            "D012": {"value": 1104, "edge_id": "E014", "status": "include"},
        },
        "218": {
            "D001": {"value": 100, "edge_id": "E003", "status": "include"},
            "D002": {"value": 428, "edge_id": "E005", "status": "include"},
            "D003": {"value": 449, "edge_id": "E006", "status": "include"},
            "D004": {"value": 294, "edge_id": "E007", "status": "exclude"},
            "D005": {"value": 153, "edge_id": "E007", "status": "exclude"},
            "D006": {"value": 303, "edge_id": "E008", "status": "include"},
            "D007": {"value": 334, "edge_id": "E010", "status": "include"},
            "D008": {"value": 194, "edge_id": "E012", "status": "include"},
            "D009": {"value": 244, "edge_id": "E012", "status": "exclude"},
            "D010": {"value": 203, "edge_id": "E013", "status": "include"},
            "D011": {"value": 11, "edge_id": "E014", "status": "exclude"},
            "D012": {"value": 294, "edge_id": "E014", "status": "exclude"},
            "D013": {"value": 303, "edge_id": "E014", "status": "include"},
            "D014": {"value": 1046, "edge_id": "E017", "status": "include"},
            "D015": {"value": 428, "edge_id": "E018", "status": "include"},
            "D016": {"value": 294, "edge_id": "E017", "status": "exclude"},
            "D017": {"value": 3104, "edge_id": "E023", "status": "include"},
            "D018": {"value": 1150, "edge_id": "E027", "status": "include"},
            "D019": {"value": 2000, "edge_id": "E029", "status": "include"},
        }
    },
}

STATUS_FIELDS = ("status", "decision")


def list_eval_groups() -> list[str]:
    return sorted(EVAL_GROUPS.keys())


def get_eval_truth(group_id: str) -> dict[str, dict[str, dict[str, Any]]] | None:
    group = EVAL_GROUPS.get(group_id)
    if group is None:
        return None
    return group


def _sheet_key(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.upper().startswith("S") and text[1:].isdigit():
        return text[1:]
    return text


def _candidate_id(item: dict[str, Any], fallback: str | None = None) -> str:
    value = item.get("candidate_id")
    if value is None:
        value = item.get("id")
    if value is None:
        value = fallback
    return str(value).strip() if value is not None else ""


def _candidate_status(item: dict[str, Any]) -> str | None:
    for field in STATUS_FIELDS:
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _candidate_sheet(item: dict[str, Any], default_sheet: str = "") -> str:
    for field in ("sheet", "sheet_id", "sheet_number", "page", "page_number"):
        value = item.get(field)
        sheet = _sheet_key(value)
        if sheet:
            return sheet
    return default_sheet


def _normalize_truth_rows(
    truth: dict[str, dict[str, dict[str, Any]]],
    sheet: int | str | None = None,
) -> dict[str, dict[str, Any]]:
    target_sheet = _sheet_key(sheet)
    output: dict[str, dict[str, Any]] = {}
    for sheet_id, sheet_rows in truth.items():
        normalized_sheet = _sheet_key(sheet_id)
        if target_sheet and normalized_sheet != target_sheet:
            continue
        if not isinstance(sheet_rows, dict):
            continue
        for candidate_id, value in sheet_rows.items():
            if not isinstance(value, dict):
                continue
            row = dict(value)
            row["candidate_id"] = _candidate_id(row, str(candidate_id))
            row["sheet"] = normalized_sheet
            row["status"] = _candidate_status(row)
            key = f"{normalized_sheet}:{row['candidate_id']}"
            output[key] = row
    return output


def _normalize_answer_rows(model_answer: dict[str, Any], default_sheet: int | str | None = None) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    sheet = _sheet_key(default_sheet)
    for item in model_answer.get("candidate_decisions", []):
        if not isinstance(item, dict):
            continue
        candidate_id = _candidate_id(item)
        if not candidate_id:
            continue
        row = dict(item)
        row["candidate_id"] = candidate_id
        row["sheet"] = _candidate_sheet(row, sheet)
        row["status"] = _candidate_status(row)
        key = f"{row['sheet']}:{candidate_id}"
        rows[key] = row
    return rows


def evaluate_prompt_against_truth(
    group_id: str,
    prompt_name: str,
    model_answer: dict[str, Any],
    sheet: int | str | None = None,
) -> dict[str, Any]:
    truth = get_eval_truth(group_id)
    if truth is None:
        return _empty_result(group_id, prompt_name, "not_configured")

    expected_rows = _normalize_truth_rows(truth, sheet)
    if not expected_rows:
        return _empty_result(group_id, prompt_name, "empty")

    actual_rows = _normalize_answer_rows(model_answer, sheet)
    correct = 0
    mismatches: list[dict[str, Any]] = []
    for candidate_key in sorted(expected_rows):
        expected = expected_rows.get(candidate_key)
        actual = actual_rows.get(candidate_key)
        expected_status = expected.get("status") if expected else None
        actual_status = actual.get("status") if actual else None
        if expected_status == actual_status:
            correct += 1
            continue
        source = expected or actual or {}
        mismatches.append(
            {
                "candidate_id": source.get("candidate_id") or candidate_key,
                "sheet": source.get("sheet") or "",
                "expected": expected_status,
                "actual": actual_status,
                "value": source.get("value"),
            }
        )

    total = len(expected_rows)
    incorrect = len(mismatches)
    accuracy = round((correct / total) * 100.0, 2) if total else 0.0
    return {
        "group_id": group_id,
        "prompt_name": prompt_name,
        "status": "evaluated",
        "total_candidates": total,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": accuracy,
        "mismatches": mismatches,
        "missing_candidates": sorted(set(expected_rows) - set(actual_rows)),
        "extra_candidates": sorted(set(actual_rows) - set(expected_rows)),
        "expected_statuses": {candidate_id: row.get("status") for candidate_id, row in expected_rows.items()},
        "actual_statuses": {candidate_id: row.get("status") for candidate_id, row in actual_rows.items()},
    }


def _empty_result(group_id: str, prompt_name: str, status: str) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "prompt_name": prompt_name,
        "status": status,
        "total_candidates": 0,
        "correct": 0,
        "incorrect": 0,
        "accuracy": 0.0,
        "mismatches": [],
        "missing_candidates": [],
        "extra_candidates": [],
        "expected_statuses": {},
        "actual_statuses": {},
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


def evaluate_line_for_prompt(
    line_id: str,
    prompt_name: str,
    model_answer: dict[str, Any],
    sheet: int | str | None = None,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for group_id in detect_eval_group_ids(line_id):
        results[group_id] = evaluate_prompt_against_truth(group_id, prompt_name, model_answer, sheet)
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
            "status": "evaluated" if "evaluated" in stats["statuses"] else ("empty" if "empty" in stats["statuses"] else "not_configured"),
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
