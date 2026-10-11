# 在线长任务运行时验收

本流程验证正式“本地客户端 + GitHub Actions Worker”链路。旧的本地 FFmpeg/ASR 长视频脚本只用于回滚诊断，不构成学生版发布证据。

## 前置条件

1. 使用已获准媒体；凭据只通过本地安全表单输入。
2. `/api/v3/remote-connection` 显示当前 Worker tree trusted、channel test valid、没有 cleanup pending。
3. 项目正式数据目录、日志和验收产物不得包含原视频、音频、课程正文、Cookie 或签名 URL。
4. 登录/秘密输入期间关闭截图、trace 和录像。
5. 在 `runtime/reports/` 只保存脱敏指标、闭集错误码、run ID、commit/tree 和资源数据。

## 五分钟复验

依次执行 automatic 字幕（AI 校对与无 Key 回退两条形态）、OCR、摘要、章节、搜索和引用跳转，并验证：

- dispatch 前新鲜预检；
- runner queued/in_progress 与签名阶段；
- completed/total、ETA、过期和 SSE 恢复；
- Artifact 验签、解密、哈希和事务导入；
- 重复 Artifact 幂等；
- Issue、Artifact 和短期 token 清理。

## 故障注入

1. runner 排队时取消；
2. ASR、OCR、校对和摘要阶段暂停/继续；
3. 播放/任务授权 URL 过期；
4. GitHub 代理断开、429/Retry-After 和短暂 5xx；
5. 本地后端退出后重启接管；
6. Artifact 下载后、数据库提交前崩溃；
7. 取消与成功完成竞态；
8. cleanup 失败后重试；
9. Worker tree 漂移和 Environment 缺项。

任何按钮操作只能显示“正在确认”；收到 GitHub 或签名 Worker 证据后才显示最终状态。

## 完整讲次

五分钟全部通过后运行一次获准完整 standard 学习包，记录：

- workflow 排队、依赖和模型缓存；
- 解码、SenseVoice、Paraformer、DeepSeek、OCR、摘要和章节耗时；
- CPU、内存、磁盘峰值；
- completed/total 与 ETA 误差；
- 检查点数量、恢复重算范围；
- Artifact 大小、导入和清理时间。

## 通过标准

- runner 峰值内存与磁盘均低于 12 GB；
- 五分钟满足既定 automatic 字幕（校对/回退）时限；
- 完整讲次 ≤330 分钟，或可从加密检查点自动续跑；
- 标准字幕字符差异、时间偏差达到质量门槛；
- 正式本地目录原媒体和临时音频为 0；
- 本地无 FFmpeg、ASR、OCR 进程；
- 日志、Issue、Artifact 元数据和诊断导出无秘密、课程正文或媒体 URL；
- 取消、恢复、重启、重复导入和清理均有真实终态证据。

失败时保留回滚实现和脱敏检查点记录，不创建清理标签，不删除旧媒体、模型或环境。
