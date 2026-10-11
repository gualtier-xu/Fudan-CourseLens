from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.release_worker_mirror import update_pin

ACTIVE_FIELDS = {
    "repository", "commit", "tree", "manifest_sha256",
    "signing_key_id", "trust_epoch", "protocol_versions",
}


def _assets() -> dict:
    return {
        "worker_mirror": {
            "active": {
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40, "tree": "b" * 40, "manifest_sha256": "c" * 64,
                "signing_key_id": "release-2026-01", "trust_epoch": 1,
                "protocol_versions": ["2"],
            }
        }
    }


class ReleasePinTests(unittest.TestCase):
    def test_update_pin_promotes_new_release_and_demotes_active(self):
        assets = _assets()
        release = {
            "repository": "gualtier-xu/Fudan-CourseLens-Worker",
            "commit": "d" * 40, "tree": "e" * 40, "manifest_sha256": "f" * 64,
            "signing_key_id": "release-2026-01", "trust_epoch": 1,
            "protocol_versions": ["2"],
        }
        update_pin(assets, release)
        mirror = assets["worker_mirror"]
        self.assertEqual(mirror["active"], release)
        self.assertEqual(mirror["previous"]["commit"], "a" * 40)
        self.assertEqual(set(mirror["active"]), ACTIVE_FIELDS)

    def test_update_pin_normalizes_case(self):
        assets = _assets()
        update_pin(assets, {
            "repository": "gualtier-xu/Fudan-CourseLens-Worker",
            "commit": "D" * 40, "tree": "E" * 40, "manifest_sha256": "F" * 64,
            "signing_key_id": "release-2026-01", "trust_epoch": "1",
            "protocol_versions": ["2"],
        })
        active = assets["worker_mirror"]["active"]
        self.assertEqual(active["commit"], "d" * 40)
        self.assertEqual(active["trust_epoch"], 1)

    def test_bundled_assets_shape_is_release_compatible(self):
        repo_root = Path(__file__).resolve().parents[1]
        assets = json.loads((repo_root / "runtime-assets.json").read_text(encoding="utf-8"))
        mirror = assets["worker_mirror"]
        previous = mirror.get("previous")
        entries = [mirror["active"]] if previous is None else [mirror["active"], previous]
        for entry in entries:
            self.assertEqual(set(entry), ACTIVE_FIELDS)
        if previous is not None:
            self.assertNotEqual(mirror["active"]["commit"], previous["commit"])
        # A future update_pin on the real assets keeps the rollback ordering.
        before_active = dict(mirror["active"])
        update_pin(assets, {
            "repository": before_active["repository"],
            "commit": "1" * 40, "tree": "2" * 40, "manifest_sha256": "3" * 64,
            "signing_key_id": before_active["signing_key_id"],
            "trust_epoch": before_active["trust_epoch"],
            "protocol_versions": before_active["protocol_versions"],
        })
        self.assertEqual(assets["worker_mirror"]["previous"], before_active)


if __name__ == "__main__":
    unittest.main()
