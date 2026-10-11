"""夜10-C T9：services 域错误信封契约钉。

四个服务边界错误类（domains.py）在 http_api 适配层的映射面冻结：
- 每个类必须由专属 except/isinstance 分支处置，错误码取自 ``exc.code``
  且带闭集兜底码；
- 响应体必须携带 ``error`` + ``error_code`` 双键（信封家族主流形态）；
- 状态码按语义分档（前置条件冲突 409 / 不存在 404 / 拒绝 400）。
静态源钉（与 ShapeWiringPins 同法）：行号漂移不误报，形态漂移必红。
"""

from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTTP_API_SOURCE = (PROJECT_ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
DOMAINS_SOURCE = (PROJECT_ROOT / "src" / "services" / "domains.py").read_text(encoding="utf-8")


class ServiceErrorEnvelopePins(unittest.TestCase):
    def test_all_service_error_classes_are_valueerror_with_code(self) -> None:
        self.assertEqual(
            DOMAINS_SOURCE.count("(ValueError)"), 4,
            "服务边界错误类应全部继承 ValueError（适配层可统一兜底）",
        )
        for class_name in (
            "AutoConnectPreferenceError", "CourseDataActionError",
            "ClientResetActionError", "CourseReviewActionError",
        ):
            self.assertIn(class_name, DOMAINS_SOURCE)
            self.assertIn(f"self.code = str(code)", DOMAINS_SOURCE)

    def _catch_site(self, class_name: str) -> str:
        marker = f"except {class_name} as exc:"
        index = HTTP_API_SOURCE.find(marker)
        if index < 0:
            # AutoConnect 走 ValueError 复用分支（isinstance 判别）。
            marker = f"isinstance(exc, {class_name})"
            index = HTTP_API_SOURCE.find(marker)
        self.assertGreaterEqual(index, 0, f"{class_name} 缺专属处置分支")
        return HTTP_API_SOURCE[index:index + 700]

    def test_course_review_error_maps_code_with_closed_fallback(self) -> None:
        body = self._catch_site("CourseReviewActionError")
        self.assertIn('getattr(exc, "code", "")', body)
        self.assertIn("course_review_failed", body)
        self.assertIn('"error_code"', body)

    def test_course_data_error_maps_code_with_closed_fallback(self) -> None:
        body = self._catch_site("CourseDataActionError")
        self.assertIn('getattr(exc, "code", "")', body)
        self.assertIn("course_data_action_invalid", body)
        self.assertIn('"error_code"', body)

    def test_client_reset_error_carries_retriable_flag(self) -> None:
        body = self._catch_site("ClientResetActionError")
        self.assertIn("courselens.api.error.v1", body, "reset 信封=带 schema 的新典形态")
        self.assertIn('"retriable"', body)
        self.assertIn("reset_action_invalid", body)

    def test_auto_connect_error_uses_conflict_status(self) -> None:
        body = self._catch_site("AutoConnectPreferenceError")
        self.assertIn("HTTPStatus.CONFLICT", body)
        self.assertIn('"error_code"', body)


if __name__ == "__main__":
    unittest.main()
