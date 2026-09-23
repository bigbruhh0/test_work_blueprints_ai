import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fitz

from src.dimension_mapping import _dimension_hints_from_text, _first_arrow_third, _handwheel_arrow_segments, _handwheel_details, _handwheel_text_rects, _is_rectangle_side, _same_directed_contour, apply_local_dimension_filter, compute_endpoint_adjustments, compute_extension_vertices, save_clean_local_markup_pdf, save_local_dimension_filter_pdf, save_preprocess_annotation_pdf
from src.dimension_mapping import _direct_dimension_stroke_from_label, _fallback_leader_stroke_from_label, _find_extension_strokes, _merge_dimension_stroke, _resolve_leader_target
from src.dimension_mapping import _annotation_arrow_segments, _final_contour_ray_for_dimension, _glyph_pipe_vertex_rows, _handwheel_glyph_parallelograms, _handwheel_glyph_rows, _reserved_annotation_strokes, annotate_valve_edges, compute_final_vertices, split_edges_by_final_vertices
from src.dimension_review import build_review_payload


class DimensionRuleTests(unittest.TestCase):
    def test_dimension_stroke_merge_does_not_absorb_other_labeled_dimensions(self) -> None:
        first = SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension")
        second = SimpleNamespace(index=2, x0=100.0, y0=0.0, x1=200.0, y1=0.0, length=100.0, kind="dimension")

        merged = _merge_dimension_stroke(first, [first, second], blocked_indices={2})

        self.assertEqual(merged["merged_indices"], [1])
        self.assertEqual(merged["length_px"], 100.0)

    def test_extension_strokes_are_selected_per_dimension_endpoint(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [0.0, 30.0], "end": [100.0, 30.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=0.0, y0=0.0, x1=0.0, y1=30.0, length=30.0, kind="dimension"),
            SimpleNamespace(index=3, x0=100.0, y0=0.0, x1=100.0, y1=30.0, length=30.0, kind="dimension"),
            SimpleNamespace(index=4, x0=50.0, y0=0.0, x1=62.0, y1=4.0, length=12.65, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual({item["index"] for item in extensions}, {2, 3})
        self.assertEqual({item["target_endpoint"] for item in extensions}, {"start", "end"})
        self.assertEqual({item["pipe_edge_id"] for item in extensions}, {"E001"})

    def test_short_extension_strokes_are_allowed_when_tightly_attached(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [100.0, 10.0], "end": [140.0, 10.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=100.0, y0=0.0, x1=109.0, y1=7.0, length=11.4, kind="dimension"),
            SimpleNamespace(index=3, x0=100.0, y0=0.0, x1=125.0, y1=40.0, length=47.2, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual([item["index"] for item in extensions], [2])

    def test_extension_stroke_can_touch_dimension_endpoint_by_its_middle(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [0.0, 28.0], "end": [60.0, 28.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=0.0, y0=-12.0, x1=0.0, y1=32.0, length=44.0, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual([item["index"] for item in extensions], [2])
        self.assertEqual(extensions[0]["target_endpoint"], "start")

    def test_extension_stroke_rejects_touch_far_from_its_endpoint(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [0.0, 28.0], "end": [60.0, 28.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=0.0, y0=-20.0, x1=0.0, y1=32.0, length=52.0, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual(extensions, [])

    def test_extension_stroke_relaxed_fallback_accepts_trimmed_line(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [0.0, 34.0], "end": [100.0, 34.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=14.0, y0=4.0, x1=14.0, y1=34.0, length=30.0, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual([item["index"] for item in extensions], [2])
        self.assertEqual(extensions[0]["fallback"], "relaxed_extension_endpoint")

    def test_extension_stroke_mirrors_missing_second_extension(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [0.0, 30.0], "end": [12.0, 30.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=0.0, y0=0.0, x1=0.0, y1=30.0, length=30.0, kind="dimension"),
            SimpleNamespace(index=3, x0=100.0, y0=0.0, x1=100.0, y1=30.0, length=30.0, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual({item["index"] for item in extensions}, {2, 3})
        mirrored = next(item for item in extensions if item["index"] == 3)
        self.assertEqual(mirrored["fallback"], "mirrored_missing_extension")
        self.assertEqual(mirrored["mirrored_from_index"], 2)

    def test_extension_stroke_mirrors_from_base_stroke_when_merge_is_too_long(self) -> None:
        target = {"index": 1, "start": [-100.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1, 4]}
        edge = {"id": "E001", "start": [100.0, 30.0], "end": [112.0, 30.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=100.0, y0=0.0, x1=100.0, y1=30.0, length=30.0, kind="dimension"),
            SimpleNamespace(index=3, x0=0.0, y0=0.0, x1=0.0, y1=30.0, length=30.0, kind="dimension"),
            SimpleNamespace(index=4, x0=-100.0, y0=0.0, x1=0.0, y1=0.0, length=100.0, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual({item["index"] for item in extensions}, {2, 3})
        mirrored = next(item for item in extensions if item["index"] == 3)
        self.assertEqual(mirrored["fallback"], "mirrored_missing_extension")
        self.assertEqual(mirrored["mirror_source"], "base_dimension_stroke")

    def test_extension_stroke_mirrors_short_dimension_with_wider_perpendicular_gap(self) -> None:
        target = {"index": 1, "start": [0.0, 25.0], "end": [0.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [24.0, 12.0], "end": [36.0, 12.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=25.0, x1=0.0, y1=0.0, length=25.0, kind="dimension"),
            SimpleNamespace(index=2, x0=0.0, y0=25.0, x1=27.0, y1=10.0, length=30.89, kind="dimension"),
            SimpleNamespace(index=3, x0=-19.0, y0=15.0, x1=46.0, y1=-23.0, length=75.29, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        self.assertEqual({item["index"] for item in extensions}, {2, 3})
        mirrored = next(item for item in extensions if item["index"] == 3)
        self.assertEqual(mirrored["fallback"], "mirrored_missing_extension")

    def test_extension_stroke_prefers_tight_endpoint_hit_over_pipe_gap(self) -> None:
        target = {"index": 1, "start": [0.0, 25.0], "end": [0.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [-30.0, 0.0], "end": [-20.0, 0.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=25.0, x1=0.0, y1=0.0, length=25.0, kind="dimension"),
            SimpleNamespace(index=2, x0=-28.0, y0=26.0, x1=3.0, y1=-10.0, length=47.51, kind="dimension"),
            SimpleNamespace(index=3, x0=3.0, y0=26.0, x1=-18.0, y1=10.0, length=26.4, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes)

        start_extension = next(item for item in extensions if item["target_endpoint"] == "start")
        self.assertEqual(start_extension["index"], 3)

    def test_reserved_annotation_strokes_ignore_crossing_dimension_lines(self) -> None:
        segments = [((0.0, 0.0), (100.0, 0.0))]
        strokes = [
            SimpleNamespace(index=1, x0=10.0, y0=0.0, x1=40.0, y1=0.0),
            SimpleNamespace(index=2, x0=0.0, y0=-20.0, x1=0.0, y1=20.0),
            SimpleNamespace(index=3, x0=0.0, y0=2.0, x1=100.0, y1=2.0),
        ]

        self.assertEqual(_reserved_annotation_strokes(strokes, segments), {1, 3})

    def test_annotation_arrow_segments_include_handwheel_and_connection_arrows(self) -> None:
        page = SimpleNamespace(rect=fitz.Rect(0, 0, 400, 400))
        handwheel_segment = ((10.0, 10.0), (40.0, 40.0))
        connection_segment = ((100.0, 100.0), (140.0, 120.0))
        connections = [
            {"arrow_segments": [{"start": list(connection_segment[0]), "end": list(connection_segment[1])}]}
        ]
        page.get_text = lambda *args, **kwargs: []
        page.get_drawings = lambda: []

        with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]), mock.patch(
            "src.dimension_mapping._handwheel_text_rects",
            return_value=[("ШТУРВАЛ", fitz.Rect(0, 0, 5, 5))],
        ), mock.patch(
            "src.dimension_mapping._handwheel_arrow_paths",
            return_value=[[handwheel_segment]],
        ):
            segments = _annotation_arrow_segments(page, connections)

        self.assertEqual(segments, [handwheel_segment, connection_segment])

    def test_extension_strokes_skip_reserved_annotation_lines(self) -> None:
        target = {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]}
        edge = {"id": "E001", "start": [0.0, 30.0], "end": [100.0, 30.0]}
        strokes = [
            SimpleNamespace(index=1, x0=0.0, y0=0.0, x1=100.0, y1=0.0, length=100.0, kind="dimension"),
            SimpleNamespace(index=2, x0=0.0, y0=0.0, x1=0.0, y1=30.0, length=30.0, kind="dimension"),
            SimpleNamespace(index=3, x0=100.0, y0=0.0, x1=100.0, y1=30.0, length=30.0, kind="dimension"),
        ]

        extensions = _find_extension_strokes({"dimension_stroke": target}, edge, strokes, blocked_indices={2})

        self.assertEqual({item["index"] for item in extensions}, {3})

    def test_endpoint_adjustment_uses_nearest_extension_contact(self) -> None:
        mapping = {
            "vertices": [
                {"id": "V01", "role": "endpoint", "x": 0.0, "y": 0.0},
                {"id": "V02", "role": "endpoint", "x": 100.0, "y": 0.0},
            ],
            "edges": [
                {"id": "E001", "start": [0.0, 0.0], "end": [100.0, 0.0], "pixel_length": 100.0},
            ],
            "dimensions": [
                {
                    "id": "D001",
                    "edge_id": "E001",
                    "extension_strokes": [
                        {"index": 10, "start": [25.0, -20.0], "end": [25.0, 0.0], "pipe_edge_id": "E001"},
                        {"index": 11, "start": [70.0, -20.0], "end": [70.0, 0.0], "pipe_edge_id": "E001"},
                    ],
                }
            ],
        }

        adjustments = compute_endpoint_adjustments(mapping)

        self.assertEqual(len(adjustments), 2)
        by_vertex = {item["vertex_id"]: item for item in adjustments}
        self.assertEqual(by_vertex["V01"]["adjusted"], [25.0, 0.0])
        self.assertEqual(by_vertex["V01"]["dimension_id"], "D001")
        self.assertEqual(by_vertex["V02"]["adjusted"], [70.0, 0.0])

    def test_endpoint_adjustment_stays_empty_without_extension_contact(self) -> None:
        mapping = {
            "vertices": [{"id": "V01", "role": "endpoint", "x": 0.0, "y": 0.0}],
            "edges": [{"id": "E001", "start": [0.0, 0.0], "end": [100.0, 0.0], "pixel_length": 100.0}],
            "dimensions": [{"id": "D001", "edge_id": "E001", "extension_strokes": []}],
        }

        self.assertEqual(compute_endpoint_adjustments(mapping), [])

    def test_endpoint_adjustment_ignores_distant_extension_endpoint(self) -> None:
        mapping = {
            "vertices": [{"id": "V01", "role": "endpoint", "x": 0.0, "y": 0.0}],
            "edges": [{"id": "E001", "start": [0.0, 0.0], "end": [200.0, 0.0], "pixel_length": 200.0}],
            "dimensions": [
                {
                    "id": "D001",
                    "edge_id": "E001",
                    "extension_strokes": [
                        {"index": 10, "start": [120.0, -20.0], "end": [120.0, 0.0], "pipe_edge_id": "E001"},
                    ],
                }
            ],
        }

        self.assertEqual(compute_endpoint_adjustments(mapping), [])

    def test_endpoint_adjustment_keeps_endpoint_when_extension_is_already_close(self) -> None:
        mapping = {
            "vertices": [{"id": "V01", "role": "endpoint", "x": 0.0, "y": 0.0}],
            "edges": [{"id": "E001", "start": [0.0, 0.0], "end": [100.0, 0.0], "pixel_length": 100.0}],
            "dimensions": [
                {
                    "id": "D001",
                    "edge_id": "E001",
                    "extension_strokes": [
                        {"index": 10, "start": [5.0, -20.0], "end": [5.0, 0.0], "pipe_edge_id": "E001"},
                    ],
                }
            ],
        }

        self.assertEqual(compute_endpoint_adjustments(mapping), [])

    def test_leader_target_resolves_touched_dimension_line_without_edge_angle_match(self) -> None:
        leader = SimpleNamespace(index=1, x0=10.0, y0=10.0, x1=55.0, y1=45.0, length=57.0, kind="dimension")
        target = SimpleNamespace(index=2, x0=50.0, y0=45.0, x1=90.0, y1=45.0, length=40.0, kind="dimension")
        distant = SimpleNamespace(index=3, x0=55.0, y0=80.0, x1=95.0, y1=80.0, length=40.0, kind="dimension")
        vertical_edge = {"id": "E1", "start": [60.0, 0.0], "end": [60.0, 100.0]}

        resolved = _resolve_leader_target(
            leader,
            vertical_edge,
            [leader, target, distant],
            label_center=(10.0, 10.0),
            blocked_indices={leader.index},
        )

        self.assertIsNotNone(resolved)
        stroke, meta = resolved
        self.assertEqual(stroke.index, target.index)
        self.assertEqual(meta["leader_target_point"], [55.0, 45.0])

    def test_fallback_leader_stroke_finds_short_line_near_label(self) -> None:
        label_center = (219.5, 261.1)
        distant = SimpleNamespace(index=1, x0=300.0, y0=260.0, x1=340.0, y1=280.0, length=44.7, kind="dimension")
        leader = SimpleNamespace(index=2, x0=240.0, y0=240.9, x1=228.7, y1=261.3, length=23.3, kind="dimension")

        stroke = _fallback_leader_stroke_from_label(label_center, [distant, leader])

        self.assertIsNotNone(stroke)
        self.assertEqual(stroke.index, leader.index)

    def test_direct_dimension_stroke_wins_when_label_sits_on_dimension_line(self) -> None:
        label_center = (308.1, 487.1)
        pipe_edge = {"id": "E1", "start": [300.0, 540.0], "end": [300.0, 430.0]}
        false_leader = SimpleNamespace(index=1, x0=323.3, y0=500.4, x1=252.0, y1=466.5, length=78.9, kind="dimension")
        direct_dimension = SimpleNamespace(index=2, x0=313.9, y0=510.9, x1=313.9, y1=457.2, length=53.8, kind="dimension")

        stroke = _direct_dimension_stroke_from_label(label_center, pipe_edge, [false_leader, direct_dimension])

        self.assertIsNotNone(stroke)
        self.assertEqual(stroke.index, direct_dimension.index)

    def test_local_dimension_filter_excludes_smaller_covered_dimension(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "from_node_id": "N1", "to_node_id": "N2", "start": [0, 20], "end": [120, 20]}],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 1200,
                    "edge_id": "E1",
                    "status": "projected",
                    "dimension_stroke": {"start": [0, 0], "end": [120, 0]},
                },
                {
                    "id": "D002",
                    "value": 300,
                    "edge_id": "E1",
                    "status": "projected",
                    "dimension_stroke": {"start": [30, 0], "end": [70, 0]},
                },
            ],
        }

        apply_local_dimension_filter(mapping)

        decisions = {item["id"]: item["local_filter_decision"] for item in mapping["dimensions"]}
        self.assertEqual(decisions["D001"], "include")
        self.assertEqual(decisions["D002"], "exclude")
        self.assertEqual(mapping["dimensions"][1]["local_filter_conflict_with"], "D001")

    def test_local_dimension_filter_excludes_offset_parallel_nested_dimension(self) -> None:
        mapping = {
            "edges": [
                {"id": "E1", "from_node_id": "N1", "to_node_id": "N2", "start": [0, 20], "end": [60, 20]},
                {"id": "E2", "from_node_id": "N2", "to_node_id": "N3", "start": [60, 20], "end": [120, 20]},
            ],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 4000,
                    "edge_id": "E2",
                    "status": "projected",
                    "dimension_stroke": {"start": [0, 0], "end": [120, 0]},
                },
                {
                    "id": "D002",
                    "value": 1253,
                    "edge_id": "E1",
                    "status": "projected",
                    "dimension_stroke": {"start": [20, 17], "end": [75, 17]},
                },
            ],
        }

        apply_local_dimension_filter(mapping)

        self.assertEqual(mapping["dimensions"][1]["local_filter_decision"], "exclude")

    def test_local_dimension_filter_keeps_parallel_dimension_without_shared_reference(self) -> None:
        mapping = {
            "edges": [
                {"id": "E1", "from_node_id": "N1", "to_node_id": "N2", "start": [0, 20], "end": [120, 20]},
                {"id": "E2", "from_node_id": "N3", "to_node_id": "N4", "start": [30, 44], "end": [90, 44]},
            ],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 5600,
                    "edge_id": "E1",
                    "status": "projected",
                    "dimension_stroke": {"start": [0, 0], "end": [120, 0], "merged_indices": [1]},
                    "extension_strokes": [{"index": 3, "pipe_edge_id": "E1"}],
                },
                {
                    "id": "D002",
                    "value": 281,
                    "edge_id": "E2",
                    "status": "projected",
                    "dimension_stroke": {"start": [30, 18], "end": [90, 18], "merged_indices": [2]},
                    "extension_strokes": [{"index": 4, "pipe_edge_id": "E2"}],
                },
            ],
        }

        apply_local_dimension_filter(mapping)

        self.assertEqual(mapping["dimensions"][1]["local_filter_decision"], "include")

    def test_local_dimension_filter_propagates_excluded_shared_dimension_stroke(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "from_node_id": "N1", "to_node_id": "N2", "start": [0, 20], "end": [120, 20]}],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 3000,
                    "edge_id": "E1",
                    "status": "projected",
                    "attachment_kind": "leader_to_dimension_arrow",
                    "dimension_stroke": {"start": [0, 0], "end": [60, 0], "length_px": 60, "merged_indices": [1, 2]},
                    "leader_stroke": {"start": [-40, 20], "end": [0, 0], "length_px": 45, "merged_indices": [3]},
                },
                {
                    "id": "D002",
                    "value": 6000,
                    "edge_id": "E1",
                    "status": "invalid_overlap",
                    "valid": False,
                    "dimension_stroke": {"start": [0, 0], "end": [80, 0], "length_px": 80, "merged_indices": [1, 2, 4]},
                    "reason": "overlaps_larger_dimension_on_same_directed_contour",
                },
            ],
        }

        apply_local_dimension_filter(mapping)

        self.assertEqual(mapping["dimensions"][0]["local_filter_decision"], "exclude")
        self.assertEqual(mapping["dimensions"][0]["local_filter_reason"], "shares_excluded_dimension_stroke")
        self.assertEqual(mapping["dimensions"][0]["local_filter_conflict_with"], "D002")

    def test_local_dimension_filter_excludes_short_endpoint_leader_overlap(self) -> None:
        mapping = {
            "edges": [
                {"id": "E1", "from_node_id": "N1", "to_node_id": "N2", "start": [0, 0], "end": [0, 120]},
                {"id": "E2", "from_node_id": "N2", "to_node_id": "N3", "start": [20, 0], "end": [20, 120]},
            ],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 303,
                    "edge_id": "E2",
                    "status": "projected",
                    "dimension_stroke": {"start": [20, 100], "end": [20, 0], "length_px": 100, "merged_indices": [8]},
                },
                {
                    "id": "D002",
                    "value": 153,
                    "edge_id": "E2",
                    "status": "projected",
                    "attachment_kind": "leader_to_dimension_arrow",
                    "dimension_stroke": {"start": [20, 45], "end": [20, 25], "length_px": 20, "merged_indices": [7]},
                    "leader_stroke": {"start": [80, 25], "end": [20, 25], "length_px": 60, "merged_indices": [9]},
                    "leader_resolution": {"target_position": 1.0},
                },
            ],
        }

        apply_local_dimension_filter(mapping)

        self.assertEqual(mapping["dimensions"][1]["local_filter_decision"], "exclude")
        self.assertEqual(mapping["dimensions"][1]["local_filter_reason"], "covered_by_short_endpoint_leader_overlap")

    def test_extension_vertices_project_onto_pipe_edge(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 40.0], "end": [200.0, 40.0]}],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [10.0, 0.0], "end": [60.0, 0.0], "merged_indices": [1]},
                    "extension_strokes": [
                        {"index": 11, "start": [10.0, 30.0], "end": [10.0, 10.0], "pipe_edge_id": "E1"},
                        {"index": 12, "start": [60.0, 10.0], "end": [60.0, 30.0], "pipe_edge_id": "E1"},
                    ],
                }
            ],
        }

        vertices = compute_extension_vertices(mapping)

        self.assertEqual([vertex["id"] for vertex in vertices], ["VE-01", "VE-02"])
        self.assertEqual(sorted(vertex["point"] for vertex in vertices), [[10.0, 40.0], [60.0, 40.0]])

    def test_extension_vertices_merge_close_hits_into_one(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 40.0], "end": [200.0, 40.0]}],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [10.0, 0.0], "end": [60.0, 0.0], "merged_indices": [1]},
                    "extension_strokes": [{"index": 11, "start": [10.0, 30.0], "end": [10.0, 10.0], "pipe_edge_id": "E1"}],
                },
                {
                    "id": "D002",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [12.0, 0.0], "end": [20.0, 0.0], "merged_indices": [2]},
                    "extension_strokes": [{"index": 21, "start": [12.0, 10.0], "end": [12.0, 30.0], "pipe_edge_id": "E1"}],
                },
            ],
        }

        vertices = compute_extension_vertices(mapping)

        self.assertEqual(len(vertices), 1)
        self.assertEqual(vertices[0]["id"], "VE-01")
        self.assertEqual(vertices[0]["point"], [10.0, 40.0])
        self.assertEqual(vertices[0]["dimension_ids"], ["D001", "D002"])

    def test_extension_vertices_extend_away_from_dimension_line_side(self) -> None:
        mapping = {
            "vertices": [{"id": "V01", "role": "corner", "x": 10.0, "y": 40.0}],
            "edges": [
                {"id": "E1", "start": [0.0, 40.0], "end": [200.0, 40.0]},
                {"id": "E2", "start": [10.0, 6.0], "end": [60.0, 6.0]},
            ],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [10.0, 0.0], "end": [60.0, 0.0], "merged_indices": [1]},
                    "extension_strokes": [
                        {"index": 11, "start": [10.0, 30.0], "end": [10.0, 5.0], "target_endpoint": "start", "pipe_edge_id": "E1"},
                    ],
                }
            ],
        }

        vertices = compute_extension_vertices(mapping)

        self.assertEqual(vertices[0]["point"], [10.0, 40.0])
        self.assertEqual(vertices[0]["pipe_edge_id"], "E1")
        self.assertEqual(mapping["suppressed_vertex_ids"], ["V01"])

    def test_extension_vertices_accept_near_miss_pipe_contour(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [20.0, 40.0], "end": [200.0, 40.0]}],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [10.0, 0.0], "end": [60.0, 0.0], "merged_indices": [1]},
                    "extension_strokes": [
                        {"index": 11, "start": [12.0, 30.0], "end": [12.0, 4.0], "target_endpoint": "start", "pipe_edge_id": "E1"},
                    ],
                }
            ],
        }

        vertices = compute_extension_vertices(mapping)

        self.assertEqual(vertices[0]["point"], [20.0, 40.0])
        self.assertEqual(vertices[0]["projection_kind"], "near_miss")
        self.assertEqual(vertices[0]["side_gap_px"], 8.0)

    def test_extension_vertices_use_middle_touch_to_choose_outward_endpoint(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [328.32, 319.7], "end": [328.32, 345.4]}],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [296.64, 363.33], "end": [296.64, 337.89], "merged_indices": [1]},
                    "extension_strokes": [
                        {"index": 11, "start": [255.6, 361.65], "end": [320.16, 324.45], "target_endpoint": "end", "pipe_edge_id": "E1"},
                    ],
                }
            ],
        }

        vertices = compute_extension_vertices(mapping)

        self.assertEqual(vertices[0]["point"], [328.32, 319.75])
        self.assertEqual(vertices[0]["ray_source"], "target_touch_to_end")

    def test_extension_vertices_skip_excluded_dimensions(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 40.0], "end": [200.0, 40.0]}],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "exclude",
                    "dimension_stroke": {"start": [10.0, 0.0], "end": [60.0, 0.0], "merged_indices": [1]},
                    "extension_strokes": [{"index": 11, "start": [10.0, 30.0], "end": [10.0, 10.0], "pipe_edge_id": "E1"}],
                },
                {
                    "id": "D002",
                    "local_filter_decision": "ambiguous",
                    "dimension_stroke": {"start": [60.0, 0.0], "end": [90.0, 0.0], "merged_indices": [2]},
                    "extension_strokes": [{"index": 21, "start": [60.0, 10.0], "end": [60.0, 30.0], "pipe_edge_id": "E1"}],
                },
            ],
        }

        self.assertEqual(compute_extension_vertices(mapping), [])

    def test_extension_vertices_skip_when_pipe_is_too_far(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 300.0], "end": [200.0, 300.0]}],
            "dimensions": [
                {
                    "id": "D001",
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [10.0, 0.0], "end": [60.0, 0.0], "merged_indices": [1]},
                    "extension_strokes": [{"index": 11, "start": [10.0, 2.0], "end": [10.0, 0.0], "pipe_edge_id": "E1"}],
                }
            ],
        }

        self.assertEqual(compute_extension_vertices(mapping), [])

    def test_handwheel_glyph_parallelogram_wraps_crossing_legs(self) -> None:
        drawings = [{"items": [
            ("l", fitz.Point(100.0, 100.0), fitz.Point(140.0, 160.0)),
            ("l", fitz.Point(100.0, 160.0), fitz.Point(140.0, 100.0)),
        ]}]

        glyphs = _handwheel_glyph_parallelograms(drawings)

        self.assertEqual(len(glyphs), 1)
        center = glyphs[0]["center"]
        self.assertEqual(center, [120.0, 130.0])
        corners = glyphs[0]["corners"]
        self.assertEqual(len(corners), 4)
        min_x = min(corner[0] for corner in corners)
        max_x = max(corner[0] for corner in corners)
        min_y = min(corner[1] for corner in corners)
        max_y = max(corner[1] for corner in corners)
        self.assertLessEqual(min_x, 100.0)
        self.assertGreaterEqual(max_x, 140.0)
        self.assertLessEqual(min_y, 100.0)
        self.assertGreaterEqual(max_y, 160.0)
        self.assertEqual(glyphs[0]["legs"], [
            [[100.0, 100.0], [140.0, 160.0]],
            [[100.0, 160.0], [140.0, 100.0]],
        ])

    def test_glyph_pipe_vertices_span_glyph_along_edge(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [200.0, 100.0]}],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [50.0, 98.0],
                    "legs": [
                        [[30.0, 88.0], [70.0, 88.0]],
                        [[40.0, 108.0], [60.0, 108.0]],
                    ],
                },
            ],
        }

        rows = _glyph_pipe_vertex_rows(mapping)
        start = next(row for row in rows if row["role"] == "start")
        end = next(row for row in rows if row["role"] == "end")

        self.assertEqual([row["id"] for row in rows], ["HG-01-A", "HG-01-B"])
        self.assertEqual(start["point"], [30.0, 100.0])
        self.assertEqual(end["point"], [70.0, 100.0])
        self.assertEqual(start["edge_id"], "E1")
        self.assertEqual(start["source"], "handwheel_glyph_span")
        self.assertEqual(start["symbol_points"], [[30.0, 88.0], [70.0, 88.0], [40.0, 108.0], [60.0, 108.0]])
        self.assertEqual(len(rows[0]["symbol_points"]), 4)

    def test_glyph_pipe_vertices_vertical_diagonal_and_clamped(self) -> None:
        mapping = {
            "edges": [
                {"id": "E_V", "start": [50.0, 0.0], "end": [50.0, 200.0]},
                {"id": "E_D", "start": [0.0, 0.0], "end": [100.0, 100.0]},
            ],
            "handwheel_glyphs": [
                {
                    "id": "HG-V",
                    "center": [50.0, 50.0],
                    "legs": [
                        [[48.0, 42.0], [52.0, 58.0]],
                        [[52.0, 42.0], [48.0, 58.0]],
                    ],
                },
                {
                    "id": "HG-D",
                    "center": [70.0, 70.0],
                    "legs": [
                        [[58.0, 62.0], [82.0, 78.0]],
                        [[58.0, 78.0], [82.0, 62.0]],
                    ],
                },
            ],
        }

        rows = _glyph_pipe_vertex_rows(mapping)

        self.assertEqual([row["id"] for row in rows], ["HG-V-A", "HG-V-B", "HG-D-A", "HG-D-B"])
        vertical = [row for row in rows if row["handwheel_id"] == "HG-V"]
        diagonal = [row for row in rows if row["handwheel_id"] == "HG-D"]
        start_vertical = next(row for row in vertical if row["role"] == "start")
        end_vertical = next(row for row in vertical if row["role"] == "end")
        self.assertEqual(start_vertical["point"], [50.0, 42.0])
        self.assertEqual(end_vertical["point"], [50.0, 58.0])
        start_diagonal = next(row for row in diagonal if row["role"] == "start")
        end_diagonal = next(row for row in diagonal if row["role"] == "end")
        self.assertAlmostEqual(start_diagonal["point"][0], 60.0, places=6)
        self.assertAlmostEqual(start_diagonal["point"][1], 60.0, places=6)
        self.assertAlmostEqual(end_diagonal["point"][0], 80.0, places=6)
        self.assertAlmostEqual(end_diagonal["point"][1], 80.0, places=6)

    def test_glyph_pipe_vertices_clamp_to_edge_bounds(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [40.0, 100.0], "end": [60.0, 100.0]}],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [50.0, 98.0],
                    "legs": [
                        [[30.0, 88.0], [70.0, 88.0]],
                        [[40.0, 108.0], [60.0, 108.0]],
                    ],
                },
            ],
        }

        rows = _glyph_pipe_vertex_rows(mapping)
        start = next(row for row in rows if row["role"] == "start")
        end = next(row for row in rows if row["role"] == "end")

        self.assertEqual(start["point"], [40.0, 100.0])
        self.assertEqual(end["point"], [60.0, 100.0])
        self.assertEqual(start["source_point"], [50.0, 100.0])

    def test_glyph_pipe_vertices_gap_glyph_places_vertex_at_symbol_far_end(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [40.0, 100.0]}],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [50.0, 98.0],
                    "legs": [
                        [[40.0, 92.0], [80.0, 92.0]],
                        [[50.0, 108.0], [70.0, 108.0]],
                    ],
                },
            ],
        }

        rows = _glyph_pipe_vertex_rows(mapping)
        start = next(row for row in rows if row["role"] == "start")
        end = next(row for row in rows if row["role"] == "end")

        self.assertEqual(start["point"], [40.0, 100.0])
        self.assertEqual(end["point"], [80.0, 100.0])
        self.assertNotEqual(start["point"], end["point"])

    def test_glyph_pipe_vertices_skip_when_no_pipe_edge_nearby(self) -> None:
        mapping = {
            "edges": [{"id": "E9", "start": [500.0, 500.0], "end": [600.0, 500.0]}],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [50.0, 50.0],
                    "legs": [
                        [[40.0, 40.0], [60.0, 60.0]],
                        [[40.0, 60.0], [60.0, 40.0]],
                    ],
                },
            ],
        }

        self.assertEqual(_glyph_pipe_vertex_rows(mapping), [])

    def test_final_vertices_priority_leaves_only_handwheel_vertex(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [200.0, 100.0]}],
            "extension_vertices": [
                {"id": "VE-01", "dimension_ids": ["D001"], "pipe_edge_id": "E1", "point": [31.0, 100.0]},
                {"id": "VE-02", "dimension_ids": ["D002"], "pipe_edge_id": "E1", "point": [120.0, 99.0]},
                {"id": "VE-03", "dimension_ids": ["D002"], "pipe_edge_id": "E1", "point": [150.0, 100.0]},
            ],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [49.0, 96.0],
                    "legs": [
                        [[30.0, 88.0], [70.0, 88.0]],
                        [[40.0, 108.0], [60.0, 108.0]],
                    ],
                },
            ],
        }

        final = compute_final_vertices(mapping)

        ids = [row["id"] for row in final]
        self.assertIn("HG-01-A", ids)
        self.assertIn("HG-01-B", ids)
        self.assertIn("VE-02", ids)
        self.assertIn("VE-03", ids)
        self.assertNotIn("VE-01", ids)
        handwheel_row = next(row for row in final if row["id"] == "HG-01-A")
        self.assertEqual(handwheel_row["replaced_vertex_ids"], ["VE-01"])
        self.assertEqual(handwheel_row["replace_reason"], "handwheel_nearest_ve")

    def test_final_vertices_merge_along_pipe_axis_only(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [200.0, 100.0]}],
            "extension_vertices": [
                {"id": "VE-01", "pipe_edge_id": "E1", "point": [42.0, 100.0]},
                {"id": "VE-02", "pipe_edge_id": "E1", "point": [40.0, 130.0]},
                {"id": "VE-03", "pipe_edge_id": "E_OTHER", "point": [44.0, 140.0]},
            ],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [48.0, 96.0],
                    "legs": [
                        [[30.0, 88.0], [70.0, 88.0]],
                        [[40.0, 108.0], [60.0, 108.0]],
                    ],
                },
            ],
        }

        final = compute_final_vertices(mapping)
        ids = [row["id"] for row in final]

        self.assertNotIn("VE-01", ids)
        self.assertEqual(next(row for row in final if row["id"] == "HG-01-A")["replaced_vertex_ids"], ["VE-01"])
        self.assertIn("VE-02", ids, "поперечное смещение > 6 px не считается совпадением")
        self.assertIn("VE-03", ids, "вершины с других рёбер не затрагиваются")

    def test_final_vertices_keep_close_vertices_of_different_handwheels(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [200.0, 100.0]}],
            "extension_vertices": [],
            "handwheel_glyphs": [
                {
                    "id": "HG-01",
                    "center": [40.0, 98.0],
                    "legs": [
                        [[30.0, 92.0], [70.0, 92.0]],
                        [[30.0, 104.0], [50.0, 104.0]],
                    ],
                },
                {
                    "id": "HG-02",
                    "center": [45.0, 98.0],
                    "legs": [
                        [[35.0, 92.0], [75.0, 92.0]],
                        [[35.0, 104.0], [55.0, 104.0]],
                    ],
                },
            ],
        }

        final = compute_final_vertices(mapping)

        handwheel_ids = list(dict.fromkeys(row["handwheel_id"] for row in final))
        self.assertEqual(handwheel_ids, ["HG-01", "HG-02"])
        for helper in ("HG-01-A", "HG-01-B", "HG-02-A", "HG-02-B"):
            self.assertIn(helper, [row["id"] for row in final])

    def test_split_edges_by_final_vertices_no_gaps(self) -> None:
        mapping = {
            "final_vertices": [
                {"id": "VE-01", "point": [30.0, 100.0], "vertex_source": "extension"},
                {"id": "VE-02", "point": [70.0, 100.0], "vertex_source": "extension"},
            ],
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [100.0, 100.0], "pixel_length": 100.0, "from_node_id": "N1", "to_node_id": "N2"}],
            "dimensions": [],
        }

        segments = split_edges_by_final_vertices(mapping)

        self.assertEqual([segment["id"] for segment in segments], ["F-VE-01-VE-02"])
        self.assertEqual(segments[0]["from_vertex"], "VE-01")
        self.assertEqual(segments[0]["to_vertex"], "VE-02")
        self.assertEqual(segments[0]["start"], [30.0, 100.0])
        self.assertEqual(segments[0]["end"], [70.0, 100.0])
        self.assertAlmostEqual(float(segments[0]["pixel_length"]), 40.0)
        self.assertNotIn("parent_edge_id", segments[0])
        self.assertEqual(mapping["parent_edges"][0]["id"], "E1")
        self.assertEqual(mapping["edges"][0]["id"], "F-VE-01-VE-02")
        self.assertFalse(any(segment.get("is_handwheel_segment") for segment in segments))

    def test_split_marks_handwheel_segments_as_valve(self) -> None:
        mapping = {
            "final_vertices": [
                {"id": "HG-01-A", "handwheel_id": "HG-01", "point": [30.0, 100.0],
                 "source": "handwheel_glyph_span", "symbol_span_px": [30.0, 47.5]},
                {"id": "HG-01-B", "handwheel_id": "HG-01", "point": [47.5, 100.0],
                 "source": "handwheel_glyph_span", "symbol_span_px": [30.0, 47.5]},
            ],
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [100.0, 100.0], "element_type": "pipe", "is_valve_edge": False, "from_node_id": "N1", "to_node_id": "N2"}],
            "dimensions": [],
        }

        split_edges_by_final_vertices(mapping)

        acting = [segment for segment in mapping["edge_segments"] if segment.get("is_handwheel_segment")]
        self.assertEqual([segment["id"] for segment in acting], ["F-HG-01-A-HG-01-B"])
        self.assertEqual(acting[0]["element_type"], "valve")
        self.assertEqual(acting[0]["handwheel_ids"], ["HG-01"])

    def test_handwheel_in_gap_builds_own_valve_segment(self) -> None:
        mapping = {
            "handwheel_vertices": [
                {
                    "id": "HG-01-A",
                    "handwheel_id": "HG-01",
                    "edge_id": "E1",
                    "point": [40.0, 100.0],
                    "symbol_span_px": [40.0, 80.0],
                    "source": "handwheel_glyph_span",
                },
                {
                    "id": "HG-01-B",
                    "handwheel_id": "HG-01",
                    "edge_id": "E1",
                    "point": [40.0, 100.0],
                    "symbol_span_px": [40.0, 80.0],
                    "source": "handwheel_glyph_span",
                },
            ],
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [40.0, 100.0], "element_type": "pipe", "from_node_id": "N1", "to_node_id": "N2"}],
            "final_vertices": [
                {"id": "HG-01-A", "handwheel_id": "HG-01", "point": [40.0, 100.0], "vertex_source": "handwheel"},
                {"id": "HG-01-B", "handwheel_id": "HG-01", "point": [80.0, 100.0], "vertex_source": "handwheel"},
            ],
            "dimensions": [],
        }

        split_edges_by_final_vertices(mapping)

        bridge = next(segment for segment in mapping["edge_segments"] if segment["id"] == "F-HG-01-A-HG-01-B")
        self.assertEqual(bridge["start"], [40.0, 100.0])
        self.assertEqual(bridge["end"], [80.0, 100.0])
        self.assertEqual(bridge["from_vertex"], "HG-01-A")
        self.assertEqual(bridge["to_vertex"], "HG-01-B")
        self.assertTrue(bridge["is_handwheel_segment"])
        self.assertEqual(bridge["element_type"], "valve")
        self.assertEqual(bridge["handwheel_ids"], ["HG-01"])

    def test_handwheel_endpoint_gap_connects_adjacent_pipe_segment(self) -> None:
        mapping = {
            "final_vertices": [
                {"id": "HG-01-A", "handwheel_id": "HG-01", "point": [40.0, 100.0], "vertex_source": "handwheel", "edge_id": "E1"},
                {"id": "HG-01-B", "handwheel_id": "HG-01", "point": [48.0, 100.0], "vertex_source": "handwheel", "edge_id": "E1"},
                {"id": "VE-01", "point": [80.0, 100.0], "vertex_source": "extension", "edge_id": "E2"},
            ],
            "edges": [
                {"id": "E1", "start": [0.0, 100.0], "end": [40.0, 100.0], "from_node_id": "N1", "to_node_id": "N2"},
                {"id": "E2", "start": [52.0, 100.0], "end": [100.0, 100.0], "from_node_id": "N3", "to_node_id": "N4"},
            ],
            "dimensions": [],
        }

        split_edges_by_final_vertices(mapping)

        self.assertIn(
            ("HG-01-B", "VE-01"),
            {(segment["from_vertex"], segment["to_vertex"]) for segment in mapping["edge_segments"]},
        )

    def test_handwheel_dimension_conflict_prefers_valve_sized_dimension(self) -> None:
        mapping = {
            "final_vertices": [
                {"id": "HG-01-A", "handwheel_id": "HG-01", "point": [40.0, 100.0], "vertex_source": "handwheel"},
                {"id": "HG-01-B", "handwheel_id": "HG-01", "point": [70.0, 100.0], "vertex_source": "handwheel"},
            ],
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [100.0, 100.0], "from_node_id": "N1", "to_node_id": "N2"}],
            "dimensions": [
                {
                    "id": "D011",
                    "value": 11.0,
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [42.0, 90.0], "end": [70.0, 90.0], "length_px": 28.0},
                    "extension_strokes": [{"start": [42.0, 90.0], "end": [42.0, 110.0], "length_px": 20.0}],
                },
                {
                    "id": "D012",
                    "value": 294.0,
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [40.0, 80.0], "end": [70.0, 80.0], "length_px": 30.0},
                    "extension_strokes": [{"start": [40.0, 80.0], "end": [40.0, 110.0], "length_px": 30.0}],
                },
            ],
        }

        split_edges_by_final_vertices(mapping)

        by_id = {dimension["id"]: dimension for dimension in mapping["dimensions"]}
        self.assertEqual(by_id["D012"]["final_interval_id"], "F-HG-01-A-HG-01-B")
        self.assertEqual(by_id["D011"]["final_interval_status"], "unresolved")
        self.assertEqual(by_id["D011"]["final_interval_reason"], "handwheel_interval_replaced_by_better_dimension")

    def test_extension_near_miss_snaps_to_source_corner(self) -> None:
        mapping = {
            "vertices": [{"id": "V05", "role": "corner", "x": 209.58, "y": 354.75, "degree": 2}],
            "edges": [{"id": "E012", "start": [224.16, 367.17], "end": [291.6, 406.05]}],
            "dimensions": [
                {
                    "id": "D005",
                    "value": 650.0,
                    "local_filter_decision": "include",
                    "dimension_stroke": {"start": [233.76, 372.93], "end": [353.52, 304.29], "length_px": 138.04},
                    "extension_strokes": [
                        {
                            "index": 135,
                            "start": [238.08, 370.77],
                            "end": [228.24, 365.01],
                            "target_endpoint": "start",
                            "pipe_edge_id": "E012",
                        }
                    ],
                }
            ],
        }

        vertices = compute_extension_vertices(mapping)

        self.assertEqual(vertices[0]["point"], [209.58, 354.75])
        self.assertEqual(vertices[0]["projection_kind"], "near_miss")

    def test_dimension_projection_uses_final_ve_hg_interval(self) -> None:
        mapping = {
            "final_vertices": [
                {"id": "VE-01", "point": [30.0, 100.0], "vertex_source": "extension"},
                {"id": "VE-02", "point": [70.0, 100.0], "vertex_source": "extension"},
            ],
            "edges": [{"id": "E1", "start": [0.0, 100.0], "end": [100.0, 100.0]}],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 40.0,
                    "dimension_stroke": {"start": [40.0, 90.0], "end": [60.0, 90.0], "length_px": 20.0},
                    "extension_strokes": [{"start": [40.0, 90.0], "end": [40.0, 110.0], "length_px": 20.0}],
                },
            ],
        }

        split_edges_by_final_vertices(mapping)

        first = mapping["dimensions"][0]
        self.assertEqual(first["final_interval_id"], "F-VE-01-VE-02")
        self.assertEqual(first["final_from_vertex"], "VE-01")
        self.assertEqual(first["final_to_vertex"], "VE-02")
        self.assertEqual(first["final_interval_status"], "resolved")
        self.assertEqual(first["final_contact_point"], [50.0, 100.0])

    def test_final_ray_is_parallel_and_one_and_half_extension_lengths(self) -> None:
        ray = _final_contour_ray_for_dimension(
            {
                "dimension_stroke": {"start": [0.0, 0.0], "end": [20.0, 0.0]},
            },
            {"index": 7, "start": [0.0, 10.0], "end": [0.0, 30.0]},
        )

        self.assertIsNotNone(ray)
        self.assertEqual(ray["start"], (10.0, 0.0))
        self.assertEqual(ray["draw_end"], (10.0, 30.0))

    def test_local_dimension_filter_does_not_render_legacy_vertices(self) -> None:
        source = Path("src/dimension_mapping.py").read_text(encoding="utf-8")
        filter_start = source.index("def save_local_dimension_filter_pdf")
        filter_end = source.index("def save_clean_graph_pdf")
        block = source[filter_start:filter_end]
        self.assertNotIn('mapping.get("vertices", [])', block)
        self.assertNotIn("mapping.get(\"vertices\")", block)
        self.assertIn("final_vertices", block)

    def test_handwheel_glyph_ignores_t_cross_and_parallel_lines(self) -> None:
        drawings = [{"items": [
            ("l", fitz.Point(100.0, 100.0), fitz.Point(140.0, 100.0)),
            ("l", fitz.Point(120.0, 100.0), fitz.Point(120.0, 160.0)),
        ]}, {"items": [
            ("l", fitz.Point(200.0, 200.0), fitz.Point(280.0, 200.0)),
            ("l", fitz.Point(200.0, 220.0), fitz.Point(280.0, 220.0)),
        ]}]

        self.assertEqual(_handwheel_glyph_parallelograms(drawings), [])

    def test_handwheel_glyph_rows_keep_only_lead_confirmed_crosses(self) -> None:
        page = SimpleNamespace(
            rect=fitz.Rect(0, 0, 500, 500),
            get_drawings=lambda: [{"items": [
                ("l", fitz.Point(110.0, 120.0), fitz.Point(150.0, 160.0)),
                ("l", fitz.Point(110.0, 160.0), fitz.Point(150.0, 120.0)),
                ("l", fitz.Point(300.0, 300.0), fitz.Point(340.0, 340.0)),
                ("l", fitz.Point(300.0, 340.0), fitz.Point(340.0, 300.0)),
            ]}],
            get_text=lambda _kind: [(10, 10, 80, 24, "ШТУРВАЛ", 0, 0, 0)],
        )

        with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]), mock.patch(
            "src.dimension_mapping._handwheel_arrow_paths",
            return_value=[[
                ((80.0, 24.0), (130.0, 140.0)),
            ]],
        ):
            glyphs = _handwheel_glyph_rows(page)

        self.assertEqual(len(glyphs), 1)
        self.assertEqual(glyphs[0]["matched_source"], "handwheel_lead")
        self.assertEqual(glyphs[0]["center"], [130.0, 140.0])

    def test_handwheel_annotation_marks_pipe_edge_as_valve(self) -> None:
        mapping = {
            "edges": [{"id": "E001", "start": [0, 0], "end": [100, 0]}],
            "handwheels": [
                {
                    "id": "HW-001",
                    "edge_id": "E001",
                    "edge_created": False,
                    "arrow_end": [50, 0],
                    "edge_distance_px": 0.0,
                }
            ],
        }

        valve_edges = annotate_valve_edges(mapping)

        self.assertEqual(valve_edges[0]["edge_id"], "E001")
        self.assertTrue(mapping["edges"][0]["is_valve_edge"])
        self.assertEqual(mapping["edges"][0]["element_type"], "valve")
        self.assertEqual(mapping["edges"][0]["valve_handwheel_ids"], ["HW-001"])

    def test_local_dimension_filter_keeps_middle_hit_leader_callout(self) -> None:
        mapping = {
            "edges": [{"id": "E1", "from_node_id": "N1", "to_node_id": "N2", "start": [20, 0], "end": [20, 120]}],
            "dimensions": [
                {
                    "id": "D001",
                    "value": 191,
                    "edge_id": "E1",
                    "status": "projected",
                    "dimension_stroke": {"start": [20, 120], "end": [20, 0], "length_px": 120, "merged_indices": [7, 8]},
                },
                {
                    "id": "D002",
                    "value": 134,
                    "edge_id": "E1",
                    "status": "projected",
                    "attachment_kind": "leader_to_dimension_arrow",
                    "dimension_stroke": {"start": [20, 65], "end": [20, 43], "length_px": 22, "merged_indices": [8]},
                    "leader_stroke": {"start": [80, 54], "end": [20, 54], "length_px": 60, "merged_indices": [9]},
                    "leader_resolution": {"target_position": 0.5},
                },
            ],
        }

        apply_local_dimension_filter(mapping)

        self.assertEqual(mapping["dimensions"][1]["local_filter_decision"], "include")

    def test_dimension_review_prompt_uses_deterministic_overlap_rules(self) -> None:
        text = Path("prompts/defaults/dimension_review.txt").read_text(encoding="utf-8")
        self.assertIn("existing_mapping.valid=false", text)
        self.assertIn("последовательно разделена стрелками", text.lower())
        self.assertIn("не считай соседние значения перекрытием", text.lower())
        self.assertIn("preliminary_decision", text)
        self.assertIn("не копируй preliminary_decision автоматически", text.lower())

    def test_review_payload_exposes_preliminary_decisions(self) -> None:
        mapping = {
            "page": 1,
            "connections": [
                {
                    "id": "CN-C1",
                    "label": "СМ. ЛИСТ 2",
                    "connection_type": "continuation",
                    "target_sheet": "2",
                    "text_has_sheet_ref": True,
                }
            ],
            "dimensions": [
                {
                    "id": "D001",
                    "value_mm": 300,
                    "selected_edge_id": "E001",
                    "existing_mapping": {
                        "local_filter_decision": "exclude",
                        "local_filter_reason": "shares_excluded_dimension_stroke",
                        "local_filter_conflict_with": "D002",
                    },
                }
            ],
        }

        payload = build_review_payload(mapping, "map")

        self.assertEqual(payload["dimensions"][0]["preliminary_decision"]["decision"], "exclude")
        self.assertEqual(payload["dimensions"][0]["preliminary_decision"]["reason"], "shares_excluded_dimension_stroke")
        self.assertEqual(payload["preliminary_decisions"][0]["candidate_id"], "D001")
        self.assertEqual(payload["preliminary_decisions"][0]["conflict_with"], "D002")
        self.assertEqual(payload["connections"][0]["id"], "CN-C1")

    def test_dimension_review_prompt_covers_handwheel_and_cross_sheet(self) -> None:
        text = Path("prompts/defaults/dimension_review.txt").read_text(encoding="utf-8")
        self.assertIn("штурвал", text.lower())
        self.assertIn("handwheel", text.lower())
        self.assertIn("с двух сторон", text.lower())
        self.assertTrue("СМ. ЛИСТ" in text or "cross_sheet_dimension" in text)

    def test_dimension_hints_detect_cross_sheet_and_handwheel(self) -> None:
        self.assertIn("cross_sheet_reference", _dimension_hints_from_text("СМ. ЛИСТ 12"))
        self.assertIn("handwheel", _dimension_hints_from_text("Штурвал чугунный"))
        self.assertIn("handwheel", _dimension_hints_from_text("рукоятка"))

    def test_same_axis_dimensions_are_not_overlap(self) -> None:
        first = {
            "edge_id": "E003",
            "value": 3150,
            "offset_px": 0.0,
            "dimension_stroke": {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]},
            "status": "mapped",
        }
        second = {
            "edge_id": "E003",
            "value": 3200,
            "offset_px": 12.0,
            "dimension_stroke": {"start": [0.0, 12.0], "end": [120.0, 12.0], "merged_indices": [2]},
            "status": "mapped",
        }
        edge_by_id = {"E003": {"id": "E003", "from_node_id": "N001", "to_node_id": "N002", "start": [0.0, 0.0], "end": [100.0, 0.0]}}
        self.assertFalse(_same_directed_contour(first, second, edge_by_id))

    def test_offset_parallel_dimensions_can_overlap(self) -> None:
        first = {
            "edge_id": "E003",
            "value": 3150,
            "offset_px": 0.0,
            "dimension_stroke": {"start": [0.0, 0.0], "end": [100.0, 0.0], "merged_indices": [1]},
            "status": "mapped",
        }
        second = {
            "edge_id": "E003",
            "value": 3200,
            "offset_px": 40.0,
            "dimension_stroke": {"start": [0.0, 40.0], "end": [120.0, 40.0], "merged_indices": [2]},
            "status": "mapped",
        }
        edge_by_id = {"E003": {"id": "E003", "from_node_id": "N001", "to_node_id": "N002", "start": [0.0, 0.0], "end": [100.0, 0.0]}}
        self.assertTrue(_same_directed_contour(first, second, edge_by_id))

    def test_small_overlapping_parallel_labels_remain_not_overlap(self) -> None:
        first = {
            "edge_id": "E003",
            "value": 748,
            "offset_px": 0.0,
            "dimension_stroke": {"start": [0.0, 0.0], "end": [80.0, 0.0], "merged_indices": [1]},
            "status": "mapped",
        }
        second = {
            "edge_id": "E003",
            "value": 2748,
            "offset_px": 18.0,
            "dimension_stroke": {"start": [0.0, 18.0], "end": [120.0, 18.0], "merged_indices": [2]},
            "status": "mapped",
        }
        edge_by_id = {"E003": {"id": "E003", "from_node_id": "N001", "to_node_id": "N002", "start": [0.0, 0.0], "end": [120.0, 0.0]}}
        self.assertFalse(_same_directed_contour(first, second, edge_by_id))

    def test_preprocess_annotation_pdf_marks_cross_sheet_and_handwheel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_pdf = tmp_path / "sample.pdf"
            output_pdf = tmp_path / "preprocess.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=200)
                page.draw_line((30, 100), (170, 100), color=(0.1, 0.4, 0.9), width=2)
                doc.save(source_pdf)

            mapping = {
                "edges": [{"id": "E001", "start": [30, 100], "end": [170, 100]}],
                "vertices": [{"id": "V01", "x": 30, "y": 100, "role": "endpoint"}],
                "dimensions": [
                    {
                        "id": "D001",
                        "text": "СМ. ЛИСТ 12",
                        "hints": ["cross_sheet_reference"],
                        "status": "cross_sheet_reference",
                        "label_center": [70, 70],
                        "dimension_stroke": {"start": [68, 80], "end": [112, 80]},
                        "leader_stroke": {"start": [70, 70], "end": [112, 80]},
                        "valid": False,
                    },
                    {
                        "id": "D002",
                        "text": "Штурвал",
                        "hints": ["handwheel"],
                        "status": "handwheel",
                        "label_center": [120, 60],
                        "dimension_stroke": {"start": [120, 70], "end": [150, 70]},
                        "leader_stroke": {"start": [120, 60], "end": [150, 70]},
                        "valid": False,
                    },
                ],
            }

            save_preprocess_annotation_pdf(source_pdf, 1, output_pdf, mapping)

            self.assertTrue(output_pdf.exists())
            self.assertGreater(output_pdf.stat().st_size, 0)

    def test_clean_local_markup_pdf_only_draws_vertices_and_dimension_boxes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_pdf = tmp_path / "sample.pdf"
            output_pdf = tmp_path / "clean_local_markup.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=200)
                page.draw_line((30, 100), (170, 100), color=(0.1, 0.4, 0.9), width=2)
                doc.save(source_pdf)

            mapping = {
                "vertices": [{"id": "V01", "x": 30, "y": 100, "role": "endpoint"}],
                "dimensions": [
                    {
                        "id": "D001",
                        "text": "3100",
                        "status": "mapped",
                        "label_center": [90, 70],
                        "valid": True,
                    },
                    {
                        "id": "D002",
                        "text": "Штурвал",
                        "status": "handwheel",
                        "label_center": [120, 60],
                        "valid": False,
                    },
                ],
            }

            save_clean_local_markup_pdf(source_pdf, 1, output_pdf, mapping)

            self.assertTrue(output_pdf.exists())
            self.assertGreater(output_pdf.stat().st_size, 0)
            with fitz.open(str(output_pdf)) as doc:
                page = doc[0]
                self.assertGreater(len(page.get_drawings()), 0)

    def test_clean_local_markup_pdf_marks_connection_boxes_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_pdf = tmp_path / "sample.pdf"
            output_pdf = tmp_path / "clean_local_markup.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=200)
                page.draw_line((30, 100), (170, 100), color=(0.1, 0.4, 0.9), width=2)
                doc.save(source_pdf)

            mapping = {
                "vertices": [],
                "dimensions": [],
                "connections": [
                    {
                        "id": "CN-1",
                        "label": "ПОДКЛЮЧЕНИЕ V-505",
                        "bbox": [20, 30, 110, 50],
                        "connection_type": "tie_in",
                    }
                ],
            }

            save_clean_local_markup_pdf(source_pdf, 1, output_pdf, mapping)

            self.assertTrue(output_pdf.exists())
            with fitz.open(str(output_pdf)) as doc:
                page = doc[0]
                self.assertGreater(len(page.get_drawings()), 0)

    def test_clean_local_markup_pdf_marks_shtrval_text_found_on_page(self) -> None:
        rects = _handwheel_text_rects([
            (40, 60, 100, 80, "ШТУРВАЛ", 0, 0, 0),
            (10, 10, 20, 20, "3100", 1, 1, 0),
        ])
        self.assertEqual(rects, [("ШТУРВАЛ", fitz.Rect(40, 60, 100, 80))])

    def test_handwheel_text_rect_contains_complete_same_line_label(self) -> None:
        rows = _handwheel_text_rects([
            (40, 60, 100, 80, "ШТУРВАЛ", 2, 3, 0),
            (104, 60, 140, 80, "EAST", 2, 3, 1),
            (10, 10, 20, 20, "3100", 4, 1, 0),
        ])

        self.assertEqual(rows, [("ШТУРВАЛ EAST", fitz.Rect(40, 60, 140, 80))])

    def test_handwheel_arrow_segment_starts_near_label(self) -> None:
        rects = [fitz.Rect(40, 60, 100, 80)]
        drawings = [{"items": [
            ("l", fitz.Point(160, 120), fitz.Point(100, 70)),
            ("l", fitz.Point(160, 120), fitz.Point(145, 105)),
            ("l", fitz.Point(160, 120), fitz.Point(150, 125)),
        ]}]

        segments = _handwheel_arrow_segments(rects, drawings)

        self.assertEqual(segments, [((100.0, 70.0), (160.0, 120.0))])

    def test_handwheel_plain_line_is_not_an_arrow(self) -> None:
        rects = [fitz.Rect(40, 60, 100, 80)]
        drawings = [{"items": [("l", fitz.Point(150, 100), fitz.Point(100, 70))]}]

        self.assertEqual(_handwheel_arrow_segments(rects, drawings), [])

    def test_rectangle_side_is_not_an_arrow_segment(self) -> None:
        rectangles = [fitz.Rect(40, 40, 80, 70)]

        self.assertTrue(_is_rectangle_side((40, 40), (40, 70), rectangles))
        self.assertFalse(_is_rectangle_side((80, 70), (100, 90), rectangles))

    def test_handwheel_distant_neighboring_v_is_not_an_arrow(self) -> None:
        rects = [fitz.Rect(40, 60, 100, 80)]
        drawings = [{"items": [
            ("l", fitz.Point(130, 70), fitz.Point(180, 120)),
            ("l", fitz.Point(180, 120), fitz.Point(165, 112)),
            ("l", fitz.Point(180, 120), fitz.Point(170, 135)),
        ]}]

        self.assertEqual(_handwheel_arrow_segments(rects, drawings), [])

    def test_handwheel_narrow_realistic_arrowhead_is_detected(self) -> None:
        rects = [fitz.Rect(40, 60, 100, 80)]
        drawings = [{"items": [
            ("l", fitz.Point(120, 70), fitz.Point(160, 120)),
            ("l", fitz.Point(160, 120), fitz.Point(155, 115)),
            ("l", fitz.Point(157, 114), fitz.Point(160, 120)),
        ]}]

        self.assertEqual(_handwheel_arrow_segments(rects, drawings), [((120.0, 70.0), (160.0, 120.0))])

    def test_handwheel_details_report_arrow_and_edge(self) -> None:
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=200)
            page_type = type(page)
            mapping = {"edges": [{"id": "E001", "start": [160, 120], "end": [260, 120]}]}
            with mock.patch.object(page_type, "get_text", return_value=[(40, 60, 100, 80, "ШТУРВАЛ", 0, 0, 0)]), mock.patch.object(
                page_type,
                "get_drawings",
                return_value=[{"items": [
                    ("l", fitz.Point(160, 120), fitz.Point(100, 70)),
                    ("l", fitz.Point(160, 120), fitz.Point(145, 105)),
                    ("l", fitz.Point(160, 120), fitz.Point(150, 125)),
                ]}],
            ):
                details = _handwheel_details(page, mapping)

        self.assertEqual(details[0]["arrow_found"], True)
        self.assertEqual(details[0]["edge_id"], "E001")

    def test_handwheel_without_pipe_edge_gets_non_pipe_attachment_edge(self) -> None:
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=200)
            page_type = type(page)
            with mock.patch.object(page_type, "get_text", return_value=[(40, 60, 100, 80, "ШТУРВАЛ", 0, 0, 0)]), mock.patch.object(
                page_type,
                "get_drawings",
                return_value=[{"items": [
                    ("l", fitz.Point(160, 120), fitz.Point(100, 70)),
                    ("l", fitz.Point(160, 120), fitz.Point(145, 105)),
                    ("l", fitz.Point(160, 120), fitz.Point(150, 125)),
                ]}],
            ):
                details = _handwheel_details(page, {"edges": []})

        self.assertEqual(details[0]["edge_id"], "HW_EDGE_001")
        self.assertTrue(details[0]["edge_created"])
        self.assertFalse(details[0]["is_pipe_edge"])

    def test_clean_markup_uses_visible_blue_arrow_overlay(self) -> None:
        source = Path('src/dimension_mapping.py').read_text(encoding='utf-8')
        self.assertIn('segments[0]', source)
        self.assertIn('width=1.6', source)

    def test_clean_markup_uses_first_third_of_arrow_segment(self) -> None:
        start, end = _first_arrow_third({"start": [100.0, 70.0], "end": [160.0, 120.0]})

        self.assertEqual(start, [100.0, 70.0])
        self.assertEqual(end, [120.0, 86.67])

    def test_review_map_with_provider_accepts_single_page_markup_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            generated_pdf = tmp_path / "clean_page2.pdf"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=200)
                page.draw_line((10, 10), (110, 10), color=(0.1, 0.4, 0.9), width=2)
                doc.save(generated_pdf)

            from src.dimension_review import review_map_with_provider

            class DummyResponse:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"choices": [{"message": {"content": "```json\n{\"candidate_decisions\": []}\n```"}}]}

            with mock.patch("src.dimension_review.requests.post", return_value=DummyResponse()) as mocked_post:
                result = review_map_with_provider(
                    {"page": 2, "vertices": [], "uncertain_vertices": [], "edges": [], "dimensions": [], "edge_candidate_groups": [], "map_text": "test"},
                    "test map",
                    "fake-key",
                    "test-model",
                    pdf_path=generated_pdf,
                    page_number=2,
                )
                self.assertIn("answer", result)
                body = mocked_post.call_args.kwargs["json"]["messages"][1]["content"]
                self.assertIsInstance(body, list)
                self.assertEqual(body[1]["type"], "image_url")
                self.assertIn("data:image/png;base64,", body[1]["image_url"]["url"])


if __name__ == "__main__":
    unittest.main()
