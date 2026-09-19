from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LineRunState:
    line_id: str
    pages: list[int] = field(default_factory=list)
    stage: str = "vertices"
    status: str = "pending"
    vertices: list[dict] = field(default_factory=list)
    numbers: list[dict] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    document_id: str
    source_name: str
    line_ids: list[str]
    status: str = "running"
    lines: dict[str, LineRunState] = field(default_factory=dict)
