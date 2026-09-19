from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

import pdfplumber

from .models import LineGroup, PageInfo


LINE_RE = re.compile(r"\b([A-ZА-Я]{1,4}\d{0,2})[_-](\d{4})\b")
EXCLUDED_PREFIXES = {"RD"}
STAMP_VALUE_Y_MIN_OFFSET = 8
STAMP_VALUE_Y_MAX_OFFSET = 42
STAMP_VALUE_X_PADDING = 30


def normalize_line_id(raw: str) -> str:
    return raw.replace("-", "_").upper()


def extract_line_ids(text: str) -> list[str]:
    line_ids: list[str] = []
    for prefix, number in LINE_RE.findall(text):
        if prefix.upper() in EXCLUDED_PREFIXES:
            continue
        line_ids.append(f"{prefix.upper()}_{number}")
    return sorted(set(line_ids))


def extract_line_ids_from_value(text: str) -> list[str]:
    return extract_line_ids(text.replace(" ", ""))


def extract_stamp_line_id(words: list[dict]) -> str | None:
    for index, word in enumerate(words):
        if word["text"].strip().lower() != "номер":
            continue
        for next_word in words[index + 1 : index + 6]:
            if next_word["text"].strip().lower() != "линии":
                continue
            if abs(float(next_word["top"]) - float(word["top"])) > 4:
                continue
            header_x0 = min(float(word["x0"]), float(next_word["x0"]))
            header_x1 = max(float(word["x1"]), float(next_word["x1"]))
            header_bottom = max(float(word["bottom"]), float(next_word["bottom"]))
            value_words = [
                candidate
                for candidate in words
                if header_bottom + STAMP_VALUE_Y_MIN_OFFSET <= float(candidate["top"]) <= header_bottom + STAMP_VALUE_Y_MAX_OFFSET
                and header_x0 - STAMP_VALUE_X_PADDING <= float(candidate["x0"])
                and float(candidate["x1"]) <= header_x1 + STAMP_VALUE_X_PADDING
            ]
            value_text = " ".join(candidate["text"] for candidate in sorted(value_words, key=lambda item: (item["top"], item["x0"])))
            line_ids = extract_line_ids_from_value(value_text)
            if line_ids:
                return line_ids[0]
    return None


def choose_primary_line_id(
    text: str,
    line_ids: list[str],
    page_number: int,
    stamp_line_id: str | None = None,
) -> str:
    if stamp_line_id:
        return stamp_line_id
    if not line_ids:
        return f"UNKNOWN_{page_number:03d}"

    counts = Counter(line_ids)
    stamp_like_ids = [
        normalize_line_id(match.group(0))
        for match in re.finditer(r"\b[A-ZА-Я]{1,4}\d{0,2}_\d{4}\b", text)
        if match.group(0).split("_", 1)[0].upper() not in EXCLUDED_PREFIXES
    ]
    if stamp_like_ids:
        return sorted(set(stamp_like_ids), key=lambda item: (-counts[item], item))[0]

    preferred_prefix_order = ("LC_", "CO_", "BB_", "CB_")
    for prefix in preferred_prefix_order:
        candidates = [line_id for line_id in line_ids if line_id.startswith(prefix)]
        if candidates:
            return sorted(candidates, key=lambda item: (-counts[item], item))[0]
    return sorted(line_ids, key=lambda item: (-counts[item], item))[0]


def read_pdf_pages(pdf_path: str | Path) -> list[PageInfo]:
    pages: list[PageInfo] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            words = page.extract_words(
                x_tolerance=1,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            line_ids = extract_line_ids(text)
            stamp_line_id = extract_stamp_line_id(words)
            if stamp_line_id and stamp_line_id not in line_ids:
                line_ids = sorted({*line_ids, stamp_line_id})
            primary_line_id = choose_primary_line_id(text, line_ids, index, stamp_line_id)
            pages.append(
                PageInfo(
                    page_number=index,
                    width=float(page.width),
                    height=float(page.height),
                    text=text,
                    line_ids=line_ids,
                    primary_line_id=primary_line_id,
                )
            )
    return pages


def group_pages_by_line(pages: list[PageInfo]) -> list[LineGroup]:
    grouped: dict[str, list[PageInfo]] = defaultdict(list)
    for page in pages:
        grouped[page.primary_line_id].append(page)

    return [
        LineGroup(line_id=line_id, pages=sorted(group_pages, key=lambda item: item.page_number))
        for line_id, group_pages in sorted(grouped.items(), key=lambda item: item[0])
    ]
