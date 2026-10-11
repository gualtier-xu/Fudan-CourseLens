"""Read-only "where is your data" map (``courselens.data-map.v1``).

DATAMAP-P1（D10 信任产品化首选提案）：把 README「安全与隐私架构」与
docs/privacy-notice.md v1.2.1 的既有口径搬进产品内页——零新采集、零新外联、
零新依赖。载荷三块：

- ``domains``：三域（本机 / 你自己的 GitHub 仓 / 云端任务状态）逐域声明
  存哪里、谁能看到、保留多久，文案逐字对齐 privacy-notice.md v1.2.1 同节；
- ``counters``：复用 :class:`CourseDataInventory` 的只读聚合输出本机各类
  数据的 count/text_bytes（不读内容列，跨页有界聚合，超出页帽诚实降级
  ``complete=false``，绝不编造）；
- ``outbound_hosts``：外联主机具名闭集 10 主机 + 每主机一句话用途。
  静态声明与代码常量的一致性由 tests/test_data_map_api.py 逐主机对账钉死
  （SERVICE_PROBES / TICKET_REDIRECT_HOSTS / ALLOWED_TARGET_HOSTS /
  distribution allowed_hosts），本模块刻意不 import api 层与 requests。

凭据、会话、令牌与课程标识符永不进入本载荷：聚合只到类别层，不回显
任何 course_id / 课程名 / 主机以外的主机名。
"""

from __future__ import annotations

import time
from typing import Any

from src.runtime.course_data_inventory import CourseDataInventory, MAX_SUMMARY_PAGE_SIZE

DATA_MAP_SCHEMA = "courselens.data-map.v1"

# 三域闭集（前端三域图按序渲染；键集冻结，前端未知键省略不渲染）。
DATA_MAP_DOMAINS = ("local", "own_github", "cloud_state")

# 外联主机关闭分组（分组徽标闭集；未知分组前端按「其他」兜底渲染）。
DATA_MAP_HOST_GROUPS = ("campus", "github", "ai", "update")

# 动作闭集：纯聚合既有动作入口，零新动作、零新开关（自动化缺省纪律）。
DATA_MAP_ACTIONS = ("erase-hotspots", "delete-records", "revoke-cloud", "reset-client")

# 外联主机具名闭集（README「客户端会和哪些服务器通信」表的产品内同源版；
# 一句话用途口径与 privacy-notice.md v1.2.1「谁能看到你的数据」一节一致）。
OUTBOUND_HOSTS = (
    {"host": "id.fudan.edu.cn", "group": "campus", "purpose": "复旦统一身份认证登录"},
    {"host": "webvpn.fudan.edu.cn", "group": "campus", "purpose": "学校官方的校外访问通道（课程平台入口）"},
    {"host": "icourse.fudan.edu.cn", "group": "campus", "purpose": "课程目录、课表、直播与回放"},
    {"host": "fdjwgl.fudan.edu.cn", "group": "campus", "purpose": "本科课表与考试安排数据源"},
    {"host": "yjsxktest.fudan.sh.cn", "group": "campus", "purpose": "研究生课表数据源"},
    {"host": "github.com", "group": "github", "purpose": "你的专属云端仓库与学习任务"},
    {"host": "api.github.com", "group": "github", "purpose": "云端任务的派发与状态读取"},
    {"host": "api.deepseek.com", "group": "ai", "purpose": "AI 问答与校对（用你自己配置的 DeepSeek Key 直接访问）"},
    {"host": "release-assets.githubusercontent.com", "group": "update", "purpose": "应用更新包下载（逐跳白名单 + IP 钉定，仅 HTTPS）"},
    {"host": "objects.githubusercontent.com", "group": "update", "purpose": "应用更新包与清单下载（更新链专用验签白名单）"},
)

# 云端保留期（privacy-notice.md v1.2.1「云端代算」节口径；单位=天）。
CLOUD_MATERIALS_RETENTION_DAYS = 30
CLOUD_STATE_RETENTION_DAYS = 90

# 本机计数聚合上界：恰好一页冻结页帽（course-data-summary.v1 的
# MAX_SUMMARY_PAGE_SIZE）。学生课程数远低于此；超出即诚实降级 complete=false，
# 不为一张静态地图做逐页重扫（每页都是一次全库 GROUP BY）。
COUNTERS_PAGE_CAP = MAX_SUMMARY_PAGE_SIZE


def _domain(
    key: str, title: str, where: str, who: str, retention: str, items: tuple[str, ...],
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "key": key,
        "title": title,
        "where": where,
        "who": who,
        "retention": retention,
        "items": list(items),
    }
    return value


