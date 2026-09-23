# Provider Review Contract

This document describes the data exchanged with the dimension-review provider.

Machine-readable schemas:

- `schemas/provider_payload.schema.json` - request payload sent to the provider.
- `schemas/provider_response.schema.json` - JSON response expected from the provider.

## Request Payload

Top-level shape:

```json
{
  "page": 215,
  "vertices": [],
  "uncertain_vertices": [],
  "edges": [],
  "dimensions": [],
  "preliminary_decisions": [],
  "edge_candidate_groups": [],
  "handwheels": [],
  "valve_edges": [],
  "connections": [],
  "map_text": "..."
}
```

### dimensions[]

Each dimension candidate is a number found in the drawing and mapped to pipe geometry.

```json
{
  "id": "D011",
  "page": 215,
  "value_mm": 250.0,
  "text": "250",
  "bbox": [x0, y0, x1, y1],
  "label_center": [x, y],
  "edge_candidates": [
    {
      "edge_id": "E002",
      "distance_px": 12.3,
      "position": 0.42,
      "parallel_score": 0.91,
      "source": "dimension_mapping"
    }
  ],
  "selected_edge_id": "E002",
  "status": "mapped",
  "source": "drawing_dimension",
  "mapping_method": "dimension_mapping",
  "preliminary_decision": {
    "decision": "exclude",
    "reason": "shares_excluded_dimension_stroke",
    "conflict_with": "D010",
    "status": "projected",
    "valid": true,
    "source": "local_dimension_filter"
  },
  "existing_mapping": {
    "attachment_kind": "leader_to_dimension_arrow",
    "attachment_source": "vector_attachment",
    "dimension_stroke": {
      "index": 72,
      "start": [x, y],
      "end": [x, y],
      "length_px": 36.0,
      "merged_indices": [72]
    },
    "leader_stroke": null,
    "extension_strokes": [],
    "pipe_anchor": {
      "edge_id": "E012",
      "position": 0.3,
      "point": [x, y],
      "gap_px": 10.0
    },
    "local_filter_decision": "exclude",
    "local_filter_reason": "shares_excluded_dimension_stroke",
    "local_filter_conflict_with": "D010"
  }
}
```

`preliminary_decision` is a local algorithm hypothesis, not final truth. The provider should verify it. If the prompt asks for conservative review, a contradicted preliminary decision may be returned as `ambiguous` for manual inspection.

### preliminary_decisions[]

Compact duplicate of the local hypothesis for each candidate:

```json
{
  "candidate_id": "D011",
  "value_mm": 250.0,
  "edge_id": "E002",
  "decision": "exclude",
  "reason": "shares_excluded_dimension_stroke",
  "conflict_with": "D010",
  "status": "projected",
  "valid": true,
  "source": "local_dimension_filter"
}
```

### edges[]

```json
{
  "id": "E002",
  "from_vertex": "V01",
  "to_vertex": "V02",
  "start": [x, y],
  "end": [x, y],
  "path_points": [[x, y], [x, y]],
  "pixel_length": 123.45,
  "status": "mapped",
  "stroke_ids": [12, 13]
}
```

### vertices[]

```json
{
  "id": "V01",
  "role": "endpoint",
  "x": 100.0,
  "y": 200.0,
  "degree": 1,
  "confidence": 1.0,
  "source": "pipe_component"
}
```

### edge_candidate_groups[]

```json
{
  "from_vertex": "V01",
  "to_vertex": "V02",
  "edge_ids": ["E002"],
  "candidate_ids": ["D001", "D002"],
  "candidate_values_mm": [3000.0, 5000.0],
  "status": "mapped"
}
```

### handwheels[]

May be empty. When present:

```json
{
  "id": "HW-001",
  "label": "ШТУРВАЛ EAST",
  "bbox": [x0, y0, x1, y1],
  "arrow_found": true,
  "arrow_start": [x, y],
  "arrow_end": [x, y],
  "arrow_segments": [{ "start": [x, y], "end": [x, y] }],
  "edge_id": "E012",
  "edge_created": false,
  "edge_kind": "pipe_edge",
  "is_pipe_edge": true,
  "edge_distance_px": 12.0
}
```

### connections[]

Connection/continuation/tie-in labels detected on the drawing.

