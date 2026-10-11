"""Local junk-page screening: ports the worker filter into the client run.

The managed client ships no numpy and no OCR stack, so the local pipeline
runs the text-free stages only (M15 blacklist, U4 featureless + chrome
gates, near-duplicate families).  These pins hold the local port to the
same behavior as ``worker/courselens_worker/junk_filter.py``: identical
perceptual hashes, identical frozen thresholds, and no kill path that a
real lecture page can reach without sitting in the blacklist near-miss
zone.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from random import Random
from unittest.mock import patch

from PIL import Image, ImageDraw

from src.runtime import junk_page_filter
from src.runtime.courseware_pdf import (
    POLICY_VERSION,
    SKIP_REASONS,
    CoursewarePdfRun,
    PageBudget,
)
from src.runtime.junk_page_filter import (
    JUNK_PAGE_SKIP_REASON,
    adjudicate_deck_local,
    dual_hash,
    entry_for,
    featureless_verdict,
    frame_features,
    hamming_distance,
    near_duplicate_families,
    page_is_junk,
)

BASE_W, BASE_H = 800, 450


def _naked_table(seed: int, coarse: bool = False) -> Image.Image:
    """A slide-like notice table with no browser chrome bands.

    Same deterministic bordered-table construction as the worker fixtures,
    minus the window chrome: rich enough to clear the featureless and
    chrome gates, so only the hash stages can touch it.  ``coarse`` drops a
    seed-positioned block over several grid cells, perturbing enough
    gradient cells to land in the blacklist near-miss zone (13..18 bits).
    """
    rng = Random(seed)
    image = Image.new("RGB", (BASE_W, BASE_H), "white")
    draw = ImageDraw.Draw(image)
    left, top = int(BASE_W * 0.10), int(BASE_H * 0.12)
    right, bottom = int(BASE_W * 0.90), int(BASE_H * 0.88)
    draw.rectangle([left, top, right, bottom], outline=(120, 120, 120), width=3)
    rows, columns = 6, 4
    for row in range(1, rows):
        y = top + (bottom - top) * row // rows
        draw.line([left, y, right, y], fill=(150, 150, 150), width=2)
    for column in range(1, columns):
        x = left + (right - left) * column // columns
        draw.line([x, top, x, bottom], fill=(150, 150, 150), width=2)
    for row in range(rows):
        for column in range(columns - 1):
            x0 = left + (right - left) * column // columns + 8
            y0 = top + (bottom - top) * row // rows + 8
            x1 = x0 + 30 + int(rng.randint(0, 40))
            y1 = y0 + 6
            if x1 < left + (right - left) * (column + 1) // columns - 4:
                draw.rectangle([x0, y0, x1, y1], fill=(60, 60, 66))
    if coarse:
        block_x = left + 20 + (seed * 37) % max(1, (right - left) - 130)
        block_y = top + 20 + (seed * 53) % max(1, (bottom - top) - 70)
        draw.rectangle([block_x, block_y, block_x + 110, block_y + 50],
                       fill=(172, 88, 72), outline=(90, 40, 30), width=2)
    return image


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _near(image: Image.Image, row: dict[str, str]) -> int:
    dhash_hex, ahash_hex = dual_hash(image)
    return min(
        hamming_distance(dhash_hex, row["dhash"]),
        hamming_distance(ahash_hex, row["ahash"]),
    )


def _near_miss_variants(row: dict[str, str], count: int) -> list[Image.Image]:
    """Build a variant family landing in the 13..18 near-miss zone.

    One coarse perturbed table sits 13-18 bits from the (registered) master
    row — past the blacklist, inside the family-eligible zone.  Members
    differ only in the ROI-excluded top strip, so they hash identically and
    cluster as one family, exactly like re-captures of one junk page.
    """
    for seed in range(2, 4000):
        base = _naked_table(seed, coarse=True)
        if not 12 < _near(base, row) <= 18:
            continue
        members = []
        for member in range(count):
            image = base.copy()
            draw = ImageDraw.Draw(image)
            draw.rectangle([0, 0, BASE_W, 36], fill=(30 * member + 10, 190, 40))
            members.append(image)
        return members
    raise AssertionError("fixture search found no near-miss variant")


def _chrome_window() -> Image.Image:
    """A full-window browser screenshot shape without hashable notice content.

    The top 12% band is a uniform bar (low row variance); the strong
    contrast rows sit just below it, inside the 12-15% edge band — the same
    anatomy as tab strip + address bar.  A striped taskbar fills the bottom
    band.  Far from any blacklist row, so only the chrome gate can kill it.
    """
    image = Image.new("RGB", (BASE_W, BASE_H), (128, 128, 128))
    draw = ImageDraw.Draw(image)
    top_h = int(BASE_H * 0.12)
    draw.rectangle([0, 0, BASE_W, top_h], fill=(205, 205, 205))
    edge_bottom = int(BASE_H * 0.15)
    for y in range(top_h + 1, edge_bottom):
        draw.line([0, y, BASE_W, y], fill=(25, 25, 25) if y % 2 else (235, 235, 235))
    bottom_h = int(BASE_H * 0.08)
    for index in range(bottom_h):
        y = BASE_H - bottom_h + index
        draw.line([0, y, BASE_W, y], fill=(240, 240, 240) if index % 2 else (40, 40, 40))
    return image


def _blank_page() -> Image.Image:
    return Image.new("RGB", (BASE_W, BASE_H), (128, 128, 128))


def _dark_board() -> Image.Image:
    """Chalk strokes on a dark board: low variance, wide dynamic range.

    The chalk coverage must stay above the 1st/99th percentile window
    (>1% of pixels) for the range exemption to see it, matching real
    board photos.
    """
    image = Image.new("RGB", (BASE_W, BASE_H), (20, 22, 24))
    draw = ImageDraw.Draw(image)
    rng = Random(11)
    for _ in range(60):
        x0 = rng.randint(20, BASE_W - 160)
        y0 = rng.randint(20, BASE_H - 60)
        draw.line([x0, y0, x0 + rng.randint(80, 150), y0 + rng.randint(-6, 6)],
                  fill=(235, 235, 225), width=3)
    return image


def _rich_slide(seed: int = 7) -> Image.Image:
    """Ordinary lecture page: must survive every local stage."""
    image = Image.new("RGB", (BASE_W, BASE_H), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    rng = Random(seed)
    for _ in range(30):
        x0 = rng.randint(20, BASE_W - 200)
        y0 = rng.randint(20, BASE_H - 60)
        draw.rectangle([x0, y0, x0 + rng.randint(80, 180), y0 + rng.randint(10, 40)],
                       outline=(60, 60, 160), width=2)
    draw.ellipse([BASE_W * 0.6, BASE_H * 0.6, BASE_W * 0.85, BASE_H * 0.9],
                 fill=(200, 60, 60))
    return image


class JunkFilterLocalTests(unittest.TestCase):
    """Stage-level pins for the numpy-free port."""

    def test_junk_page_reason_is_registered_in_the_closed_skip_set(self):
        self.assertIn(JUNK_PAGE_SKIP_REASON, SKIP_REASONS)

    def test_blacklist_row_from_a_page_catches_exact_and_peripheral_copies(self):
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            self.assertTrue(page_is_junk(master))
            twin = master.copy()
            draw = ImageDraw.Draw(twin)
            draw.rectangle([0, 0, BASE_W, 30], fill=(10, 10, 10))  # outer band only
            self.assertTrue(page_is_junk(twin), "ROI 外的外围改动画不得影响命中")
        self.assertFalse(page_is_junk(_rich_slide()))

    def test_featureless_kills_blank_and_spares_dark_board(self):
        blank_features = frame_features(_blank_page())
        self.assertTrue(featureless_verdict(blank_features))
        board_features = frame_features(_dark_board())
        self.assertGreaterEqual(float(board_features["range"]), 60.0)
        self.assertFalse(featureless_verdict(board_features))
        self.assertFalse(page_is_junk(_dark_board()))

    def test_chrome_gate_kills_full_window_and_spares_content(self):
        window = _chrome_window()
        features = frame_features(window)
        self.assertGreaterEqual(int(features["strong_edges"]), 10)
        self.assertGreater(float(features["bottom_var"]), 1500.0)
        self.assertTrue(page_is_junk(window))
        self.assertFalse(page_is_junk(_rich_slide()))

    def test_family_of_near_variants_dies_without_any_ocr_text(self):
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        variants = _near_miss_variants(row, 3)
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            records = [frame_features(image) for image in variants]
            verdicts = adjudicate_deck_local(records)
        self.assertEqual(verdicts, [True, True, True])

    def test_two_variant_copies_are_never_a_family(self):
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        variants = _near_miss_variants(row, 2)
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            records = [frame_features(image) for image in variants]
            verdicts = adjudicate_deck_local(records)
        self.assertEqual(verdicts, [False, False])

    def test_family_clustering_needs_the_near_miss_zone(self):
        far_pages = [_rich_slide(), _dark_board(), _chrome_window()]
        records = [frame_features(image) for image in far_pages]
        self.assertEqual(near_duplicate_families(records), [])
        self.assertEqual(adjudicate_deck_local(records), [False] * 3)


class LocalRunJunkScreeningTests(unittest.TestCase):
    """End-to-end pins through CoursewarePdfRun's manual local path."""

    def _run_pass(self, blobs, *, budget=None, prefix="junk-run", work_dir=None):
        base = Path(tempfile.mkdtemp(prefix=prefix))
        fetched: list[str] = []

        def fetch(record):
            fetched.append(str(record.get("original_id") or ""))
            return blobs[int(record["id"])]

        run = CoursewarePdfRun(
            records=[{"id": index, "original_id": str(index),
                      "pptimgurl": f"https://slides.invalid/capture-{index}.jpg",
                      "created_sec": index * 10, "created_ms": index * 10_000,
                      "taskid": "t", "ocr_text": ""}
                     for index in range(len(blobs))],
            work_dir=work_dir or (base / "work"),
            out_pdf=base / "out.pdf",
            out_manifest=base / "manifest.json",
            fetch_page=fetch,
            budget=budget or PageBudget(),
        )
        return run, fetched

    def _pdf_page_count(self, path: Path) -> int:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)

    def test_notice_master_and_variant_family_are_all_filtered(self):
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        variants = _near_miss_variants(row, 3)
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            blobs = [
                _png_bytes(_rich_slide()),
                _png_bytes(master),
                *[_png_bytes(image) for image in variants],
            ]
            run, fetched = self._run_pass(blobs)
            outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], 1, "只有真实讲义页存活")
        self.assertEqual(outcome["skipped"].get(JUNK_PAGE_SKIP_REASON), 4)
        self.assertEqual(self._pdf_page_count(Path(outcome["pdf"])), 1)
        manifest = json.loads(Path(outcome["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["kept"], 1)
        self.assertEqual(
            manifest["skipped"].get(JUNK_PAGE_SKIP_REASON), 4,
            "闭集 skip 计数随清单发布",
        )
        self.assertEqual(len(fetched), 5, "过滤发生在下载之后，五页都被取过一次")

    def test_blank_and_chrome_pages_skip_while_content_and_board_survive(self):
        blobs = [
            _png_bytes(_rich_slide()),
            _png_bytes(_blank_page()),
            _png_bytes(_chrome_window()),
            _png_bytes(_dark_board()),
        ]
        run, _fetched = self._run_pass(blobs)
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], 2)
        self.assertEqual(outcome["skipped"].get(JUNK_PAGE_SKIP_REASON), 2)
        self.assertEqual(self._pdf_page_count(Path(outcome["pdf"])), 2)

    def test_filter_fault_never_fails_the_batch(self):
        blobs = [_png_bytes(_rich_slide(seed=7)), _png_bytes(_rich_slide(seed=8))]
        run, _fetched = self._run_pass(blobs)
        import src.runtime.courseware_pdf as pdf_module

        with patch.object(pdf_module, "frame_features",
                          side_effect=RuntimeError("filter exploded")):
            outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], 2, "过滤器故障时页面按内容保留")

    def test_near_solid_pages_in_measured_gap_are_junk(self):
        """追加C①：实测漏网段——深色近纯 std≈9.6（p14/p41 实测）与近白
        std≈4.8（p62 实测）必须判垃圾；真实密度页（std≥15）照常保留。"""

        def _noisy_fill(mean: float, sigma: float) -> Image.Image:
            rng = Random(int(mean))
            image = Image.new("RGB", (BASE_W, BASE_H), (int(mean),) * 3)
            draw = ImageDraw.Draw(image)
            for _ in range(int(BASE_W * BASE_H * 0.12)):
                x = rng.randint(0, BASE_W - 1)
                y = rng.randint(0, BASE_H - 1)
                delta = rng.choice([-2, -1, 1, 2])
                value = max(0, min(255, int(mean) + int(delta * sigma)))
                draw.point((x, y), fill=(value, value, value))
            return image

        dark_near_solid = _noisy_fill(40, 9.6 / 2)   # 深色板面，std≈9.6 量级
        near_white = _noisy_fill(235, 4.8 / 2)        # 近白页，std≈4.8 量级
        for image in (dark_near_solid, near_white):
            features = frame_features(image)
            self.assertLess(
                float(features["gv"]), junk_page_filter.JUNK_LOCAL_FEATURELESS_GV_MAX,
                "夹具须落进实测漏网段（方差<144）",
            )
            self.assertTrue(page_is_junk(image, features), "近纯页必须判垃圾")

        rich = _rich_slide()
        self.assertFalse(page_is_junk(rich), "真实密度内容页不得误吞")

    def test_adjacent_frames_within_dhash_five_keep_first_only(self):
        """追加C②：同构异色的相邻帧（dH=0，非字节重复）只保留首张。"""
        base = _rich_slide(seed=7)
        twin = base.copy()
        draw = ImageDraw.Draw(twin)
        draw.ellipse([10, 10, 30, 30], fill=(90, 90, 90))  # 微改一帧：dH 小但字节不同
        blobs = [_png_bytes(base), _png_bytes(twin)]
        run, fetched = self._run_pass(blobs, prefix="adjacent-pair")
        outcome = run.run()
        self.assertEqual(outcome["kept"], 1, "相邻同帧只保留首张")
        self.assertEqual(outcome["skipped"].get("duplicate_adjacent"), 1)
        self.assertEqual(outcome["duplicates"], 1, "邻帧重复计入重复台账")
        self.assertEqual(len(fetched), 2, "过滤发生在下载之后")

    def test_blacklist_near_miss_bypasses_adjacent_dedup(self):
        """追加C②旁路：黑名单近亲页不吃邻帧去重——必须作为 entry 进入
        deck 家族筛查（家族凑满 ≥3 整族杀），通知页家族不得漏杀。"""
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        variants = _near_miss_variants(row, 3)
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            blobs = [
                _png_bytes(master),
                *[_png_bytes(image) for image in variants[:2]],
            ]
            run, _fetched = self._run_pass(blobs, prefix="adjacent-nearmiss")
            outcome = run.run()
        self.assertNotIn(
            "duplicate_adjacent", outcome["skipped"],
            "近亲页之间不得邻帧折叠，否则家族凑不满成员",
        )

    def test_legacy_v1_resume_ledger_is_discarded_wholesale(self):
        # v4（SRC-CLEANUP-1 U8）：组内保信息最大张+设备占位 deck pass 入接受
        # 政策，v1-v3 旧账本一律作废重取
        self.assertEqual(POLICY_VERSION, "courseware-pdf.v4")
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            blobs = [_png_bytes(_rich_slide()), _png_bytes(master)]
            run, fetched = self._run_pass(blobs, prefix="junk-resume")
            outcome = run.run()
            self.assertEqual(outcome["kept"], 1)
            self.assertEqual(fetched, ["0", "1"])
            # 伪造 v1 旧账本（本地过滤前时代）：v2 必须整体作废、全部重取
            (run.work_dir / "resume.json").write_text(json.dumps({
                "policy_version": "courseware-pdf.v1",
                "cursor": 1,
                "ledger": [],
                "skipped": {},
                "entries": [],
                "byte_spent": 0,
            }), encoding="utf-8")
            run2, fetched2 = self._run_pass(blobs, work_dir=run.work_dir)
            outcome2 = run2.run()
        self.assertEqual(fetched2, ["0", "1"], "v1 旧账本不得被续用")
        self.assertEqual(outcome2["kept"], 1)
        self.assertEqual(outcome2["policy_version"], POLICY_VERSION)

    def test_screen_junk_families_removes_entries_and_counts_skips(self):
        master = _naked_table(1)
        row = entry_for(master, "synthetic-notice")
        variants = _near_miss_variants(row, 3)
        from src.runtime.courseware_pdf import PageEntry
        from collections import Counter

        entries = [
            PageEntry(seq=index + 1, original_id=str(index), created_sec=0,
                      created_ms=0, source_sha256=f"sha-{index}",
                      file=f"page-{index + 1:05d}.img", marker_signature="",
                      page_label="")
            for index in range(3)
        ]
        with patch.object(junk_page_filter, "JUNK_PAGE_BLACKLIST", (row,)):
            features = {index + 1: frame_features(image)
                        for index, image in enumerate(variants)}
        skipped = Counter()
        run = CoursewarePdfRun(
            records=[], work_dir=Path(tempfile.mkdtemp(prefix="fam-unit")),
            out_pdf=Path(tempfile.mkdtemp(prefix="fam-unit")) / "o.pdf",
            out_manifest=Path(tempfile.mkdtemp(prefix="fam-unit")) / "m.json",
        )
        killed = run._screen_junk_families(entries, features, skipped)
        self.assertEqual(killed, 3, "三个近失变体在合成黑名单下必成族落刀")
        self.assertEqual(entries, [], "整个变体家族一起落刀")
        self.assertEqual(skipped[JUNK_PAGE_SKIP_REASON], 3)


