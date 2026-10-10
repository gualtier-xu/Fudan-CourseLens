"""Plan-driven local PDF executor: selected-only fetch, exact-hash proof,
bounded reconciliation, and no-partial publication under courseware_plan.v1."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.runtime.courseware_pdf import (
    PAUSE_CODES,
    CoursewarePlanError,
    CoursewarePdfRun,
    PageBudget,
    courseware_plan_digest,
    validate_courseware_plan,
)


def _jpeg_bytes(color=(242, 242, 242), size=(64, 48)):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _record(index, *, created_sec=None, original_id=None):
    moment = index * 10 if created_sec is None else created_sec
    return {
        "id": index,
        "original_id": str(original_id if original_id is not None else index),
        "pptimgurl": f"https://slides.invalid/capture-{index}.jpg",
        "created_sec": moment,
        "created_ms": moment * 1000,
    }


def _entry(output_position, capture_position, record_id, capture_time,
           capture_ordinal, sha, **overrides):
    entry = {
        "output_position": output_position,
        "capture_position": capture_position,
        "record_id": record_id,
        "capture_time": capture_time,
        "capture_ordinal": capture_ordinal,
        "source_sha256": sha,
        "page_label": "",
        "page_label_source": "",
        "annotation": {"class": "unknown", "confidence": 0.0},
        "keep_reason": "distinct_capture",
        "version_of_position": 0,
        "duplicate_count": 0,
    }
    entry.update(overrides)
    return entry


def _plan(entries, excluded=None, *, course_id="c1", sub_id="s1",
          counts=None, ordering=None):
    duplicates = len(excluded or [])
    return {
        "schema": "courseware_plan.v1",
        "policy_version": 1,
        "pipeline": "cloud-automation.v3",
        "course_id": course_id,
        "sub_id": sub_id,
        "inventory_digest": "d" * 64,
        "entries": entries,
        "excluded": list(excluded or []),
        "ordering": ordering or {"mode": "capture_order", "confidence": 0.0},
        "counts": counts or {
            "input_events": len(entries) + duplicates,
            "recognized": len(entries) + duplicates,
            "kept": len(entries),
            "exact_duplicates": duplicates,
            "skipped": 0,
        },
    }


class _IdFetcher:
    """Serves prepared blobs by opaque record id and records every fetch."""

    def __init__(self, blobs_by_id):
        self.blobs = dict(blobs_by_id)
        self.fetched = []

    def __call__(self, record):
        record_id = str(record.get("original_id") or record.get("id") or "")
        self.fetched.append(record_id)
        blob = self.blobs.get(record_id)
        if blob is None:
            raise RuntimeError(f"no prepared blob for record {record_id}")
        return blob


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        raw = path.read_bytes()
        return raw.count(b"/Type /Page") - raw.count(b"/Type /Pages")


class PlanDrivenRunTests(unittest.TestCase):
    """计划驱动的本地 PDF 执行：只取计划页、逐页验哈希、漂移只对账不决策。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _run(self, records, fetcher, plan, *, plan_digest="keep", budget=None,
             subdir="run", course_id="c1", sub_id="s1", on_progress=None):
        work = self.base / subdir / "work"
        out_pdf = self.base / subdir / "slides.pdf"
        out_manifest = self.base / subdir / "manifest.json"
        if plan_digest == "keep":
            plan_digest = courseware_plan_digest(plan)
        return CoursewarePdfRun(
            records=records,
            work_dir=work,
            out_pdf=out_pdf,
            out_manifest=out_manifest,
            budget=budget,
            fetch_page=fetcher,
            plan=plan,
            plan_digest=plan_digest,
            course_id=course_id,
            sub_id=sub_id,
            on_progress=on_progress,
        )

    def test_selected_only_downloads_in_plan_output_order(self):
        blobs = {
            "1": _jpeg_bytes((10, 10, 10)),
            "2": _jpeg_bytes((20, 20, 20)),
            "3": _jpeg_bytes((30, 30, 30)),
            "4": _jpeg_bytes((40, 40, 40)),
            "5": _jpeg_bytes((50, 50, 50)),
        }
        records = [_record(1), _record(2), _record(3), _record(4), _record(5)]
        entries = [
            _entry(1, 1, "4", 40, 0, _sha(blobs["4"]), page_label="3/3",
                   page_label_source="ocr_text"),
            _entry(2, 2, "2", 20, 0, _sha(blobs["2"]), page_label="1/3",
                   page_label_source="ocr_text"),
            _entry(3, 3, "1", 10, 0, _sha(blobs["1"]), page_label="2/3",
                   page_label_source="ocr_text"),
        ]
        plan = _plan(entries, ordering={"mode": "page_label", "confidence": 0.9})
        fetcher = _IdFetcher(blobs)
        run = self._run(records, fetcher, plan, subdir="selected")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(fetcher.fetched, ["4", "2", "1"], "只下载计划选中的记录")
        self.assertEqual(_pdf_page_count(run.out_pdf), 3)
        manifest = json.loads(run.out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            [page["original_id"] for page in manifest["pages"]],
            ["4", "2", "1"],
            "输出顺序必须跟随计划而不是捕获时间",
        )
        self.assertEqual(
            [page["seq"] for page in manifest["pages"]], [1, 2, 3],
        )
        self.assertEqual(
            [page["capture_position"] for page in manifest["pages"]], [1, 2, 3],
        )
        self.assertEqual(
            [page["page_label"] for page in manifest["pages"]],
            ["3/3", "1/3", "2/3"],
        )
        self.assertEqual(outcome["plan_digest"], courseware_plan_digest(plan))

    def test_capture_time_guard_blocks_fast_path_but_hash_still_proves(self):
        blobs = {"1": _jpeg_bytes((10, 10, 10)), "2": _jpeg_bytes((20, 20, 20))}
        records = [_record(1, created_sec=10), _record(2, created_sec=20)]
        # 计划把记录 1 的捕获时刻写成 999：定位守卫失败，但不许可伪造——
        # 只有哈希能证明页面，之后照常完成。
        entry = _entry(1, 1, "1", 999, 0, _sha(blobs["1"]))
        plan = _plan([entry])
        fetcher = _IdFetcher(blobs)
        run = self._run(records, fetcher, plan, subdir="guard")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(fetcher.fetched, ["1"], "守卫挡住快速路径；对账仍可用哈希证明")
        manifest = json.loads(run.out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["pages"][0]["original_id"], "1")

    def test_locator_drift_uses_bounded_exact_hash_reconciliation(self):
        drifted = _jpeg_bytes((90, 90, 90))
        good = _jpeg_bytes((10, 10, 10))
        blobs = {"1": drifted, "2": good}
        records = [_record(1, created_sec=10), _record(2, created_sec=20)]
        entry = _entry(1, 1, "1", 10, 0, _sha(good))
        plan = _plan([entry], counts={
            "input_events": 2, "recognized": 2, "kept": 1,
            "exact_duplicates": 0, "skipped": 0,
        })
        fetcher = _IdFetcher(blobs)
        run = self._run(records, fetcher, plan, subdir="drift")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed", "漂移后哈希对账必须补证")
        self.assertEqual(fetcher.fetched, ["1", "2"])
        manifest = json.loads(run.out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["pages"][0]["original_id"], "2", "证明页面的记录入清单")
        self.assertEqual(manifest["pages"][0]["source_sha256"], _sha(good))

    def test_unprovable_page_pauses_with_courseware_plan_changed(self):
        blobs = {"1": _jpeg_bytes((10, 10, 10)), "2": _jpeg_bytes((20, 20, 20))}
        records = [_record(1, created_sec=10), _record(2, created_sec=20)]
        entries = [
            _entry(1, 1, "1", 10, 0, _sha(blobs["1"])),
            _entry(2, 2, "2", 20, 0, _sha(_jpeg_bytes((200, 200, 200)))),
        ]
        plan = _plan(entries)
        fetcher = _IdFetcher(blobs)
        run = self._run(records, fetcher, plan, subdir="changed")
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "courseware_plan_changed")
        self.assertFalse(run.out_pdf.exists(), "缺证绝不发布半成品")
        self.assertFalse(run.out_manifest.exists())
        self.assertTrue((run.work_dir / "resume.json").is_file())
        # 已证明的第一页保留在可续跑台账里
        ledger = json.loads((run.work_dir / "resume.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["entries"]), 1)
        self.assertEqual(ledger["entries"][0]["seq"], 1)

    def test_plan_changed_pause_resumes_when_inventory_recovers(self):
        blob1 = _jpeg_bytes((10, 10, 10))
        blob2 = _jpeg_bytes((20, 20, 20))
        other = _jpeg_bytes((90, 90, 90))
        entries = [
            _entry(1, 1, "1", 10, 0, _sha(blob1)),
            _entry(2, 2, "2", 20, 0, _sha(blob2)),
        ]
        plan = _plan(entries)
        first = self._run(
            [_record(1, created_sec=10), _record(9, created_sec=20)],
            _IdFetcher({"1": blob1, "9": other}),
            plan, subdir="recover",
        )
        outcome = first.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "courseware_plan_changed")

        fetcher2 = _IdFetcher({"1": blob1, "2": blob2})
        second = CoursewarePdfRun(
            records=[_record(1, created_sec=10), _record(2, created_sec=20)],
            work_dir=first.work_dir,
            out_pdf=first.out_pdf,
            out_manifest=self.base / "recover" / "manifest.json",
            fetch_page=fetcher2,
            plan=plan,
            plan_digest=courseware_plan_digest(plan),
            course_id="c1", sub_id="s1",
        )
        resumed = second.run()
        self.assertEqual(resumed["state"], "completed")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(fetcher2.fetched, ["2"], "已证明的第一页绝不重复下载")
        self.assertEqual(_pdf_page_count(second.out_pdf), 2)
        self.assertFalse((second.work_dir / "resume.json").exists())

    def test_resume_ledger_from_a_different_plan_is_discarded(self):
        blob1 = _jpeg_bytes((10, 10, 10))
        blobA = _jpeg_bytes((70, 70, 70))
        entries_a = [_entry(1, 1, "1", 10, 0, _sha(blobA))]
        plan_a = _plan(entries_a)
        # 取证器只给 blob1：计划 A 期望的哈希无法证明，第一次尝试暂停。
        first = self._run(
            [_record(1, created_sec=10)], _IdFetcher({"1": blob1}),
            plan_a, subdir="swapplan",
        )
        self.assertEqual(first.run()["state"], "paused")

        blob2 = _jpeg_bytes((20, 20, 20))
        entries_b = [
            _entry(1, 1, "1", 10, 0, _sha(blob1)),
            _entry(2, 2, "2", 20, 0, _sha(blob2)),
        ]
        plan_b = _plan(entries_b)
        fetcher2 = _IdFetcher({"1": blob1, "2": blob2})
        second = CoursewarePdfRun(
            records=[_record(1, created_sec=10), _record(2, created_sec=20)],
            work_dir=first.work_dir,
            out_pdf=first.out_pdf,
            out_manifest=self.base / "swapplan" / "manifest.json",
            fetch_page=fetcher2,
            plan=plan_b,
            plan_digest=courseware_plan_digest(plan_b),
            course_id="c1", sub_id="s1",
        )
        resumed = second.run()
        self.assertEqual(resumed["state"], "completed", "不同计划的旧台账必须整体弃用")
        manifest = json.loads(second.out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["courseware_plan_digest"], courseware_plan_digest(plan_b))
        self.assertEqual(
            [page["source_sha256"] for page in manifest["pages"]],
            [_sha(blob1), _sha(blob2)],
        )

    def test_storm_pause_then_resume_keeps_every_plan_page(self):
        blobs = {str(index): _jpeg_bytes((index * 7 + 5,) * 3) for index in range(1, 5)}
        records = [_record(index) for index in range(1, 5)]
        entries = [
            _entry(index, index, str(index), index * 10, 0, _sha(blobs[str(index)]))
            for index in range(1, 5)
        ]
        plan = _plan(entries)
        first = self._run(
            records, _IdFetcher(blobs), plan,
            budget=PageBudget(distinct_page_storm_limit=2), subdir="storm",
        )
        outcome = first.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "event_storm_paused")
        self.assertFalse(first.out_pdf.exists())
        fetcher2 = _IdFetcher(blobs)
        second = CoursewarePdfRun(
            records=records,
            work_dir=first.work_dir,
            out_pdf=first.out_pdf,
            out_manifest=self.base / "storm" / "manifest.json",
            fetch_page=fetcher2,
            plan=plan,
            plan_digest=courseware_plan_digest(plan),
            course_id="c1", sub_id="s1",
        )
        resumed = second.run()
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(_pdf_page_count(second.out_pdf), 4, "长讲次续跑补齐，绝不截断")
        manifest = json.loads(second.out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["pages"]), 4)

    def test_fetch_failure_pauses_resumably_without_partial_pdf(self):
        blobs = {"1": _jpeg_bytes((10, 10, 10))}

        class _Flaky(_IdFetcher):
            def __call__(self, record):
                raise RuntimeError("transport down")

        records = [_record(1)]
        plan = _plan([_entry(1, 1, "1", 10, 0, _sha(blobs["1"]))])
        run = self._run(records, _Flaky({}), plan, subdir="flaky")
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "plan_fetch_paused")
        self.assertFalse(run.out_pdf.exists())
        self.assertTrue((run.work_dir / "resume.json").is_file())

    def test_unreadable_proven_bytes_pause_without_publishing(self):
        html = b"<!DOCTYPE html><html><body>session expired</body></html>"
        records = [_record(1)]
        plan = _plan([_entry(1, 1, "1", 10, 0, _sha(html))])
        run = self._run(records, _IdFetcher({"1": html}), plan, subdir="html")
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "plan_page_unreadable")
        self.assertFalse(run.out_pdf.exists())

    def test_wall_clock_budget_pauses_plan_run(self):
        blobs = {"1": _jpeg_bytes((10, 10, 10))}
        records = [_record(1)]
        plan = _plan([_entry(1, 1, "1", 10, 0, _sha(blobs["1"]))])
        run = self._run(records, _IdFetcher(blobs), plan, subdir="clock")
        clock_values = iter([0.0, 10_000.0])
        run._clock = lambda: next(clock_values)
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "wall_clock_paused")
        self.assertFalse(run.out_pdf.exists())

    def test_duplicate_metadata_and_plan_digest_land_in_the_manifest(self):
        blob1 = _jpeg_bytes((10, 10, 10))
        blob2 = _jpeg_bytes((20, 20, 20))
        entries = [
            _entry(1, 1, "1", 10, 0, _sha(blob1), duplicate_count=1),
            _entry(2, 2, "2", 20, 0, _sha(blob2)),
        ]
        excluded = [{
            "kept_position": 1, "capture_time": 25, "capture_ordinal": 1,
            "source_sha256": _sha(blob1), "reason": "exact_duplicate",
        }]
        plan = _plan(entries, excluded, counts={
            "input_events": 3, "recognized": 3, "kept": 2,
            "exact_duplicates": 1, "skipped": 0,
        })
        records = [_record(1, created_sec=10), _record(2, created_sec=20)]
        run = self._run(records, _IdFetcher({"1": blob1, "2": blob2}), plan, subdir="dup")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["duplicates"], 1)
        manifest = json.loads(run.out_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["duplicates"], 1)
        self.assertEqual(manifest["courseware_plan_digest"], courseware_plan_digest(plan))
        self.assertEqual(manifest["courseware_plan"]["schema"], "courseware_plan.v1")
        self.assertEqual(manifest["courseware_plan"]["counts"]["input_events"], 3)
        self.assertEqual(
            manifest["pages"][0]["duplicates"],
            [{"original_id": "", "created_sec": 25, "capture_ordinal": 1}],
            "计划折叠的精确重复必须原样保留在清单元数据里",
        )
        self.assertEqual(manifest["pages"][1]["duplicates"], [])
        text = run.out_manifest.read_text(encoding="utf-8")
        self.assertNotIn("http", text)
        self.assertNotIn("pptimgurl", text)
        self.assertNotIn("slides.invalid", text, "清单绝不携带 URL")

    def test_annotation_and_label_metadata_come_from_the_plan(self):
        blob = _jpeg_bytes((10, 10, 10))
        entry = _entry(
            1, 1, "1", 10, 0, _sha(blob),
            page_label="7/40", page_label_source="ocr_text",
            annotation={"class": "annotated_candidate", "confidence": 0.55},
            keep_reason="version_variant_retained", version_of_position=1,
        )
        plan = _plan([entry])
        # 平坦图元在本机分类会得到 unknown；清单必须镜像计划而不是重算。
        run = self._run([_record(1)], _IdFetcher({"1": blob}), plan, subdir="annot")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        manifest = json.loads(run.out_manifest.read_text(encoding="utf-8"))
        page = manifest["pages"][0]
        self.assertEqual(page["annotation"]["class"], "annotated_candidate")
        self.assertEqual(page["annotation"]["confidence"], 0.55)
        self.assertEqual(page["page_label"], "7/40")

    def test_id_less_entry_fast_path_uses_capture_time_and_ordinal(self):
        blob_a = _jpeg_bytes((10, 10, 10))
        blob_b = _jpeg_bytes((20, 20, 20))
        records = [
            _record(1, created_sec=20, original_id="2a"),
            _record(2, created_sec=20, original_id="2b"),
        ]
        entry = _entry(1, 1, "", 20, 1, _sha(blob_b))
        plan = _plan([entry])
        fetcher = _IdFetcher({"2a": blob_a, "2b": blob_b})
        run = self._run(records, fetcher, plan, subdir="ordinal")
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(
            fetcher.fetched, ["2b"], "无记录号的条目按同时刻序数选择后仍须哈希证明",
        )

    def test_progress_totals_follow_the_plan_not_the_inventory(self):
        blobs = {"1": _jpeg_bytes((10, 10, 10)), "2": _jpeg_bytes((20, 20, 20))}
        records = [_record(index) for index in range(1, 6)]
        entries = [
            _entry(1, 1, "1", 10, 0, _sha(blobs["1"])),
            _entry(2, 2, "2", 20, 0, _sha(blobs["2"])),
        ]
        plan = _plan(entries)
        seen = []

        def on_progress(stage, processed, kept, total):
            seen.append((stage, processed, kept, total))

        run = self._run(
            records, _IdFetcher(blobs), plan, subdir="progress",
            on_progress=on_progress,
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "completed")
        pages_reports = [item for item in seen if item[0] == "pages"]
        self.assertEqual(pages_reports[-1], ("pages", 2, 2, 2), "进度只来自计划页计数")
        self.assertEqual(seen[-1][0], "assemble")

    def test_existing_valid_artifact_is_preserved_when_a_plan_run_pauses(self):
        blob = _jpeg_bytes((10, 10, 10))
        records = [_record(1)]
        plan = _plan([_entry(1, 1, "1", 10, 0, _sha(_jpeg_bytes((200, 1, 1))))])
        run = self._run(records, _IdFetcher({"1": blob}), plan, subdir="keepart")
        artifact_dir = run.out_pdf.parent
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.out_pdf.write_bytes(b"%PDF-1.4 existing valid")
        existing_manifest = b'{"schema": "courselens.courseware-pdf-manifest.v1"}'
        run.out_manifest.write_bytes(existing_manifest)
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(run.out_pdf.read_bytes(), b"%PDF-1.4 existing valid")
        self.assertEqual(run.out_manifest.read_bytes(), existing_manifest)

    def test_reconciliation_downloads_are_bounded_per_attempt(self):
        good = _jpeg_bytes((10, 10, 10))
        blobs = {str(index): _jpeg_bytes((index * 7 + 5,) * 3) for index in range(1, 7)}
        blobs["6"] = good
        records = [_record(index) for index in range(1, 7)]
        # 记录 1 定位漂移：快速路径 1 次 + 对账恰好 cap=3 次（候选 2/3/4），
        # 到达上限即暂停，绝不无限下载。
        entry = _entry(1, 1, "1", 10, 0, _sha(good))
        plan = _plan([entry])
        fetcher = _IdFetcher(blobs)
        run = self._run(
            records, fetcher, plan,
            budget=PageBudget(plan_reconcile_fetch_cap=3), subdir="cap",
        )
        outcome = run.run()
        self.assertEqual(outcome["state"], "paused")
        self.assertEqual(outcome["code"], "courseware_plan_changed")
        self.assertEqual(
            len(fetcher.fetched), 4, "快速路径 1 次 + 对账上限 3 次",
        )
        # 新一次尝试重新获得完整对账预算：这次能证明并完成。
        fetcher2 = _IdFetcher(blobs)
        second = CoursewarePdfRun(
            records=records,
            work_dir=run.work_dir,
            out_pdf=run.out_pdf,
            out_manifest=self.base / "cap" / "manifest.json",
            budget=PageBudget(plan_reconcile_fetch_cap=3),
            fetch_page=fetcher2,
            plan=plan,
            plan_digest=courseware_plan_digest(plan),
            course_id="c1", sub_id="s1",
        )
        resumed = second.run()
        self.assertEqual(resumed["state"], "completed")


