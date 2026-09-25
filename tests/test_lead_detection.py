from types import SimpleNamespace
from unittest import mock

import fitz

from src.dimension_mapping import (
    _coordinate_lead_rows,
    _leader_path_from_rect,
    attach_coordinate_leads,
)


def _seg(start, end):
    return (start, end)


def test_leader_path_follows_multi_segment_polyline_to_arrowhead():
    rect = fitz.Rect(0, 0, 10, 10)
    segments = [
        _seg((10.0, 5.0), (40.0, 5.0)),
        _seg((40.0, 5.0), (40.0, 40.0)),
        # arrowhead legs at the tip (40, 40)
        _seg((40.0, 40.0), (33.0, 37.0)),
        _seg((40.0, 40.0), (37.0, 33.0)),
    ]

    path, arrow = _leader_path_from_rect(rect, segments)

    assert arrow is True
    assert path[0][0] == (10.0, 5.0)
    assert path[-1][1] == (40.0, 40.0)
    assert len(path) >= 2


def test_leader_path_returns_plain_multi_segment_leader_without_arrowhead():
    rect = fitz.Rect(0, 0, 10, 10)
    segments = [
        _seg((10.0, 5.0), (35.0, 5.0)),
        _seg((35.0, 5.0), (35.0, 30.0)),
        _seg((35.0, 30.0), (60.0, 30.0)),
    ]

    path, arrow = _leader_path_from_rect(rect, segments)

    assert arrow is False
    assert len(path) == 3
    assert path[0][0] == (10.0, 5.0)
    assert path[-1][1] == (60.0, 30.0)


def test_leader_path_ignores_lines_far_from_label():
    rect = fitz.Rect(0, 0, 10, 10)
    segments = [_seg((100.0, 100.0), (140.0, 140.0))]

    path, arrow = _leader_path_from_rect(rect, segments)

    assert path == []
    assert arrow is False


