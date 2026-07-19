# Fudan CourseLens CPU Worker

公开的、通用 CPU GitHub Actions Worker。它只处理由用户私有客户端创建的、短时有效且加密的任务，输出字幕、OCR、摘要和章节等派生学习资料。

## 仓库边界

- 不包含复旦登录、WebVPN、课程发现、课程枚举、URL 签名或学生账号处理。
- 不提供原视频下载、断点续传、批量抓取、归档或公开媒体 API。
- 媒体只以 HTTPS 流进入有界解码管道；源容器、PCM、Cookie、URL、字幕正文和 API Key 不写入磁盘、日志或 Artifact。
- 只保留加密派生结果，客户端成功导入后立即删除 Artifact 和信箱密文。
- Pull Request CI 只使用合成数据，不取得生产 Environment secret。

## 处理模式

- `fast`：SenseVoice INT8，四线程。
- `no-proofread`：FireRedASR2 CTC INT8，四线程。
- `standard`：SenseVoice → FireRedASR2 CTC → 用户授权的 DeepSeek 校对，严格串行。
- `summary`：可选 RapidOCR、摘要和章节生成。

协议为 `job.v2` / `control.v2` / `result.v2`，输入使用 X25519 加密，结果使用一次性公钥加密并由 Ed25519 签名。模型权重从上游下载并按版本校验，不提交到 Git。

## 本地验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p "test_*.py"
python scripts/check_public_boundary.py
```

## 学生 Worker

私有客户端从本仓库的可信模板创建每名学生自己的 `Fudan-CourseLens-Worker`。个人 Worker 的文件由客户端按固定 commit/tree 自动修复；不要手工修改 workflow、协议或密钥。

个人 Worker 使用 `courselens-worker` Environment 保存短期任务令牌和 Worker 私钥，使用变量指向对应的私有 Mailbox。任务结束后短期令牌、Issue 密文和 Artifact 会被清理。

## 许可

本仓库采用 Apache License 2.0。模型权重及第三方运行库继续遵循各自上游许可证。
