import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fitz

from src.dimension_mapping import _dimension_hints_from_text, _first_arrow_third, _handwheel_arrow_segments, _handwheel_details, _handwheel_text_rects, _is_rectangle_side, _same_directed_contour, apply_local_dimension_filter, save_clean_local_markup_pdf, save_local_dimension_filter_pdf, save_preprocess_annotation_pdf
from src.dimension_mapping import _direct_dimension_stroke_from_label, _fallback_leader_stroke_from_label, _find_extension_strokes, _merge_dimension_stroke, _resolve_leader_target
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
