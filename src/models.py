from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Status = Literal["complete", "partial", "needs_review", "failed"]
CoordinateSource = Literal["drawing", "calculated", "unknown"]
RouteType = Literal["main", "branch", "unknown"]
ElementType = Literal["support", "valve", "unknown"]
CandidateKind = Literal["dimension", "coordinate_label", "coordinate_value", "coordinate_delta", "dn", "line_ref", "numeric", "text"]
CandidateClass = Literal["route_segment", "support_offset", "adjacent_line", "coordinate", "dn", "excluded_table", "unknown"]


@dataclass(slots=True)
class SourceRef:
    page: int
    label: str
    bbox: tuple[float, float, float, float] | None = None


@dataclass(slots=True)
class Annotation:
    id: str
    page: int
    label: str
    kind: str
    bbox: tuple[float, float, float, float] | None
    color: str = "#2563eb"


@dataclass(slots=True)
class VertexMark:
    id: str
    line_id: str
    page: int
    label: str
    role: str
    x: float
    y: float
    confidence: float = 0.5
    reason: str = ""


@dataclass(slots=True)
class ProviderTrace:
    id: str
    line_id: str
    stage: str
    model: str
    prompt: str
    response: str = ""
    status: str = "ok"
    image_count: int = 0
    error: str = ""


@dataclass(slots=True)
class Candidate:
    id: str
    line_id: str
    page: int
    kind: CandidateKind
    text: str
    zone: str
    bbox: tuple[float, float, float, float]
    confidence: float = 1.0


@dataclass(slots=True)
class CandidateClassification:
    id: str
    line_id: str
    page: int
    candidate_id: str
    text: str
    classification: CandidateClass
    route_type: RouteType = "unknown"
    reason: str = ""
    confidence: float = 0.5
    accepted: bool = False
    gate_reason: str = ""


@dataclass(slots=True)
class LineResult:
    id: str
    pages: list[int]
    status: Status
    main_length_mm: float | None = None
    branches_length_mm: float | None = None
    total_length_mm: float | None = None
    supports_count: int | None = None
    valves_count: int | None = None
    completeness_note: str = ""
    route_reconstruction: dict[str, Any] | None = None


@dataclass(slots=True)
class PointResult:
    id: str
    line_id: str
    role: str
    x: float | None
    y: float | None
    z: float | None
    source: CoordinateSource
    source_ref: SourceRef | None = None


@dataclass(slots=True)
class SegmentResult:
    id: str
    line_id: str
    start_point_id: str
    end_point_id: str
    dn: str
    length_mm: float | None
    route_type: RouteType
    source_size: str
    source_ref: SourceRef | None = None


@dataclass(slots=True)
class ElementResult:
    id: str
    element_type: ElementType
    line_id: str
    bound_to: str
    x: float | None = None
    y: float | None = None
    z: float | None = None
    status: Status = "partial"
    source_ref: SourceRef | None = None


@dataclass(slots=True)
class GraphNode:
    id: str
    line_id: str
    page: int
    role: str
    bbox: tuple[float, float, float, float] | None = None
    x: float | None = None
    y: float | None = None
    z: float | None = None
    source_candidate_ids: list[str] = field(default_factory=list)
    confidence: float = 0.5
    reason: str = ""


@dataclass(slots=True)
class GraphEdge:
    id: str
    line_id: str
    page: int
    from_node_id: str
    to_node_id: str
    path_type: RouteType = "unknown"
    nominal_size: str | None = None
    length_mm: float | None = None
    length_source: str = ""
    source_candidate_ids: list[str] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None
    confidence: float = 0.5
    reason: str = ""


@dataclass(slots=True)
class DimensionBinding:
    candidate_id: str
    edge_id: str
    binding_type: str
    reason: str = ""


@dataclass(slots=True)
class UnresolvedEdge:
    edge_id: str
    reason: str
    suggested_action: str


@dataclass(slots=True)
class GraphData:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    dimension_bindings: list[DimensionBinding] = field(default_factory=list)
    unresolved_edges: list[UnresolvedEdge] = field(default_factory=list)
    ignored_candidates: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.nodes and not self.edges


@dataclass(slots=True)
class Uncertainty:
    id: str
    line_id: str
    page: int | None
    target: str
    reason: str
    severity: Literal["info", "warning", "critical"] = "warning"


@dataclass(slots=True)
class AnalysisResult:
    lines: list[LineResult] = field(default_factory=list)
    points: list[PointResult] = field(default_factory=list)
    segments: list[SegmentResult] = field(default_factory=list)
    elements: list[ElementResult] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    vertices: list[VertexMark] = field(default_factory=list)
    provider_traces: list[ProviderTrace] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    candidate_classifications: list[CandidateClassification] = field(default_factory=list)
    graph_nodes: list[GraphNode] = field(default_factory=list)
    graph_edges: list[GraphEdge] = field(default_factory=list)
    dimension_bindings: list[DimensionBinding] = field(default_factory=list)
    unresolved_edges: list[UnresolvedEdge] = field(default_factory=list)
    ignored_candidates: list[dict[str, Any]] = field(default_factory=list)

    def extend(self, other: "AnalysisResult") -> None:
        self.lines.extend(other.lines)
        self.points.extend(other.points)
        self.segments.extend(other.segments)
        self.elements.extend(other.elements)
        self.uncertainties.extend(other.uncertainties)
        self.annotations.extend(other.annotations)
        self.vertices.extend(other.vertices)
        self.provider_traces.extend(other.provider_traces)
        self.candidates.extend(other.candidates)
        self.candidate_classifications.extend(other.candidate_classifications)
        self.graph_nodes.extend(other.graph_nodes)
        self.graph_edges.extend(other.graph_edges)
        self.dimension_bindings.extend(other.dimension_bindings)
        self.unresolved_edges.extend(other.unresolved_edges)
        self.ignored_candidates.extend(other.ignored_candidates)


@dataclass(slots=True)
class PageInfo:
    page_number: int
    width: float
    height: float
    text: str
    line_ids: list[str]
    primary_line_id: str


@dataclass(slots=True)
class LineGroup:
    line_id: str
    pages: list[PageInfo]


@dataclass(slots=True)
class ProjectResult:
    source_name: str
    created_at: str
    pages_count: int
    line_groups_count: int
    model_mode: str
    result: AnalysisResult

    @classmethod
    def create(
        cls,
        source_name: str,
        pages_count: int,
        line_groups_count: int,
        model_mode: str,
        result: AnalysisResult,
    ) -> "ProjectResult":
        return cls(
            source_name=source_name,
            created_at=datetime.now(timezone.utc).isoformat(),
            pages_count=pages_count,
            line_groups_count=line_groups_count,
            model_mode=model_mode,
            result=result,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
