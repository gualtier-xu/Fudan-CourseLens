import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from PIL import Image

from src.runtime.http_api import make_handler
from tests.http_services import http_services


class _Stream:
    status = 200
    content_type = "video/mp4"
    content_length = 5
    content_range = ""

    def iter_bytes(self):
        yield b"media"

    def close(self):
        pass


class _Service:
    resources = None

    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}

    auth_catalog = SimpleNamespace(authentication_snapshot=lambda: {"state": "ready"})

    @staticmethod
    def subtitle_segments(sub_id):
        return {
            "sub_id": sub_id,
            "segments": [{
                "start_ms": 0,
                "end_ms": 1000,
                "text": "x",
                "evidence_id": "seg:0123456789ab",
                "parent_evidence_id": "seg:ffffffffffff",
                "provenance": {"producer": "synthetic-worker"},
            }, {
                # C2-3FIX-1：>256KB 大载荷第二段——条件 gzip 门槛路径需要
                # 真实跨阈体量（ensure_ascii=False 中文 3B/字）。
                "start_ms": 1000,
                "end_ms": 2000,
                "text": "大" * 200_000,
            }],
        }

    @staticmethod
    def subtitle_file_path(sub_id):
        # C2-3FIX-1：VTT 轨文件路由的 gzip 选择加入钉测面；非在册讲次 404。
        if sub_id != "lecture":
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        return ApiV3MediaRouteTests.vtt_path

    @staticmethod
    def open_remote_media(sub_id, range_header, *, head_only=False):
        if sub_id == "broken":
            raise RuntimeError("upstream boom")
        if sub_id == "unreachable":
            import requests

            raise requests.ConnectionError("off-campus hop unreachable")
        if sub_id == "rejected":
            from src.runtime.media_source import _ConfirmedServiceResponse

            raise _ConfirmedServiceResponse("HTTP 403")
        if sub_id == "locked":
            raise PermissionError("Sign in before streaming this lecture")
        if sub_id != "lecture":
            raise FileNotFoundError
        return _Stream()

    @staticmethod
    def courseware_pdf_status(sub_id):
        if sub_id != "lecture":
            raise FileNotFoundError
        return {
            "sub_id": sub_id,
            "artifact": {
                "ready": True, "pages": 4, "events_total": 6, "duplicates": 1,
                "skipped_total": 1, "skipped": {"html_body": 1},
                "generated_at": 1789000000, "download_name": "2026-09-14-第一讲.pdf",
            },
            "operation": None,
        }

    @staticmethod
    def courseware_pdf_file_path(sub_id):
        if sub_id != "lecture":
            raise FileNotFoundError
        if ApiV3MediaRouteTests.pdf_path is None:
            raise FileNotFoundError
        return ApiV3MediaRouteTests.pdf_path

    @staticmethod
    def enqueue_courseware_pdf(course_id, sub_id, *, action="generate"):
        if sub_id != "lecture":
            raise FileNotFoundError
        if action == "resume":
            raise ValueError("no paused courseware task to resume")
        return {"task_id": "task-1", "state": "queued"}

    @staticmethod
    def watch_progress(sub_id):
        # 与 src/application.py 同形：讲次不在授权目录 → FileNotFoundError
        # （APP500-CLOSE：此形态曾穿透 GET /api/v3/progress 出口成 500）。
        if sub_id != "lecture":
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        return {"sub_id": sub_id, "position_seconds": 12.0, "duration_seconds": 100.0}

    @staticmethod
    def save_watch_progress(sub_id, *, position_seconds=0.0, duration_seconds=0.0,
                            playback_rate=1.0, completed=False):
        # 同 watch_progress：未在册讲次 FileNotFoundError（曾穿透 POST 出口）。
        if sub_id != "lecture":
            raise FileNotFoundError("Lecture is not in the authorized catalog")
        return {"sub_id": sub_id, "position_seconds": position_seconds}