```json
{
  "id": "CN-C1",
  "page": 215,
  "label": "СМ. ЛИСТ 2",
  "bbox": [x0, y0, x1, y1],
  "center": [x, y],
  "connection_type": "continuation",
  "target_sheet": "2",
  "text_has_sheet_ref": true,
  "vertex_id": "V01",
  "target_point": [x, y],
  "arrow_found": true,
  "arrow_start": [x, y],
  "arrow_end": [x, y],
  "arrow_segments": [{ "start": [x, y], "end": [x, y] }]
}
```

### valve_edges[]

Final edge segments that belong to a valve or handwheel. These are already
normalized edges after VE/HG vertex insertion; the payload does not expose
the old parent edge IDs.

```json
{
  "edge_id": "HW-001-SEG",
  "element_type": "valve",
  "is_handwheel_segment": true,
  "handwheel_ids": ["HW-001"],
  "from_vertex": "HG-01-A",
  "to_vertex": "HG-01-B"
}
```

## Provider Response

Top-level shape:

```json
{
  "candidate_decisions": [],
  "edge_decisions": [],
  "main_route": {},
  "branch_routes": [],
  "cross_sheet_connections": [],
  "valve_dimensions": [],
  "cross_sheet_dimensions": [],
  "pipe_objects": [],
  "notes": []
}
```

Only `candidate_decisions` is required by the runtime schema. The other arrays improve length calculation, eval, diagnostics, and future 3D modeling.

### candidate_decisions[]

```json
{
  "candidate_id": "D011",
  "kind": "pipe_length",
  "decision": "include",
  "reason": "правило local_decision (подтверждено локальное решение): ...",
  "edge_id": "E002",
  "covered_edge_ids": ["E002"],
  "route_type": "main"
}
```

Allowed values:

- `kind`: `pipe_length`, `valve_dimension`, `cross_sheet_dimension`, `other`.
- `decision`: `include`, `exclude`, `ambiguous`.
- `route_type`: `main`, `branch`, or `null`.

### edge_decisions[]

```json
{
  "edge_id": "E002",
  "vertex_pair": ["V01", "V02"],
  "route_type": "main",
  "junction_vertex_id": null,
  "candidate_ids": ["D011"],
  "included_candidate_ids": ["D011"],
  "excluded_candidate_ids": [],
  "ambiguous_candidate_ids": [],
  "covered_by_candidate_ids": [],
  "reason": "..."
}
```

### main_route

```json
{
  "endpoint_vertices": ["V01", "V09"],
  "edge_ids": ["E001", "E002"],
  "reason": "..."
}
```

### branch_routes[]

```json
{
  "junction_vertex_id": "V04",
  "endpoint_vertex_id": "V08",
  "edge_ids": ["E010"],
  "candidate_ids": ["D020"],
  "reason": "..."
}
```

### valve_dimensions[]

Dimensions excluded because they belong to a valve, handwheel, fitting, support, or similar object.

```json
{
  "candidate_id": "D015",
  "object_type": "handwheel",
  "value_mm": 300.0,
  "from_vertex": "V01",
  "to_vertex": "V02",
  "edge_id": "E012",
  "handwheel_id": "HW-001",
  "arrow_found": true,
  "reason": "..."
}
```

### cross_sheet_dimensions[]

Dimensions excluded because they refer to another sheet/continuation.

```json
{
  "candidate_id": "D021",
  "value_mm": 1000.0,
  "from_vertex": "V01",
  "to_vertex": "V02",
  "edge_id": "E012",
  "target_sheet": "2",
  "label": "СМ. ЛИСТ 2",
  "reason": "..."
}
```

### cross_sheet_connections[]

```json
{
  "vertex_id": "V01",
  "label": "СМ. ЛИСТ 2",
  "target_sheet": "2",
  "connection_type": "continuation",
  "reason": "..."
}
```

### pipe_objects[]

Objects that should be available for 3D/modeling but do not contribute to pipe length.

```json
{
  "type": "handwheel",
  "label": "ШТУРВАЛ EAST",
  "edge_id": "E012",
  "handwheel_id": "HW-001",
  "reason": "..."
}
```

## Notes

- Do not compare `value_mm` with pixel distances or `pixel_length`.
- Use `selected_edge_id` as the candidate's primary edge.
- `existing_mapping.pipe_anchor.edge_id` can disagree with `selected_edge_id`; it is an anchor hint, not the authoritative candidate edge.
- `preliminary_decision` is an input hypothesis. It can be confirmed, rejected, or turned into `ambiguous` depending on the prompt strategy.
