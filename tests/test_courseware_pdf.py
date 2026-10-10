from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from random import Random
from unittest.mock import patch

from PIL import Image, ImageDraw

from src.runtime.courseware_pdf import (
    ANNOTATION_CLASSES,
    PAUSE_CODES,
    SKIP_REASONS,
    CoursewarePdfRun,
    PageBudget,
    _excluded_zone_spans,
    classify_variant_pair,
    decode_slide,
    marker_label,
    marker_signature,
    validate_record,
)


def _stamp_content(image, color):
    """Deterministic slide-like texture on a bare color fill.

    A plain solid page is exactly what the junk screen removes (featureless
    stage), so every "content" fixture carries a fixed border/diagonal/text
    stamp derived only from its arguments — same args still yield the same
    bytes, which the sha-dedupe pins rely on.  The stamp GEOMETRY also shifts
    with the color: dHash is color-shift invariant, and adjacent-frame dedup
    (SRC-SYNDROME-1 追加C) collapses layout-identical consecutive frames by
    design, so "distinct page" fixtures must be structurally distinct, not
    just chromatically.
    """
    width, height = image.size
    if width < 8 or height < 8:
        return
    shade = tuple(max(0, channel - 90) for channel in color[:3])
    seed = sum(color[:3])
    offset = seed % max(2, width // 6)
    bars = 2 + seed % 3
    pen = ImageDraw.Draw(image)
    pen.rectangle([1, 1, width - 2, height - 2], outline=shade)
    pen.line([2, height - 3, width - 3, 2], fill=shade)
    pen.rectangle(
        [width // 4 + offset, height // 3, width // 2 + offset, height // 2],
        fill=shade,
    )
    for bar in range(bars):
        y = height // 2 + 2 + bar * 3
        if y >= height - 2:
            break
        pen.line(
            [3 + offset + bar, y, width // 2 + offset - bar, y],
            fill=shade,
            width=1,
        )


def _jpeg_bytes(color=(242, 242, 242), size=(64, 48), draw=None):
    image = Image.new("RGB", size, color)
    _stamp_content(image, color)
    if draw is not None:
        draw(image)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _scatter_dots(image, index):
    """Deterministic per-index content block: structural, not just chromatic.

    Adjacent-frame dedup (追加C) collapses layout-identical consecutive
    frames, and the tightened local featureless stage (追加C①) junks near-
    solid pages (std < 12), so a "content" fixture must carry REAL slide
    density — dark blocks over ~15% of the canvas, std in the 25-45 band a
    text slide actually has.  Identical index still yields identical bytes.
    """
    width, height = image.size
    # 点阵用与填充亮度对撞的墨色（暗底白块/亮底黑块）：_distinct_blobs 的
    # 循环色含深蓝底（L≈37），沿用「再暗 90」的 shade 会让 L 对比度只剩
    # ~25、方差掉回近纯段，被追加C①误吞。
    fill = image.getpixel((0, 0))[:3]
    fill_l = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    ink = (255, 255, 255) if fill_l < 128 else (0, 0, 0)
    # 乘数经实量测校准：200 连续帧 min dH=10、前 4 帧两两 min dH=19，
    # 保证合法长讲次与 storm 护栏夹具不被相邻帧去重折叠。
    rng = Random(index * 40503 % 2**31)
    pen = ImageDraw.Draw(image)
    # 块体必须大于 frame_features 的 4px 子采样步进，否则方差被稀释、
    # 内容页会被近纯判据误吞（追加C①校准量测教训）；网格分区布块杜绝
    # 块体重叠把覆盖率打回近纯段。
    cell_w, cell_h = width // 3, height // 2
    rng = Random(index * 2654435761 % 2**31)
    pen = ImageDraw.Draw(image)
    for cell in range(5):
        col, row = cell % 3, cell // 3
        block_w = 10 + rng.randint(0, 5)
        block_h = 6 + rng.randint(0, 3)
        # (index+cell) 奇偶交替左右锚定：相邻 index 的块位结构性镜像，
        # 偶然的随机布局重合不再被相邻帧去重折叠（实量测校准）。
        if (index + cell) % 2 == 0:
            x0 = col * cell_w + rng.randint(1, max(2, cell_w - block_w - 1))
        else:
            x0 = (col + 1) * cell_w - 1 - rng.randint(1, max(2, cell_w - block_w - 1)) - block_w
        y0 = row * cell_h + rng.randint(1, max(2, cell_h - block_h - 1))
        pen.rectangle([x0, y0, x0 + block_w, y0 + block_h], fill=ink)


def _distinct_blobs(count):
    return [
        _jpeg_bytes(
            color=(30 * (index % 7) + 10, 40 * (index % 5) + 20, 200 - index % 97),
            draw=lambda image, index=index: _scatter_dots(image, index),
        )
        for index in range(count)
    ]


def _record(index, *, created_sec=None, original_id=None, ocr_text=""):
    moment = index * 10 if created_sec is None else created_sec
    return {
        "id": index,
        "original_id": str(original_id if original_id is not None else index),
        "pptimgurl": f"https://slides.invalid/capture-{index}.jpg",
        "created_sec": moment,
        "created_ms": moment * 1000,
        "taskid": "t",
        "ocr_text": ocr_text,
    }


class _Fetcher:
    """Hands out prepared blobs in sorted-record order and counts calls."""

    def __init__(self, blobs):
        self.blobs = list(blobs)
        self.calls = 0

    def __call__(self, record):
        self.calls += 1
        return self.blobs.pop(0)


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        raw = path.read_bytes()
        return raw.count(b"/Type /Page") - raw.count(b"/Type /Pages")


class CoursewarePdfRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _run(self, records, blobs, *, budget=None, subdir="run"):
        work = self.base / subdir / "work"
        out_pdf = self.base / subdir / "slides.pdf"
        out_manifest = self.base / subdir / "manifest.json"
        fetcher = _Fetcher(blobs)
        run = CoursewarePdfRun(
            records=records,
            work_dir=work,
            out_pdf=out_pdf,
            out_manifest=out_manifest,
            budget=budget,
            fetch_page=fetcher,
        )
        return run, fetcher, out_pdf, out_manifest

    def test_long_event_stream_completes_without_a_low_page_cap(self):
        count = 200
        records = [_record(index) for index in range(count)]
        run, _fetcher, out_pdf, out_manifest = self._run(records, _distinct_blobs(count))
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], count, "两百个事件的合法长讲次必须全部保留")
        self.assertTrue(out_pdf.is_file())
        self.assertEqual(_pdf_page_count(out_pdf), count)
        manifest = json.loads(out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["kept"], count)
        self.assertEqual(len(manifest["pages"]), count)

    def test_duplicate_heavy_stream_collapses_exactly_and_stays_bounded(self):
        distinct = _distinct_blobs(4)
        records = [_record(index) for index in range(60)]
        blobs = [distinct[index % 4] for index in range(60)]
        run, _fetcher, out_pdf, out_manifest = self._run(records, blobs)
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], 4)
        self.assertEqual(outcome["duplicates"], 56)
        self.assertEqual(_pdf_page_count(out_pdf), 4)
        manifest = json.loads(out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["duplicates"], 56)
        folded = manifest["pages"][0]["duplicates"]
        self.assertEqual(len(folded), 14, "重复事件折叠进首个捕获页的清单元数据")

    def test_malformed_html_and_oversized_pages_fail_softly(self):
        good = _jpeg_bytes()
        # 内容页 stamp 后的 JPEG 已超过 1KB，预算放宽到 2048，
        # 超限样本随之放大，保证走的是 byte 预算而不是解码失败。
        oversized_bytes = b"\x00binary" * 400
        records = [_record(index) for index in range(6)]
        blobs = [
            good,
            b"",  # empty
            b"<!DOCTYPE html><html><body>login</body></html>",  # html_body
            b'{"error":"denied"}',  # json_body
            b"\xff\xff not-an-image payload",  # unidentified/decode
            oversized_bytes,
        ]
        budget = PageBudget(max_image_bytes=2048)
        run, _fetcher, out_pdf, out_manifest = self._run(records, blobs, budget=budget)
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed", "单个坏页绝不拖垮整讲")
        self.assertEqual(outcome["kept"], 1)
        self.assertEqual(outcome["skipped"].get("empty"), 1)
        self.assertEqual(outcome["skipped"].get("html_body"), 1)
        self.assertEqual(outcome["skipped"].get("json_body"), 1)
        self.assertEqual(outcome["skipped"].get("oversized_image"), 1)
        self.assertIn(outcome["skipped"].get("unidentified_image") or outcome["skipped"].get("decode_failed"), (1,))
        self.assertTrue(out_pdf.is_file())

    def test_pixel_budget_rejects_giant_images_before_decode(self):
        huge = _jpeg_bytes(size=(400, 400))
        records = [_record(0)]
        run, _fetcher, out_pdf, _manifest = self._run(
            records, [huge], budget=PageBudget(max_image_pixels=1000), subdir="pixels",
        )
        outcome = run.run()
        self.assertEqual(outcome["skipped"].get("oversized_image"), 1)
        self.assertEqual(outcome["kept"], 0)

    def test_invalid_records_are_skipped_with_closed_reasons(self):
        self.assertIsNone(validate_record("not-a-dict")[0])
        self.assertEqual(validate_record("x")[1], "record_invalid")
        self.assertEqual(validate_record({"pptimgurl": "   "})[1], "record_url_missing")
        self.assertEqual(validate_record({"pptimgurl": "u", "created_sec": "x"})[1], "record_invalid")
        self.assertEqual(validate_record({"pptimgurl": "u", "created_sec": -4})[1], "record_invalid")
        record, reason = validate_record({"pptimgurl": "u", "created_sec": 3})
        self.assertIsNone(reason)
        self.assertEqual(record["created_sec"], 3)

    def test_order_is_stable_by_created_sec_then_original_id(self):
        blobs = _distinct_blobs(4)
        records = [
            _record(1, created_sec=50, original_id="b"),
            _record(2, created_sec=50, original_id="a"),
            _record(3, created_sec=10, original_id="z"),
            _record(4, created_sec=99, original_id="m"),
        ]
        run, _fetcher, _out_pdf, out_manifest = self._run(
            records, blobs, subdir="order",
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        manifest = json.loads(out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            [(page["created_sec"], page["original_id"]) for page in manifest["pages"]],
            [(10, "z"), (50, "a"), (50, "b"), (99, "m")],
        )

    def test_hand_drawn_variant_is_preserved_and_classified(self):
        def annotate(image):
            for y in range(20, 76):
                image.putpixel((64, y), (10, 10, 10))
                image.putpixel((65, y), (10, 10, 10))

        clean = _jpeg_bytes(size=(128, 96))
        annotated = _jpeg_bytes(size=(128, 96), draw=annotate)
        records = [
            _record(1, created_sec=10, ocr_text="3/40"),
            _record(2, created_sec=25, ocr_text="3/40"),
        ]
        run, _fetcher, out_pdf, out_manifest = self._run(
            records, [clean, annotated], subdir="annotated",
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["kept"], 2, "板书痕迹是新画面变体，绝不因相似被丢弃")
        self.assertEqual(_pdf_page_count(out_pdf), 2)
        manifest = json.loads(out_manifest.read_text(encoding="utf-8"))
        second = manifest["pages"][1]
        self.assertEqual(second["annotation"]["class"], "annotated_candidate")
        self.assertGreater(second["annotation"]["confidence"], 0.0)
        self.assertIn(second["page_label"], ("3/40",))

    def test_marker_label_parsing_is_conservative(self):
        self.assertEqual(marker_label("11/60"), "11/60")
        self.assertEqual(marker_label("Slide 11 of 60"), "11/60")
        self.assertEqual(marker_label("11 / 60"), "11/60")
        self.assertEqual(marker_label("第11页"), "11")
        self.assertEqual(marker_label("60/11"), "", "页码大于总数时不可信")
        self.assertEqual(marker_label("no label here"), "")
        self.assertEqual(marker_label(""), "")

    def test_marker_signature_reflects_marker_zones_not_body_details(self):
        def body_change(image):
            for x in range(10, 50):
                for y in range(10, 30):
                    image.putpixel((x, y), (0, 0, 0))

        base = _jpeg_bytes(size=(128, 96))
        body_variant = _jpeg_bytes(size=(128, 96), draw=body_change)

        def corner_change(image):
            for x in range(110, 128):
                for y in range(0, 18):
                    image.putpixel((x, y), (0, 0, 0))

        corner_variant = _jpeg_bytes(size=(128, 96), draw=corner_change)
        base_image = Image.open(io.BytesIO(base)).convert("RGB")
        body_image = Image.open(io.BytesIO(body_variant)).convert("RGB")
        corner_image = Image.open(io.BytesIO(corner_variant)).convert("RGB")
        self.assertEqual(
            marker_signature(base_image),
            marker_signature(body_image),
            "正文涂改不影响角标区域签名",
        )
        self.assertNotEqual(marker_signature(base_image), marker_signature(corner_image))

    def test_budget_pause_then_resume_completes_without_duplicate_pages(self):
        blobs = _distinct_blobs(4)
        records = [_record(index) for index in range(4)]
        run, _fetcher, out_pdf, _manifest = self._run(
            records, blobs, budget=PageBudget(max_total_bytes=1), subdir="resume1",
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "resource_budget_paused")
        self.assertFalse(out_pdf.exists(), "暂停时绝不发布半成品 PDF")
        self.assertTrue((run.work_dir / "resume.json").is_file())

        resumed_run = CoursewarePdfRun(
            records=records,
            work_dir=run.work_dir,
            out_pdf=out_pdf,
            out_manifest=self.base / "resume1" / "manifest.json",
            fetch_page=_Fetcher(blobs),
        )
        resumed = resumed_run.run()
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(resumed["kept"], 4)
        self.assertEqual(_pdf_page_count(out_pdf), 4)
        self.assertFalse((resumed_run.work_dir / "resume.json").exists(), "成功后清理自持暂存")

    def test_storm_circuit_pauses_and_resume_keeps_every_distinct_page(self):
        blobs = _distinct_blobs(4)
        records = [_record(index) for index in range(4)]
        run, _fetcher, out_pdf, _manifest = self._run(
            records, blobs, budget=PageBudget(distinct_page_storm_limit=2), subdir="storm1",
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "event_storm_paused")
        self.assertFalse(out_pdf.exists())

        resumed_run = CoursewarePdfRun(
            records=records,
            work_dir=run.work_dir,
            out_pdf=out_pdf,
            out_manifest=self.base / "storm1" / "manifest.json",
            # 续跑只取余下事件的全新画面：前两个内容必须是未出现过的像素
            # （近纯深色页会被 junk 屏的 featureless 级裁掉，须是真实内容页；
            # 追加C② 后还须结构相异——同构异色的相邻帧会被邻帧去重折叠）
            fetch_page=_Fetcher([
                _jpeg_bytes(color=(140, 60, 60), draw=lambda image: _scatter_dots(image, 101)),
                _jpeg_bytes(color=(60, 140, 60), draw=lambda image: _scatter_dots(image, 202)),
            ]),
        )
        resumed = resumed_run.run()
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(resumed["kept"], 4, "恢复后补齐尾部，绝不静默丢弃")
        self.assertEqual(_pdf_page_count(out_pdf), 4)
        manifest = json.loads((self.base / "storm1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len({page["source_sha256"] for page in manifest["pages"]}), 4)

    def test_wall_clock_budget_pauses(self):
        clock_values = iter([0.0, 10_000.0])

        run, _fetcher, out_pdf, _manifest = self._run(
            [_record(0)], _distinct_blobs(1), subdir="clock",
        )
        run._clock = lambda: next(clock_values)
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "wall_clock_paused")
        self.assertFalse(out_pdf.exists())

    def test_empty_selection_reports_no_pages_without_publishing(self):
        run, _fetcher, out_pdf, _manifest = self._run([], [], subdir="empty")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["code"], "no_pages")
        self.assertFalse(out_pdf.exists())

    def test_manifest_never_contains_urls(self):
        records = [_record(0), _record(1)]
        run, _fetcher, _out_pdf, out_manifest = self._run(records, _distinct_blobs(2), subdir="manifest")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        text = out_manifest.read_text(encoding="utf-8")
        self.assertNotIn("http", text)
        self.assertNotIn("pptimgurl", text)
        self.assertNotIn("slides.invalid", text)

    def test_assembly_failure_publishes_nothing_and_cleans_tmp(self):
        work = self.base / "asm" / "work"
        work.mkdir(parents=True)
        good = work / "page-00001.img"
        good.write_bytes(_jpeg_bytes())
        entries = [
            dict(seq=1, original_id="1", created_sec=1, created_ms=1000,
                 source_sha256="a" * 64, file="page-00001.img"),
        ]
        from src.runtime.courseware_pdf import PageEntry
        run = CoursewarePdfRun(
            records=[],
            work_dir=work,
            out_pdf=self.base / "asm" / "slides.pdf",
            out_manifest=self.base / "asm" / "manifest.json",
        )
        page_entries = [PageEntry(
            seq=item["seq"], original_id=item["original_id"], created_sec=item["created_sec"],
            created_ms=item["created_ms"], source_sha256=item["source_sha256"], file=item["file"],
        ) for item in entries]
        run._assemble(page_entries)
        self.assertTrue(run.out_pdf.is_file())
        good.unlink()
        broken = [PageEntry(
            seq=1, original_id="1", created_sec=1, created_ms=1000,
            source_sha256="a" * 64, file="page-00001.img",
        )]
        with self.assertRaises(FileNotFoundError):
            run._assemble(broken)
        self.assertFalse(run.out_pdf.with_suffix(".pdf.tmp").exists(), "失败后无残留半成品")

    def test_decode_slide_applies_exif_orientation(self):
        image = Image.new("RGB", (40, 20), (200, 30, 30))
        exif = Image.Exif()
        exif[274] = 6  # orientation: rotate 90 CW on display
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", exif=exif.tobytes())
        page, reason = decode_slide(buffer.getvalue(), PageBudget())
        self.assertIsNone(reason)
        self.assertEqual(page.size, (20, 40))

    def test_classify_variant_pair_is_metadata_only_and_closed(self):
        base = Image.new("RGB", (320, 240), (250, 250, 250))
        same = base.copy()
        verdict = classify_variant_pair(base, same)
        self.assertEqual(verdict["class"], "clean_candidate")
        annotated = base.copy()
        for y in range(100, 140):
            annotated.putpixel((160, y), (10, 10, 10))
            annotated.putpixel((161, y), (10, 10, 10))
        verdict = classify_variant_pair(base, annotated)
        self.assertEqual(verdict["class"], "annotated_candidate")
        different = Image.new("RGB", (320, 240), (10, 10, 10))
        verdict = classify_variant_pair(base, different)
        self.assertEqual(verdict["class"], "unknown", "整页级差异必须保持 unknown")
        for value in ("clean_candidate", "annotated_candidate", "unknown"):
            self.assertIn(value, ANNOTATION_CLASSES)

    def test_same_url_is_never_fetched_twice_within_one_run(self):
        shared = _jpeg_bytes()
        records = [
            {**_record(1), "pptimgurl": "https://slides.invalid/same.jpg"},
            {**_record(2), "pptimgurl": "https://slides.invalid/same.jpg"},
            {**_record(3), "pptimgurl": "https://slides.invalid/same.jpg"},
        ]
        run, fetcher, out_pdf, out_manifest = self._run(
            records, [shared], subdir="single-fetch",
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(fetcher.calls, 1, "同一 URL 在一次运行内只下载一次")
        self.assertEqual(outcome["kept"], 1)
        self.assertEqual(outcome["duplicates"], 2)
        manifest = json.loads(out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["pages"][0]["duplicates"]), 2)
        self.assertNotIn("same.jpg", out_manifest.read_text(encoding="utf-8"))

    def test_closed_sets_stay_closed(self):
        self.assertTrue(SKIP_REASONS.issuperset({"duplicate_exact", "html_body", "oversized_image"}))
        self.assertTrue(PAUSE_CODES.issuperset({"resource_budget_paused", "event_storm_paused", "wall_clock_paused"}))


class PillowParityGoldenTests(unittest.TestCase):
    """Pillow-only marker/classifier must reproduce the retired NumPy outputs.

    Goldens were captured from the pre-swap NumPy implementation on the same
    in-memory fixtures (no JPEG encoder involvement), so any drift in the
    marker quantization, zone exclusion, thresholds, or bounding-box math
    fails here.
    """

    SIGNATURE_GOLDENS = {
        "flat": "mk:0037fa72efb8a4a0",
        "body_change": "mk:0037fa72efb8a4a0",
        "corner_change": "mk:31973475eac49a7a",
        "whole_change": "mk:8f7a932f3043a2bb",
    }

    VERDICT_GOLDENS = {
        "identical": ("clean_candidate", 0.8),
        "stroke": ("annotated_candidate", 0.55),
        "fulldiff": ("unknown", 0.0),
        "zoneonly": ("clean_candidate", 0.8),
        "onepx": ("clean_candidate", 0.8),
        "stroke_plus_zone": ("annotated_candidate", 0.55),
        "wide_spread": ("unknown", 0.0),
        "big_block": ("unknown", 0.0),
        "mismatched_sizes": ("clean_candidate", 0.8),
    }

    @staticmethod
    def _flat(color, size=(128, 96)):
        return Image.new("RGB", size, color)

    @staticmethod
    def _paint(image, x0, y0, x1, y1, color):
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                image.putpixel((x, y), color)
        return image

    def test_marker_signature_matches_numpy_goldens(self):
        images = {
            "flat": self._flat((242, 242, 242)),
            "body_change": self._paint(self._flat((242, 242, 242)), 10, 10, 49, 29, (0, 0, 0)),
            "corner_change": self._paint(self._flat((242, 242, 242)), 110, 0, 127, 17, (0, 0, 0)),
            "whole_change": self._paint(self._flat((242, 242, 242)), 0, 0, 127, 95, (0, 0, 0)),
        }
        for name, image in images.items():
            with self.subTest(case=name):
                self.assertEqual(marker_signature(image), self.SIGNATURE_GOLDENS[name])

    def test_classify_variant_pair_matches_numpy_goldens(self):
        base = self._flat((250, 250, 250), (320, 240))
        variants = {
            "identical": base.copy(),
            "stroke": self._paint(
                self._flat((250, 250, 250), (320, 240)), 160, 100, 161, 139, (10, 10, 10)),
            "fulldiff": self._flat((10, 10, 10), (320, 240)),
            "zoneonly": self._paint(
                self._flat((250, 250, 250), (320, 240)), 230, 0, 319, 31, (30, 30, 30)),
            "onepx": self._paint(
                self._flat((250, 250, 250), (320, 240)), 200, 120, 200, 120, (10, 10, 10)),
            "stroke_plus_zone": self._paint(
                self._paint(self._flat((250, 250, 250), (320, 240)), 160, 100, 161, 139, (10, 10, 10)),
                230, 0, 319, 31, (30, 30, 30)),
            "wide_spread": self._paint(
                self._paint(self._flat((250, 250, 250), (320, 240)), 5, 20, 6, 219, (10, 10, 10)),
                309, 20, 310, 219, (10, 10, 10)),
            "big_block": self._paint(
                self._flat((250, 250, 250), (320, 240)), 40, 60, 250, 180, (10, 10, 10)),
            "mismatched_sizes": self._flat((250, 250, 250), (320, 200)),
        }
        for name, variant in variants.items():
            with self.subTest(case=name):
                verdict = classify_variant_pair(base, variant)
                want_class, want_confidence = self.VERDICT_GOLDENS[name]
                self.assertEqual(verdict["class"], want_class)
                self.assertEqual(verdict["confidence"], want_confidence)

    def test_excluded_zone_spans_match_the_original_mask_slicing(self):
        # int(320 * 0.70) is 223, not 224: the shared float arithmetic of the
        # original mask slicing is itself pinned here.
        self.assertEqual(
            _excluded_zone_spans(320, 240),
            [(230, 0, 319, 42), (230, 196, 319, 239), (96, 211, 223, 239)],
        )


class CoursewarePdfServiceTests(unittest.TestCase):
    """Application 层闭环：目录授权、路径核查、显式动作去重与暂停续跑。"""

    @classmethod
    def setUpClass(cls):
        cls._scratch_parent = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        cls._scratch_parent.mkdir(parents=True, exist_ok=True)

    def _service(self):
        from src.application import CourseLensApplication
        tmp = tempfile.TemporaryDirectory(dir=self._scratch_parent)
        self.addCleanup(tmp.cleanup)
        service = CourseLensApplication(Path(tmp.name))
        self.addCleanup(service.close)
        service.catalog_repository.upsert_lecture("c1", {
            "sub_id": "s1", "sub_title": "第一讲", "lecturer_name": "",
            "date": "2026-09-14", "has_playback": True,
        })
        return service

    def test_paths_are_opaque_and_path_checked(self):
        service = self._service()
        root = service.output_dir.resolve()
        artifact_dir, pdf_path, manifest_path = service._courseware_pdf_paths("c1", "s1")
        self.assertTrue(str(artifact_dir).startswith(str(root)))
        self.assertRegex(artifact_dir.name, r"^lec-[0-9a-f]{16}$")
        self.assertNotIn("s1", artifact_dir.name, "磁盘目录不携带明文讲次标识")
        self.assertEqual(pdf_path.parent, artifact_dir)
        self.assertEqual(manifest_path.parent, artifact_dir)

    def test_enqueue_is_explicit_deduped_and_resume_aware(self):
        service = self._service()
        status = service.courseware_pdf_status("s1")
        self.assertIsNone(status["artifact"])
        self.assertIsNone(status["operation"])
        with patch.object(service, "_ensure_courseware_pdf_worker", lambda: None):
            task = service.enqueue_courseware_pdf("c1", "s1")
            self.assertEqual(task["state"], "queued")
            again = service.enqueue_courseware_pdf("c1", "s1")
            self.assertEqual(str(again["task_id"]), str(task["task_id"]),
                             "重操作排队期间重复点击必须复用既有任务")
            with self.assertRaisesRegex(FileNotFoundError, "authorized catalog"):
                service.enqueue_courseware_pdf("c1", "missing")
            # 没有暂停任务时 resume 冲突；暂停后 resume 恢复排队
            with self.assertRaises(ValueError):
                service.enqueue_courseware_pdf("c1", "s1", action="resume")
        service.task_store.update_task(str(task["task_id"]), state="paused")
        with patch.object(service, "_ensure_courseware_pdf_worker", lambda: None):
            resumed = service.enqueue_courseware_pdf("c1", "s1", action="resume")
            self.assertEqual(resumed["state"], "queued")

    def test_status_reports_nested_artifact_and_operation(self):
        service = self._service()
        status = service.courseware_pdf_status("s1")
        self.assertIsNone(status["artifact"])
        self.assertIsNone(status["operation"])
        artifact_dir, pdf_path, manifest_path = service._courseware_pdf_paths("c1", "s1")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.4 synthetic")
        manifest_path.write_text(json.dumps({
            "schema": "courselens.courseware-pdf-manifest.v1",
            "events_total": 6, "kept": 4, "duplicates": 1,
            "skipped": {"html_body": 1},
            "generated_at": 1789000000,
        }, ensure_ascii=False), encoding="utf-8")
        status = service.courseware_pdf_status("s1")
        artifact = status["artifact"]
        self.assertTrue(artifact["ready"])
        self.assertEqual(artifact["pages"], 4)
        self.assertEqual(artifact["events_total"], 6)
        self.assertEqual(artifact["duplicates"], 1)
        self.assertEqual(artifact["skipped_total"], 1)
        self.assertEqual(artifact["generated_at"], 1789000000)
        self.assertTrue(artifact["download_name"].endswith(".pdf"))
        self.assertNotIn("s1", artifact["download_name"], "下载文件名不携带内部标识")
        self.assertIsNone(status["operation"], "成品事实不被无活动操作遮盖")
        # 运行中操作与成品可共存；manifest 摘要不覆盖操作进度
        with patch.object(service, "_ensure_courseware_pdf_worker", lambda: None):
            task = service.enqueue_courseware_pdf("c1", "s1")
            service.task_store.update_task(str(task["task_id"]), state="running", progress={
                "schema_version": 2, "kind": "courseware_pdf",
                "percent": 37.5, "percent_measured": True,
                "label": "正在读取课堂画面", "phase": "正在读取课堂画面",
                "stage": "pages", "counts": {"processed": 15, "kept": 12, "total": 40},
            })
        status = service.courseware_pdf_status("s1")
        self.assertTrue(status["artifact"]["ready"], "旧成品在新版运行期仍可用")
        operation = status["operation"]
        self.assertEqual(operation["state"], "running")
        self.assertTrue(operation["percent_measured"])
        self.assertEqual(operation["percent"], 37.5)
        self.assertEqual(operation["counts"]["kept"], 12)
        self.assertFalse(operation["resumable"])
        # 未测量的百分比不会被标记为可信
        service.task_store.update_task(str(task["task_id"]), progress={
            "schema_version": 2, "kind": "courseware_pdf",
            "percent": 60.0, "percent_measured": False,
            "label": "已暂停", "phase": "正在读取课堂画面", "stage": "paused",
        })
        service.task_store.update_task(str(task["task_id"]), state="paused")
        operation = service.courseware_pdf_status("s1")["operation"]
        self.assertIsNone(operation["percent"], "未测量百分比不对外暴露")
        self.assertFalse(operation["percent_measured"])
        self.assertTrue(operation["resumable"])
        resolved = service.courseware_pdf_file_path("s1")
        self.assertTrue(resolved.is_file())
        self.assertTrue(str(resolved).startswith(str(service.output_dir.resolve())))
        with self.assertRaises(FileNotFoundError):
            service.courseware_pdf_file_path("missing")

    def test_queue_passes_payload_plan_to_executor(self):
        """队列把任务 payload 里的已验证计划原样传入执行器；手动任务保持旧行为。"""
        service = self._service()
        service.catalog_repository.upsert_lecture("c1", {
            "sub_id": "s2", "sub_title": "第二讲", "lecturer_name": "",
            "date": "2026-09-14", "has_playback": True,
        })
        plan = {"schema": "courseware_plan.v1", "course_id": "c1", "sub_id": "s1"}
        with patch.object(service, "_ensure_courseware_pdf_worker", lambda: None):
            planned = service.enqueue_courseware_pdf(
                "c1", "s1", plan=plan, plan_digest="a" * 64,
            )
            legacy = service.enqueue_courseware_pdf("c1", "s2")
        self.assertEqual(planned["payload"]["courseware_plan"], plan)
        self.assertEqual(planned["payload"]["courseware_plan_digest"], "a" * 64)
        self.assertNotIn("courseware_plan", legacy["payload"])

        constructions = []

        class SpyRun:
            def __init__(self, **kwargs):
                constructions.append(kwargs)

            def run(self):
                return {
                    "state": "completed", "events_total": 1,
                    "kept": 1, "duplicates": 0, "skipped": {},
                }

        service.client = lambda *a, **k: type("FakeClient", (), {
            "get_ppt_list": staticmethod(lambda *a, **k: [_record(0)]),
        })()
        with patch("src.application.CoursewarePdfRun", SpyRun):
            service._run_courseware_pdf_queue()
        self.assertEqual(len(constructions), 2)
        self.assertEqual(constructions[0]["plan"], plan)
        self.assertEqual(constructions[0]["plan_digest"], "a" * 64)
        self.assertEqual(constructions[0]["course_id"], "c1")
        self.assertEqual(constructions[0]["sub_id"], "s1")
        self.assertIsNone(constructions[1]["plan"], "手动任务不携带计划，执行器走既有旧路径")
        self.assertEqual(constructions[1]["plan_digest"], "")
        self.assertEqual(
            service.task_store.get_task(str(planned["task_id"]))["state"],
            "completed",
        )

    def test_queue_pause_labels_cover_plan_pause_codes(self):
        """计划相关闭集暂停码命中专属文案，不再回退到通用资源护栏。"""
        for code, reason in (
            ("courseware_plan_changed", "课件清单已变化"),
            ("plan_fetch_paused", "课堂画面获取失败"),
            ("plan_page_unreadable", "课件页面无法读取"),
        ):
            with self.subTest(code=code):
                service = self._service()
                with patch.object(service, "_ensure_courseware_pdf_worker", lambda: None):
                    task = service.enqueue_courseware_pdf("c1", "s1")

                class PausedRun:
                    def __init__(self, **kwargs):
                        pass

                    def run(self):
                        return {"state": "paused", "code": code}

                service.client = lambda *a, **k: type("FakeClient", (), {
                    "get_ppt_list": staticmethod(lambda *a, **k: [_record(0)]),
                })()
                with patch("src.application.CoursewarePdfRun", PausedRun):
                    service._run_courseware_pdf_queue()
                stored = service.task_store.get_task(str(task["task_id"]))
                self.assertEqual(stored["state"], "paused")
                progress = stored["progress"]
                self.assertEqual(progress["pause_code"], code)
                self.assertIn(reason, progress["label"])
                self.assertNotIn("已达到资源护栏", progress["label"],
                                 "闭集暂停码不得回退通用文案")


if __name__ == "__main__":
    unittest.main()
