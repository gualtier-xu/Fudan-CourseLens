# 夜10-A APP-SHAPE-2 T2/T1/T8 钉面：窗态持久化是纯 pathlib/json 逻辑——
# 几何消毒/多显示器钳制/托盘偏好合并写/清理标记一次性消费/缓存体量求和。
# 损坏状态文件绝不拒绝开窗（永远回退默认值），原子写失败返回 False 不抛出。
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.runtime.window_state import (
    WINDOW_STATE_SCHEMA,
    clamp_geometry,
    consume_webview_cache_clear_request,
    load_window_state,
    request_webview_cache_clear,
    sanitize_geometry,
    save_window_state,
    webview_cache_bytes,
    window_state_path,
)


class WindowStatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-n10a-state-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_missing_state_file_falls_back_to_defaults(self) -> None:
        state = load_window_state(self.data_dir)
        self.assertEqual(state["geometry"]["width"], 1280)
        self.assertEqual(state["geometry"]["height"], 800)
        self.assertNotIn("x", state["geometry"])
        self.assertFalse(state["geometry"]["maximized"])
        # TRAY-FIX-1：默认=关窗收托盘（exit_on_close=False），旧键不迁移。
        self.assertFalse(state["exit_on_close"])
        self.assertFalse(state["tray_hint_shown"])

    def test_geometry_roundtrip_preserves_position_and_flag(self) -> None:
        self.assertTrue(
            save_window_state(
                self.data_dir,
                geometry={"x": -1400, "y": 20, "width": 1100, "height": 720, "maximized": True},
                exit_on_close=True,
            )
        )
        state = load_window_state(self.data_dir)
        self.assertEqual(state["geometry"]["x"], -1400)
        self.assertEqual(state["geometry"]["width"], 1100)
        self.assertTrue(state["geometry"]["maximized"])
        self.assertTrue(state["exit_on_close"])
        payload = json.loads(window_state_path(self.data_dir).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], WINDOW_STATE_SCHEMA)
        self.assertNotIn("minimize_to_tray", payload)

    def test_partial_save_merges_with_existing_state(self) -> None:
        save_window_state(
            self.data_dir,
            geometry={"x": 10, "y": 20, "width": 900, "height": 600, "maximized": False},
        )
        save_window_state(self.data_dir, exit_on_close=True)
        state = load_window_state(self.data_dir)
        self.assertEqual(state["geometry"]["x"], 10)
        self.assertTrue(state["exit_on_close"])

    def test_corrupt_state_file_degrades_to_defaults(self) -> None:
        window_state_path(self.data_dir).write_text("{not json", encoding="utf-8")
        state = load_window_state(self.data_dir)
        self.assertEqual(state["geometry"]["width"], 1280)
        self.assertFalse(state["exit_on_close"])

    def test_legacy_minimize_to_tray_key_is_ignored(self) -> None:
        # TRAY-FIX-1 迁移语义：旧键两种历史值都正确映射到 exit_on_close=False
        # （旧 true=收托盘恰为新默认；旧 false=退出正是本次修复要改掉的行为）。
        window_state_path(self.data_dir).write_text(
            json.dumps({
                "schema": WINDOW_STATE_SCHEMA,
                "geometry": {"width": 1280, "height": 800, "maximized": False},
                "minimize_to_tray": True,
            }),
            encoding="utf-8",
        )
        state = load_window_state(self.data_dir)
        self.assertFalse(state["exit_on_close"])

    def test_tray_hint_flag_roundtrips(self) -> None:
        save_window_state(self.data_dir, tray_hint_shown=True)
        self.assertTrue(load_window_state(self.data_dir)["tray_hint_shown"])
        save_window_state(self.data_dir, tray_hint_shown=False)
        self.assertFalse(load_window_state(self.data_dir)["tray_hint_shown"])

    def test_sanitize_geometry_rejects_malformed_values(self) -> None:
        self.assertIsNone(sanitize_geometry(None))
        self.assertIsNone(sanitize_geometry({"x": "a", "y": 0, "width": 10, "height": 10}))
        self.assertIsNone(sanitize_geometry({"x": 0, "y": 0, "width": -1, "height": 10}))
        self.assertIsNone(sanitize_geometry({"x": 0, "y": 0, "width": 10}))
        self.assertIsNone(sanitize_geometry({"x": True, "y": 0, "width": 10, "height": 10}))
        sanitized = sanitize_geometry({"x": 1.0, "y": 2.0, "width": 800.0, "height": 600.0})
        self.assertEqual(sanitized, {"x": 1, "y": 2, "width": 800, "height": 600, "maximized": False})

    def test_save_rejects_invalid_geometry_without_clobbering(self) -> None:
        save_window_state(
            self.data_dir,
            geometry={"x": 1, "y": 2, "width": 800, "height": 600, "maximized": False},
        )
        self.assertFalse(save_window_state(self.data_dir, geometry={"width": -5, "height": 0}))
        self.assertEqual(load_window_state(self.data_dir)["geometry"]["width"], 800)


class ClampGeometryTests(unittest.TestCase):
    def test_inside_virtual_screen_is_untouched(self) -> None:
        geometry = {"x": 100, "y": 100, "width": 800, "height": 600, "maximized": False}
        self.assertEqual(clamp_geometry(geometry, (0, 0, 3840, 2160)), geometry)

    def test_offscreen_window_snaps_to_virtual_origin(self) -> None:
        clamped = clamp_geometry(
            {"x": -5000, "y": -5000, "width": 800, "height": 600, "maximized": False},
            (0, 0, 1920, 1080),
        )
        self.assertEqual(clamped["x"], 0)
        self.assertEqual(clamped["y"], 0)

    def test_second_monitor_with_enough_visible_band_is_kept(self) -> None:
        # 虚拟桌面 (0..3840)：x=1920 的窗口有完整可见带——合法，不动。
        geometry = {"x": 1920, "y": 100, "width": 800, "height": 600, "maximized": False}
        self.assertEqual(clamp_geometry(geometry, (0, 0, 3840, 1080)), geometry)

    def test_oversized_window_is_capped_to_screen(self) -> None:
        clamped = clamp_geometry(
            {"x": 0, "y": 0, "width": 9999, "height": 9999, "maximized": False},
            (0, 0, 1920, 1080),
        )
        self.assertEqual(clamped["width"], 1920)
        self.assertEqual(clamped["height"], 1080)

    def test_degenerate_virtual_screen_leaves_geometry_alone(self) -> None:
        geometry = {"x": 5, "y": 5, "width": 800, "height": 600, "maximized": False}
        self.assertEqual(clamp_geometry(geometry, (0, 0, 0, 0)), geometry)


class WebviewCacheSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cl-n10a-cache-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def test_cache_marker_is_one_shot(self) -> None:
        self.assertFalse(consume_webview_cache_clear_request(self.data_dir))
        self.assertTrue(request_webview_cache_clear(self.data_dir))
        self.assertTrue(consume_webview_cache_clear_request(self.data_dir))
        self.assertFalse(consume_webview_cache_clear_request(self.data_dir))

    def test_cache_bytes_counts_profile_only(self) -> None:
        from src.runtime.window_state import webview_profile_dir

        profile = webview_profile_dir(self.data_dir)
        (profile / "EBWebView").mkdir(parents=True)
        (profile / "EBWebView" / "blob_storage").write_bytes(b"x" * 1024)
        (profile / "cache").write_bytes(b"y" * 512)
        self.assertEqual(webview_cache_bytes(self.data_dir), 1536)
        self.assertEqual(webview_cache_bytes(self.data_dir / "missing-root"), 0)


if __name__ == "__main__":
    unittest.main()