class WorkerTwinParityTests(unittest.TestCase):
    """When the worker tree is importable, the port must be its exact twin."""

    WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"

    def _load_worker_filter(self):
        if not (self.WORKER_DIR / "courselens_worker" / "junk_filter.py").is_file():
            self.skipTest("worker tree not present")
        if str(self.WORKER_DIR) not in sys.path:
            sys.path.insert(0, str(self.WORKER_DIR))
        # 全量套件里有测试向 sys.modules 塞假 numpy（WTELEM 已知坑）：stub 检测
        # 必须先于 worker 导入，否则 parity 会对着 MagicMock 算出全错的特征。
        try:
            import numpy

            # types.ModuleType 替身（test_ocr_evidence_pipeline 的最小 stand-in）
            # 没有 __version__/ndarray；真包必有两者的 C 级实现。
            if not isinstance(getattr(numpy, "__version__", None), str) or getattr(numpy, "ndarray", None) is None:
                self.skipTest("numpy polluted by another test's stub")
        except ModuleNotFoundError:
            self.skipTest("numpy unavailable (managed-client parity is dev-side)")
        try:
            from courselens_worker import junk_filter as worker_filter
        except Exception:
            self.skipTest("worker filter deps unavailable")
        return worker_filter

    def test_frozen_thresholds_and_blacklist_are_identical(self):
        worker_filter = self._load_worker_filter()
        constant_names = [
            "JUNK_PAGE_SKIP_REASON", "JUNK_HAMMING_THRESHOLD", "JUNK_NORM_SIZE",
            "JUNK_ROI", "JUNK_FEATURELESS_GV_MAX", "JUNK_AHASH_MIN_BITS",
            "JUNK_BOARD_RANGE_MIN", "JUNK_CHROME_TOP_FRAC",
            "JUNK_CHROME_TOP_EDGE_FRAC", "JUNK_CHROME_BOTTOM_FRAC",
            "JUNK_CHROME_ROWVAR_MAX", "JUNK_CHROME_EDGE_MIN",
            "JUNK_CHROME_EDGE_DELTA", "JUNK_CHROME_BOTTOM_VAR_MIN",
            "JUNK_FAMILY_RADIUS", "JUNK_FAMILY_MIN_MEMBERS",
            "JUNK_BLACKLIST_NEAR_MAX",
        ]
        for name in constant_names:
            self.assertEqual(
                getattr(junk_page_filter, name), getattr(worker_filter, name),
                f"冻结阈值 {name} 两端必须一致",
            )
        self.assertEqual(
            list(junk_page_filter.JUNK_PAGE_BLACKLIST),
            list(worker_filter.JUNK_PAGE_BLACKLIST),
            "黑名单行两端必须逐字一致（先改 worker 再镜像）",
        )

    def test_dual_hash_and_stage_verdicts_are_bit_identical(self):
        worker_filter = self._load_worker_filter()
        samples = [
            _naked_table(1), _naked_table(2), _rich_slide(), _dark_board(),
            _chrome_window(), _blank_page(),
        ]
        for image in samples:
            with self.subTest(size=image.size):
                self.assertEqual(dual_hash(image), worker_filter.dual_hash(image))
                local_features = frame_features(image)
                worker_features = worker_filter.frame_features(image)
                self.assertEqual(
                    local_features["dhash"], worker_features["dhash"])
                self.assertEqual(
                    local_features["ahash"], worker_features["ahash"])
                self.assertEqual(
                    local_features["ahash_bits"], worker_features["ahash_bits"])
                self.assertEqual(local_features["near"], worker_features["near"])
                self.assertEqual(
                    local_features["chrome"], worker_features["chrome"])
                self.assertEqual(
                    local_features["weak_chrome"], worker_features["weak_chrome"])
                for key in ("gv", "range", "top_var", "bottom_var"):
                    self.assertAlmostEqual(
                        float(local_features[key]), float(worker_features[key]),
                        delta=max(2.0, float(worker_features[key]) * 0.05),
                        msg=f"{key} 直方图/BOX 等价实现须落在 numpy 值的容差内",
                    )
                self.assertEqual(
                    int(local_features["strong_edges"]),
                    int(worker_features["strong_edges"]))
                self.assertEqual(
                    featureless_verdict(local_features),
                    worker_filter.featureless_verdict(worker_features),
                )
                self.assertEqual(
                    junk_page_filter.chrome_verdict(local_features),
                    worker_filter.chrome_verdict(worker_features),
                )


if __name__ == "__main__":
    unittest.main()