class PlanValidationTests(unittest.TestCase):
    """courseware_plan.v1 结构校验：字段闭集、置换、绑定与摘要全部门。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _valid_plan(self):
        entries = [
            _entry(1, 1, "11", 30, 0, "a" * 64),
            _entry(2, 2, "12", 60, 0, "b" * 64),
        ]
        return _plan(entries)

    def test_valid_plan_round_trips_with_stable_digest(self):
        plan = self._valid_plan()
        verified = validate_courseware_plan(
            plan, course_id="c1", sub_id="s1",
            plan_digest=courseware_plan_digest(plan),
        )
        self.assertEqual(verified.digest, courseware_plan_digest(plan))
        self.assertEqual(verified.entries[0]["record_id"], "11")
        self.assertEqual(verified.counts["kept"], 2)

    def test_binding_mismatch_is_a_distinct_closed_code(self):
        with self.assertRaises(CoursewarePlanError) as caught:
            validate_courseware_plan(self._valid_plan(), course_id="c1", sub_id="other")
        self.assertEqual(caught.exception.code, "courseware_plan_mismatch")

    def test_committed_digest_mismatch_fails_closed(self):
        with self.assertRaises(CoursewarePlanError):
            validate_courseware_plan(
                self._valid_plan(), plan_digest="0" * 64,
            )

    def test_malformed_plans_fail_closed(self):
        base = self._valid_plan()

        def mutate(**changes):
            plan = json.loads(json.dumps(base, ensure_ascii=False))
            for key, value in changes.items():
                if value is ...:
                    plan.pop(key, None)
                else:
                    plan[key] = value
            return plan

        entries = json.loads(json.dumps(base["entries"]))
        first, second = entries
        cases = {
            "not_an_object": "plan",
            "missing_key": mutate(inventory_digest=...),
            "extra_key": mutate(unexpected="x"),
            "bad_schema": mutate(schema="courseware_plan.v2"),
            "bad_policy": mutate(policy_version=True),
            "bad_pipeline": mutate(pipeline="cloud-automation.v2"),
            "bad_inventory_digest": mutate(inventory_digest="d" * 63),
            "empty_entries": mutate(entries=[]),
            "entries_not_a_list": mutate(entries={"a": 1}),
            "output_not_permutation": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64),
                _entry(1, 2, "12", 60, 0, "b" * 64),
            ]),
            "capture_not_permutation": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64),
                _entry(2, 1, "12", 60, 0, "b" * 64),
            ]),
            "entry_extra_field": mutate(entries=[dict(first, url="https://x")]),
            "entry_missing_field": mutate(entries=[
                {k: v for k, v in first.items() if k != "keep_reason"},
                second,
            ]),
            "url_record_id": mutate(entries=[
                _entry(1, 1, "https://slides.invalid/1", 30, 0, "a" * 64),
                second,
            ]),
            "oversize_record_id": mutate(entries=[
                _entry(1, 1, "x" * 65, 30, 0, "a" * 64), second,
            ]),
            "bad_sha": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "zz" * 32), second,
            ]),
            "same_sha_twice": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64),
                _entry(2, 2, "12", 60, 0, "a" * 64),
            ]),
            "bad_annotation_class": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64,
                       annotation={"class": "junk", "confidence": 0.0}),
                second,
            ]),
            "bad_annotation_confidence": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64,
                       annotation={"class": "unknown", "confidence": 1.5}),
                second,
            ]),
            "bad_keep_reason": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64, keep_reason="looks_nice"),
                second,
            ]),
            "bad_label_source": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64, page_label_source="vibes"),
                second,
            ]),
            "excluded_sha_not_keeper": mutate(excluded=[{
                "kept_position": 1, "capture_time": 25, "capture_ordinal": 0,
                "source_sha256": "c" * 64, "reason": "exact_duplicate",
            }]),
            "excluded_bad_reason": mutate(excluded=[{
                "kept_position": 1, "capture_time": 25, "capture_ordinal": 0,
                "source_sha256": "a" * 64, "reason": "blurry",
            }]),
            "excluded_out_of_range": mutate(excluded=[{
                "kept_position": 9, "capture_time": 25, "capture_ordinal": 0,
                "source_sha256": "a" * 64, "reason": "exact_duplicate",
            }]),
            "bad_ordering_mode": mutate(ordering={"mode": "vibes", "confidence": 0.9}),
            "bad_ordering_keys": mutate(ordering={"mode": "capture_order"}),
            "counts_disagree": mutate(counts={
                "input_events": 2, "recognized": 2, "kept": 1,
                "exact_duplicates": 0, "skipped": 0,
            }),
            "counts_missing_key": mutate(counts={
                "input_events": 2, "recognized": 2, "kept": 2,
                "exact_duplicates": 0,
            }),
            "negative_capture_time": mutate(entries=[
                _entry(1, 1, "11", -1, 0, "a" * 64), second,
            ]),
            "float_position": mutate(entries=[
                _entry(1, 1, "11", 30, 0, "a" * 64) | {"output_position": 1.0},
                second,
            ]),
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(CoursewarePlanError) as caught:
                    validate_courseware_plan(value, course_id="c1", sub_id="s1")
                self.assertEqual(caught.exception.code, "courseware_plan_invalid")

    def test_run_rejects_a_misbound_plan_terminally(self):
        plan = self._valid_plan()
        run = CoursewarePdfRun(
            records=[_record(1)],
            work_dir=self.base / "misbound" / "work",
            out_pdf=self.base / "misbound" / "slides.pdf",
            out_manifest=self.base / "misbound" / "manifest.json",
            plan=plan,
            plan_digest=courseware_plan_digest(plan),
            course_id="c1", sub_id="other",
        )
        with self.assertRaises(CoursewarePlanError) as caught:
            run.run()
        self.assertEqual(caught.exception.code, "courseware_plan_mismatch")
        self.assertFalse(run.out_pdf.exists())

    def test_pause_codes_stay_closed(self):
        self.assertTrue(PAUSE_CODES.issuperset({
            "courseware_plan_changed", "plan_fetch_paused", "plan_page_unreadable",
        }))


if __name__ == "__main__":
    unittest.main()
