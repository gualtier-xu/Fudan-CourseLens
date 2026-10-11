"""S10-A 重启恢复探针：全新进程从 DPAPI 加密库一键恢复已保存账号。

进程 A（子进程）经真实 CourseLensApplication 写入合成凭据（登录 remember 语义、
DeepSeek remember 语义、网络设置）；进程 B/C 是完全新的 Python 进程，只消费
公共/闭集快照验证恢复语义。合同点：
- 恰一个可用已保存账号在新进程可列出（student_id + requires_rotation 闭集）；
- use_saved_credentials 一键恢复内存凭据（等价登录 dialog 的 use-saved 动作）；
- DeepSeek key 本机加密保存跨进程恢复（只在子进程内存比对，绝不打印）；
- 网络设置经 state.db 跨进程保留；
- auto_connect 默认关闭时不消费任何已保存账号（会话 opt-in 语义保持）；
- auto_connect 显式开启时只恢复其选定账号，绝不猜测其他账号；
- DPAPI 信封（credentials.json）与探针输出均不含明文密码/密钥。

凭据仅使用本文件内联的合成值，不触碰 runtime/data 与任何真实账号。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SYNTHETIC_ID = "s10a-probe-student"
SYNTHETIC_PASSWORD = "s10a-probe-password-9f3a"
SYNTHETIC_DEEPSEEK_KEY = "sk-s10a-probe-key-1c7e"
NETWORK_MODE = "direct"

_CHILD_SCRIPT_HEAD = """
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path({project_root!r})
sys.path.insert(0, str(PROJECT_ROOT))

mode = sys.argv[1]
data_dir = Path(sys.argv[2])
student_id = sys.argv[3]
password = sys.argv[4]
deepseek_key = sys.argv[5]
NETWORK_MODE = "direct"

from src.application import CourseLensApplication
"""

_CHILD_SCRIPT_BODY = r"""
if mode == "write":
    app = CourseLensApplication(data_dir)
    app.set_credentials(student_id, password, remember=True)
    app.set_deepseek_key(deepseek_key, remember=True)
    snapshot = app.network.update(NETWORK_MODE, "")
    assert snapshot["mode"] == NETWORK_MODE, snapshot
    accounts = app.credentials.list_accounts()
    assert accounts == [{"student_id": student_id, "requires_rotation": False}], accounts
    print(json.dumps({"ok": True, "accounts": accounts}))
elif mode == "restore":
    app = CourseLensApplication(data_dir)
    # auto_connect 默认关闭：新进程绝不自行消费已保存账号（会话 opt-in 语义）
    resume = app.start_auto_connect_resume()
    assert resume == {"state": "off"}, resume
    with app._lock:
        before = dict(app._credentials)
    assert before == {"student_id": "", "password": ""}, before
    accounts = app.credentials.list_accounts()
    assert accounts == [{"student_id": student_id, "requires_rotation": False}], accounts
    # 登录 dialog use-saved 的后端等价动作：一键免密恢复内存凭据
    app.use_saved_credentials(student_id)
    with app._lock:
        restored = dict(app._credentials)
    assert restored["student_id"] == student_id
    assert restored["password"] == password  # 仅子进程内存比对，不进入 stdout
    # DeepSeek：本机加密保存跨进程恢复（_deepseek_key 懒加载自 DPAPI 信封）
    assert app.has_deepseek_key() is True
    assert app._deepseek_key() == deepseek_key
    # 网络设置经 state.db 保留
    assert app.network.snapshot()["mode"] == NETWORK_MODE
    print(json.dumps({"ok": True, "accounts": accounts, "restored_id": restored["student_id"]}))