class ApiV3MediaRouteTests(unittest.TestCase):
    pdf_path = None
    vtt_path = None

    @classmethod
    def setUpClass(cls):
        frontend = Path(__file__).resolve().parents[1] / "frontend"
        tmp = tempfile.TemporaryDirectory()
        cls._pdf_dir = tmp
        image = Image.new("RGB", (32, 24), (220, 220, 220))
        pdf_file = Path(tmp.name) / "slides.pdf"
        image.save(pdf_file, format="PDF")
        cls.pdf_path = pdf_file
        # >256KB 合成 VTT：跨过条件 gzip 门槛（_GZIP_MIN_BYTES），轨道大文件路径可钉
        vtt_file = Path(tmp.name) / "track-big.vtt"
        cue = "这一段合成字幕用于钉住大文本轨文件的压缩选择加入路径行为。"
        vtt_text = "WEBVTT\n\n" + "".join(
            f"{i}\n00:00:00.000 --> 00:00:02.000\n{cue}\n\n" for i in range(1, 3000)
        )
        vtt_file.write_text(vtt_text, encoding="utf-8")
        cls.vtt_path = vtt_file
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(_Service()), frontend))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.pdf_path = None
        cls.vtt_path = None
        cls._pdf_dir.cleanup()

    def test_subtitle_segments_use_v3_envelope(self):
        with urlopen(f"{self.base}/api/v3/subtitles/segments?sub_id=lecture") as response:
            value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["schema"], "courselens.api.v3")
        segment = value["data"]["segments"][0]
        self.assertEqual(segment["text"], "x")
        # 附加证据字段按原样透传，不影响既有字段
        self.assertEqual(segment["evidence_id"], "seg:0123456789ab")
        self.assertEqual(segment["parent_evidence_id"], "seg:ffffffffffff")
        self.assertEqual(segment["provenance"], {"producer": "synthetic-worker"})
        self.assertEqual(segment["start_ms"], 0)
        self.assertEqual(segment["end_ms"], 1000)

    def test_subtitle_segments_identity_without_gzip_header(self):
        # C2-3FIX-1：不带 Accept-Encoding 的客户端（脚本/钉测）拿到逐字节原样
        # 响应，零 Content-Encoding——契约保持的负路径钉。
        request = Request(f"{self.base}/api/v3/subtitles/segments?sub_id=lecture")
        with urlopen(request) as response:
            body = response.read()
            self.assertIsNone(response.headers["Content-Encoding"])
        self.assertIn("大".encode("utf-8") if False else "大".encode("utf-8"), body)

    def test_subtitle_segments_gzip_when_client_accepts(self):
        import gzip as _gzip
        request = Request(f"{self.base}/api/v3/subtitles/segments?sub_id=lecture")
        request.add_header("Accept-Encoding", "gzip")
        with urlopen(request) as response:
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            wire = response.read()
        payload = json.loads(_gzip.decompress(wire).decode("utf-8"))
        segments = payload["data"]["segments"]
        self.assertEqual(segments[0]["evidence_id"], "seg:0123456789ab")
        self.assertEqual(segments[0]["text"], "x")
        self.assertEqual(segments[1]["text"], "大" * 200_000)

    def test_subtitle_file_gzip_when_client_accepts(self):
        import gzip as _gzip
        request = Request(f"{self.base}/api/v3/subtitles/file?sub_id=lecture")
        request.add_header("Accept-Encoding", "gzip")
        with urlopen(request) as response:
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            wire = response.read()
        self.assertTrue(_gzip.decompress(wire).startswith(b"WEBVTT"))

    def test_subtitle_file_identity_without_gzip_header(self):
        request = Request(f"{self.base}/api/v3/subtitles/file?sub_id=lecture")
        with urlopen(request) as response:
            self.assertIsNone(response.headers["Content-Encoding"])
            self.assertTrue(response.read().startswith(b"WEBVTT"))

    def test_media_is_no_store_and_supports_head(self):
        with urlopen(f"{self.base}/api/v3/media?sub_id=lecture") as response:
            self.assertEqual(response.read(), b"media")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        request = Request(f"{self.base}/api/v3/media?sub_id=lecture", method="HEAD")
        with urlopen(request) as response:
            self.assertEqual(response.read(), b"")
            self.assertEqual(response.headers["Content-Length"], "5")

    def test_media_upstream_failure_leaves_closed_set_evidence_line(self):
        # M1（LIVE-VALIDATE-2）：502 路径必须在 tee 日志留闭集归因行（异常类名），
        # 消息文本（可能携带上游 URL）不得出现。
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(Exception) as caught:
                urlopen(f"{self.base}/api/v3/media?sub_id=broken")
        self.assertIn("502", str(caught.exception))
        line = buffer.getvalue()
        self.assertIn("[media] failure_code=media_upstream_unavailable route=open", line)
        self.assertIn("kind=RuntimeError", line)
        self.assertNotIn("upstream boom", line)

    def test_media_open_failures_record_closed_set_status_for_card_refinement(self):
        # MEDIA-001-20261001：<video> 把一切开流失败（502/404/401）塌缩成
        # MediaError code 4；路由把精确结局记成闭集码，错误卡凭
        # /api/v3/media/stream-status 细分（校外不可达/授权/通用兜底）。
        import contextlib
        buffer = io.StringIO()

        def status():
            with urlopen(f"{self.base}/api/v3/media/stream-status") as response:
                return json.loads(response.read().decode("utf-8"))["data"]

        seq_before = status()["seq"]
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(Exception):
                urlopen(f"{self.base}/api/v3/media?sub_id=unreachable")
        recorded = status()
        self.assertEqual(recorded["failure_code"], "upstream_unreachable")
        self.assertGreater(recorded["seq"], seq_before)

        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(Exception):
                urlopen(f"{self.base}/api/v3/media?sub_id=rejected")
        self.assertEqual(status()["failure_code"], "upstream_rejected")

        with self.assertRaises(Exception):
            urlopen(f"{self.base}/api/v3/media?sub_id=locked")
        self.assertEqual(status()["failure_code"], "auth_required")

        with self.assertRaises(Exception):
            urlopen(f"{self.base}/api/v3/media?sub_id=elsewhere")
        self.assertEqual(status()["failure_code"], "missing")

        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(Exception):
                urlopen(f"{self.base}/api/v3/media?sub_id=broken")
        self.assertEqual(status()["failure_code"], "unknown")

        # 成功开流清空（码=空串，seq 继续推进）：错误卡绝不把陈旧失败
        # 安到后续新请求头上。
        with urlopen(f"{self.base}/api/v3/media?sub_id=lecture") as response:
            response.read()
        cleared = status()
        self.assertEqual(cleared["failure_code"], "")
        self.assertGreater(cleared["seq"], recorded["seq"])

    def test_media_stream_status_uses_v3_envelope(self):
        with urlopen(f"{self.base}/api/v3/media/stream-status") as response:
            value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["schema"], "courselens.api.v3")
        self.assertIn(value["data"]["failure_code"], {
            "", "upstream_unreachable", "upstream_rejected",
            "auth_required", "missing", "invalid", "unknown",
        })
        self.assertIsInstance(value["data"]["seq"], int)
        # MEDIA-VPN-1：aTrust 在位三态并入同一回读——闭集键、无路径/进程
        # 列表/代理地址值（错误卡据此分「未装→引导安装 / 在位→确认接入」）。
        atrust = value["data"]["atrust"]
        self.assertEqual(sorted(atrust.keys()), ["proxy", "signals", "state"])
        self.assertIn(atrust["state"], {"present", "not_installed", "unknown"})
        self.assertEqual(sorted(atrust["signals"].keys()), ["directory", "process", "service"])
        self.assertEqual(sorted(atrust["proxy"].keys()), ["env_configured", "system_configured"])
        atrust_blob = json.dumps(atrust)
        self.assertNotIn("\\", atrust_blob, "闭集快照不得携带路径类文本")
        self.assertNotIn("://", atrust_blob, "闭集快照不得携带代理地址值")

    def test_courseware_pdf_status_uses_v3_envelope_and_closed_metadata(self):
        with urlopen(f"{self.base}/api/v3/courseware-pdf?sub_id=lecture") as response:
            value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["schema"], "courselens.api.v3")
        status = value["data"]
        self.assertTrue(status["artifact"]["ready"])
        self.assertEqual(status["artifact"]["pages"], 4)
        self.assertEqual(status["artifact"]["events_total"], 6)
        self.assertNotIn("http", json.dumps(status))
        self.assertNotIn("/", status["artifact"]["download_name"].replace("\\", ""))

    def test_courseware_pdf_status_rejects_unknown_lectures(self):
        with self.assertRaises(Exception) as caught:
            urlopen(f"{self.base}/api/v3/courseware-pdf?sub_id=elsewhere")
        self.assertIn("404", str(caught.exception))

    def test_courseware_pdf_file_streams_path_checked_pdf(self):
        with urlopen(f"{self.base}/api/v3/courseware-pdf/file?sub_id=lecture") as response:
            body = response.read()
            self.assertEqual(response.headers["Content-Type"], "application/pdf")
            self.assertIn("private, no-store", response.headers.get_all("Cache-Control"))
        self.assertGreater(len(body), 100)
        self.assertTrue(body.startswith(b"%PDF"))
        with self.assertRaises(Exception) as caught:
            urlopen(f"{self.base}/api/v3/courseware-pdf/file?sub_id=../../state.db")
        self.assertIn("404", str(caught.exception))

    def test_courseware_pdf_action_route_maps_errors_to_closed_codes(self):
        request = Request(
            f"{self.base}/api/v3/courseware-pdf/actions",
            data=json.dumps({"action": "generate", "course_id": "c", "sub_id": "lecture"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["data"]["task_id"], "task-1")
        resume = Request(
            f"{self.base}/api/v3/courseware-pdf/actions",
            data=json.dumps({"action": "resume", "course_id": "c", "sub_id": "lecture"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(Exception) as caught:
            urlopen(resume)
        self.assertIn("409", str(caught.exception))

    def test_progress_read_roundtrip_uses_v3_envelope(self):
        with urlopen(f"{self.base}/api/v3/progress?sub_id=lecture") as response:
            value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["schema"], "courselens.api.v3")
        self.assertEqual(value["data"]["progress"]["position_seconds"], 12.0)
        request = Request(
            f"{self.base}/api/v3/progress",
            data=json.dumps({"sub_id": "lecture", "position_seconds": 30}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            value = json.loads(response.read().decode("utf-8"))
        self.assertEqual(value["data"]["progress"]["position_seconds"], 30.0)

    def test_progress_get_unknown_lecture_stays_in_closed_set_not_500(self):
        """APP500-CLOSE（MEDIA-RETRY-1 停车场同族）：讲次不在授权目录时
        watch_progress 的 FileNotFoundError 曾穿透 GET /api/v3/progress 出口
        （无 try），被顶层兜底洗成 500 runtime_failed——学生看到生硬错误而非
        「这门讲次不在当前课程目录里了。刷新目录即可同步。」的人话指引。
        此钉防回退：必须是 404 闭集码 lecture_not_found。"""
        with self.assertRaises(Exception) as caught:
            urlopen(f"{self.base}/api/v3/progress?sub_id=elsewhere")
        self.assertIn("404", str(caught.exception))
        payload = json.loads(caught.exception.read())
        self.assertEqual(payload["error_code"], "lecture_not_found")
        self.assertNotEqual(payload["error_code"], "runtime_failed")

    def test_progress_post_unknown_lecture_stays_in_closed_set_not_500(self):
        """APP500-CLOSE 同族第二穿透：save_watch_progress 的 FileNotFoundError
        曾逃过 POST /api/v3/progress 出口闭集（KeyError/TypeError/ValueError），
        被顶层兜底洗成 500 runtime_failed。必须是 404 闭集码 lecture_not_found。"""
        request = Request(
            f"{self.base}/api/v3/progress",
            data=json.dumps({"sub_id": "elsewhere", "position_seconds": 5}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(Exception) as caught:
            urlopen(request)
        self.assertIn("404", str(caught.exception))
        payload = json.loads(caught.exception.read())
        self.assertEqual(payload["error_code"], "lecture_not_found")
        self.assertNotEqual(payload["error_code"], "runtime_failed")


if __name__ == "__main__":
    unittest.main()
