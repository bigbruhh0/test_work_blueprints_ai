import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from src.dimension_mapping import _dimension_hints_from_text, _handwheel_arrow_segments, _handwheel_text_rects, _same_directed_contour, save_clean_local_markup_pdf, save_preprocess_annotation_pdf


class DimensionRuleTests(unittest.TestCase):
    def test_dimension_review_prompt_uses_deterministic_overlap_rules(self) -> None:
        text = Path("mark_pipeline_test/dimension_review_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("existing_mapping.valid=false", text)
        self.assertIn("последовательно разделена стрелками", text.lower())
        self.assertIn("не считай соседние значения перекрытием", text.lower())

    def test_dimension_review_prompt_covers_handwheel_and_cross_sheet(self) -> None:
        text = Path("mark_pipeline_test/dimension_review_prompt.txt").read_text(encoding="utf-8")
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

    def test_clean_local_markup_pdf_marks_shtrval_text_found_on_page(self) -> None:
        rects = _handwheel_text_rects([
            (40, 60, 100, 80, "ШТУРВАЛ", 0, 0, 0),
            (10, 10, 20, 20, "3100", 0, 0, 1),
        ])
        self.assertEqual(rects, [fitz.Rect(40, 60, 100, 80)])

    def test_handwheel_arrow_segment_starts_near_label(self) -> None:
        rects = [fitz.Rect(40, 60, 100, 80)]
        drawings = [{"items": [("l", fitz.Point(150, 100), fitz.Point(100, 70))]}]

        segments = _handwheel_arrow_segments(rects, drawings)

        self.assertEqual(segments, [((100.0, 70.0), (150.0, 100.0))])

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
