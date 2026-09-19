from __future__ import annotations

import re
from pathlib import Path
from dataclasses import dataclass

import pdfplumber

from .models import Candidate, LineGroup


DIMENSION_RE = re.compile(r"^\d{2,5}$")
COORD_VALUE_RE = re.compile(r"^\d{5}$")
DN_RE = re.compile(r"^DN\d+(X\d+)?$", re.IGNORECASE)
LINE_REF_RE = re.compile(r"\b[A-ZА-Я]{2}[_-]\d{4}\b")
COORD_LABELS = {"X", "Y", "Z", "Z+"}


def extract_candidates_for_groups(pdf_path: str | Path, groups: list[LineGroup]) -> list[Candidate]:
    page_to_lines: dict[int, set[str]] = {}
    for group in groups:
        for page in group.pages:
            page_to_lines.setdefault(page.page_number, set()).add(group.line_id)

    candidates: list[Candidate] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, line_ids in sorted(page_to_lines.items()):
            page = pdf.pages[page_number - 1]
            words = page.extract_words(
                x_tolerance=1,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            for word in words:
                text = word["text"].strip()
                if not text:
                    continue
                kind = classify_candidate(text)
                if kind is None:
                    continue
                bbox = (
                    float(word["x0"]),
                    float(word["top"]),
                    float(word["x1"]),
                    float(word["bottom"]),
                )
                zone = classify_zone(bbox, float(page.width), float(page.height))
                for line_id in line_ids:
                    candidates.append(
                        Candidate(
                            id=f"C-{page_number:03d}-{len(candidates) + 1:04d}",
                            line_id=line_id,
                            page=page_number,
                            kind=kind,
                            text=text,
                            zone=zone,
                            bbox=bbox,
                            confidence=1.0,
                        )
                    )
    candidates.extend(_derive_coordinate_delta_candidates(candidates))
    return candidates


@dataclass(frozen=True)
class CoordinateBlock:
    line_id: str
    page: int
    values: dict[str, int]
    bbox: tuple[float, float, float, float]
    source_ids: tuple[str, ...]


def classify_candidate(text: str):
    normalized = text.upper().replace("Х", "X")
    if normalized in COORD_LABELS:
        return "coordinate_label"
    if DN_RE.match(normalized):
        return "dn"
    if LINE_REF_RE.search(normalized):
        return "line_ref"
    if COORD_VALUE_RE.match(normalized):
        return "coordinate_value"
    if DIMENSION_RE.match(normalized):
        return "dimension"
    return None


def classify_zone(bbox: tuple[float, float, float, float], page_width: float, page_height: float) -> str:
    x0, y0, x1, y1 = bbox
    center_x = (x0 + x1) / 2
    center_y = (y0 + y1) / 2

    if center_y >= page_height * 0.86:
        return "title_block"
    if center_x >= page_width * 0.68 and center_y <= page_height * 0.72:
        return "materials_spec"
    if center_x >= page_width * 0.68 and center_y <= page_height * 0.86:
        return "length_table"
    return "drawing"


def _derive_coordinate_delta_candidates(candidates: list[Candidate]) -> list[Candidate]:
    derived: list[Candidate] = []
    by_line_page: dict[tuple[str, int], list[Candidate]] = {}
    for candidate in candidates:
        if candidate.zone == "drawing" and candidate.kind in {"coordinate_label", "coordinate_value"}:
            by_line_page.setdefault((candidate.line_id, candidate.page), []).append(candidate)

    next_index_by_page: dict[int, int] = {}
    for candidate in candidates:
        next_index_by_page[candidate.page] = max(
            next_index_by_page.get(candidate.page, 0),
            int(candidate.id.rsplit("-", 1)[-1]),
        )

    for (line_id, page), page_candidates in by_line_page.items():
        blocks = _coordinate_blocks(line_id, page, page_candidates)
        drawing_dimension_values = {
            candidate.text
            for candidate in candidates
            if candidate.line_id == line_id
            and candidate.page == page
            and candidate.kind == "dimension"
            and candidate.zone == "drawing"
        }
        emitted_axis_deltas: set[tuple[str, str]] = set()
        for left_index, left in enumerate(blocks):
            for right in blocks[left_index + 1:]:
                changed_axes = [
                    axis for axis in ("X", "Y", "Z")
                    if axis in left.values
                    and axis in right.values
                    and left.values[axis] != right.values[axis]
                ]
                shared_axes = [
                    axis for axis in ("X", "Y", "Z")
                    if axis in left.values
                    and axis in right.values
                    and left.values[axis] == right.values[axis]
                ]
                for axis in changed_axes:
                    delta = abs(left.values[axis] - right.values[axis])
                    delta_text = str(delta)
                    if delta <= 0 or delta_text not in drawing_dimension_values or not shared_axes:
                        continue
                    delta_key = (axis, delta_text)
                    if delta_key in emitted_axis_deltas:
                        continue
                    emitted_axis_deltas.add(delta_key)
                    next_index_by_page[page] = next_index_by_page.get(page, 0) + 1
                    derived.append(
                        Candidate(
                            id=f"C-{page:03d}-{next_index_by_page[page]:04d}",
                            line_id=line_id,
                            page=page,
                            kind="coordinate_delta",
                            text=delta_text,
                            zone="drawing",
                            bbox=_union_bbox(left.bbox, right.bbox),
                            confidence=0.9,
                        )
                    )
                if len(changed_axes) != 1 or len(shared_axes) < 2:
                    continue
                axis = changed_axes[0]
                delta = abs(left.values[axis] - right.values[axis])
                if delta <= 0:
                    continue
                next_index_by_page[page] = next_index_by_page.get(page, 0) + 1
                derived.append(
                    Candidate(
                        id=f"C-{page:03d}-{next_index_by_page[page]:04d}",
                        line_id=line_id,
                        page=page,
                        kind="coordinate_delta",
                        text=str(delta),
                        zone="drawing",
                        bbox=_union_bbox(left.bbox, right.bbox),
                        confidence=0.9,
                    )
                )

        z_values = sorted({block.values["Z"] for block in blocks if "Z" in block.values})
        if len(z_values) == 2:
            delta = abs(z_values[1] - z_values[0])
            z_blocks = [block for block in blocks if block.values.get("Z") in set(z_values)]
            if delta > 0 and len(z_blocks) >= 2:
                next_index_by_page[page] = next_index_by_page.get(page, 0) + 1
                derived.append(
                    Candidate(
                        id=f"C-{page:03d}-{next_index_by_page[page]:04d}",
                        line_id=line_id,
                        page=page,
                        kind="coordinate_delta",
                        text=str(delta),
                        zone="drawing",
                        bbox=_union_many([block.bbox for block in z_blocks]),
                        confidence=0.85,
                    )
                )
    return _dedupe_derived(candidates, derived)


def _coordinate_blocks(line_id: str, page: int, candidates: list[Candidate]) -> list[CoordinateBlock]:
    labels = [candidate for candidate in candidates if candidate.kind == "coordinate_label"]
    values = [candidate for candidate in candidates if candidate.kind == "coordinate_value"]
    rows: list[tuple[str, int, Candidate, Candidate]] = []
    for label in labels:
        axis = label.text.upper().replace("Z+", "Z").replace("Х", "X")
        if axis not in {"X", "Y", "Z"}:
            continue
        label_cy = _center(label.bbox)[1]
        matches = [
            value for value in values
            if abs(_center(value.bbox)[1] - label_cy) <= 2.0
            and value.bbox[0] >= label.bbox[2]
            and value.bbox[0] - label.bbox[2] <= 8.0
        ]
        if not matches:
            continue
        value = min(matches, key=lambda item: item.bbox[0] - label.bbox[2])
        rows.append((axis, int(value.text), label, value))

    rows.sort(key=lambda item: (_center(item[2].bbox)[0], _center(item[2].bbox)[1]))
    blocks: list[CoordinateBlock] = []
    used: set[int] = set()
    for index, row in enumerate(rows):
        if index in used:
            continue
        group = [row]
        used.add(index)
        base_x = row[2].bbox[0]
        last_y = _center(row[2].bbox)[1]
        for next_index in range(index + 1, len(rows)):
            if next_index in used:
                continue
            next_row = rows[next_index]
            next_y = _center(next_row[2].bbox)[1]
            if abs(next_row[2].bbox[0] - base_x) <= 3.0 and 0 < next_y - last_y <= 12.0:
                group.append(next_row)
                used.add(next_index)
                last_y = next_y
            if len(group) == 3:
                break
        values_by_axis = {axis: value for axis, value, _label, _candidate in group}
        if len(values_by_axis) < 2:
            continue
        bboxes = [label.bbox for _axis, _value, label, _candidate in group]
        bboxes.extend(value_candidate.bbox for _axis, _value, _label, value_candidate in group)
        source_ids = tuple(item.id for _axis, _value, _label, item in group)
        blocks.append(
            CoordinateBlock(
                line_id=line_id,
                page=page,
                values=values_by_axis,
                bbox=_union_many(bboxes),
                source_ids=source_ids,
            )
        )
    return blocks


def _dedupe_derived(existing: list[Candidate], derived: list[Candidate]) -> list[Candidate]:
    seen = {
        (candidate.line_id, candidate.page, candidate.kind, candidate.text, tuple(round(value, 1) for value in candidate.bbox))
        for candidate in existing
    }
    result: list[Candidate] = []
    for candidate in derived:
        key = (candidate.line_id, candidate.page, candidate.kind, candidate.text, tuple(round(value, 1) for value in candidate.bbox))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 + x1) / 2, (y0 + y1) / 2


def _union_bbox(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def _union_many(bboxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def summarize_candidates(candidates: list[Candidate]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for candidate in candidates:
        key = f"{candidate.kind}:{candidate.zone}"
        summary[key] = summary.get(key, 0) + 1
    return summary
