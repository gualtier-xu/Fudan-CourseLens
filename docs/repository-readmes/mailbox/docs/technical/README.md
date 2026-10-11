<!-- courselens-managed-readme:v5:technical -->

# CourseLens Mailbox 技术说明

> 本文用于解释受管 Mailbox 的协议和恢复边界。实际操作仍应由 CourseLens 客户端完成。
>
> Technical reference for the managed private mailbox. Operational changes must be performed through the CourseLens client.

The generated public Worker migration changes source ownership only. Mailbox
privacy, sealed Issue/comment transport, cleanup and token-lease duties are
unchanged and remain separate from the personal Worker repository.

## 角色与权限

Mailbox 是客户端主动任务的短期密文传输层：

```text
私有客户端 → 密封 Issue/评论 → 私有 Mailbox → 个人 Worker
私有客户端 ← 签名控制消息 ← 私有 Mailbox ← 个人 Worker
私有客户端 ← 加密 Artifact ← 个人 Worker
```

仓库必须属于当前 GitHub App 授权用户、保持私有、启用 Issues，并使用受管描述。它不保存云端无人值守的账号或规则；无人值守凭据位于个人 Worker Environment Secrets，增量状态和结果使用加密 Artifact。

## 主动任务生命周期

1. 客户端生成 task ID、input hash、结果一次性密钥和短期 token lease。
2. `job.v2` 任务使用 Worker X25519 公钥密封，按有界大小写入专用 Issue 评论。
3. Worker 读取并校验密文，使用 `control.v2` 更新阶段、真实处理量、检查点和闭集错误码。
4. 控制消息由 Worker Ed25519 密钥签名，必须匹配 task ID、input hash 和递增 sequence。
5. `result.v2` 使用客户端结果公钥加密并签名，写入随机命名 Artifact。
6. 客户端验签、解密、校验 schema 与哈希，再事务导入本地数据库。
7. 导入成功后，客户端删除 Artifact 和受管评论、关闭 Issue，并在最后一个 lease 结束后删除短期任务 token。

前端只有取得对应后端证据后才显示“已取消”“已导入”或“云端数据已清理”。

## 幂等与重放防护

- 操作使用稳定 `operation_id`，重复请求返回同一操作结果。
- 任务、attempt、input hash 和结果哈希共同约束重复 Artifact 导入。
- sequence 不递增、签名错误、任务不匹配、过期或乱序控制消息会被拒绝。
- 数据库提交前崩溃时，客户端重启后对账远端 run 和导入记录。
- 已成功导入的结果不会因重复 Artifact 再次写入学习数据库。

## 失败和恢复

- **网络或授权中断**：保留最后证据并标记过期；重新连接后继续对账，不乐观推断成功。
- **取消竞态**：点击取消只表示请求已发送，等待 GitHub conclusion 或签名确认。
- **结果私钥缺失**：任务标记为 `unrecoverable`，停止导入，不重新派发同一任务。
- **Artifact 损坏**：验签、解密或哈希失败即拒绝导入，并保留闭集诊断。
- **清理失败**：进入 `cleanup_pending`，保留恢复授权并允许重试。
- **重复或过期内容**：按任务事实忽略或清理，不修改已导入结果。

## 数据保留与诊断边界

主动任务 Artifact 使用短保留期；云端每日结果和加密状态使用各自独立保留策略。客户端导入成功后会主动请求删除，不依赖保留期自然过期作为“已清理”证据。

Issue、评论、Artifact 元数据、日志和诊断只允许随机任务 ID、阶段、计数、耗时、资源和闭集错误码。不得包含课程正文、永久媒体地址、Cookie、账号、密码或 API Key。

## 受管文档

客户端从自身发布包读取两个带版本标记的模板，并通过 GitHub Contents API 幂等同步：

- `README.md`
- `docs/technical/README.md`

部分同步失败时客户端返回明确失败状态；不能因为其中一份文件成功就宣称全部文档已经同步。
