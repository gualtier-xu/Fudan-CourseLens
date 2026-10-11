from __future__ import annotations

import ast
import re
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# http_api 的 service.<attr> 访问名 → domains.py 门面类名（N15-R4 审计映射）。
ATTR_TO_CLASS = {
    "auth_catalog": "AuthCatalogService",
    "automation": "AutomationService",
    "client_update": "ClientUpdateService",
    "learning": "LearningService",
    "lifecycle": "LifecycleService",
    "live_room": "LiveRoomDomainService",
    "media_session": "MediaSessionService",
    "remote_compute": "RemoteComputeService",
    "settings": "SettingsService",
    "tasks": "TaskService",
    "timetable": "TimetableService",
}


def _facade_fields() -> dict[str, set[str]]:
    tree = ast.parse((ROOT / "src" / "services" / "domains.py").read_text(encoding="utf-8"))
    fields: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            members = {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
            members |= {
                stmt.name for stmt in node.body if isinstance(stmt, ast.FunctionDef)
            }
            fields[node.name] = members
    return fields


def _facade_operation_fields_in_order() -> dict[str, list[str]]:
    """门面 Operation 字段（保序，不含 Any 装配字段）。A 向死操作门与位置
    注入对齐哨兵共用。"""
    tree = ast.parse((ROOT / "src" / "services" / "domains.py").read_text(encoding="utf-8"))
    fields: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Service"):
            continue
        names: list[str] = []
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and isinstance(stmt.annotation, ast.Name)
                and stmt.annotation.id == "Operation"
            ):
                names.append(stmt.target.id)
        fields[node.name] = names
    return fields


def _container_field_map() -> dict[str, str]:
    """container.py 聚合门面：容器字段名 -> 门面类名。"""
    tree = ast.parse((ROOT / "src" / "services" / "container.py").read_text(encoding="utf-8"))
    mapping: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CourseLensServices":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and isinstance(stmt.annotation, ast.Name)
                    and stmt.annotation.id.endswith("Service")
                ):
                    mapping[stmt.target.id] = stmt.annotation.id
    return mapping


