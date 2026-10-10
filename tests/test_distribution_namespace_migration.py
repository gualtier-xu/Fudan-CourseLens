"""Pins for the 2026-09-30 official release-face namespace migration.

The official distribution face moved from the ``gualtier-xu-co`` test
auxiliary account to the ``gualtier-xu`` release account.  These pins lock
the structural guarantee that a worker binding left pointing at the old co
template no longer collides with the bundled public template, so the
``personal_worker_migration_required`` guard in
``GitHubAppClient.check_worker_integrity`` can never fire for a leftover co
binding again (B1 structural-disappearance proof).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.distribution import MANIFEST_SOURCE_ID, load_registry  # noqa: E402
from src.remote.github_app import _bundled_worker_config, _same_repository  # noqa: E402

OLD_TEMPLATE = "gualtier-xu-co/Fudan-CourseLens-Worker"
# Worker 模板镜像仓（runtime-assets.json 钉面；REPO-GOVERN-1 已正名为
# gualtier-xu/Fudan-CourseLens-Worker，REPUBLISH-23 起镜像 pin 随 45dd6230
# 环换代至正名，旧名 -Worker-Release 经 GitHub 改名转发仍可达）。
NEW_TEMPLATE = "gualtier-xu/Fudan-CourseLens-Worker"
# 客户端更新门户=总仓（REBUILD-9 重指，2026-10-04）。
MASTER_DISTRIBUTION = "gualtier-xu/Fudan-CourseLens"


def test_bundled_template_points_at_official_release_account():
    bundled = _bundled_worker_config()
    assert bundled["repository"] == NEW_TEMPLATE


def test_dev_binding_on_old_co_template_is_structurally_not_the_template():
    # B1 structural-disappearance proof: the official template moved off the
    # name a dev machine may still have bound, so the binding-vs-template
    # comparison behind the migration guard compares unequal for any
    # leftover co binding, in every case fold.
    bundled = _bundled_worker_config()
    assert not _same_repository(OLD_TEMPLATE, str(bundled.get("repository") or ""))
    assert _same_repository(OLD_TEMPLATE, OLD_TEMPLATE)
    assert _same_repository(NEW_TEMPLATE, "gualtier-xu/FUDAN-COURSELENS-WORKER")


def test_registry_follows_the_migration():
    registry = load_registry()
    assert registry["distribution_repository"] == MASTER_DISTRIBUTION
    # CO-NEUTRAL-1: the payload registry no longer carries the private repo
    # name — both source keys hold the neutral manifest source id, and the
    # real private name lives only in config/ops-private.json.
    assert registry["source_repository"] == MANIFEST_SOURCE_ID
    assert registry["manifest_source_id"] == MANIFEST_SOURCE_ID
