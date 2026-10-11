from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from credentials import CredentialStore
from src.application import CourseLensApplication
from path_utils import PROJECT_ROOT


class OnlineOnlyModeTests(unittest.TestCase):
    def test_missing_remote_token_does_not_block_reauthorization_ui(self):
        """U1：开关退役后，缺 token 的装机（含老 remote_enabled=1 残留）仍能打开
        重新授权的 UI——启动不因远端配置不全而阻塞，协调器如实为 None。"""
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            output = Path(directory)
            CredentialStore(output / "credentials.json").save_secret("remote_enabled", "1")

            service = CourseLensApplication(output)
            try:
                self.assertIsNone(service.remote_coordinator)
                self.assertFalse(service.remote_compute_snapshot()["configured"])
            finally:
                service.close()

    def test_settings_remain_readable_after_background_services_start(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            try:
                service.start_search_index()
                service.remote_connection.start()
                snapshot = service.settings_privacy_snapshot()
                self.assertEqual(snapshot["code"], "settings_snapshot_ready")
                self.assertIn(snapshot["analytics"]["state"], {"ready", "action_required"})
            finally:
                service.close()

    def test_existing_video_is_neither_exposed_nor_used_for_playback(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            output = Path(directory)
            video = output / "source.mp4"
            video.write_bytes(b"private-media")
            service = CourseLensApplication(output)
            try:
                service.catalog_repository.upsert_course("course", "Course")
                service.catalog_repository.upsert_lecture(
                    "course",
                    {
                        "sub_id": "lecture",
                        "sub_title": "Lecture",
                        "has_playback": True,
                        "legacy_file_path": str(video.relative_to(PROJECT_ROOT)),
                    },
                )

                class Response:
                    status_code = 200
                    headers = {"content-length": "6", "content-type": "video/mp4"}

                    def raise_for_status(self):
                        return None

                    def iter_content(self, chunk_size):
                        yield b"remote"

                    def close(self):
                        return None

                class Session:
                    def get(self, *args, **kwargs):
                        return Response()

                    def close(self):
                        return None

                class Vpn:
                    session = Session()

                class Client:
                    vpn = Vpn()

                    def get_video_url(self, course_id, sub_id):
                        return "https://media.invalid/stream"

                    def get_stream_params(self, url):
                        return url, ""

                service._credentials = {"student_id": "synthetic", "password": "synthetic"}
                service._client = Client()
                service._client_last_verified_at = time.monotonic()
                stream = service.open_remote_media("lecture")
                try:
                    self.assertEqual(b"".join(stream.iter_bytes()), b"remote")
                finally:
                    stream.close()
                self.assertFalse(hasattr(service, "online_only"))
                self.assertEqual(service.config_snapshot()["player"]["kind"], "browser")
                self.assertEqual(video.read_bytes(), b"private-media")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
