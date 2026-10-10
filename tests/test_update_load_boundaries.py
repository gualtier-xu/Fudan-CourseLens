"""SRC-CLEANUP-1 U4: 更新域加载边界裸异常族——红绿探针（N6CP EX5 复用）。

损坏的本地工件（trust 配置 BOM/截断/UTF-16、槽位身份文件、helper 布局/状态、
manifest 字节）必须落闭集码，绝不向学生裸抛堆栈或被宽网误标。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from scripts.client_update_helper import apply as helper_apply
from src.update.service import MANIFEST_SOURCE_ID, UpdateError, UpdateService, TrustPolicy


def _service(tmp: Path, trust_bytes: bytes) -> UpdateService:
    trust = tmp / "trust.json"
    trust.write_bytes(trust_bytes)
    return UpdateService(
        current_version="0.1.0", trust_path=trust,
        state_root=tmp / "state", install_root=tmp / "install",
    )


class TrustLoadBoundaryTests(unittest.TestCase):
    def test_truncated_trust_config_reports_closed_code(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            service = _service(tmp, b'{"schema": "courselens.client-update-trust.v2", "chan')
            result = service.check()
            self.assertEqual(result["error_code"], "trust_config_invalid")
            self.assertEqual(result["state"], "policy_blocked")

    def test_utf8_bom_trust_config_reports_closed_code(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            service = _service(tmp, b'\xef\xbb\xbf{"schema": "courselens.client-update-trust.v2"}')
            result = service.check()
            self.assertEqual(result["error_code"], "trust_config_invalid")

    def test_utf16_trust_config_reports_closed_code(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            service = _service(tmp, '{"schema": "courselens.client-update-trust.v2"}'.encode("utf-16"))
            result = service.check()
            self.assertEqual(result["error_code"], "trust_config_invalid")


class ManifestDecodeBoundaryTests(unittest.TestCase):
    def test_corrupt_manifest_bytes_report_manifest_invalid(self):
        # 钉 :739 fetch 解码边界：即使 trust 有效，坏 manifest 字节也必须
        # 落 manifest_invalid 闭集码（而非裸 ValueError 逃逸）。
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            trust = tmp / "trust.json"
            trust.write_text(json.dumps({"schema": "courselens.client-update-trust.v2"}), encoding="utf-8")
            service = UpdateService(
                current_version="0.1.0", trust_path=trust,
                state_root=tmp / "state", install_root=tmp / "install",
            )
            policy = TrustPolicy.load(trust, now=1_000_000.0)
            with patch.object(service, "_policy", return_value=policy), \
                 patch.object(service, "_fetch", return_value=b"{not-json"):
                result = service.check()
            self.assertEqual(result["error_code"], "manifest_invalid")


class SlotMetadataBoundaryTests(unittest.TestCase):
    def test_corrupt_staged_slot_metadata_reports_identity_code(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            service = _service(tmp, b'{"schema": "courselens.client-update-trust.v2"}')
            source = {"repository": MANIFEST_SOURCE_ID, "commit": "a" * 40, "tree": "b" * 40}
            payload = b"print('hello')\n"
            metadata = {
                "schema": "courselens.client-package.v1",
                "version": "1.1.0", "release_id": "client-v1.1.0",
                "platform": "windows", "architecture": "x86_64",
                "source": source,
                "files": {"app/main.py": hashlib.sha256(payload).hexdigest()},
            }
            service._package_path.parent.mkdir(parents=True)
            with zipfile.ZipFile(service._package_path, "w") as archive:
                archive.writestr("courselens-package.json", json.dumps(metadata))
                archive.writestr("app/main.py", payload)
            # 既有槽位带损坏身份文件——裸读必须变闭集码，绝不裸抛。
            slot = service.install_root / "versions" / "1.1.0"
            slot.mkdir(parents=True)
            (slot / "courselens-package.json").write_bytes(b"{corrupt")
            with self.assertRaises(UpdateError) as caught:
                service._extract(metadata)
            self.assertEqual(caught.exception.code, "package_identity_mismatch")


class HelperLoadBoundaryTests(unittest.TestCase):
    def test_corrupt_install_layout_reports_managed_install_required(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            (state / "install-layout.json").write_bytes(b"{corrupt")
            with self.assertRaises(RuntimeError) as caught:
                helper_apply(root)
            self.assertEqual(str(caught.exception), "managed_install_required")

    def test_corrupt_current_json_without_pending_reports_pending_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / "state"
            state.mkdir()
            (state / "install-layout.json").write_text(
                json.dumps({"schema": "courselens.managed-install.v1"}), encoding="utf-8"
            )
            (state / "current.json").write_bytes(b"{corrupt")
            with self.assertRaises(RuntimeError) as caught:
                helper_apply(root)
            self.assertEqual(str(caught.exception), "pending_update_missing")


if __name__ == "__main__":
    unittest.main()
