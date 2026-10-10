"""Regression: cleanup_job must delete job-issue comments across every page.

REMOTE-FIX-2: the pre-fix implementation fetched a single
``comments?per_page=100`` page, so long lectures with more than 100 mailbox
comments left residue behind.  These tests drive the real ``GitHubClient``
against a fake session and pin: full pagination until a short page, every
comment deleted exactly once, collection finished before any DELETE, the
sealing PATCH always issued, explicit ``comment_ids`` bypassing pagination,
and 404 tolerance on rerun.
"""

from __future__ import annotations

import unittest

from src.remote.github_client import GitHubClient


REPO = "student/mailbox"


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class _FakeSession:
    """Serves ``comments`` pages of 100 and records every request in order."""

    def __init__(self, total_comments=0, delete_statuses=None):
        self.total = total_comments
        self.delete_statuses = dict(delete_statuses or {})
        self.events = []
        self.get_pages = []
        self.deleted = []
        self.patched = False

    def request(self, method, url, timeout=None, **kwargs):
        if method == "GET" and url.endswith("/comments"):
            params = kwargs.get("params") or {}
            page = int(params.get("page", 1))
            per_page = int(params.get("per_page", 100))
            self.events.append(("get_page", page))
            self.get_pages.append(page)
            start = (page - 1) * per_page
            count = min(per_page, max(0, self.total - start))
            items = [{"id": start + index + 1} for index in range(count)]
            return _FakeResponse(200, items)
        if method == "DELETE":
            comment_id = int(url.rsplit("/", 1)[1])
            self.events.append(("delete", comment_id))
            self.deleted.append(comment_id)
            return _FakeResponse(self.delete_statuses.get(comment_id, 204), None)
        if method == "PATCH":
            self.events.append(("patch",))
            self.patched = True
            return _FakeResponse(200, {})
        raise AssertionError(f"unexpected request {method} {url}")


def _client_with(session):
    client = GitHubClient("token")
    client.session = session
    return client


class CleanupJobPaginationTests(unittest.TestCase):
    def test_long_issue_deletes_every_comment_across_all_pages(self):
        session = _FakeSession(total_comments=250)
        client = _client_with(session)

        client.cleanup_job(REPO, 7)

        self.assertEqual(session.get_pages, [1, 2, 3])
        self.assertEqual(sorted(session.deleted), list(range(1, 251)))
        self.assertEqual(len(session.deleted), len(set(session.deleted)))
        self.assertTrue(session.patched)
        kinds = [event[0] for event in session.events]
        self.assertLess(kinds.index("get_page"), kinds.index("delete"))
        self.assertEqual(session.events[-1], ("patch",))

    def test_exact_page_boundary_fetches_empty_terminal_page(self):
        session = _FakeSession(total_comments=200)
        client = _client_with(session)

        client.cleanup_job(REPO, 7)

        self.assertEqual(session.get_pages, [1, 2, 3])
        self.assertEqual(sorted(session.deleted), list(range(1, 201)))
        self.assertEqual(len(session.deleted), len(set(session.deleted)))
        self.assertTrue(session.patched)

    def test_short_issue_stays_single_page(self):
        session = _FakeSession(total_comments=5)
        client = _client_with(session)

        client.cleanup_job(REPO, 7)

        self.assertEqual(session.get_pages, [1])
        self.assertEqual(sorted(session.deleted), [1, 2, 3, 4, 5])
        self.assertTrue(session.patched)

    def test_zero_comments_still_seals_issue(self):
        session = _FakeSession(total_comments=0)
        client = _client_with(session)

        client.cleanup_job(REPO, 7)

        self.assertEqual(session.get_pages, [1])
        self.assertEqual(session.deleted, [])
        self.assertTrue(session.patched)

    def test_explicit_comment_ids_bypass_pagination(self):
        session = _FakeSession(total_comments=0)
        client = _client_with(session)

        client.cleanup_job(REPO, 7, comment_ids=[11, 22])

        self.assertEqual(session.get_pages, [])
        self.assertEqual(session.deleted, [11, 22])
        self.assertTrue(session.patched)

    def test_delete_404_is_tolerated_for_idempotent_rerun(self):
        session = _FakeSession(total_comments=2, delete_statuses={1: 404, 2: 404})
        client = _client_with(session)

        client.cleanup_job(REPO, 7)

        self.assertEqual(sorted(session.deleted), [1, 2])
        self.assertTrue(session.patched)


if __name__ == "__main__":
    unittest.main()
