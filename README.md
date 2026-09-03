# read-paper-babeldoc-lab

一个只监听本机回环地址的 BabelDOC worker 和效果验证站。它与 `read-paper` 进程隔离，接收 PDF 后在后台运行固定版本的 BabelDOC，并同时提供原 PDF、译文 PDF、双语 PDF 和 compact IR；现有浏览器 UI 继续使用同一组接口。

## 当前边界

- BabelDOC 固定到 `38d3896dcde9b5a940c62cf5563cadea673a64d3`（包版本 0.6.4）。
- 服务启动时会从已安装包的 `direct_url.json` 核验版本、requested revision 和实际 commit。校验失败时任务会失败，不会返回缺失 IR 的完成状态。
- 使用 OpenAI-compatible 翻译接口；默认按本机已有 `GLM_API_KEY` 配置运行 GLM-4 Flash。
- 上游 v0.6.4 已退役额外的 RapidOCR 表格文字检测，`--translate-table-text` 参数会被忽略。文本型 PDF 的表格仍可能由常规文字管线正确翻译；扫描件或图片表格不保证。实验页面会明确显示该边界。
- 只绑定 `127.0.0.1:8787`，没有公网入口和用户系统。
- 上传、工作目录和产物位于本项目 `data/`，不会读写 read-paper 数据库。
- compact IR v1 通过受控 method observer 在内存中提取，保留 BabelDOC 产出的全部原始段落，并包含页级 layouts、段落 bbox/layout、原译文和必要样式；完整 debug IL JSON 不是生产接口。
- compact IR v1 暂不支持 BabelDOC split parts；worker 会显式拒绝非空 `split_strategy`。

## Worker API

- `POST /api/jobs`：提交 PDF。可选 Form 字段 `idempotency_key`，只接受 32 或 64 位小写十六进制字符串。
- `GET /api/jobs/{job_id}`：查询状态。状态包含 `engine`、版本、revision 和模型，不包含 API key 或 worker token。
- `GET /api/jobs/{job_id}/files/mono`：纯译文 PDF。
- `GET /api/jobs/{job_id}/files/dual`：双语 PDF。
- `GET /api/jobs/{job_id}/files/ir`：`read-paper.babeldoc.compact-ir` v1 JSON。
- `GET /api/jobs/{job_id}/files/manifest`：任务产物清单。

设置 `BABELDOC_WORKER_TOKEN` 后，任务、样本、预览和文件接口要求：

```text
Authorization: Bearer <BABELDOC_WORKER_TOKEN>
```

`/api/info` 和首页保持公开。未设置 token 时维持原有本地免认证行为；浏览器 UI 可在当前会话中输入 token。

同一 `idempotency_key`、source SHA256、页码、扫描检测和术语配置会复用已有 queued、running 或 completed 任务；QPS 不影响幂等判断。key 已用于不同 source 或配置时返回 409。failed 任务可用相同参数重试，新任务会替换该 key 的当前指针。key 保存在任务状态中，服务重启后会从 `status.json` 恢复索引。

## 启动

```bash
./run.sh
```

打开 <http://127.0.0.1:8787>。`run.sh` 会从交互 shell 继承已有密钥，但不会把密钥写入项目或命令行参数。

可覆盖配置：

```bash
BABELDOC_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \
BABELDOC_MODEL=glm-4-flash-250414 \
BABELDOC_API_KEY=... \
BABELDOC_WORKER_TOKEN=... \
./run.sh
```

## 验证

```bash
uv sync --frozen
uv run --frozen pytest -q
```

本项目以及依赖的 BabelDOC 均按 AGPL-3.0-only 发布。
