from __future__ import annotations

import json
import unittest
from types import SimpleNamespace


from src.remote.worker_migration import (
    _known_mailbox_issues,
    _managed_mailbox_inventory,
)


CONSUMED = json.dumps({"schema": "mailbox.v2", "state": "consumed"})


def _issue(number, *, title="[courselens-job] task", body=CONSUMED, state="closed",
           comments=0, labels=("courselens-job",)):
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "comments": comments,
        "labels": [{"name": name} for name in labels],
    }


def _api(issues, comments_by_number=None, pages=None):
    """Fake GitHubApp API: unfiltered issue list, direct issue GET, no comments."""
    requested = {"paths": [], "comment_paths": 0}

    class _Api:
        def _api(self, _method, path, *, token=None, expected=None, params=None):
            requested["paths"].append(path)
            if path.endswith("/comments"):
                requested["comment_paths"] += 1
                raise AssertionError("inventory must never read comments")
            if path.endswith("/issues"):
                if pages is not None:
                    page = int((params or {}).get("page") or 1)
                    values = pages[page - 1] if page <= len(pages) else []
                    return SimpleNamespace(json=lambda: values)
                return SimpleNamespace(json=lambda: issues)
            if "/issues/" in path:
                number = int(path.rsplit("/", 1)[-1])
                value = (comments_by_number or {}).get(number, "missing")
                if value == "missing":
                    return SimpleNamespace(status_code=404)
                return SimpleNamespace(status_code=200, json=lambda: value)
            raise AssertionError(path)

    return _Api(), requested


