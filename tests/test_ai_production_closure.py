"""N7R 生产闭环补包测试：镜像登记、Assessment IR 生产接线、删除级联。

三处缺口此前都只差「最后一根线」——模块、函数、测试全都在，缺的是生产路径上的调用：

1. `worker/courselens_worker/course_knowledge.py` 没有登记进
   `scripts/worker_mirror_allowlist.json`，而镜像里已收录的 `runner.py`/`llm.py`
   会 import 它 → 发布 Worker 后 summary/learning_pack 任务 ImportError。
2. `refresh_assessment_items` 只有测试在叫；文档导入生产路径不调用它，课程
   「练习与真题」视图读的就是这张表，因此视图是空壳。
3. 删除学习文档时不级联删题，`assessment_items` 留在 course_id 上变成点不开的
   幽灵题。

这里只钉这三条生产调用确实接上了，以及接上之后的行为（幂等、只删本文档、
普通资料不误生题）。所有夹具都是临时库与合成文档，不碰 runtime/data。
"""

from __future__ import annotations

import ast
import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from nacl.signing import SigningKey

from scripts.export_worker_mirror import export_snapshot
from shared.protocol.mirror import TRUST_SCHEMA, pretty_json, sign_document
from src.application import CourseLensApplication
from src.runtime.assessment_ir import list_assessment_items
from src.runtime.document_alignment import ensure_document_schema
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import ensure_student_feature_schema

REPO_ROOT = Path(__file__).resolve().parents[1]

# 提取层会把页内空白折叠成空格（document_alignment._normal_text），所以文本资料里
# 只有「分页符 \f」能切出多页、页首标记才能开题。夹具照这个真实形状写：两页两题。
EXAM_TEXT = (
    "一、选择题（每小题 3 分）\n1. 下列说法正确的是\nA. 甲\nB. 乙\n\f"
    "二、填空题\n2. 计算下列各式的值"
)
HOMEWORK_TEXT = "一、求下列函数的导数\n1. f(x)=x^2\n2. f(x)=sin x"

_CACHE_DIRS = {"__pycache__", ".pytest_cache"}


