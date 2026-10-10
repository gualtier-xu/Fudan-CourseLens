"""直播闭集码等集钉（N6L S1 U4 · 两处同改纪律的钉）。

钉三件事：
1. ENTRY_FAILURES（前端 live-state.js）≡ 专卡集（11 个学生可行动码）——
   设计纸 §12.3 逐码表；两侧任何一侧增删改名单即红。
2. 专卡集 ⊆ 后端可发集（LiveRoomError/gate 表/denied 的字面量并集）——
   后端改名一个专卡码而前端未同改，此钉红（emittership 齿）。
3. 后端可发集 ⊆ 专卡集 ∪ GUARD_FAMILY_ALLOWLIST——后端新增一个
   LiveRoomError 码而未给专卡时必须在此清单显式登记（走 fatal 卡+诊断行的
   第廿六案兜底），否则红；清单条目后端已删除的也红（防陈旧）。

边界（诚实记录）：
- status 观测通道的动态码（``live_{state}`` 缺省拼法与 raw.code 透传）不在
  静态扫描内——它们经第廿六案 resolveLiveErrorCard 的 fatal+诊断行兜底，
  任何未知码对学生都诚实可行动。
- live_network_interrupted/live_decode_failed/live_format_unsupported/
  runtime_failed 为前端侧 taxonomy 码（播放器/页面本地判定），后端不发。
"""

import re
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SRC = Path(__file__).resolve().parents[1] / "src"

# 学生可行动专卡码（§12.3；前端 ENTRY_FAILURES 必须与此表等集）
DEDICATED_ENTRY_CODES = {
    "live_session_expired", "live_resource_expired", "live_grant_invalid",
    "live_grant_identity_changed", "live_authorization_denied",
    "live_authorization_revoked", "live_playback_not_open",
    "live_upstream_rejected", "live_upstream_unreachable",
    "live_stream_unavailable", "live_content_type_rejected",
}

# 防护族/观测族长尾：后端可发、无专卡——由 fatal 卡+诊断行兜底（第廿六案）。
# 后端新增 LiveRoomError 码时：或给专卡并移入 DEDICATED_ENTRY_CODES，
# 或有意识地登记于此；后端删除码时同步移出，两边都不许静默。
GUARD_FAMILY_ALLOWLIST = {
    "live_dns_failed", "live_dns_invalid", "live_dns_empty",
    "live_dns_rebinding_rejected", "live_private_address_rejected",
    "live_https_required", "live_userinfo_rejected", "live_port_rejected",
    "live_peer_unverified", "live_range_rejected",
    "live_cross_origin_redirect_rejected", "live_redirect_limit",
    "live_response_too_large", "live_content_length_invalid",
    "live_manifest_encoding_rejected", "live_manifest_invalid",
    "live_flv_rejected", "live_session_unavailable",
    "live_session_cookie_required", "live_observation_offline",
    "live_observation_failed", "live_view_unknown",
}

FRONTEND_ONLY_CODES = {
    "live_network_interrupted", "live_decode_failed",
    "live_format_unsupported", "runtime_failed",
}
FRONTEND_ONLY_LIVE_PREFIX_CODES = FRONTEND_ONLY_CODES - {"runtime_failed"}


def _live_state_source() -> str:
    return (FRONTEND / "modules" / "live-state.js").read_text(encoding="utf-8")


def _entry_failure_codes() -> set:
    source = _live_state_source()
    section = source.split("export const ENTRY_FAILURES", 1)[1].split("});", 1)[0]
    return set(re.findall(r"(live_[a-z_]+):", section))


def _error_card_codes() -> set:
    source = _live_state_source()
    section = source.split("export const LIVE_ERROR_CARDS", 1)[1].split("\n});", 1)[0]
    return set(re.findall(r"(live_[a-z_]+):", section))


