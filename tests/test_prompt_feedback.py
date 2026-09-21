import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
from src import db, prompts
from src.ai_client import _find_connection_rows
from src.dimension_review import review_map_with_provider
from src.models import Candidate, VertexMark


class PromptFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        registry = {}
        for name in ("analyze", "dimension_review"):
            default = root / (name + ".txt")
            default.write_text("default " + name, encoding="utf-8")
            registry[name] = {"title": name, "default_file": str(default)}
        for patcher in (
            mock.patch.object(prompts, "OVERRIDE_DIR", root / "prompts"),
            mock.patch.object(prompts, "PROMPT_REGISTRY", registry),
            mock.patch.object(db, "DB_PATH", root / "runs.db"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        connections = []
        connect = db._connect

        def tracked_connect():
            connection = connect()
            connections.append(connection)
            return connection

        connection_patch = mock.patch.object(db, "_connect", side_effect=tracked_connect)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        self.addCleanup(lambda: [connection.close() for connection in connections])
        db.init_db()
        prompts.save_prompt("dimension_review", "version zero")
        prompts.save_prompt("dimension_review", "version one")

    def feedback(self, candidate, rating, text, version=None, name="dimension_review"):
        return db.save_feedback({
            "run_id": "test-run", "line_id": "test-line", "page_number": 1,
            "candidate_id": candidate, "rating": rating, "prompt_name": name,
            "prompt_version": version,
            "prompt_sha256": hashlib.sha256(text.encode()).hexdigest() if version is not None else None,
            "context": {"prompt": text},
        })

    def test_switch_changes_current_counts_not_version_rows_or_total(self):
        self.feedback("D1", "up", "version zero", version=0)
        self.feedback("D2", "down", "version one", version=1)
        self.feedback("D3", "up", "version one")
        self.feedback("D4", "up", "unknown text")
        self.feedback("D5", "down", "another prompt", name="analyze")
        before_rows = server.get_prompt("dimension_review")["versions"]
        expected_rows = {row["version"]: row["feedback"] for row in before_rows}
        for version, up, down in ((0, 1, 0), (1, 1, 1), (0, 1, 0)):
            detail = server.restore_prompt_version("dimension_review", server.RestoreBody(version=version))
            catalog = next(row for row in server.list_prompts() if row["name"] == "dimension_review")
            self.assertEqual(detail["version"], version)
            self.assertEqual(detail["active_feedback"], {"positive": up, "negative": down, "total": up + down})
            self.assertEqual(detail["feedback"], catalog["feedback"])
            self.assertEqual(detail["active_feedback"], catalog["active_feedback"])
            self.assertEqual(detail["prompt_feedback"], {"positive": 3, "negative": 1, "total": 4})
            self.assertEqual(detail["prompt_feedback"], catalog["prompt_feedback"])
            self.assertEqual(detail["unversioned_feedback"]["total"], 1)
            self.assertEqual({r["version"]: r["feedback"] for r in detail["versions"]}, expected_rows)
            current = [row for row in detail["versions"] if row["is_active"]]
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["feedback"], detail["active_feedback"])

    def test_list_prompts_reports_active_version_metadata(self):
        catalog = server.list_prompts()
        dimension = next(row for row in catalog if row["name"] == "dimension_review")
        self.assertEqual(dimension["active_version"], 1)
        self.assertIn("active_sha256", dimension)
        self.assertEqual(dimension["active_feedback"]["total"], 0)

    def test_identical_texts_preserve_selected_version_and_separate_counts(self):
        prompts.save_prompt("dimension_review", "version zero")  # v2, same text as v0
        self.feedback("D1", "up", "version zero", version=0)
        self.feedback("D2", "down", "version zero", version=2)
        self.feedback("D3", "up", "version zero")  # Cannot infer v0 vs v2.
        for version in (0, 2, 0):
            detail = server.restore_prompt_version("dimension_review", server.RestoreBody(version=version))
            self.assertEqual(detail["version"], version)
            self.assertEqual(detail["active_feedback"]["positive"], int(version == 0))
            self.assertEqual(detail["active_feedback"]["negative"], int(version == 2))
            self.assertEqual(detail["unversioned_feedback"]["total"], 1)
            self.assertEqual(detail["prompt_feedback"]["total"], 3)

    def test_legacy_recovery_does_not_modify_saved_feedback(self):
        self.feedback("D1", "up", "version one")
        self.assertEqual(server.get_prompt("dimension_review")["active_feedback"]["total"], 1)
        saved = db.feedback_rows()[0]
        self.assertIsNone(saved["prompt_version"])
        self.assertIsNone(saved["prompt_sha256"])

    def test_rating_old_result_after_switch_uses_request_revision_including_zero(self):
        prompts.restore_version("dimension_review", 0)
        revision = prompts.load_prompt_revision("dimension_review")
        page = {
            "page_number": 1, "prompt_revision": revision,
            "provider_trace": {"prompt": revision["text"]},
            "analysis": {
                "dimension_mapping": {"dimensions": [{"id": "D1"}]},
                "dimension_review": {"answer": {"candidate_decisions": [{"candidate_id": "D1"}]}},
            },
        }
        prompts.restore_version("dimension_review", 1)
        run = {"lines": [{"line_id": "test-line", "page_results": [page]}]}
        with mock.patch.object(server, "get_run", return_value=run):
            saved = server.save_analysis_feedback(server.FeedbackBody(
                run_id="test-run", line_id="test-line", page_number=1, candidate_id="D1", rating="up",
            ))
        self.assertEqual(saved["prompt_version"], "0")
        self.assertEqual(saved["prompt_sha256"], revision["sha256"])
        detail = server.get_prompt("dimension_review")
        self.assertEqual(detail["active_feedback"]["total"], 0)
        self.assertEqual(detail["versions"][0]["feedback"]["total"], 1)

    def test_provider_records_revision_used_before_request(self):
        prompts.restore_version("dimension_review", 0)
        response = mock.Mock()
        response.json.return_value = {"choices": [{"message": {"content": '{"candidate_decisions": []}'}}]}

        def provider_reply(*args, **kwargs):
            prompts.restore_version("dimension_review", 1)
            return response

        with mock.patch("src.dimension_review.requests.post", side_effect=provider_reply) as post:
            trace = review_map_with_provider({}, "map", "test-key", "test-model")
        sent_text = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual(trace["prompt"], sent_text)
        self.assertEqual(trace["prompt_version"], 0)
        self.assertEqual(trace["prompt_name"], "dimension_review")
        self.assertEqual(trace["prompt_sha256"], hashlib.sha256(sent_text.encode()).hexdigest())
        self.assertEqual(prompts.load_prompt_revision("dimension_review")["version"], 1)

    def test_untracked_override_does_not_claim_latest_version(self):
        (prompts.OVERRIDE_DIR / "dimension_review.txt").write_text("external edit", encoding="utf-8")
        self.assertEqual(prompts.load_prompt_revision("dimension_review")["version"], "unversioned")

    def test_find_connection_rows_detects_tie_in_and_continuation(self):
        candidates = [
            Candidate(id="C1", line_id="L1", page=216, kind="text", text="ПОДКЛЮЧЕНИЕ V-505/СЛИВ КОНДЕНСАТА 40 mm PE", zone="drawing", bbox=(10, 20, 100, 40)),
            Candidate(id="C2", line_id="L1", page=216, kind="text", text="СМ. CO-0031 ЛИСТ 2", zone="drawing", bbox=(120, 40, 220, 60)),
            Candidate(id="C3", line_id="L1", page=216, kind="text", text="СМ. СО-0031", zone="drawing", bbox=(220, 70, 320, 90)),
        ]
        vertices = [
            VertexMark(id="V04", line_id="L1", page=216, label="V04", role="junction", x=50, y=30, confidence=1.0),
            VertexMark(id="V03", line_id="L1", page=216, label="V03", role="junction", x=150, y=50, confidence=1.0),
            VertexMark(id="V05", line_id="L1", page=216, label="V05", role="junction", x=250, y=80, confidence=1.0),
        ]
        rows = _find_connection_rows(candidates, vertices)
        self.assertEqual([row["connection_type"] for row in rows], ["tie_in", "continuation", "other"])
        self.assertEqual(rows[0]["vertex_id"], "V04")
        self.assertEqual(rows[1]["target_sheet"], "2")
        self.assertTrue(rows[1]["text_has_sheet_ref"])
        self.assertEqual(rows[2]["target_sheet"], None)
        self.assertTrue(rows[2]["text_has_sheet_ref"])


if __name__ == "__main__":
    unittest.main()
