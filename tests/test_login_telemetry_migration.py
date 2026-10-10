"""login-stage 遥测缓存目录改名迁移 + 留存龄期上界的离线行为测试。

全部 fake 驱动、零真实外联、零真实缓存目录触碰（临时目录沙箱，PATH 全部
落在 TemporaryDirectory 内）：
- 旧批次名目录 → 新语义名目录的一次性整目录迁移（内容与 mtime 原样）；
- 幂等：迁完不再动 / 无旧目录 no-op / 两目录并存保守不动；
- 迁移失败静默（Path.rename 注入故障）且绝不影响登录写入，标记留待重试；
- 迁移动作留痕（新目录内 migration 日志一行 JSONL，不匹配 prune glob）；
- 留存龄期上界（>30 天一律清除，与「保最新 20」并行生效）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from path_utils import PROJECT_ROOT
from src import application as app_module
from src.application import (
    LOGIN_STAGE_CACHE_DIRNAME,
    LOGIN_STAGE_LEGACY_CACHE_DIRNAME,
    LoginStageTelemetry,
)


class _SandboxCase(unittest.TestCase):
    """公共沙箱：临时根目录 + 进程级一次性迁移标记逐例复位。"""

    def setUp(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)
        app_module._LOGIN_STAGE_MIGRATED = False

    def tearDown(self):
        app_module._LOGIN_STAGE_MIGRATED = False
        self.temporary.cleanup()

    def _seed_legacy(self, *, files: int, age_seconds: float = 0.0):
        """在沙箱根下造旧批次名目录与假 artifact，返回 (路径, 名单)。"""
        legacy = self.root / LOGIN_STAGE_LEGACY_CACHE_DIRNAME
        legacy.mkdir(parents=True)
        now = time.time()
        names = []
        for index in range(files):
            path = legacy / f"login-stage-{1000 + index}.jsonl"
            path.write_text(
                json.dumps({"stage": "catalog_context", "n": index}) + "\n",
                encoding="utf-8",
            )
            stamp = now - index * 60 - age_seconds
            os.utime(path, (stamp, stamp))
            names.append(path.name)
        return legacy, names

    def _seed_target(self, *, count: int, age_seconds: float = 0.0):
        """在沙箱根下预建新语义名目录与假 artifact，返回名单。"""
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        target.mkdir(parents=True)
        now = time.time()
        names = []
        for index in range(count):
            path = target / f"login-stage-{5000 + index}.jsonl"
            path.write_text(
                json.dumps({"stage": "catalog_context", "n": index}) + "\n",
                encoding="utf-8",
            )
            stamp = now - index * 60 - age_seconds
            os.utime(path, (stamp, stamp))
            names.append(path.name)
        return names


class NamingTests(_SandboxCase):
    def test_default_directory_targets_semantic_name_without_date(self):
        writer = LoginStageTelemetry()  # 默认目录只做路径拼装，无任何 I/O
        self.assertEqual(writer._directory.name, "fudan-login-stage-telemetry")
        self.assertEqual(writer._directory.name, LOGIN_STAGE_CACHE_DIRNAME)
        self.assertNotIn("20260906", LOGIN_STAGE_CACHE_DIRNAME)
        self.assertNotEqual(LOGIN_STAGE_CACHE_DIRNAME, LOGIN_STAGE_LEGACY_CACHE_DIRNAME)


class MigrationTests(_SandboxCase):
    def test_legacy_directory_migrates_whole_on_first_write(self):
        legacy, names = self._seed_legacy(files=3)
        before = {name: (legacy / name).stat().st_mtime for name in names}
        payloads = {name: (legacy / name).read_bytes() for name in names}
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        writer = LoginStageTelemetry(target)
        writer("catalog_context")  # 首写触发：一次性迁移 → prune → 追加当前文件

        self.assertFalse(legacy.exists(), "旧目录必须整目录消失")
        migrated_names = [path.name for path in target.glob("login-stage-*.jsonl")]
        self.assertEqual(len(migrated_names), 4, "3 个历史 artifact + 1 个当前 artifact")
        for name in names:
            self.assertIn(name, migrated_names)
            path = target / name
            self.assertEqual(path.read_bytes(), payloads[name], "迁移只搬家不改内容")
            self.assertEqual(
                path.stat().st_mtime, before[name], "mtime 必须原样保留"
            )
        self.assertTrue(writer._path.exists(), "当前 artifact 必须落在新目录")

    def test_migration_log_line_records_action(self):
        self._seed_legacy(files=2)
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        LoginStageTelemetry(target)("catalog_context")
        log = target / LoginStageTelemetry.MIGRATION_LOG_BASENAME
        self.assertTrue(log.is_file(), "迁移动作必须留痕")
        self.assertFalse(
            log.name.endswith(".jsonl"), "migration 日志不得匹配 prune glob"
        )
        record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["event"], "legacy_cache_dir_migrated")
        self.assertEqual(record["from"], LOGIN_STAGE_LEGACY_CACHE_DIRNAME)
        self.assertEqual(record["to"], LOGIN_STAGE_CACHE_DIRNAME)
        self.assertEqual(record["files"], 2)
        self.assertIsInstance(record["ts"], int)

    def test_migration_is_idempotent_across_instances(self):
        self._seed_legacy(files=1)
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        LoginStageTelemetry(target)("catalog_context")
        self.assertTrue(app_module._LOGIN_STAGE_MIGRATED)
        LoginStageTelemetry(target)("catalog_context")  # 标记已置位：不再迁移
        self.assertEqual(len(list(target.glob("login-stage-*.jsonl"))), 3)

    def test_absent_legacy_directory_is_noop(self):
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        writer = LoginStageTelemetry(target)
        writer("catalog_context")
        self.assertEqual(len(list(target.glob("login-stage-*.jsonl"))), 1)
        self.assertFalse(
            (self.root / LOGIN_STAGE_LEGACY_CACHE_DIRNAME).exists(),
            "无旧目录时绝不能凭空造出旧目录",
        )

    def test_both_directories_present_is_conservative_noop(self):
        legacy, names = self._seed_legacy(files=1)
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        target.mkdir()
        keeper = target / "login-stage-9000.jsonl"
        keeper.write_text('{"stage": "catalog_context"}\n', encoding="utf-8")
        LoginStageTelemetry(target)("catalog_context")

        self.assertTrue(legacy.is_dir(), "两目录并存属异常态：旧目录必须原样保留")
        self.assertTrue(
            (legacy / names[0]).is_file(), "旧目录内容不得被迁移或删除"
        )
        self.assertTrue(keeper.exists(), "新目录既有内容不得被动")
        self.assertEqual(len(list(target.glob("login-stage-*.jsonl"))), 2)

    def test_rename_failure_is_swallowed_and_write_still_lands(self):
        legacy, _ = self._seed_legacy(files=1)
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        writer = LoginStageTelemetry(target)
        with patch("pathlib.Path.rename", side_effect=OSError("synthetic lock")):
            writer("catalog_context")  # 迁移失败必须静默，绝不影响写入

        self.assertTrue(legacy.is_dir(), "迁移失败：旧目录原样保留")
        files = list(target.glob("login-stage-*.jsonl"))
        self.assertEqual(len(files), 1, "当前 artifact 照常写入新目录")
        self.assertFalse(app_module._LOGIN_STAGE_MIGRATED, "失败必须留待下例重试")


class RetentionAgeTests(_SandboxCase):
    def test_stale_artifacts_removed_even_within_keep_20(self):
        self._seed_target(count=2)  # 2 个新鲜历史文件
        stale_names = []
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        now = time.time()
        for index in range(3):  # 3 个超龄文件（mtime 排最旧但远在保 20 限额内）
            path = target / f"login-stage-{3000 + index}.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            stamp = now - 31 * 86400 - index * 60
            os.utime(path, (stamp, stamp))
            stale_names.append(path.name)

        LoginStageTelemetry(target)("catalog_context")  # 触发 prune

        remaining = {path.name for path in target.glob("login-stage-*.jsonl")}
        for name in stale_names:
            self.assertNotIn(name, remaining, "超龄 artifact 必须清除，与保 20 无关")
        self.assertEqual(len(remaining), 3, "2 个新鲜历史 + 1 个当前文件")

    def test_age_bound_and_keep_20_combine(self):
        target = self.root / LOGIN_STAGE_CACHE_DIRNAME
        now = time.time()
        recent_names = self._seed_target(count=25)  # 新鲜：mtime 逐一递减
        stale_names = []
        for index in range(3):  # 超龄：排最旧
            path = target / f"login-stage-{2000 + index}.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            stamp = now - 31 * 86400 - index * 60
            os.utime(path, (stamp, stamp))
            stale_names.append(path.name)

        LoginStageTelemetry(target)("catalog_context")

        remaining = {path.name for path in target.glob("login-stage-*.jsonl")}
        self.assertEqual(
            len(remaining), 21, "新鲜最新 20 个 + 1 个当前文件"
        )
        for name in stale_names:
            self.assertNotIn(name, remaining, "超龄文件无论排名一律清除")
        for index in range(20):  # 新鲜最新 20 个保留
            self.assertIn(recent_names[index], remaining)
        for index in range(20, 25):  # 新鲜但超出限额的最旧 5 个清理
            self.assertNotIn(recent_names[index], remaining)


class OfflineConventionTests(unittest.TestCase):
    """约定自检：本文件必须保持纯离线（fake 驱动）。"""

    def test_no_real_network_tooling_in_this_module(self):
        source = Path(__file__).read_text(encoding="utf-8")
        forbidden = ["url" + "open", "urllib." + "request", "socket." + "socket"]
        forbidden.append("ht" + "tp.server")
        for needle in forbidden:
            self.assertNotIn(needle, source.lower())


if __name__ == "__main__":
    unittest.main()
