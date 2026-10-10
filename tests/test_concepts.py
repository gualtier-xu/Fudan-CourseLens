from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.runtime.concepts import (
    analyze_concepts,
    list_concept_graph,
    mark_stale_edges,
    update_concept_edge,
)


class CrossCourseConceptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "learning.db"
        self.left_text = "本节使用梯度下降算法进行优化"
        self.right_text = "机器学习课程采用梯度下降算法"

    def tearDown(self):
        self.temp.cleanup()

    def _analyze(self):
        return analyze_concepts(self.path, [
            {"course_id": "course-a", "sub_id": "lecture-a", "segments": [
                {"start_ms": 1000, "end_ms": 3000, "text": self.left_text},
            ]},
            {"course_id": "course-b", "sub_id": "lecture-b", "segments": [
                {"start_ms": 4000, "end_ms": 6000, "text": self.right_text},
            ]},
        ])

    def test_relationship_requires_evidence_from_two_courses(self):
        single = analyze_concepts(self.path, [{
            "course_id": "course-a", "sub_id": "lecture-a",
            "segments": [{"start_ms": 0, "end_ms": 1, "text": self.left_text}],
        }])
        self.assertEqual(single["edge_count"], 0)
        result = self._analyze()
        self.assertEqual(result["concept_count"], 1)
        graph = list_concept_graph(self.path)
        self.assertEqual(len(graph["edges"]), 1)
        edge = graph["edges"][0]
        self.assertEqual({item["course_id"] for item in edge["evidence"]}, {"course-a", "course-b"})
        for item in edge["evidence"]:
            self.assertEqual(item["source_hash"], hashlib.sha256(item["text"].encode()).hexdigest())

    def test_manual_relation_and_dismissal_are_persistent(self):
        self._analyze()
        edge_id = list_concept_graph(self.path)["edges"][0]["edge_id"]
        changed = update_concept_edge(self.path, edge_id=edge_id, action="set_relation", relation="prerequisite")
        self.assertEqual(changed["relation"], "prerequisite")
        self.assertEqual(changed["origin"], "manual")
        dismissed = update_concept_edge(self.path, edge_id=edge_id, action="dismiss")
        self.assertEqual(dismissed["status"], "dismissed")
        self._analyze()
        self.assertEqual(list_concept_graph(self.path)["edges"][0]["status"], "dismissed")

    def test_missing_source_hash_marks_relationship_for_review(self):
        self._analyze()
        changed = mark_stale_edges(self.path, {
            "lecture-a": {hashlib.sha256(self.left_text.encode()).hexdigest()},
            "lecture-b": set(),
        })
        self.assertEqual(changed, 1)
        self.assertEqual(list_concept_graph(self.path)["edges"][0]["status"], "review_required")


if __name__ == "__main__":
    unittest.main()
