"""夜10-C T25：api/v3 线协议信封直钉。

``api_v3.envelope`` 是前端消费的线协议形状（此前只有路由级隐式覆盖）：
schema/version/request_id/data 四键冻结，request_id 空值不出键。
"""

from __future__ import annotations

import unittest

from src.runtime.api_v3 import API_VERSION, envelope


class ApiV3EnvelopePins(unittest.TestCase):
    def test_envelope_shape_is_frozen(self) -> None:
        value = envelope({"k": "v"}, request_id="req-1")
        self.assertEqual(
            list(value.keys()), ["schema", "version", "request_id", "data"]
        )
        self.assertEqual(value["schema"], "courselens.api.v3")
        self.assertEqual(value["version"], API_VERSION)
        self.assertEqual(value["request_id"], "req-1")
        self.assertEqual(value["data"], {"k": "v"})

    def test_envelope_without_request_id_defaults_to_empty_string(self) -> None:
        value = envelope([])
        self.assertEqual(value["request_id"], "", "缺省 request_id=空串（键恒在）")
        self.assertEqual(value["data"], [])

    def test_envelope_never_wraps_a_second_time(self) -> None:
        inner = envelope({"k": 1})
        outer = envelope(inner)
        self.assertEqual(outer["data"], inner, "透传语义：信封只包一层")


if __name__ == "__main__":
    unittest.main()
