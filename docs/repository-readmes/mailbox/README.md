<!-- courselens-managed-readme:v5:user -->

# Fudan CourseLens Mailbox

> CourseLens 客户端为你自动创建的私有机器信箱。它只传递密文任务和受管状态，不需要在 GitHub 网页中操作。
>
> Private machine mailbox managed by the CourseLens client. Do not edit or use it as general-purpose storage.

The private monorepo migration does not merge this Mailbox with the personal
Worker. It remains a separate private encrypted transport managed by the client.

## 为什么会有这个仓库

CourseLens 的本地客户端与个人 GitHub Actions Worker 不建立永久连接。客户端把短期任务密封后写入这个私有仓库，Worker 只读取密文，并把签名进度和加密结果返回给客户端。

这个仓库必须保持私有。它不是课程目录、讨论区、网盘、备份仓库或普通任务队列。

## 请不要手工操作

不要在 GitHub 网页中：

- 新建、编辑或删除受管 Issue 和评论；
- 上传课程文件、字幕、截图、音视频或其他资料；
- 删除正在使用的 Artifact；
- 放入课程地址、Cookie、学号、密码、GitHub token 或 API Key；
- 把本仓库用于与 CourseLens 无关的自动化。

手工操作可能使任务无法恢复或使客户端进入 `cleanup_pending`。需要取消、暂停、继续、重试、导入、断开或清理时，请回到 CourseLens 客户端的“任务”或“设置”工作区。

## 客户端显示异常时

| 状态 | 含义与操作 |
| --- | --- |
| 数据已过期 | 客户端暂时拿不到新证据；先诊断 GitHub 连接，不要重复创建任务 |
| `cleanup_pending` | 远端删除尚未确认；在客户端重试清理，不能把它当作已经删除 |
| 结果不可恢复 | 本地结果私钥缺失或数据校验失败；客户端不会重复派发同一任务或伪装完成 |
| 授权需要修复 | 回到客户端重新授权或修复 Worker，不要手工改本仓库权限 |

协议、保留期、幂等和故障恢复说明见 [技术 README](docs/technical/README.md)。

本 README 带有受管版本标记，后续由 CourseLens 客户端安全、幂等地更新。
