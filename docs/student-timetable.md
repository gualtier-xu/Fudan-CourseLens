# CourseLens 学生课表

CourseLens 的“课表”工作区面向本科生和研究生，产品目标是 Windows 桌面端；macOS 仅保留 CI 冒烟覆盖，不提供产品安装包或产品验收。

## 功能

- 本科生可读取教学管理系统提供的学期列表、学期首周和课程活动。
- 研究生可读取研究生选课系统当前返回的课程、教室、教师、周次、星期和节次。
- 支持上一周、下一周、回到本周、当前课程、下一节课、课程详情和课程冲突。
- 已与 iCourse 唯一匹配的课程可从课表跳转到课程工作区。
- 整个学期可导出为 `Asia/Shanghai` 时区的 ICS 文件，每个实际上课日期对应一个稳定事件。

CourseLens 不提供手工新建或编辑课程。研究生系统没有给出准确学期首周时，只要求用户确认一次首周周一；这项设置用于日期、当前课程和日历导出，不会修改学校课程数据。

## 数据来源与边界

课表获取只发生在私有客户端：

```text
浏览器课表工作区
  -> localhost API
  -> 已验证的 WebVPN 会话
  -> 本科或研究生教学系统
  -> identity-scoped state.db cache
```

- 本科数据源主机固定为 `fdjwgl.fudan.edu.cn`。
- 研究生数据源主机固定为 `yjsxktest.fudan.sh.cn`，请求必须通过 WebVPN HTTPS 包装。
- 密码只进入复旦统一身份认证流程，不会提交给课程表数据接口。
- 公开 Worker、个人 Worker 和 Mailbox 不获取、不保存、不传输课表。
- 缓存新鲜期为 6 小时；已验证身份最多可查看 30 天内的过期缓存，并看到明确的过期状态。
- 注销后前端内存和 API 响应立即脱敏，本地缓存仍按哈希身份范围隔离。

## 课程关联

课表课程与 iCourse 课程只有在以下条件全部成立时才会关联：

1. 标准化学期一致。
2. 标准化课程标题完全一致。
3. 教师集合至少有一人相同。
4. 最终只剩一个候选课程。

多个候选显示为歧义，无候选显示为未关联。系统不会仅凭课程标题合并，也不会覆盖 iCourse 原始课程字段。

## API

- `GET /api/v3/timetable?semester_id=&week=` 返回学期、周视图、课程、当前/下一节课、冲突和关联证据。
- `POST /api/v3/timetable/actions` 支持 `refresh` 与 `set-semester-start`，必须提供幂等 `operation_id`。
- `GET /api/v3/timetable/export.ics?semester_id=` 返回 `text/calendar`，使用 `no-store`。

未登录、会话失效、来源不可用、非法响应、部分成功、缓存过期和首周日期缺失都有独立错误码。日志不记录 Cookie、密码、上游响应正文或完整课程表。

## DanXi 参考说明

本功能参考了 DanXi 的课表能力范围和学校公开服务的响应形态，但解析器、数据模型、缓存、界面、课程关联和 ICS 生成均在 CourseLens 中独立实现，没有复制 DanXi 的 GPL-3.0 源代码。

## 四仓库同步结论

课表属于本地身份数据，不需要修改公开 Worker 协议：

- 私有仓库同步前端、localhost API、解析、缓存、测试和文档。
- 公开模板继续只承载通用受信 Worker、加密协议和合成任务。
- 个人 Worker 保持与固定公开模板 tree 一致。
- Mailbox 继续只保存密封任务和签名控制消息，不增加课表字段、Issue 或 Artifact。

### 2026-07-20 历史核对证据

以下为 2026-07-20 当天的核对快照，仅作历史证据保留；当前提交与镜像状态以 [handoff](handoff.md) 为唯一动态入口：

- 公开模板 `main`：commit `c9f4cba748869202c5225427995daa16c6d9aa7c`，tree `afd4b9763b324af065e0584bddbd63c67b27f1af`，与私有 `runtime-assets.json` 一致；公开 40 项 unittest、compileall、公共边界、Markdown 和 UTF-8 检查通过。
- 个人 Worker：commit `d0ae8f58b47617ccefb5e90862a3bb84768fdb77`，tree 与公开模板一致；用户 README 和技术 README blob 均一致，无需受管修复。
- Mailbox：仍为私有、启用 Issues、未归档、未禁用；用户 README 和技术 README 与客户端受管模板一致；课表相关 Issue、Artifact、Secret 匹配数均为 0。
- 因为没有共享协议或公开代码变化，公开模板、个人 Worker 和 Mailbox 均不产生课表提交；只有私有客户端仓库需要合并和发布。

真实学校上游验证不在合成浏览器中执行，也不索取凭据。用户之后在本地安全表单建立 WebVPN 会话后，可追加一次本科或研究生只读冒烟验证。
