"""Contract pins for the README「安全与隐私架构」chapter (SECURITY-DOC-1).

这些钉把 README 安全章节的对外承诺钉在代码事实上：外联主机闭集、DPAPI
凭据路径、回环绑定、gitleaks 豁免形状、主题本机存储与版本引用。新增外联
主机或移动这些锚点时，请在同一次变更里同步 README 章节——对外承诺必须
与代码同速走（北极星：安全是面向学生的核心卖点，不是内部脚注）。
"""

from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]

# 外联主机闭集的代码锚点面：SERVICE_PROBES 探针 + 会话客户端主机常量 +
# 更新下载 allowed_hosts。与 README「客户端会和哪些服务器通信」表同源；
# 加主机 = 加锚点文件或常量，本测试随之红，提醒同步 README 表。
_OUTBOUND_ANCHOR_FILES = (
    "src/runtime/network.py",
    "src/runtime/config.py",
    "src/runtime/timetable.py",
    "src/runtime/exam_schedule.py",
    "src/api/webvpn.py",
    "src/remote/github_app.py",
    "src/distribution.py",
)

# 本机回环不是外联主机。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}

_REQUIRED_SECTIONS = (
    "## 安全与隐私架构",
    "### 你的数据在哪里",
    "### 谁能看到什么",
    "### 客户端会和哪些服务器通信",
    "### 原生窗口壳的对外动作清单",
    "### 出问题时怎么表现",
    "### 我们自己怎么核对",
)

_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
_HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _readme_security_chapter() -> str:
    readme = _read("README.md")
    start = readme.index("## 安全与隐私架构")
    end = readme.index("\n## ", start + 1)
    return readme[start:end]


def _code_outbound_hosts() -> set:
    hosts = set()
    for relative_path in _OUTBOUND_ANCHOR_FILES:
        for match in _URL_HOST_RE.finditer(_read(relative_path)):
            hosts.add(match.group(1).lower())
    # 更新下载面在 distribution 注册表里是裸主机名（allowed_hosts），
    # 不带 scheme，URL 正则抓不到——单独从权威 JSON 取。
    registry = json.loads(_read("config/distribution.json"))
    for host in registry.get("allowed_hosts", []):
        hosts.add(str(host).lower())
    return hosts - _LOCAL_HOSTS


def _readme_table_hosts(chapter: str) -> set:
    hosts = set()
    in_table = False
    for line in chapter.splitlines():
        if line.startswith("### 客户端会和哪些服务器通信"):
            in_table = True
            continue
        if in_table and line.startswith("### "):
            break
        if in_table and line.startswith("|"):
            for token in _BACKTICK_RE.findall(line):
                token = token.strip().lower()
                if _HOSTNAME_RE.match(token):
                    hosts.add(token)
    return hosts


class SecurityChapterContractTest(unittest.TestCase):
    """README 安全章节 ↔ 代码事实的对账钉。"""

    def test_security_chapter_sections_present(self) -> None:
        chapter = _readme_security_chapter()
        for heading in _REQUIRED_SECTIONS:
            self.assertIn(heading, chapter)

    def test_outbound_host_closed_set_matches_readme(self) -> None:
        code_hosts = _code_outbound_hosts()
        chapter = _readme_security_chapter()
        table_hosts = _readme_table_hosts(chapter)
        self.assertEqual(
            code_hosts,
            table_hosts,
            "README 外联闭集表与代码主机常量不一致："
            f"代码有而表无={sorted(code_hosts - table_hosts)}，"
            f"表有而代码无={sorted(table_hosts - code_hosts)}",
        )
        count = len(code_hosts)
        self.assertIn(f"全部 {count} 个固定外联主机", chapter)
        self.assertIn(f"上表 {count} 个主机", chapter)

    def test_dpapi_claim_anchored(self) -> None:
        credentials = _read("credentials.py")
        self.assertIn("CryptProtectData", credentials)
        self.assertIn("CryptUnprotectData", credentials)
        self.assertIn("DPAPI", _readme_security_chapter())

    def test_loopback_bind_claim_anchored(self) -> None:
        app = _read("src/app.py")
        self.assertIn("CourseLens only binds the loopback interface", app)
        self.assertIn("回环地址", _readme_security_chapter())

    def test_gitleaks_exemption_shape(self) -> None:
        root_config = _read(".gitleaks.toml")
        self.assertIn("useDefault = true", root_config)
        self.assertIn("sk-s10a-probe-key-1c7e", root_config)
        self.assertIn("sk-0123456789abcdef012345", root_config)
        worker_config = _read("worker/.gitleaks.toml")
        self.assertIn("useDefault = true", worker_config)
        self.assertIn("install_models", worker_config)
        chapter = _readme_security_chapter()
        self.assertIn("零真实凭据发现", chapter)
        self.assertIn("豁免只有两处", chapter)

    def test_theme_preference_is_local(self) -> None:
        shell = _read("frontend/modules/shell.js")
        self.assertIn('THEME_PREF_KEY = "courselens.theme.v2"', shell)
        self.assertIn("localStorage.setItem(THEME_PREF_KEY", shell)
        self.assertIn("纯为本机显示偏好", _readme_security_chapter())

    def test_task_envelope_disclosure_present(self) -> None:
        # SEC-4：媒体/课件任务的密封信封携带校方会话凭据与 DeepSeek Key
        # （application.py build_job 两处），README「谁能看到什么」必须如实
        # 点名，不得让「凭据只在托管时离开本机」被误读为信封零携带。
        application = _read("src/application.py")
        self.assertIn('secrets["source_credentials"]', application)
        chapter = _readme_security_chapter()
        self.assertIn("任务信封", chapter)
        self.assertIn("校方会话凭据", chapter)

    def test_readme_version_matches_canon(self) -> None:
        canon = json.loads(_read("courselens-version.json"))
        version = canon["version"]
        readme = _read("README.md")
        self.assertIn(
            f"当前版本 **{version}**",
            readme,
            "README 当前版本与 courselens-version.json 正典不同步：发版时请同一次改齐"
            "（含三步开始里的 setup/checksum/安全指引直链）。",
        )
        # 版本耦合面不止「当前版本」一行：setup/checksum 文件名、certutil
        # 示例、tag 资产直链全部必须与正典同版，防「改了标题忘了链接」。
        import re

        for match in re.finditer(r"CourseLens-(\d+\.\d+\.\d+)-", readme):
            self.assertEqual(
                match.group(1),
                version,
                f"README 内文件名版本 {match.group(1)} 与正典 {version} 不同步",
            )
        for match in re.finditer(r"client-v(\d+\.\d+\.\d+)/", readme):
            self.assertEqual(
                match.group(1),
                version,
                f"README 内 tag 直链版本 {match.group(1)} 与正典 {version} 不同步",
            )


