"""SRC-CLEANUP-1 U8: 相邻帧组内保信息最大张 + 设备/系统占位画面 deck pass。

判据与冻结阈值校准记录见 ``junk_page_filter.JUNK_PLACEHOLDER_*``（2026-09-22
五讲 437 页离线重放：当日讲次 52→46 全中、设备占位全消，跨讲次真稀疏标题页
零误杀、教室桌面真占位同灭）。合成夹具特征常数经实量测校准：内容页
std≈63-68、占位页 std≈24-29、折叠对 dH=0、组内密度差 ≈5；若夹具漂移，
下方 self-check 断言会先于行为断言失败并指明哪一项漂了。
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from random import Random

from PIL import Image, ImageDraw

from src.runtime.courseware_pdf import CoursewarePdfRun
from src.runtime.junk_page_filter import (
    DEVICE_PLACEHOLDER_SKIP_REASON,
    JUNK_PLACEHOLDER_MIN_DECK,
    JUNK_PLACEHOLDER_STD_ABS_MAX,
    JUNK_PLACEHOLDER_STD_MEDIAN_RATIO,
    frame_features,
    hamming_distance,
    information_density,
    placeholder_verdict,
)


# --- 夹具（特征常数已实量测校准，见模块 docstring） -------------------------

def _content_page(index: int, w: int = 800, h: int = 450) -> Image.Image:
    """密集内容页：亮底+三区六块深色块阵+注解横线，std≈62-66。"""
    img = Image.new("RGB", (w, h), (236, 238, 242))
    rng = Random(index * 2654435761 % 2**31)
    pen = ImageDraw.Draw(img)
    cell_w, cell_h = w // 3, h // 2
    for cell in range(6):
        col, row = cell % 3, cell // 3
        x0 = col * cell_w + rng.randint(4, cell_w - 90)
        y0 = row * cell_h + rng.randint(4, cell_h - 60)
        pen.rectangle([x0, y0, x0 + 70 + rng.randint(0, 30), y0 + 40 + rng.randint(0, 20)], fill=(24, 26, 46))
        for bar in range(6):
            pen.line([x0, y0 + 48 + bar * 7, x0 + 90 + rng.randint(0, 110), y0 + 48 + bar * 7], fill=(40, 44, 76), width=3)
    return img


def _placeholder_light(w: int = 800, h: int = 450) -> Image.Image:
    """Miracast 型投屏待机页：浅灰均匀底+居中深块+细线，std≈24。"""
    img = Image.new("RGB", (w, h), (208, 209, 211))
    pen = ImageDraw.Draw(img)
    pen.rectangle([w // 2 - 120, h // 2 - 60, w // 2 + 120, h // 2 - 20], fill=(70, 74, 78))
    for bar in range(3):
        pen.line([w // 2 - 90, h // 2 + 4 + bar * 10, w // 2 + 90 - bar * 30, h // 2 + 4 + bar * 10], fill=(120, 124, 128), width=4)
    pen.rectangle([w // 2 - 30, h // 2 + 60, w // 2 + 30, h // 2 + 90], fill=(100, 104, 108))
    pen.line([40, h - 40, w // 2, h - 40], fill=(150, 152, 156), width=3)
    return img


def _placeholder_dark(w: int = 800, h: int = 450) -> Image.Image:
    """显示器确认框型：深色底+居中浅框，std≈28。"""
    img = Image.new("RGB", (w, h), (52, 54, 56))
    pen = ImageDraw.Draw(img)
    pen.rectangle([w // 2 - 150, h // 2 - 50, w // 2 + 150, h // 2 + 50], fill=(170, 170, 168))
    pen.rectangle([w // 2 - 150, h // 2 - 50, w // 2 + 150, h // 2 - 30], fill=(96, 102, 108))
    for bar in range(2):
        pen.line([w // 2 - 110, h // 2 - 8 + bar * 16, w // 2 + 110 - bar * 40, h // 2 - 8 + bar * 16], fill=(80, 84, 88), width=4)
    return img


def _title_page(w: int = 800, h: int = 450) -> Image.Image:
    """真稀疏标题页（「Thanks!」回归钉）：大字块+横线，std≈48。"""
    img = Image.new("RGB", (w, h), (240, 242, 246))
    pen = ImageDraw.Draw(img)
    pen.rectangle([w // 2 - 180, h // 2 - 40, w // 2 + 180, h // 2 + 10], fill=(40, 44, 72))
    pen.line([60, h - 60, w - 60, h - 60], fill=(60, 64, 96), width=5)
    pen.rectangle([60, 40, 240, 52], fill=(90, 96, 130))
    return img


def _add_corner_block(img: Image.Image) -> Image.Image:
    """叠加角部墨块：dHash 不动（折叠对前提），信息密度显著上升。"""
    pen = ImageDraw.Draw(img)
    w, h = img.size
    pen.rectangle([8, h - 52, 128, h - 8], fill=(16, 18, 36))
    pen.rectangle([136, h - 44, 206, h - 8], fill=(16, 18, 36))
    return img


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _self_check():
    """夹具特征 self-check：漂移时先于行为断言失败。"""
    contents = [_content_page(i) for i in range(8)]
    stds = [information_density(img) for img in contents]
    assert all(60.0 <= s <= 70.0 for s in stds), f"content std drifted: {stds}"
    light, dark, title = _placeholder_light(), _placeholder_dark(), _title_page()
    f_light, f_dark, f_title = (frame_features(x) for x in (light, dark, title))
    s_light, s_dark = information_density(light), information_density(dark)
    assert 20.0 <= s_light <= 30.0, f"light placeholder std drifted: {s_light}"
    assert 24.0 <= s_dark <= 34.0, f"dark placeholder std drifted: {s_dark}"
    assert float(f_light["range"]) < 200 and float(f_dark["range"]) < 200, "placeholder range drifted"
    assert int(f_light["near"]) > 18 and int(f_dark["near"]) > 18, "placeholder near drifted into blacklist zone"
    s_title = information_density(title)
    assert 42.0 <= s_title <= 54.0, f"title std drifted: {s_title}"
    base, dense = _content_page(99), _add_corner_block(_content_page(99))
    d_h = hamming_distance(str(frame_features(base)["dhash"]), str(frame_features(dense)["dhash"]))
    assert d_h <= 5, f"fold pair dHash drifted apart: {d_h}"
    assert information_density(dense) > information_density(base), "corner block failed to add density"
    return contents, light, dark, title, base, dense


# --- 判定函数钉 -------------------------------------------------------------

class PlaceholderVerdictTests(unittest.TestCase):
    def test_low_structure_page_with_deck_anchor_dies(self):
        features = {"gv": 24.0 ** 2, "range": 150.0}
        self.assertTrue(placeholder_verdict(features, 95.0))

    def test_sparse_title_page_survives_thanks_regression_anchor(self):
        # 「Thanks!」回归钉（真实校准数）：std 28.7 的真标题页在 median 52.2
        # 的讲次（阈值 26.1）必须存活——单看绝对窗会误杀。
        features = {"gv": 28.7 ** 2, "range": 173.0}
        self.assertFalse(placeholder_verdict(features, 52.2))
        # 但同一页掉进 Dense 讲次（median 95.5 → 阈值 47.75）则判占位——
        # 该边界残余风险已在结果档案记录（宁漏勿杀方向上的已知让步）。
        self.assertTrue(placeholder_verdict(features, 95.5))

    def test_absolute_window_gates(self):
        self.assertFalse(placeholder_verdict({"gv": 60.0 ** 2, "range": 150.0}, 95.0))
        self.assertFalse(placeholder_verdict({"gv": 24.0 ** 2, "range": 210.0}, 95.0))
        self.assertTrue(placeholder_verdict({"gv": 24.0 ** 2, "range": 150.0}, None))


# --- 管线级行为钉 -----------------------------------------------------------

class _Fetcher:
    def __init__(self, blobs):
        self.blobs = list(blobs)

    def __call__(self, record):
        return self.blobs.pop(0)


def _record(index):
    return {
        "id": index,
        "original_id": str(index),
        "pptimgurl": f"https://slides.invalid/capture-{index}.jpg",
        "created_sec": index * 10,
        "created_ms": index * 10000,
        "taskid": "t",
        "ocr_text": "",
    }


class CoursewareU8PipelineTests(unittest.TestCase):
    def setUp(self):
        (
            self.contents, self.light, self.dark, self.title,
            self.base, self.dense,
        ) = _self_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base_dir = Path(self._tmp.name)

    def _run(self, images):
        blobs = [_jpeg(img) for img in images]
        records = [_record(i) for i in range(len(blobs))]
        run = CoursewarePdfRun(
            records=records,
            work_dir=self.base_dir / "work",
            out_pdf=self.base_dir / "slides.pdf",
            out_manifest=self.base_dir / "manifest.json",
            fetch_page=_Fetcher(blobs),
        )
        outcome = run.run()
        manifest = json.loads((self.base_dir / "manifest.json").read_text(encoding="utf-8"))
        return outcome, manifest, blobs

    def test_adjacent_group_keeps_the_densest_frame(self):
        # U8①：板书渐增组（候选严格更密）→ 候选原位接替，原基张折进台账
        outcome, manifest, blobs = self._run([self.base, self.dense])
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], 1)
        self.assertEqual(outcome["skipped"].get("duplicate_adjacent"), 1)
        page = manifest["pages"][0]
        self.assertEqual(page["source_sha256"], hashlib.sha256(blobs[1]).hexdigest())
        self.assertEqual(page["original_id"], "1")
        self.assertEqual(
            [item["original_id"] for item in page["duplicates"]], ["0"],
        )

    def test_sparser_adjacent_frame_keeps_first(self):
        # U8①：擦板反向（候选更稀）→ 保首张不替换
        outcome, manifest, blobs = self._run([self.dense, self.base])
        self.assertEqual(outcome["kept"], 1)
        self.assertEqual(outcome["skipped"].get("duplicate_adjacent"), 1)
        page = manifest["pages"][0]
        self.assertEqual(page["source_sha256"], hashlib.sha256(blobs[0]).hexdigest())
        self.assertEqual(page["original_id"], "0")
        self.assertEqual(
            [item["original_id"] for item in page["duplicates"]], ["1"],
        )

    def test_device_placeholder_pages_die_in_deck_pass(self):
        # U8②：内容讲次末尾的投屏待机页+确认框 → 整杀、闭集计数
        images = list(self.contents) + [self.light, self.dark]
        outcome, manifest, blobs = self._run(images)
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], len(self.contents))
        self.assertEqual(outcome["skipped"].get(DEVICE_PLACEHOLDER_SKIP_REASON), 2)
        kept_shas = {page["source_sha256"] for page in manifest["pages"]}
        self.assertEqual(len(kept_shas), len(self.contents))
        for blob in blobs[-2:]:
            self.assertNotIn(hashlib.sha256(blob).hexdigest(), kept_shas)

    def test_deck_below_anchor_floor_never_judges(self):
        # 锚稳定性守卫：保留页 < JUNK_PLACEHOLDER_MIN_DECK 不判占位
        small = list(self.contents[:3]) + [self.light, self.dark]
        outcome, _manifest, _blobs = self._run(small)
        self.assertEqual(outcome["kept"], 5)
        self.assertIsNone(outcome["skipped"].get(DEVICE_PLACEHOLDER_SKIP_REASON))

    def test_sparse_title_page_survives_dense_deck(self):
        # 「Thanks!」回归钉（管线级）：密集讲次里的真稀疏标题页必须存活
        images = list(self.contents) + [self.title]
        outcome, manifest, _blobs = self._run(images)
        self.assertEqual(outcome["kept"], 9)
        self.assertIsNone(outcome["skipped"].get(DEVICE_PLACEHOLDER_SKIP_REASON))


if __name__ == "__main__":
    unittest.main()