_DOMAINS = (
    _domain(
        "local",
        "你的电脑",
        "本机数据目录（本地服务只监听本机回环地址）",
        "只有你能看到",
        "一直保留，直到你在应用里删除或重置",
        (
            "课程目录、课表、观看进度、回看热点、书签、笔记、测验与复习计划，全部只写入这台电脑上的本机数据目录，不经任何第三方服务器中转",
            "回看热点只记你反复回看的位置（回拖、重放、暂停、慢速），帮你在进度条上标出难点；不想记某一讲时，在播放器操作区点「抹掉本讲热点」",
            "「下次接着播」的续播点与主题、排序等本机偏好，同样只存在这台电脑上",
            "本地学习统计默认未开启（不影响使用）；开启后在本机加记观看事件（位置、时长、倍速、完成）与引用点击、停留计数，全都只存这台电脑，随时可以在设置页「隐私」节关闭或删除",
            "学号、密码和 API Key 经 Windows 数据保护接口（DPAPI）加密后存盘：文件里只有密文，日志、界面和诊断输出都不会回显",
            "macOS 测试版：凭据经 macOS 钥匙串（Keychain）加密、只存在本机，学习数据保存在「Application Support/CourseLens」目录——同为「只存这台电脑」的本地存储不变式",
        ),
    ),
    _domain(
        "own_github",
        "你自己的 GitHub 仓库",
        "你的专属云端仓库（GitHub Actions 运行环境）",
        "只有你能看到内容：传输与仓库里只见密文",
        "课程材料与结果（密文）最长保留 30 天",
        (
            "只有生成字幕、AI 总结这类重活会使用云端代算，且只在你主动发起时发生；在线播放和课程目录始终走你的复旦登录会话，两者相互独立",
            "任务在端到端加密的「任务信封」里运送：信封只密封给你自己的专属 Worker，GitHub 与传输链路上只见密文，CourseLens 作者侧读不到内容",
            "云端代算不经过 CourseLens 作者的任何服务器；只有你勾选的课程会被处理",
            "课程材料与回传结果全程加密；到期不再可用，你也可以随时撤销授权或手动清理",
        ),
    ),
    _domain(
        "cloud_state",
        "云端任务状态记录",
        "你自己的 GitHub 仓库里的任务状态",
        "只有你能看到（只含状态与错误码，不含课程内容）",
        "任务状态记录最长保留 90 天",
        (
            "每个任务留有派发、执行、回传、清理的状态记录与闭集错误码，用于任务中心与恢复",
            "状态记录不含课程材料内容，也不含你的账号信息",
            "随时可以在连接卡的「高级操作与诊断」里撤销云端授权，并删除云端凭据与状态",
        ),
    ),
)

_ACTIONS = (
    {"key": "erase-hotspots", "title": "抹掉某一讲的回看热点", "where": "该讲次播放器操作区的「抹掉本讲热点」"},
    {"key": "delete-records", "title": "删除本机学习记录", "where": "设置的数据管理页；只删记录，已生成的字幕与课件不受影响"},
    {"key": "revoke-cloud", "title": "撤销云端授权并删除云端凭据与状态", "where": "连接卡的「高级操作与诊断」"},
    {"key": "reset-client", "title": "一键重置", "where": "设置页「重置」；重置不可恢复，动它之前请想清楚"},
)


def local_counters(inventory: CourseDataInventory) -> dict[str, Any]:
    """Aggregate one summary page into per-category local totals.

    只读、只到类别层：把各课程行的类别聚合并成全库总数，外加未归课桶
    （跨课程任务等）与数据库文件字节。页帽外行数存在时如实降级
    ``complete=false``；空库输出全零类别（不省略键，前端可稳定渲染）。
    """
    value = inventory.summary(page=1, page_size=COUNTERS_PAGE_CAP, include_orphans=True)
    totals: dict[str, dict[str, int]] = {}
    for row in value.get("rows") or []:
        for category, aggregate in (row.get("categories") or {}).items():
            bucket = totals.setdefault(str(category), {"count": 0, "text_bytes": 0})
            bucket["count"] += int((aggregate or {}).get("count") or 0)
            bucket["text_bytes"] += int((aggregate or {}).get("text_bytes") or 0)
    for category, aggregate in ((value.get("unattributed") or {}).get("categories") or {}).items():
        bucket = totals.setdefault(str(category), {"count": 0, "text_bytes": 0})
        bucket["count"] += int((aggregate or {}).get("count") or 0)
        bucket["text_bytes"] += int((aggregate or {}).get("text_bytes") or 0)
    total_rows = int((value.get("page") or {}).get("total") or 0)
    categories = [
        {
            "category": category,
            "count": totals[category]["count"],
            "text_bytes": totals[category]["text_bytes"],
        }
        for category in sorted(totals)
        if totals[category]["count"] > 0
    ]
    return {
        "complete": total_rows <= COUNTERS_PAGE_CAP,
        "database_bytes": dict(value.get("database_bytes") or {}),
        "categories": categories,
    }


def data_map_snapshot(
    *,
    learning_store: Any,
    catalog_repository: Any,
    task_store: Any,
    data_root: Any = None,
) -> dict[str, Any]:
    """Compose one ``data-map.v1`` payload (read-only, zero network)."""
    inventory = CourseDataInventory(
        learning_store=learning_store,
        catalog_repository=catalog_repository,
        task_store=task_store,
        data_root=data_root,
    )
    return {
        "schema": DATA_MAP_SCHEMA,
        "generated_at": time.time(),
        "domains": [dict(domain) for domain in _DOMAINS],
        "counters": local_counters(inventory),
        "outbound_hosts": [dict(host) for host in OUTBOUND_HOSTS],
        "retention": {
            "materials_days": CLOUD_MATERIALS_RETENTION_DAYS,
            "state_days": CLOUD_STATE_RETENTION_DAYS,
        },
        "actions": [dict(action) for action in _ACTIONS],
    }