def _encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class WorkerMirrorRegistrationTests(unittest.TestCase):
    """缺口 1：发布镜像必须带上 course_knowledge 运行期模块。"""

    def _allowlist(self) -> set[str]:
        payload = json.loads(
            (REPO_ROOT / "scripts" / "worker_mirror_allowlist.json").read_text(encoding="utf-8")
        )
        return {entry["source"] for entry in payload["files"]}

    def test_allowlist_covers_every_worker_source_including_new_modules(self):
        """清单与工作树必须一一对应（gitignore 掉的缓存目录不算源文件）。"""
        actual: set[str] = set()
        for root in ("worker", "shared/protocol"):
            for dirpath, dirnames, filenames in os.walk(REPO_ROOT / root):
                dirnames[:] = [name for name in dirnames if name not in _CACHE_DIRS]
                for name in filenames:
                    actual.add((Path(dirpath) / name).relative_to(REPO_ROOT).as_posix())
        actual.add("shared/__init__.py")
        actual.add("shared/evidence_contract.py")
        allowlist = self._allowlist()
        self.assertEqual(sorted(actual - allowlist), [], "工作树里有未登记的源文件")
        self.assertEqual(sorted(allowlist - actual), [], "清单登记了不存在的源文件")
        self.assertIn("worker/courselens_worker/course_knowledge.py", allowlist)

    def test_allowlisted_worker_modules_are_import_resolvable(self):
        """已收录模块 import 的镜像内相对模块必须也已收录（否则镜像 ImportError）。

        这正是本缺口的事故形状：`runner.py`/`llm.py` 已收录且在函数内
        `from .course_knowledge import ...`，而 `course_knowledge.py` 没收录。
        """
        allowlist = self._allowlist()
        resolved: set[str] = set()
        for source in sorted(allowlist):
            if not source.startswith("worker/courselens_worker/") or not source.endswith(".py"):
                continue
            tree = ast.parse((REPO_ROOT / source).read_text(encoding="utf-8"), filename=source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                    continue
                target = f"worker/courselens_worker/{node.module.split('.')[0]}.py"
                resolved.add(target)
                self.assertIn(target, allowlist, f"{source} 相对 import 未登记的 {target}")
        # 非空断言：真被依赖的那条边必须在集合里，否则本测试可能空转通过
        self.assertIn("worker/courselens_worker/course_knowledge.py", resolved,
                      "runner/llm 对 course_knowledge 的相对 import 必须被扫到")

    def _clean_repo_copy(self, destination: Path) -> Path:
        """纯净副本：真实导出跑在干净检出上，缓存目录会触发 Unknown source。"""
        for root in ("worker", "shared/protocol"):
            source_root = REPO_ROOT / root
            target_root = destination / root
            shutil.copytree(
                source_root,
                target_root,
                ignore=shutil.ignore_patterns(*_CACHE_DIRS, "*.pyc"),
            )
        (destination / "shared" / "__init__.py").write_bytes(
            (REPO_ROOT / "shared" / "__init__.py").read_bytes()
        )
        (destination / "shared" / "evidence_contract.py").write_bytes(
            (REPO_ROOT / "shared" / "evidence_contract.py").read_bytes()
        )
        return destination

    def _release_root(self, directory: Path):
        """一次性根/发布密钥对：只写临时目录，不碰仓库信任三件。"""
        root_key = SigningKey.generate()
        release_key = SigningKey.generate()
        root_public = base64.b64encode(bytes(root_key.verify_key)).decode("ascii")
        release_public = base64.b64encode(bytes(release_key.verify_key)).decode("ascii")
        release_private = base64.b64encode(bytes(release_key)).decode("ascii")
        root_id, release_id = "root-n7r", "release-n7r"
        root = {"schema": "courselens.worker-mirror.root.v1",
                "root_keys": [{"key_id": root_id, "public_key": root_public}]}
        trust = {
            "schema": TRUST_SCHEMA,
            "epoch": 1,
            "expires_at": int(time.time()) + 3600,
            "release_keys": [{"key_id": release_id, "public_key": release_public, "status": "active"}],
            "revoked_manifests": [],
        }
        root_private = base64.b64encode(bytes(root_key)).decode("ascii")
        signature = sign_document(trust, key_id=root_id, private_key=root_private)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "worker-mirror-root.json").write_bytes(pretty_json(root))
        (directory / "worker-mirror-trust.json").write_bytes(pretty_json(trust))
        (directory / "worker-mirror-trust.sig").write_bytes(pretty_json(signature))
        return directory, release_id, release_private

    def test_release_export_carries_course_knowledge_module(self):
        """真跑一次发布导出（本地临时目录 + 一次性密钥，无远端写入）。"""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repo = self._clean_repo_copy(workspace / "repo")
            release_root, release_id, release_private = self._release_root(workspace / "release")
            result = export_snapshot(
                repo,
                workspace / "output",
                signing_private_key=release_private,
                signing_key_id=release_id,
                source_commit="1" * 40,
                worker_tree="2" * 40,
                allowlist_path=REPO_ROOT / "scripts" / "worker_mirror_allowlist.json",
                release_root=release_root,
            )
            exported = {item["path"] for item in result["manifest"]["payload"]["files"]}
            self.assertIn("courselens_worker/course_knowledge.py", exported)
            self.assertIn("courselens_worker/runner.py", exported)
            self.assertIn("courselens_worker/llm.py", exported)
            self.assertIn("tests/test_course_knowledge.py", exported)
            # 冻结合同只留在本地：镜像侧刻意不收录它（course_knowledge 自己镜像闭集）
            self.assertNotIn("shared/course_knowledge_contract.py", exported)