class PrivacyNoticeContractTest(unittest.TestCase):
    """B32（N15-R4/OV-5a）：privacy-notice.md 主机披露 ↔ 代码外联闭集 parity。

    此前 notice 的主机清单只靠 AS5 少量 assertIn 事实钉——README↔代码有双向
    parity 门，notice 在门外：代码增主机漏更 notice = 隐私承诺静默缩水，全量
    仍绿。notice 是学生对「我会联哪些服务器」的第一承诺面，必须与代码闭集
    同速走。干跑基线（N15-R4）：抽取 10 token 与代码闭集 10 主机恰等。

    注意 `github.com` 一类的裸主机（无前缀）也要抽到——正则用「主机形
    token」（点分段 + com/cn 顶级域），不是 URL 前缀式。
    """

    _NOTICE_HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|cn)\b")

    def test_notice_host_disclosure_matches_code_closed_set(self) -> None:
        notice = _read("docs/privacy-notice.md")
        tokens = {token.lower() for token in self._NOTICE_HOST_RE.findall(notice)}
        code_hosts = _code_outbound_hosts()
        self.assertGreaterEqual(
            len(tokens), 5, "notice 主机抽取异常少：抽取器或 notice 文件失效须先修",
        )
        self.assertEqual(
            tokens - code_hosts, set(),
            f"隐私声明披露了代码外联闭集之外的主机: {sorted(tokens - code_hosts)}",
        )
        self.assertEqual(
            code_hosts - tokens, set(),
            f"代码外联闭集主机未进隐私声明（承诺静默缩水，与 README 表脱同步）: "
            f"{sorted(code_hosts - tokens)}",
        )

    def test_notice_discloses_task_envelope_credential_carriage(self) -> None:
        # SEC-4 同源：任务信封携带校方会话凭据与 DeepSeek Key 的如实披露。
        notice = _read("docs/privacy-notice.md")
        self.assertIn("任务信封", notice)
        self.assertIn("校方会话凭据", notice)
        self.assertIn("DeepSeek Key", notice)


class ReleaseMaterialContractTest(unittest.TestCase):
    """发布材料安全叙事与新章节同口径、直链用改后仓名。"""

    def test_release_materials_use_renamed_repo_in_urls(self) -> None:
        # 旧仓名 URL 形如 "Fudan-CourseLens-Worker/releases"；新名含
        # "-Release/releases"，不含该子串。历史性纯文字提及（无斜杠）不算。
        for relative_path in (
            "README.md",
            "docs/release-announcement-draft.md",
            "docs/release-notes-0100-draft.md",
        ):
            self.assertNotIn(
                "Fudan-CourseLens-Worker/",
                _read(relative_path),
                f"{relative_path} 残留改仓名前的资产直链（旧名）。",
            )

    def test_security_narrative_present_in_release_materials(self) -> None:
        self.assertIn("外联只在固定清单内", _read("docs/release-announcement-draft.md"))
        self.assertIn("固定闭集清单", _read("docs/release-notes-0100-draft.md"))
        dataflow = _read("docs/architecture-dataflow.md")
        count = len(_code_outbound_hosts())
        self.assertIn(f"{count} 个的固定闭集", dataflow)


if __name__ == "__main__":
    unittest.main()