class ManagedMailboxInventoryTests(unittest.TestCase):
    def test_consumed_closed_history_is_never_residual_and_never_reads_comments(self):
        issues = [_issue(7, comments=203)]
        api, requested = _api(issues)
        inventory = _managed_mailbox_inventory(api, "repo", "t")
        self.assertEqual(inventory["consumed_closed_count"], 1)
        self.assertEqual(inventory["closed_unconsumed_count"], 0)
        self.assertEqual(inventory["open_unconsumed_count"], 0)
        self.assertEqual(inventory["reconcilable_issue_numbers"], [])
        self.assertEqual(requested["comment_paths"], 0)

    def test_closed_unconsumed_with_181_comments_is_reconcilable_not_an_error(self):
        # Regression for #128: closed history must not hit the comment ceiling.
        issues = [_issue(128, body="sealed", state="closed", comments=181)]
        api, requested = _api(issues)
        inventory = _managed_mailbox_inventory(api, "repo", "t")
        self.assertEqual(inventory["closed_unconsumed_count"], 1)
        self.assertEqual(inventory["reconcilable_issue_numbers"], [128])
        self.assertEqual(requested["comment_paths"], 0)

    def test_open_unconsumed_large_backlog_still_fails_closed(self):
        issues = [_issue(8, body="sealed", state="open", comments=100)]
        api, _requested = _api(issues)
        with self.assertRaisesRegex(RuntimeError, "mailbox_comment_inventory_too_large"):
            _managed_mailbox_inventory(api, "repo", "t")

    def test_open_unconsumed_counts_metadata_comments_without_downloading(self):
        issues = [_issue(9, body="sealed", state="open", comments=3)]
        api, requested = _api(issues)
        inventory = _managed_mailbox_inventory(api, "repo", "t")
        self.assertEqual(inventory["open_unconsumed_count"], 1)
        self.assertEqual(inventory["active_comment_count"], 3)
        self.assertEqual(inventory["closed_unconsumed_count"], 0)
        self.assertEqual(requested["comment_paths"], 0)

    def test_label_or_title_drift_is_reported_and_never_reconcilable(self):
        issues = [
            _issue(10, title="renamed", body="sealed", state="closed"),  # label kept, title changed
            _issue(11, labels=()),  # title kept, label removed
        ]
        api, _requested = _api(issues)
        inventory = _managed_mailbox_inventory(api, "repo", "t")
        self.assertEqual(inventory["metadata_drift_count"], 2)
        self.assertEqual(inventory["managed_issue_count"], 0)
        self.assertEqual(inventory["reconcilable_issue_numbers"], [])

    def test_unattributable_issue_is_ignored_and_locally_bound_one_is_drift(self):
        issues = [
            _issue(20, title="personal note", labels=(), body="", state="open", comments=2),
            _issue(21, title="renamed too", labels=(), body="sealed", state="closed"),
        ]
        api, _requested = _api(issues)
        inventory = _managed_mailbox_inventory(api, "repo", "t", known_issues={21: "history"})
        self.assertEqual(inventory["metadata_drift_count"], 1)
        self.assertEqual(inventory["managed_issue_count"], 0)

    def test_missing_known_active_issue_is_active_missing(self):
        issues = [_issue(30, body="sealed", state="closed")]
        api, _requested = _api(issues, comments_by_number={44: "missing"})
        inventory = _managed_mailbox_inventory(api, "repo", "t", known_issues={44: "active"})
        self.assertEqual(inventory["active_issue_missing_count"], 1)
        self.assertEqual(inventory["history_issue_missing_count"], 0)

    def test_missing_known_history_issue_is_warning_only(self):
        issues = [_issue(30, body="sealed", state="closed")]
        api, _requested = _api(issues, comments_by_number={45: "missing"})
        inventory = _managed_mailbox_inventory(api, "repo", "t", known_issues={45: "history"})
        self.assertEqual(inventory["history_issue_missing_count"], 1)
        self.assertEqual(inventory["active_issue_missing_count"], 0)

    def test_known_number_present_but_absent_from_listing_fails_closed(self):
        issues = [_issue(30, body="sealed", state="closed")]
        api, _requested = _api(issues, comments_by_number={50: _issue(50, body="sealed", state="closed")})
        with self.assertRaisesRegex(RuntimeError, "mailbox_inventory_incomplete"):
            _managed_mailbox_inventory(api, "repo", "t", known_issues={50: "history"})

    def test_invalid_fields_fail_closed(self):
        for bad in (
            {"number": "x", "title": "[courselens-job] t", "state": "closed", "comments": 0, "labels": []},
            _issue(31) | {"state": "merged"},
            _issue(31) | {"comments": -1},
            _issue(31) | {"comments": "3"},
            _issue(31) | {"labels": [{"no_name": 1}]},
            _issue(31) | {"title": None},
        ):
            api, _requested = _api([bad])
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(RuntimeError, "mailbox_inventory_incomplete"):
                    _managed_mailbox_inventory(api, "repo", "t")

    def test_duplicate_or_oversized_listing_fails_closed(self):
        duplicate = SimpleNamespace(json=lambda: [_issue(7), _issue(7)])
        api_dup = SimpleNamespace(_api=lambda *_a, **_k: duplicate)
        with self.assertRaisesRegex(RuntimeError, "mailbox_inventory_incomplete"):
            _managed_mailbox_inventory(api_dup, "repo", "t")
        full = [_issue(index) for index in range(100)]
        api_pages, _requested = _api(None, pages=[full] * 11)
        with self.assertRaisesRegex(RuntimeError, "mailbox_issue_inventory_too_large"):
            _managed_mailbox_inventory(api_pages, "repo", "t")

    def test_known_mailbox_issues_map_comes_from_recent_remote_runs(self):
        class Store:
            def list_remote_runs(self, *, limit=10):
                return [
                    {"issue_number": 1, "remote_state": "imported"},
                    {"issue_number": 2, "remote_state": "running"},
                    {"issue_number": 0, "remote_state": "running"},
                    {"issue_number": 1, "remote_state": "failed"},
                ]
        self.assertEqual(_known_mailbox_issues(Store()), {1: "history", 2: "active"})

    def test_known_mailbox_issue_read_failure_fails_closed(self):
        class BrokenStore:
            def list_remote_runs(self, *, limit=10):
                raise sqlite_error()
        def sqlite_error():
            return RuntimeError("store unavailable")
        with self.assertRaisesRegex(RuntimeError, "mailbox_inventory_incomplete"):
            _known_mailbox_issues(BrokenStore())


if __name__ == "__main__":
    unittest.main()