def _backend_error_codes() -> set:
    """静态可证的后端错误码出口：LiveRoomError 字面量、gate 映射值、
    _public_status 的观测/授权码、denied 直发。

    SWEEPFIX-R2 W2：宽松尾参扫描以 ``(?<!purpose=)`` 排除审计用途标签——
    ``ensure_egress_allowed(..., purpose="live_media")`` 的标签只进审计行
    （EgressBlockedError 审计 purpose= 字段），拒绝本身由接线点收编为
    live_upstream_unreachable（live_room.py:424），零学生错误码面；不排除
    会把审计标签误报成「未登记码」（两车道全量回归同报红即此误报）。"""
    codes = set()
    live_room = (SRC / "runtime" / "live_room.py").read_text(encoding="utf-8")
    codes.update(re.findall(r'LiveRoomError\("([a-z_]+)"', live_room))
    # _public_status 第四参（观测/授权码；含跨行调用）；purpose= 标签非出口
    codes.update(re.findall(r'(?<!purpose=)"(live_[a-z_]+)"\s*\)', live_room))
    codes.update(re.findall(r'_public_status\([^)]*?"(live_[a-z_]+)"', live_room, re.S))
    icourse = (SRC / "api" / "icourse.py").read_text(encoding="utf-8")
    codes.update(re.findall(r'LiveRoomError\("([a-z_]+)"', icourse))
    gate = icourse.split("_PLAYBACK_GATE_CODES", 1)[1].split("}", 1)[0]
    codes.update(re.findall(r'"(live_[a-z_]+)"', gate))
    return codes


class LiveErrorCodeParityTests(unittest.TestCase):
    def test_entry_failures_equal_dedicated_closed_set(self):
        self.assertEqual(
            _entry_failure_codes(), DEDICATED_ENTRY_CODES,
            "ENTRY_FAILURES ≢ 专卡闭集：前端码表须两处同改",
        )

    def test_dedicated_codes_are_actually_emitted_by_backend(self):
        missing = DEDICATED_ENTRY_CODES - _backend_error_codes()
        self.assertEqual(
            missing, set(),
            f"专卡码后端已不再发出（改名/删除未同改前端）：{sorted(missing)}",
        )

    def test_backend_codes_all_accounted_for(self):
        backend = _backend_error_codes()
        # 审计用途标签永不出现在出口集（扫描器误报防回退钉，SWEEPFIX-R2 W2）：
        self.assertNotIn("live_media", backend,
                         "purpose= 审计标签漏排除：宽松扫描误把审计标签当错误码")
        covered = DEDICATED_ENTRY_CODES | GUARD_FAMILY_ALLOWLIST
        self.assertEqual(
            backend - covered, set(),
            "后端新增直播错误码未登记：给专卡（入 DEDICATED_ENTRY_CODES）"
            "或登记 GUARD_FAMILY_ALLOWLIST（fatal 卡+诊断行兜底）",
        )
        stale = GUARD_FAMILY_ALLOWLIST - backend
        self.assertEqual(
            stale, set(),
            f"防护族清单含后端已删除的码（陈旧条目须移出）：{sorted(stale)}",
        )

    def test_dedicated_codes_have_rendering_cards(self):
        missing = DEDICATED_ENTRY_CODES - _error_card_codes()
        self.assertEqual(missing, set(), "专卡码缺逐码错误卡（§12.3）")

    def test_frontend_only_codes_have_cards(self):
        self.assertEqual(
            FRONTEND_ONLY_LIVE_PREFIX_CODES,
            _error_card_codes() - DEDICATED_ENTRY_CODES,
            "前端 taxonomy 码闭集漂移",
        )
        self.assertIn("runtime_failed: Object.freeze", _live_state_source(),
                      "fatal 兜底卡（runtime_failed）必须存在")

    def test_cards_carry_title_body_actions(self):
        source = _live_state_source()
        section = source.split("export const LIVE_ERROR_CARDS", 1)[1].split("\n});", 1)[0]
        for code in re.findall(r"(live_[a-z_]+):", section):
            entry = section.split(f"{code}:", 1)[1].split("\n", 1)[0]
            self.assertIn("title:", entry, f"{code} 卡缺标题")
            self.assertIn("body:", entry, f"{code} 卡缺正文")
            self.assertIn("actions:", entry, f"{code} 卡缺动作")


if __name__ == "__main__":
    unittest.main()