class AssessmentImportProductionTests(unittest.TestCase):
    """缺口 2+3：导入投影题目、重复导入幂等、删除级联、普通资料不生题。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "learning.db"
        ensure_document_schema(self.db_path)
        ensure_student_feature_schema(self.db_path)
        self.service = CourseLensApplication.__new__(CourseLensApplication)
        self.service.learning_store = LearningStore(self.db_path)
        self.service.search_index = SimpleNamespace(request_refresh=lambda *_a, **_k: None)

        def unavailable(_sub_id):
            raise FileNotFoundError("subtitle unavailable")

        self.service.subtitle_segments = unavailable

    def _import(self, text: str, *, doc_type: str, name: str = "paper.txt",
                course_id: str = "course-1", sub_id: str = "lecture-1",
                scope: str = "lecture") -> dict:
        return self.service.import_learning_document(
            course_id=course_id,
            sub_id=sub_id,
            title="合成考核文档",
            original_name=name,
            media_type="text/plain",
            content_base64=_encode(text),
            doc_type=doc_type,
            scope=scope,
        )

    def _items(self, document_id: str) -> list[dict]:
        return list_assessment_items(self.db_path, document_id=str(document_id))

    def test_exam_paper_import_projects_items(self):
        document = self._import(EXAM_TEXT, doc_type="exam_paper")
        items = self._items(document["document_id"])
        self.assertEqual(len(items), 2, "真题导入后「练习与真题」视图必须真的有题")
        self.assertEqual([int(item["question_no"]) for item in items], [1, 2])
        self.assertEqual({str(item["kind"]) for item in items}, {"exam_paper"})
        self.assertEqual({str(item["document_id"]) for item in items}, {document["document_id"]})

    def test_homework_import_projects_items(self):
        document = self._import(HOMEWORK_TEXT, doc_type="homework")
        items = self._items(document["document_id"])
        self.assertEqual(len(items), 1, "作业与真题同列为考核文档，同样要投影")
        self.assertEqual({str(item["kind"]) for item in items}, {"homework"})

    def test_reimport_is_idempotent(self):
        first = self._import(EXAM_TEXT, doc_type="exam_paper")
        before = self._items(first["document_id"])
        second = self._import(EXAM_TEXT, doc_type="exam_paper")
        self.assertEqual(second["document_id"], first["document_id"], "同内容同讲次应复用文档")
        after = self._items(first["document_id"])
        self.assertEqual(len(after), len(before), "重复导入不得翻倍造题")
        self.assertEqual(sorted(item["item_id"] for item in after),
                         sorted(item["item_id"] for item in before), "题身份不得漂移")

    def test_ordinary_material_projects_no_items(self):
        """正文带题号标记，但 doc_type 不是考核文档 → 不凭空生题。"""
        for doc_type, name in (("other", "paper.txt"), ("notes", "paper.txt"),
                               ("courseware", "paper.txt"), ("textbook", "paper.txt")):
            with self.subTest(doc_type=doc_type):
                document = self._import(EXAM_TEXT, doc_type=doc_type, name=name)
                self.assertEqual(self._items(document["document_id"]), [],
                                 f"{doc_type} 不得产出考核题目")
        # 课程级资料同样不生题
        course_level = self._import(EXAM_TEXT, doc_type="exam_paper", name="paper.txt",
                                    sub_id="", scope="course")
        self.assertEqual(self._items(course_level["document_id"]), [],
                         "课程级资料不生题")

    def test_imported_page_text_is_whitespace_collapsed_upstream(self):
        """钉住上游现状：提取层折叠空白，页内换行不存在，拆分只能按页开题。

        `document_alignment._normal_text` 是 `re.sub(r"\\s+", " ", ...)`，PDF/DOCX/
        PPTX/文本四条提取路径全走它（document_alignment.py:231/197/172/292），入库
        时又折叠一次（同文件 :393）。`split_questions` 只认行首标记（exam_paper_split
        .py:156 `text.splitlines()`），于是没有分页符的文档整份塌成一题、第二题被
        吞进第一题题干。修它要动这两个文件（都不在本包可写路径），故此处只如实
        记录上限——上游修好后本测试会失败，正好提醒更新断言。
        """
        document = self._import(EXAM_TEXT.replace("\f", ""), doc_type="exam_paper")
        items = self._items(document["document_id"])
        self.assertEqual(len(items), 1, "无分页符时整份文档塌成一题")
        self.assertIn("二、填空题", str(items[0]["stem"]), "第二题被吞进第一题题干")

    def test_delete_document_cascades_items(self):
        document = self._import(EXAM_TEXT, doc_type="exam_paper")
        document_id = str(document["document_id"])
        self.assertEqual(len(self._items(document_id)), 2)
        self.assertTrue(self.service.delete_learning_document(document_id))
        self.assertEqual(self._items(document_id), [], "删除文档后不得留幽灵题")
        self.assertEqual(list_assessment_items(self.db_path, course_id="course-1"), [],
                         "课程「练习与真题」视图不得再列出已删文档的题")

    def test_delete_only_cascades_its_own_document(self):
        kept = self._import(EXAM_TEXT, doc_type="exam_paper", name="keep.txt")
        dropped = self._import(EXAM_TEXT + "\n三、简答题\n3. 论述", doc_type="exam_paper",
                              name="drop.txt")
        kept_items = self._items(kept["document_id"])
        self.assertTrue(self._items(dropped["document_id"]))
        self.service.delete_learning_document(str(dropped["document_id"]))
        self.assertEqual(self._items(dropped["document_id"]), [])
        self.assertEqual(sorted(item["item_id"] for item in self._items(kept["document_id"])),
                         sorted(item["item_id"] for item in kept_items),
                         "级联只针对明确 document_id，不得连坐同类文档")


class CourseDataRemovalCascadeTests(unittest.TestCase):
    """清空课程数据是同一边界的第二处删档口子，同样要级联。"""

    def test_course_data_removal_cascades_items(self):
        from src.application import _course_data_remove_document_copy

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "learning.db"
            ensure_document_schema(db_path)
            ensure_student_feature_schema(db_path)
            service = CourseLensApplication.__new__(CourseLensApplication)
            service.learning_store = LearningStore(db_path)
            service.search_index = SimpleNamespace(request_refresh=lambda *_a, **_k: None)
            service.subtitle_segments = lambda _sub_id: (_ for _ in ()).throw(
                FileNotFoundError("subtitle unavailable")
            )
            document = service.import_learning_document(
                course_id="course-1", sub_id="lecture-1", title="合成真题",
                original_name="paper.txt", media_type="text/plain",
                content_base64=_encode(EXAM_TEXT), doc_type="exam_paper",
            )
            document_id = str(document["document_id"])
            self.assertTrue(list_assessment_items(db_path, document_id=document_id))
            _course_data_remove_document_copy(service.learning_store, document_id)
            self.assertEqual(list_assessment_items(db_path, document_id=document_id), [])


if __name__ == "__main__":
    unittest.main()