def _container_toplevel_operations() -> set[str]:
    """container 顶层读数/动作 Operation 字段（U⑩ 契约：路由按顶层读取）。"""
    tree = ast.parse((ROOT / "src" / "services" / "container.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CourseLensServices":
            names |= {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and isinstance(stmt.annotation, ast.Name)
                and stmt.annotation.id == "Operation"
            }
    return names


def _app_constructor_args(class_name: str, container_field: str) -> list[str]:
    """app.py 里 `<container_field>=<class_name>(...)` 的实参表达式列表
    （顶层逗号切分，括号深度感知）。"""
    app = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
    match = re.search(rf"\b{container_field}={class_name}\(", app)
    if not match:
        return []
    start = match.end() - 1
    depth, i = 0, start
    while i < len(app):
        if app[i] == "(":
            depth += 1
        elif app[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    inner = app[start + 1:i]
    parts: list[str] = []
    buf: list[str] = []
    nested = 0
    for ch in inner:
        if ch in "([{":
            nested += 1
        elif ch in ")]}":
            nested -= 1
        if ch == "," and nested == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


class ServiceFacadeWiringTests(unittest.TestCase):
    def test_every_http_api_service_call_exists_on_its_facade(self):
        """接线哨兵（N15-R4 审计直升格；F3/F4/F5 真机三 500 的防复发门）：
        http_api 全部 service.<attr>.<method>( 调用必须落在 domains.py 门面
        字段集内。第 9 处断线从此在 CI 即红，不再等真机 500（8 处断线曾静默
        存活 2 天～3 周：测试侧适配器是宽松 SimpleNamespace，缝合测试全绿而
        生产冻结门面断裂）。"""
        api = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        facade = _facade_fields()
        calls = Counter(
            re.findall(r"service\.([a-z_]+)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", api)
        )
        breaks = [
            (attr, method, ATTR_TO_CLASS[attr], count)
            for (attr, method), count in sorted(calls.items())
            if attr in ATTR_TO_CLASS and method not in facade.get(ATTR_TO_CLASS[attr], set())
        ]
        self.assertEqual(
            breaks,
            [],
            "http_api 调用了门面上不存在的成员（接线断线）: "
            + ", ".join(f"service.{a}.{m} -> {cls}" for a, m, cls, _ in breaks),
        )
        # 普查锚：被审计的调用面不缩水（三域曾断线的主力面在位）。
        self.assertGreaterEqual(calls.total(), 100, "http_api service.* 调用面骤减")

    def test_app_wiring_names_match_facade_operation_fields_in_order(self):
        """位置接线哨兵（N15-W1 自审产出，限 Learning/AuthCatalog 两族）：
        app.py 对这两族按**位置**传参且字段名=实参名，错位会静默绑错方法
        （成员哨兵测不到——arity 恒等）。其余门面（remote/automation/
        timetable/settings 等）是有档案异名映射的历史设计，不在本钉范围；
        管道头三个既有改名（repository/catalog/task_repository）按档案豁免。"""
        tree = ast.parse((ROOT / "src" / "services" / "domains.py").read_text(encoding="utf-8"))
        app = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        documented_renames = {"repository", "catalog", "task_repository"}
        name_matched_facades = {"LearningService", "AuthCatalogService"}
        problems = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in name_matched_facades:
                continue
            fields = [
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ]
            start = app.index(f"{node.name}(")
            depth, i = 0, start + len(node.name)
            while True:
                if app[i] == "(":
                    depth += 1
                elif app[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            args = re.findall(
                r"application\.([a-zA-Z_][a-zA-Z0-9_]*)", app[start + len(node.name) + 1:i]
            )
            if len(args) != len(fields):
                problems.append(f"{node.name}: {len(fields)} 字段 vs {len(args)} 实参")
                continue
            for field, arg in zip(fields, args):
                if field == arg or field in documented_renames:
                    continue
                problems.append(f"{node.name}: 字段 {field} 位上实参是 application.{arg}")
        self.assertEqual(problems, [], "门面接线错位: " + "; ".join(problems))

    def test_every_facade_operation_is_dispatched_or_archived_internal(self):
        """A 向死操作门（SWEEPFIX-N20 · WIRING-FIX-1 F2-P1 备案收口）：
        门面声明的每个 Operation 必须被 http_api 派发（service.<字段>.<操作>(），
        或落在下方**具名内部消费档案**——F2-P1 的本形（应用层方法在、门面字段在、
        http_api 分支缺失=学生面功能不可达而全绿）从此在 CI 即红。原 F2-P1
        （set-media-webvpn-relay 分支缺失）已由「自动化缺省」裁决整开关移除
        （workbench assertNotIn 钉在位），本门防的是同形残留再发。"""
        api = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        app = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        # 内部消费档案（具名+理由，禁无名扩容）：
        # - LifecycleService 全族：装配期/生命周期消费（app 装配、UpdateService、
        #   壳层关闭路径），本就不走 HTTP 路由；
        # - LiveRoomDomainService.revoke_all：app.py 关闭路径 services.live_room.revoke_all()。
        internal_consumed = {"LifecycleService"}
        internal_consumed_ops = {("LiveRoomDomainService", "revoke_all")}
        container_map = _container_field_map()
        operations = _facade_operation_fields_in_order()
        dispatched = set(re.findall(r"service\.([a-z_]+)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", api))
        toplevel_dispatched = set(re.findall(r"\bservice\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", api))
        toplevel_ops = _container_toplevel_operations()
        dead: list[str] = []
        for container_field, class_name in sorted(container_map.items(), key=lambda kv: kv[1]):
            for op in operations.get(class_name, []):
                if (container_field, op) in dispatched:
                    continue
                if op in toplevel_ops and op in toplevel_dispatched:
                    continue
                if class_name in internal_consumed:
                    continue
                if (class_name, op) in internal_consumed_ops and f"{container_field}.{op}()" in app:
                    continue
                dead.append(f"{class_name}.{op}（container.{container_field}）")
        self.assertEqual(
            dead, [],
            "门面 Operation 声明了但没有任何 http_api 派发、也不在内部消费档案里"
            "（F2-P1 同形残留：学生面功能不可达）: " + ", ".join(dead),
        )
        # 普查锚：门面操作面不缩水。
        self.assertGreaterEqual(
            sum(len(ops) for cls, ops in operations.items() if cls != "LifecycleService"),
            60,
            "门面 Operation 总面骤减",
        )

    def test_app_positional_injection_matches_facade_rename_archive(self):
        """container 注入防护（SWEEPFIX-N20 · WIRING-FIX-1 F2-P1 备案收口）：
        app.py 对全部 11 族门面按**位置**传参，字段名≠实参名的既有档案改名
        之外出现任何偏差即红——位置注入错位 arity 恒等、成员哨兵测不到，却会
        静默绑错实现（A 服务调到 B 方法）。档案=开工审计逐族实读（零漂移），
        新增改名必须同步本档案，禁无名扩容。"""
        # 档案值=完整实参表达式（缺省字段按 root.<字段名> 校验）。
        rename_archive: dict[str, dict[str, str]] = {
            "LifecycleService": {
                "start_remote_connection": "application.remote_connection.start",
                "recover_remote_runs": "application.recover_remote_runs_on_startup",
                "start_daily_schedule": "application.start_daily_schedule_if_due",
                "start_auto_connect": "application.start_auto_connect_resume",
                # 生命周期控制器三读数/一动作绑 controller 实例。
                "snapshot": "controller.snapshot",
                "accepting_requests": "controller.accepting_requests",
                "request_shutdown": "controller.request_shutdown",
            },
            "AuthCatalogService": {
                "catalog": "application.catalog_repository",
                "task_repository": "application.task_store",
            },
            "MediaSessionService": {"catalog": "application.catalog_repository"},
            "LiveRoomDomainService": {},
            "LearningService": {"repository": "application.learning_store"},
            "TaskService": {"repository": "application.task_store"},
            "RemoteComputeService": {
                "coordinator": "application.remote_coordinator",
                "lease_repository": "application.task_store",
                "connection_action": "application.remote_connection_action",
                "connection_snapshot": "application.remote_connection_snapshot",
                "runs_snapshot": "application.remote_runs_snapshot",
            },
            "AutomationService": {
                "repository": "application.task_store",
                "integration": "application.automation",
                "action": "application.automation_action",
                "snapshot": "application.automation_snapshot",
                "update_config": "application.update_automation_config",
                "upload_secrets": "application.upload_automation_secrets",
            },
            "TimetableService": {
                "runtime": "application.timetable",
                "action": "application.timetable_action",
                "ics": "application.timetable_ics",
                "snapshot": "application.timetable_snapshot",
            },
            "SettingsService": {
                "privacy_snapshot": "application.settings_privacy_snapshot",
                "migration_export_action": "application.data_migration_export_action",
                "migration_import_action": "application.data_migration_import_action",
                "migration_stage_upload": "application.data_migration_stage_upload",
                "migration_download_file": "application.data_migration_download_file",
            },
            "ClientUpdateService": {},
        }
        # 非应用层装配根档案：这两族绑定的不是 application 对象（UpdateService
        # 实例 / live_room 引擎），实参根不同但字段名一一对应。
        root_archive = {
            "LiveRoomDomainService": "live_room",
            "ClientUpdateService": "client_update",
        }
        container_map = _container_field_map()
        operations = _facade_operation_fields_in_order()
        problems: list[str] = []
        for container_field, class_name in sorted(container_map.items()):
            tree = ast.parse(
                (ROOT / "src" / "services" / "domains.py").read_text(encoding="utf-8")
            )
            any_fields: list[str] = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    any_fields = [
                        stmt.target.id
                        for stmt in node.body
                        if isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and isinstance(stmt.annotation, ast.Name)
                        and stmt.annotation.id == "Any"
                    ]
            expected = any_fields + operations.get(class_name, [])
            args = _app_constructor_args(class_name, container_field)
            if not args:
                problems.append(f"{class_name}: app.py 未找到 {container_field}={class_name}(...) 装配")
                continue
            if len(args) != len(expected):
                problems.append(f"{class_name}: {len(expected)} 字段 vs {len(args)} 实参")
                continue
            archive = rename_archive.get(class_name, {})
            root = root_archive.get(class_name, "application")
            for field, arg in zip(expected, args):
                want = archive.get(field, f"{root}.{field}")
                if arg != want:
                    problems.append(f"{class_name}: 字段 {field} 位上实参是 {arg}（档案期望 {want}）")
        # U⑩ 顶层读数/动作：kwarg 注入必须同名直挂 application 同名方法。
        app = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        for op in sorted(_container_toplevel_operations()):
            if not re.search(rf"\b{op}=application\.{op}\b", app):
                problems.append(f"container 顶层 {op} 未以 kwarg 同名挂 application.{op}")
        self.assertEqual(
            problems, [],
            "门面位置注入偏离改名档案: " + "; ".join(problems),
        )

    def test_http_services_adapter_covers_every_settings_operation(self):
        """壳适配层白名单对账钉（D-20261009-06）：tests/http_services.py 的
        settings 命名空间必须覆盖 domains.SettingsService 的全部 Operation
        字段（提供该操作的源前派下非 None）——缺透传=合成壳对应路由恒 500
        假阴性挡测量（N9-F/本案同族两例），从此 CI 即红。"""
        import dataclasses
        import importlib

        from src.services.domains import SettingsService
        http_services = importlib.import_module("tests.http_services").http_services

        class _AllSettingsSource:
            pass

        source = _AllSettingsSource()
        # domains.py 启用 future annotations：注解以字符串形态存储。
        operation_fields = [
            field.name
            for field in dataclasses.fields(SettingsService)
            if str(field.type) == "Operation"
        ]
        for name in operation_fields:
            setattr(source, name, lambda *args, **kwargs: None)
        # 适配层档案改名（同 app.py 位置注入档案）：privacy_snapshot 读
        # source.settings_privacy_snapshot。
        setattr(source, "settings_privacy_snapshot", lambda *args, **kwargs: None)
        adapted = http_services(source)
        # U⑩ 两读数在生产容器 settings+顶层双挂，路由按顶层读取——适配层
        # settings 位缺失不算缺口，顶层位在位单独核对。
        toplevel_readouts = {"max_deepseek_tokens_limit", "ai_usage_month"}
        missing = [
            name for name in operation_fields
            if name not in toplevel_readouts
            and getattr(adapted.settings, name, None) is None
        ]
        self.assertEqual(
            missing, [],
            "http_services settings 适配缺透传（合成壳 500 假阴性家族）: " + ", ".join(missing),
        )
        for name in sorted(toplevel_readouts):
            self.assertIsNotNone(
                getattr(adapted, name, None),
                f"container 顶层 {name} 适配缺失",
            )
        # 本案两枚具名缺口显式点名（防回归锚）。
        for name in ("set_update_background_checks", "set_media_stream_proxy"):
            self.assertIsNotNone(
                getattr(adapted.settings, name, None),
                f"settings.{name} 透传缺失（D-20261009-06）",
            )

    def test_search_answer_and_assessment_chains_fully_wired(self):
        """search-answer / 考核判出两链三向钉（SWEEPFIX-N20 · WIRING-FIX-1
        F2-P1 备案点名的两条链）：深度问答与考核 AI 解答的每个门面操作必须
        同时满足 ①domains.py 声明 ②app.py 按位注入 ③http_api 派发——
        任一环缺失即红（WIRING-FIX-1 之前这两链正是分环断线重灾区）。"""
        api = (ROOT / "src" / "runtime" / "http_api.py").read_text(encoding="utf-8")
        operations = _facade_operation_fields_in_order()
        declared = set(operations.get("LearningService", []))
        args = _app_constructor_args("LearningService", "learning")
        injected = {re.fullmatch(r"application\.(\w+)", arg).group(1) for arg in args}
        dispatched = {
            method for attr, method in re.findall(
                r"service\.([a-z_]+)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", api
            ) if attr == "learning"
        }
        chains = {
            "search_learning（检索问答 GET）",
            "answer_from_evidence（深度问答 POST search/answer）",
            "refresh_search_index（检索索引重建 POST search-index/actions）",
            "explain_assessment_item（考核 AI 解答 course-review/actions）",
            "explain_question_bookmark（疑问解释链共用预算闸）",
        }
        broken = [
            name for name in chains
            for op in [re.search(r"(\w+)（", name).group(1)]
            if not (op in declared and op in injected and op in dispatched)
        ]
        self.assertEqual(
            broken, [],
            "search-answer/考核判出链三向接线断环（声明/注入/派发）: " + ", ".join(broken),
        )


if __name__ == "__main__":
    unittest.main()
