from __future__ import annotations

import hashlib
import pickle
from pathlib import Path

from .models import LineGroup, PageInfo
from .pdf_ingest import group_pages_by_line, read_pdf_pages


PDF_CACHE_DIR = Path(".cache") / "pdf_groups"
PDF_CACHE_VERSION = "v5-p21-line-prefix"


def prepare_pdf_groups(pdf_path: str | Path, use_disk_cache: bool = True) -> tuple[list[PageInfo], list[LineGroup]]:
    path = Path(pdf_path)
    if use_disk_cache:
        cached = _read_pdf_cache(path)
        if cached is not None:
            return cached

    pages = read_pdf_pages(path)
    groups = group_pages_by_line(pages)
    if use_disk_cache:
        _write_pdf_cache(path, pages, groups)
    return pages, groups


def get_pdf_cache_status(pdf_path: str | Path) -> str:
    return "hit" if _cache_path_for(Path(pdf_path)).exists() else "miss"


def _cache_key_for(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    raw = f"{PDF_CACHE_VERSION}|{resolved}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cache_path_for(path: Path) -> Path:
    return PDF_CACHE_DIR / f"{_cache_key_for(path)}.pkl"


def _read_pdf_cache(path: Path) -> tuple[list[PageInfo], list[LineGroup]] | None:
    cache_path = _cache_path_for(path)
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("rb") as file:
            cached = pickle.load(file)
        return cached["pages"], cached["groups"]
    except Exception:
        return None


def _write_pdf_cache(path: Path, pages: list[PageInfo], groups: list[LineGroup]) -> None:
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path_for(path)
    with cache_path.open("wb") as file:
        pickle.dump({"pages": pages, "groups": groups}, file)