elif mode == "resume":
    app = CourseLensApplication(data_dir)
    # 合成环境绝不触发真实复旦刷新：换成本地只读快照
    app.refresh_authorized_catalog_async = app.authentication_snapshot
    preference = app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": student_id}})
    assert preference["fudan"]["status"] == "ready", preference
    started = app.start_auto_connect_resume()
    assert started["state"] == "started" and started["fudan"] is True, started
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with app._lock:
            if app._credentials.get("student_id"):
                break
        time.sleep(0.05)
    with app._lock:
        restored_id = app._credentials.get("student_id")
    assert restored_id == student_id, restored_id
    # last_resume 记录闭集 code（进度仍以真实登录流为准）
    preference = app._auto_connect_preference()
    assert preference["last_resume"]["fudan"].get("code") == "fudan_resume_started", preference
    print(json.dumps({"ok": True, "resumed_id": restored_id}))
else:
    raise SystemExit(f"unknown mode {mode}")
"""


def _run_child(mode: str, data_dir: Path, output_dir: Path) -> dict:
    script = output_dir / f"probe-child-{mode}.py"
    script.write_text(
        _CHILD_SCRIPT_HEAD.format(project_root=str(PROJECT_ROOT)) + _CHILD_SCRIPT_BODY,
        encoding="utf-8",
    )
    env = {**os.environ, "COURSELENS_DISABLE_AUTOMATION_MONITOR": "1"}
    completed = subprocess.run(
        [sys.executable, str(script), mode, str(data_dir), SYNTHETIC_ID, SYNTHETIC_PASSWORD, SYNTHETIC_DEEPSEEK_KEY],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"probe child {mode} failed:\n{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
        )
    # 探针输出只含闭集事实：明文密码/密钥不得出现在 stdout/stderr
    assert SYNTHETIC_PASSWORD not in completed.stdout
    assert SYNTHETIC_DEEPSEEK_KEY not in completed.stdout
    assert SYNTHETIC_PASSWORD not in completed.stderr
    assert SYNTHETIC_DEEPSEEK_KEY not in completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@unittest.skipUnless(os.name == "nt", "DPAPI credential probe requires Windows")
class SavedAccountRestoreProbeTests(unittest.TestCase):
    def test_fresh_process_restores_single_saved_account_without_plaintext(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="s10a-restore-probe-", dir=scratch) as temp:
            output = Path(temp)
            data_dir = output / "data"
            data_dir.mkdir()
            written = _run_child("write", data_dir, output)
            self.assertEqual(
                written["accounts"],
                [{"student_id": SYNTHETIC_ID, "requires_rotation": False}],
            )
            restored = _run_child("restore", data_dir, output)
            self.assertEqual(restored["restored_id"], SYNTHETIC_ID)
            # DPAPI 信封只含密文：明文密码/密钥不得出现在序列化文件中
            envelope = (data_dir / "credentials.json").read_text(encoding="utf-8")
            self.assertNotIn(SYNTHETIC_PASSWORD, envelope)
            self.assertNotIn(SYNTHETIC_DEEPSEEK_KEY, envelope)
            envelope_data = json.loads(envelope)
            self.assertEqual(list(envelope_data["accounts"].keys()), [SYNTHETIC_ID])
            # 信封内密码字段只存 DPAPI 密文（base64 ASCII）；明文断言已在上方完成
            ciphertext = envelope_data["accounts"][SYNTHETIC_ID]["password"]
            self.assertIsInstance(ciphertext, str)
            self.assertTrue(all(char.isascii() for char in ciphertext))

    def test_explicit_auto_connect_resume_only_uses_selected_account(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="s10a-resume-probe-", dir=scratch) as temp:
            output = Path(temp)
            data_dir = output / "data"
            data_dir.mkdir()
            _run_child("write", data_dir, output)
            resumed = _run_child("resume", data_dir, output)
            self.assertEqual(resumed["resumed_id"], SYNTHETIC_ID)
            envelope = (data_dir / "credentials.json").read_text(encoding="utf-8")
            self.assertNotIn(SYNTHETIC_PASSWORD, envelope)
            self.assertNotIn(SYNTHETIC_DEEPSEEK_KEY, envelope)


if __name__ == "__main__":
    unittest.main()
