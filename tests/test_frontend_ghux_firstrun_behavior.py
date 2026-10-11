from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_ghux_firstrun_behavior.mjs"


class FrontendGhuxFirstRunBehaviorTests(unittest.TestCase):
    def test_real_modules_auto_advance_first_run_github_link_with_single_click(self):
        """GH-UX-REWORK-1 首跑三硬指标的行为钉（真实 settings/remote-panel 模块）：

        - 除输设备码与点「授权并创建专属仓库」外全链零必要点击（自动 bootstrap
          ×2、安装侦测自动继续、收口自动加密测试）；
        - 授权确认即刻呈现（设备码区「正在初始化专属仓库…」），状态空窗消除；
        - 验证码过期自动重发恰 2 次（一次登录面不被过期打断成死端）；
        - 自动打开安装页即布防等待，pending 呈现剩余有效期。
        """
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for frontend behavior tests")
        result = subprocess.run(
            [str(node), str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"frontend ghux first-run behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend ghux first-run behavior passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