def test_coordinate_lead_rows_bind_label_to_arrow_and_keep_missing():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(60.0, 40.0), fitz.Point(120.0, 90.0)),
        ("l", fitz.Point(120.0, 90.0), fitz.Point(110.0, 80.0)),
        ("l", fitz.Point(120.0, 90.0), fitz.Point(112.0, 100.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "226150", "label_bbox": [10, 30, 30, 40], "value_bbox": [32, 30, 58, 40]},
            {"id": "C002", "label": "Y", "value": "82500", "label_bbox": [300, 300, 320, 310], "value_bbox": [322, 300, 348, 310]},
        ],
        "vertices": [{"id": "V01", "x": 120.0, "y": 90.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert mapping["coordinate_leads"] is rows
    assert rows[0]["id"] == "C001"
    assert rows[0]["arrow_found"] is True
    assert rows[0]["arrow_has_arrowhead"] is True
    assert rows[0]["arrow_end"] == [120.0, 90.0]
    assert rows[0]["matched_vertex_id"] == "V01"
    assert len(rows[0]["arrow_segments"]) == 1
    assert rows[1]["id"] == "C002"
    assert rows[1]["arrow_found"] is False


def test_coordinate_lead_uses_upper_coordinate_block_designation_as_start_area():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (20.0, 20.0, 50.0, 30.0, "CM.", 7, 0, 0),
        (20.0, 32.0, 82.0, 42.0, "4137-14", 7, 1, 0),
        (20.0, 44.0, 28.0, 54.0, "X", 7, 2, 0),
        (32.0, 44.0, 64.0, 54.0, "226150", 7, 2, 1),
        (20.0, 56.0, 28.0, 66.0, "Y", 7, 3, 0),
        (32.0, 56.0, 64.0, 66.0, "82500", 7, 3, 1),
        (20.0, 68.0, 32.0, 78.0, "Z+", 7, 4, 0),
        (36.0, 68.0, 64.0, 78.0, "2804", 7, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(78.0, 31.0), fitz.Point(130.0, 90.0)),
        ("l", fitz.Point(130.0, 90.0), fitz.Point(120.0, 84.0)),
        ("l", fitz.Point(130.0, 90.0), fitz.Point(125.0, 79.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "226150", "label_bbox": [20, 44, 28, 54], "value_bbox": [32, 44, 64, 54]},
            {"id": "C002", "label": "Y", "value": "82500", "label_bbox": [20, 56, 28, 66], "value_bbox": [32, 56, 64, 66]},
        ],
        "vertices": [{"id": "VE-03", "x": 130.0, "y": 90.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is True
    assert rows[0]["lead_search_source"] == "coordinate_callout_block"
    assert rows[0]["arrow_start"] == [78.0, 31.0]
    assert rows[0]["matched_vertex_id"] == "VE-03"
    assert rows[0]["matched_vertex_point"] == [130.0, 90.0]
    assert rows[1]["arrow_found"] is True
    assert rows[1]["matched_vertex_id"] == "VE-03"


def test_coordinate_lead_pulls_upper_reference_from_separate_text_block():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (18.0, 8.0, 44.0, 18.0, "П14Б7", 9, 0, 0),
        (46.0, 8.0, 82.0, 18.0, "C0-0039", 9, 0, 1),
        (20.0, 44.0, 28.0, 54.0, "X", 7, 2, 0),
        (32.0, 44.0, 64.0, 54.0, "226150", 7, 2, 1),
        (20.0, 56.0, 28.0, 66.0, "Y", 7, 3, 0),
        (32.0, 56.0, 64.0, 66.0, "82500", 7, 3, 1),
        (20.0, 68.0, 32.0, 78.0, "Z+", 7, 4, 0),
        (36.0, 68.0, 64.0, 78.0, "2804", 7, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(84.0, 13.0), fitz.Point(140.0, 88.0)),
        ("l", fitz.Point(140.0, 88.0), fitz.Point(128.0, 84.0)),
        ("l", fitz.Point(140.0, 88.0), fitz.Point(134.0, 77.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "226150", "label_bbox": [20, 44, 28, 54], "value_bbox": [32, 44, 64, 54]},
            {"id": "C002", "label": "Y", "value": "82500", "label_bbox": [20, 56, 28, 66], "value_bbox": [32, 56, 64, 66]},
            {"id": "C003", "label": "Z+", "value": "2804", "label_bbox": [20, 68, 32, 78], "value_bbox": [36, 68, 64, 78]},
        ],
        "vertices": [{"id": "HG-01-A", "x": 140.0, "y": 88.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert all(row["arrow_found"] for row in rows)
    assert all(row["matched_vertex_id"] == "HG-01-A" for row in rows)
    assert rows[0]["arrow_start"] == [84.0, 13.0]
    assert rows[0]["coordinate_block_refs"] == ["П14Б7", "C0-0039"]
    assert rows[2]["coordinate_block_refs"] == ["П14Б7", "C0-0039"]


def test_coordinate_lead_accepts_start_in_blank_gap_right_of_callout_block():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (20.0, 20.0, 76.0, 30.0, "4137-14", 9, 0, 0),
        (20.0, 44.0, 28.0, 54.0, "X", 7, 2, 0),
        (32.0, 44.0, 64.0, 54.0, "24050", 7, 2, 1),
        (20.0, 56.0, 28.0, 66.0, "Y", 7, 3, 0),
        (32.0, 56.0, 64.0, 66.0, "86474", 7, 3, 1),
        (20.0, 68.0, 32.0, 78.0, "Z+", 7, 4, 0),
        (36.0, 68.0, 64.0, 78.0, "22863", 7, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(102.0, 34.0), fitz.Point(150.0, 90.0)),
        ("l", fitz.Point(150.0, 90.0), fitz.Point(139.0, 85.0)),
        ("l", fitz.Point(150.0, 90.0), fitz.Point(144.0, 79.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "24050", "label_bbox": [20, 44, 28, 54], "value_bbox": [32, 44, 64, 54]},
            {"id": "C002", "label": "Y", "value": "86474", "label_bbox": [20, 56, 28, 66], "value_bbox": [32, 56, 64, 66]},
            {"id": "C003", "label": "Z+", "value": "22863", "label_bbox": [20, 68, 32, 78], "value_bbox": [36, 68, 64, 78]},
        ],
        "vertices": [{"id": "V02", "x": 150.0, "y": 90.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is True
    assert rows[0]["matched_vertex_id"] == "V02"
    assert rows[0]["arrow_start"] == [102.0, 34.0]


def test_coordinate_lead_without_upper_reference_uses_compact_coordinate_value_block():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (20.0, 44.0, 28.0, 54.0, "X", 7, 2, 0),
        (32.0, 44.0, 64.0, 54.0, "23650", 7, 2, 1),
        (20.0, 56.0, 28.0, 66.0, "Y", 7, 3, 0),
        (32.0, 56.0, 64.0, 66.0, "85600", 7, 3, 1),
        (20.0, 68.0, 32.0, 78.0, "Z+", 7, 4, 0),
        (36.0, 68.0, 64.0, 78.0, "22341", 7, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(86.0, 73.0), fitz.Point(130.0, 98.0)),
        ("l", fitz.Point(130.0, 98.0), fitz.Point(118.0, 96.0)),
        ("l", fitz.Point(130.0, 98.0), fitz.Point(123.0, 88.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "23650", "label_bbox": [20, 44, 28, 54], "value_bbox": [32, 44, 64, 54]},
            {"id": "C002", "label": "Y", "value": "85600", "label_bbox": [20, 56, 28, 66], "value_bbox": [32, 56, 64, 66]},
            {"id": "C003", "label": "Z+", "value": "22341", "label_bbox": [20, 68, 32, 78], "value_bbox": [36, 68, 64, 78]},
        ],
        "vertices": [{"id": "V02", "x": 130.0, "y": 98.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is True
    assert rows[0]["lead_search_source"] == "coordinate_value_block"
    assert rows[0]["coordinate_block_refs"] == []
    assert rows[0]["matched_vertex_id"] == "V02"


def test_standalone_lower_coordinate_does_not_pull_numeric_values_above_as_refs():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (20.0, 20.0, 28.0, 30.0, "X", 7, 0, 0),
        (32.0, 20.0, 64.0, 30.0, "23650", 7, 0, 1),
        (20.0, 32.0, 28.0, 42.0, "Y", 7, 1, 0),
        (32.0, 32.0, 64.0, 42.0, "85600", 7, 1, 1),
        (20.0, 44.0, 32.0, 54.0, "Z+", 7, 2, 0),
        (36.0, 44.0, 64.0, 54.0, "22341", 7, 2, 1),
        (74.0, 68.0, 86.0, 78.0, "Z+", 8, 3, 0),
        (90.0, 68.0, 118.0, 78.0, "22341", 8, 3, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(130.0, 73.0), fitz.Point(165.0, 90.0)),
        ("l", fitz.Point(165.0, 90.0), fitz.Point(155.0, 88.0)),
        ("l", fitz.Point(165.0, 90.0), fitz.Point(159.0, 82.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "23650", "label_bbox": [20, 20, 28, 30], "value_bbox": [32, 20, 64, 30]},
            {"id": "C002", "label": "Y", "value": "85600", "label_bbox": [20, 32, 28, 42], "value_bbox": [32, 32, 64, 42]},
            {"id": "C003", "label": "Z+", "value": "22341", "label_bbox": [20, 44, 32, 54], "value_bbox": [36, 44, 64, 54]},
            {"id": "C004", "label": "Z+", "value": "22341", "label_bbox": [74, 68, 86, 78], "value_bbox": [90, 68, 118, 78]},
        ],
        "vertices": [{"id": "V07", "x": 165.0, "y": 90.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[3]["arrow_found"] is True
    assert rows[3]["lead_search_source"] == "coordinate_value_block"
    assert rows[3]["coordinate_block_refs"] == []
    assert rows[3]["matched_vertex_id"] == "V07"


def test_coordinate_blocks_are_built_before_refs_are_attached():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (20.0, 8.0, 70.0, 18.0, "4137-14", 9, 0, 0),
        (20.0, 32.0, 28.0, 42.0, "X", 7, 1, 0),
        (32.0, 32.0, 64.0, 42.0, "23650", 7, 1, 1),
        (20.0, 44.0, 28.0, 54.0, "Y", 7, 2, 0),
        (32.0, 44.0, 64.0, 54.0, "85600", 7, 2, 1),
        (20.0, 56.0, 32.0, 66.0, "Z+", 7, 3, 0),
        (36.0, 56.0, 64.0, 66.0, "22341", 7, 3, 1),
        # Separate coordinate block nearby; it must not inherit the ref above
        # the left X/Y/Z block.
        (86.0, 78.0, 98.0, 88.0, "Z+", 8, 4, 0),
        (102.0, 78.0, 130.0, 88.0, "22341", 8, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(142.0, 83.0), fitz.Point(172.0, 100.0)),
        ("l", fitz.Point(172.0, 100.0), fitz.Point(163.0, 97.0)),
        ("l", fitz.Point(172.0, 100.0), fitz.Point(167.0, 92.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "23650", "label_bbox": [20, 32, 28, 42], "value_bbox": [32, 32, 64, 42]},
            {"id": "C002", "label": "Y", "value": "85600", "label_bbox": [20, 44, 28, 54], "value_bbox": [32, 44, 64, 54]},
            {"id": "C003", "label": "Z+", "value": "22341", "label_bbox": [20, 56, 32, 66], "value_bbox": [36, 56, 64, 66]},
            {"id": "C004", "label": "Z+", "value": "22341", "label_bbox": [86, 78, 98, 88], "value_bbox": [102, 78, 130, 88]},
        ],
        "vertices": [{"id": "V07", "x": 172.0, "y": 100.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["coordinate_block_refs"] == ["4137-14"]
    assert rows[3]["coordinate_block_refs"] == []
    assert rows[3]["lead_search_source"] == "coordinate_value_block"
    assert rows[3]["arrow_found"] is True
    assert rows[3]["matched_vertex_id"] == "V07"


def test_coordinate_lead_can_start_outside_visual_search_box_when_nearest_to_center():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (20.0, 20.0, 50.0, 30.0, "CM.", 7, 0, 0),
        (20.0, 32.0, 76.0, 42.0, "4137-14", 7, 1, 0),
        (20.0, 50.0, 28.0, 60.0, "X", 7, 2, 0),
        (32.0, 50.0, 64.0, 60.0, "24050", 7, 2, 1),
        (20.0, 62.0, 28.0, 72.0, "Y", 7, 3, 0),
        (32.0, 62.0, 64.0, 72.0, "86474", 7, 3, 1),
        (20.0, 74.0, 32.0, 84.0, "Z+", 7, 4, 0),
        (36.0, 74.0, 64.0, 84.0, "22863", 7, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        # Starts outside the visual bbox extension, but is still the closest
        # proven leader from the coordinate block center to a vertex.
        ("l", fitz.Point(122.0, 44.0), fitz.Point(170.0, 94.0)),
        ("l", fitz.Point(170.0, 94.0), fitz.Point(159.0, 90.0)),
        ("l", fitz.Point(170.0, 94.0), fitz.Point(164.0, 84.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "24050", "label_bbox": [20, 50, 28, 60], "value_bbox": [32, 50, 64, 60]},
            {"id": "C002", "label": "Y", "value": "86474", "label_bbox": [20, 62, 28, 72], "value_bbox": [32, 62, 64, 72]},
            {"id": "C003", "label": "Z+", "value": "22863", "label_bbox": [20, 74, 32, 84], "value_bbox": [36, 74, 64, 84]},
        ],
        "vertices": [{"id": "V05", "x": 170.0, "y": 94.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is True
    assert rows[0]["matched_vertex_id"] == "V05"
    assert rows[0]["arrow_start"] == [122.0, 44.0]
    assert rows[0]["lead_search_center"]


def test_coordinate_lead_checks_alternative_two_sector_paths_to_vertex():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_text = lambda kind: [
        (18.0, 8.0, 76.0, 18.0, "4137-14", 9, 0, 0),
        (20.0, 44.0, 28.0, 54.0, "X", 7, 2, 0),
        (32.0, 44.0, 64.0, 54.0, "226150", 7, 2, 1),
        (20.0, 56.0, 28.0, 66.0, "Y", 7, 3, 0),
        (32.0, 56.0, 64.0, 66.0, "82500", 7, 3, 1),
        (20.0, 68.0, 32.0, 78.0, "Z+", 7, 4, 0),
        (36.0, 68.0, 64.0, 78.0, "2804", 7, 4, 1),
    ] if kind == "words" else []
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(78.0, 13.0), fitz.Point(104.0, 44.0)),
        # Wrong continuation is farther from the block center and used to win
        # in the old greedy chain builder.
        ("l", fitz.Point(104.0, 44.0), fitz.Point(160.0, 44.0)),
        # Correct continuation reaches the vertex.
        ("l", fitz.Point(104.0, 44.0), fitz.Point(130.0, 90.0)),
        ("l", fitz.Point(130.0, 90.0), fitz.Point(119.0, 85.0)),
        ("l", fitz.Point(130.0, 90.0), fitz.Point(125.0, 79.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "226150", "label_bbox": [20, 44, 28, 54], "value_bbox": [32, 44, 64, 54]},
            {"id": "C002", "label": "Y", "value": "82500", "label_bbox": [20, 56, 28, 66], "value_bbox": [32, 56, 64, 66]},
            {"id": "C003", "label": "Z+", "value": "2804", "label_bbox": [20, 68, 32, 78], "value_bbox": [36, 68, 64, 78]},
        ],
        "vertices": [{"id": "VE-09", "x": 130.0, "y": 90.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is True
    assert rows[0]["arrow_has_arrowhead"] is True
    assert rows[0]["matched_vertex_id"] == "VE-09"
    assert rows[0]["arrow_end"] == [130.0, 90.0]
    assert rows[0]["checked_start_count"] > 0
    assert rows[0]["checked_path_count"] > 0


def test_coordinate_lead_skips_single_sector_without_arrowhead_even_if_it_ends_near_vertex():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(10.0, 5.0), fitz.Point(50.0, 5.0)),
        ("l", fitz.Point(10.0, 8.0), fitz.Point(30.0, 60.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "1", "label_bbox": [0, 0, 10, 10], "value_bbox": [12, 0, 20, 10]},
        ],
        "vertices": [{"id": "V02", "x": 32.0, "y": 62.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is False
    assert rows[0]["matched_vertex_id"] is None


def test_coordinate_lead_uses_multi_segment_chain_to_reach_vertex():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(10.0, 5.0), fitz.Point(60.0, 5.0)),
        ("l", fitz.Point(60.0, 5.0), fitz.Point(60.0, 40.0)),
        ("l", fitz.Point(60.0, 40.0), fitz.Point(53.0, 37.0)),
        ("l", fitz.Point(60.0, 40.0), fitz.Point(57.0, 33.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "Z+", "value": "1700", "label_bbox": [0, 0, 10, 10], "value_bbox": [12, 0, 22, 10]},
        ],
        "vertices": [{"id": "V07", "x": 60.0, "y": 41.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is True
    assert len(rows[0]["arrow_segments"]) == 2
    assert rows[0]["matched_vertex_id"] == "V07"
    assert rows[0]["arrow_end"] == [60.0, 40.0]


def test_coordinate_lead_skips_multi_segment_without_arrowhead():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(10.0, 5.0), fitz.Point(60.0, 5.0)),
        ("l", fitz.Point(60.0, 5.0), fitz.Point(60.0, 40.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "Z+", "value": "1700", "label_bbox": [0, 0, 10, 10], "value_bbox": [12, 0, 22, 10]},
        ],
        "vertices": [{"id": "V07", "x": 60.0, "y": 41.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is False


def test_coordinate_lead_ignores_segments_lying_on_pipe():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 500, 500))
    page.get_drawings = lambda: [{"items": [
        ("l", fitz.Point(10.0, 5.0), fitz.Point(200.0, 5.0)),
    ]}]
    mapping = {
        "coordinates": [
            {"id": "C001", "label": "X", "value": "1", "label_bbox": [0, 0, 10, 10], "value_bbox": [12, 0, 20, 10]},
        ],
        "edges": [{"id": "E001", "start": [10.0, 5.0], "end": [200.0, 5.0]}],
        "vertices": [{"id": "V01", "x": 200.0, "y": 5.0}],
    }

    with mock.patch("src.dimension_mapping.mark_pipeline.find_rectangles", return_value=[]):
        rows = attach_coordinate_leads(page, mapping)

    assert rows[0]["arrow_found"] is False


def test_coordinate_lead_rows_handles_no_coordinates():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 100, 100))
    page.get_drawings = lambda: []
    mapping = {"coordinates": []}

    rows = _coordinate_lead_rows(page, mapping)

    assert rows == []
    assert mapping["coordinate_leads"] == []
