"""T2（夜14-R7 P0 合同面）：api_v3 校验器执法层直测。

v3 合同的闭集执法层（``src/runtime/api_v3.py``）此前负分支全零：五条错误
文案在测试树零命中。本件按 R7 钉稿三 + N15-R4 T2 终稿冻结现值分支——
每条错误文案本身即合同（学生可见的闭集拒绝理由），改动即须显式过门。

哨兵齿（emittership）：http_api.py 实际调用的全部 ``validate_*`` 名单
必须被本文件点名过——将来新增第 8 个校验器而忘测即红。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.runtime.api_v3 import (
    SCHEDULE_OUTPUTS,
    SCHEDULE_PIPELINE_VERSION,
    validate_client_reset_action,
    validate_course_data_action,
    validate_course_data_lectures_query,
    validate_course_data_summary_query,
    validate_data_migration_action,
    validate_feature_update,
    validate_reference_body,
    validate_schedule,
)
from src.runtime.course_data_inventory import (
    COURSE_DATA_ACTIONS,
    MAX_LECTURE_PAGE_SIZE,
    MAX_SUMMARY_PAGE_SIZE,
)


ROOT = Path(__file__).resolve().parents[1]
_HTTP_API_SOURCE = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
_THIS_SOURCE = Path(__file__).read_text(encoding="utf-8")


def _http_api_validators() -> list[str]:
    return sorted(set(re.findall(r"\b(validate_[a-z_]+)\(", _HTTP_API_SOURCE)))


class FeatureUpdateValidatorTests(unittest.TestCase):
    def test_name_branches_are_closed(self):
        for body in (
            {"name": "", "enabled": True},
            {"name": "x" * 81, "enabled": True},
            {"name": "bad name!", "enabled": True},
            {"name": "bad/name", "enabled": True},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as caught:
                    validate_feature_update(body)
                self.assertEqual(str(caught.exception), "feature flag name is invalid")

    def test_enabled_must_be_boolean(self):
        for value in ("yes", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    validate_feature_update({"name": "flag", "enabled": value})
                self.assertEqual(str(caught.exception), "feature flag enabled must be boolean")

    def test_happy_path_returns_normalized_pair(self):
        self.assertEqual(
            validate_feature_update({"name": " ui.compact_v2-1 ", "enabled": False}),
            ("ui.compact_v2-1", False),
        )


class ScheduleValidatorTests(unittest.TestCase):
    def test_time_must_use_hhmm(self):
        for bad in ("7:30", "24:00", "12:60", "0730", "07:3o", "07-30"):
            with self.subTest(time=bad):
                with self.assertRaises(ValueError) as caught:
                    validate_schedule({"time": bad})
                self.assertEqual(str(caught.exception), "schedule time must use HH:MM")
        # 空/空白 time 不拒绝：``or "07:30"`` 回落默认（现行为钉死）。

    def test_outputs_outside_closed_set_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_schedule({"outputs": ["subtitle", "nope"]})
        self.assertEqual(str(caught.exception), "schedule output is invalid")

    def test_course_ids_must_be_numeric(self):
        with self.assertRaises(ValueError) as caught:
            validate_schedule({"course_ids": ["123", "abc"]})
        self.assertEqual(str(caught.exception), "course_ids must contain numeric identifiers")

    def test_enabled_requires_at_least_one_course(self):
        with self.assertRaises(ValueError) as caught:
            validate_schedule({"enabled": True})
        self.assertEqual(str(caught.exception), "enabled schedules require at least one course")

    def test_outputs_and_course_ids_deduplicate_silently(self):
        # 现行语义：重复项静默去重（不 raise）。改语义须显式过门。
        value = validate_schedule({"outputs": ["summary", " summary ", "summary"], "course_ids": ["7", "7"]})
        self.assertEqual(value["outputs"], ["summary"])
        self.assertEqual(value["course_ids"], ["7"])

    def test_empty_outputs_falls_back_to_default_subtitle(self):
        # 已知死分支注记：``validate_schedule`` 的 "at least one schedule output
        # is required" raise 不可达——显式空列表被 ``body.get("outputs") or
        # ["subtitle"]`` 回落为默认字幕。此处钉现行为（回落），死分支清理归
        # 产品决策；若未来该 raise 变为可达，本用例红＝行为已变，须重新过门。
        value = validate_schedule({"outputs": []})
        self.assertEqual(value["outputs"], ["subtitle"])

    def test_defaults_and_catch_up_semantics(self):
        value = validate_schedule({})
        self.assertEqual(value["time"], "07:30")
        self.assertEqual(value["outputs"], ["subtitle"])
        self.assertEqual(value["enabled"], False)
        self.assertEqual(value["course_ids"], [])
        self.assertEqual(value["pipeline_version"], SCHEDULE_PIPELINE_VERSION)
        self.assertEqual(value["pending"], [])
        # catch_up 缺省 True（补跑默认开）；显式 False 才关。
        self.assertTrue(value["catch_up"])
        self.assertFalse(validate_schedule({"catch_up": False})["catch_up"])
        self.assertEqual(set(validate_schedule({})), {
            "enabled", "time", "course_ids", "outputs", "catch_up",
            "pipeline_version", "pending",
        })

    def test_closed_output_set_constant_matches_frozen_contract(self):
        # SCHEDULE_OUTPUTS 是调度输出闭集本身（合同）；此处防静默调参。
        self.assertEqual(SCHEDULE_OUTPUTS, {"subtitle", "ocr", "summary", "chapters", "quiz"})


class CourseDataQueryValidatorTests(unittest.TestCase):
    def test_defaults_when_query_absent(self):
        self.assertEqual(validate_course_data_summary_query({}), {
            "page": 1, "page_size": 50, "include_orphans": False,
        })

    def test_non_integer_page_rejected(self):
        for raw in ("abc", "1.5"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    validate_course_data_summary_query({"page": [raw]})
                self.assertEqual(str(caught.exception), "page must be an integer")

    def test_below_minimum_clamps_up(self):
        self.assertEqual(validate_course_data_summary_query({"page": ["0"]})["page"], 1)
        self.assertEqual(validate_course_data_summary_query({"page": ["-9"]})["page"], 1)

    def test_above_maximum_clamps_to_closed_caps(self):
        self.assertEqual(validate_course_data_summary_query({"page_size": ["500"]})["page_size"], MAX_SUMMARY_PAGE_SIZE)
        lectures = validate_course_data_lectures_query({"course_id": ["123"], "limit": ["99"]})
        self.assertEqual(lectures["limit"], MAX_LECTURE_PAGE_SIZE)
        self.assertEqual(
            validate_course_data_lectures_query({"course_id": ["123"], "limit": ["0"]})["limit"], 1,
        )

    def test_flag_closed_set(self):
        for raw, expected in (("1", True), ("true", True), ("YES", True), ("", False), ("no", False)):
            with self.subTest(raw=raw):
                self.assertEqual(
                    validate_course_data_summary_query({"include_orphans": [raw]})["include_orphans"],
                    expected,
                )

    def test_lectures_query_requires_course_id(self):
        with self.assertRaises(ValueError) as caught:
            validate_course_data_lectures_query({})
        self.assertEqual(str(caught.exception), "course_id is required")
        value = validate_course_data_lectures_query({"course_id": [" 123 "]})
        self.assertEqual(value, {"course_id": "123", "limit": 50, "offset": 0})


class CourseDataActionValidatorTests(unittest.TestCase):
    _BASE = {"action": "export", "operation_id": "op-12345678"}

    def test_body_must_be_object(self):
        with self.assertRaises(ValueError) as caught:
            validate_course_data_action(["export"])  # type: ignore[arg-type]
        self.assertEqual(str(caught.exception), "course data action body is invalid")

    def test_action_outside_frozen_closed_set_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_course_data_action({"action": "nuke-everything", "operation_id": "op-12345678"})
        self.assertEqual(str(caught.exception), "course data action is invalid")

    def test_every_frozen_action_is_accepted(self):
        for action in COURSE_DATA_ACTIONS:
            with self.subTest(action=action):
                value = validate_course_data_action({"action": action, "operation_id": "op-12345678"})
                self.assertEqual(value["action"], action)

    def test_operation_id_shape_is_enforced(self):
        for bad in ("", "short", "has space-123", "op/" + "x" * 8):
            with self.subTest(operation_id=bad):
                with self.assertRaises(ValueError) as caught:
                    validate_course_data_action({"action": "export", "operation_id": bad})
                self.assertEqual(str(caught.exception), "operation_id is required")

    def test_identifiers_must_be_string_lists(self):
        for name, value in (("course_ids", "123"), ("sub_ids", [1, 2]), ("course_ids", {"a": 1})):
            with self.subTest(name=name, value=value):
                body = dict(self._BASE)
                body[name] = value
                with self.assertRaises(ValueError) as caught:
                    validate_course_data_action(body)
                self.assertEqual(str(caught.exception), f"{name} must be a list of identifiers")

    def test_boolean_and_typed_fields_are_type_checked(self):
        for name, value, message in (
            ("confirm", "yes", "confirm must be boolean"),
            ("confirm_typed", 7, "confirm_typed must be a string"),
            ("include_orphans", "true", "include_orphans must be boolean"),
        ):
            with self.subTest(name=name, value=value):
                body = dict(self._BASE)
                body[name] = value
                with self.assertRaises(ValueError) as caught:
                    validate_course_data_action(body)
                self.assertEqual(str(caught.exception), message)

    def test_happy_path_normalizes_action_and_identifiers(self):
        value = validate_course_data_action({
            "action": " Export ", "operation_id": "op-12345678",
            "course_ids": [" 123 ", "123", "456"], "sub_ids": ["9"],
            "confirm": True, "confirm_typed": "删除", "include_orphans": True,
        })
        self.assertEqual(value, {
            "action": "export", "operation_id": "op-12345678",
            "course_ids": ["123", "456"], "sub_ids": ["9"],
            "confirm": True, "confirm_typed": "删除", "include_orphans": True,
        })


class ClientResetValidatorDirectTests(unittest.TestCase):
    def test_delete_github_repos_must_be_boolean(self):
        # 路由级负分支主体已由 tests/test_client_reset.py 覆盖；此处补其
        # 未见专断的一腿（ASC-8 残余）：非布尔 delete_github_repos 直测。
        with self.assertRaises(ValueError) as caught:
            validate_client_reset_action({
                "action": "reset", "operation_id": "op-12345678",
                "delete_github_repos": "no",
            })
        self.assertEqual(str(caught.exception), "delete_github_repos must be boolean")


class ReferenceBodyValidatorTests(unittest.TestCase):
    _GOOD = {
        "course_id": "123", "sub_id": "456", "start_ms": 1000, "end_ms": 2000,
        "source_hash": "ABCDEF0123456789ABCDEF0123456789",
    }

    def test_body_must_be_object(self):
        with self.assertRaises(ValueError) as caught:
            validate_reference_body("nope")  # type: ignore[arg-type]
        self.assertEqual(str(caught.exception), "reference must be an object")

    def test_ids_must_be_numeric_and_present(self):
        for body in ({}, {"course_id": "abc", "sub_id": "1"}, {"course_id": "1", "sub_id": ""}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as caught:
                    validate_reference_body(body)
                self.assertEqual(str(caught.exception), "reference course_id and sub_id must be numeric")

    def test_source_hash_shape_is_enforced(self):
        for bad in ("", "XYZ", "abc123", "g" * 64):
            with self.subTest(hash=bad):
                body = dict(self._GOOD, source_hash=bad)
                with self.assertRaises(ValueError) as caught:
                    validate_reference_body(body)
                self.assertEqual(str(caught.exception), "reference source_hash is invalid")

    def test_source_closed_set(self):
        with self.assertRaises(ValueError) as caught:
            validate_reference_body(dict(self._GOOD, source="pptx"))
        self.assertEqual(str(caught.exception), "reference source is unsupported")

    def test_source_ref_length_cap(self):
        with self.assertRaises(ValueError) as caught:
            validate_reference_body(dict(self._GOOD, source_ref="x" * 513))
        self.assertEqual(str(caught.exception), "reference source_ref is too long")

    def test_time_range_is_sanitized_not_rejected(self):
        # end < start 钳到 start（max 语义）；非数字时间拒绝。
        value = validate_reference_body(dict(self._GOOD, end_ms=5))
        self.assertEqual(value["end_ms"], 1000)
        with self.assertRaises(ValueError) as caught:
            validate_reference_body(dict(self._GOOD, start_ms="abc"))
        self.assertEqual(str(caught.exception), "reference time range is invalid")

    def test_happy_path_round_trip_is_canonical(self):
        value = validate_reference_body(dict(self._GOOD, source_ref=" 08:02 引用 "))
        self.assertEqual(value, {
            "course_id": "123", "sub_id": "456", "start_ms": 1000, "end_ms": 2000,
            "source_hash": "abcdef0123456789abcdef0123456789",
            "source": "transcript", "source_ref": "08:02 引用",
        })


class DataMigrationActionValidatorTests(unittest.TestCase):
    """D12 搬家包动作校验器执法层（两动作闭集+口令形状+包标识形状）。"""

    def test_export_happy_path(self):
        self.assertEqual(
            validate_data_migration_action({
                "action": "export", "operation_id": "migration-op-0001",
                "password": "migrate-2026",
            }),
            {"action": "export", "operation_id": "migration-op-0001",
             "password": "migrate-2026", "package_id": ""},
        )

    def test_import_happy_path_normalizes_package_id_case(self):
        value = validate_data_migration_action({
            "action": "import", "operation_id": "migration-op-0002",
            "password": "migrate-2026", "package_id": "A" * 32,
        })
        self.assertEqual(value["package_id"], "a" * 32)

    def test_action_closed_set(self):
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "wipe", "operation_id": "migration-op-0003",
                "password": "migrate-2026",
            })

    def test_operation_id_shape(self):
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "export", "operation_id": "short", "password": "migrate-2026",
            })

    def test_password_bounds_and_type(self):
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "export", "operation_id": "migration-op-0004", "password": "short",
            })
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "export", "operation_id": "migration-op-0005",
                "password": "x" * 257,
            })
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "export", "operation_id": "migration-op-0006", "password": 12345678,
            })

    def test_import_requires_package_id_shape(self):
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "import", "operation_id": "migration-op-0007",
                "password": "migrate-2026", "package_id": "zzz",
            })
        with self.assertRaises(ValueError):
            validate_data_migration_action({
                "action": "import", "operation_id": "migration-op-0008",
                "password": "migrate-2026",
            })


class ValidatorEmittershipSentinelTests(unittest.TestCase):
    def test_every_validator_called_by_http_api_is_exercised_here(self):
        # emittership 哨兵：http_api 实际调用的校验器名单 ⊆ 本文件点过名的
        # 校验器集合。新增校验器接入路由而漏测 → 此齿红。
        called = _http_api_validators()
        exercised = set(re.findall(r"\b(validate_[a-z_]+)\(", _THIS_SOURCE))
        self.assertTrue(called, "http_api 校验器调用点扫描不得为空（扫描器失效须先修）")
        for name in called:
            self.assertIn(name, exercised, f"校验器 {name} 已接入 http_api 但本执法层测试未覆盖")


if __name__ == "__main__":
    unittest.main()
