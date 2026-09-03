# read-paper-babeldoc-lab

一个只监听本机回环地址的 BabelDOC 效果验证站。它与 `read-paper` 完全独立，接收 PDF 后在后台运行固定版本的 BabelDOC，并同时提供原 PDF、纯译文 PDF、双语 PDF 的浏览与下载。

## 当前边界

- BabelDOC 固定到 `38d3896dcde9b5a940c62cf5563cadea673a64d3`（包版本 0.6.4）。
- 使用 OpenAI-compatible 翻译接口；默认按本机已有 `GLM_API_KEY` 配置运行 GLM-4 Flash。
- 上游 v0.6.4 已退役额外的 RapidOCR 表格文字检测，`--translate-table-text` 参数会被忽略。文本型 PDF 的表格仍可能由常规文字管线正确翻译；扫描件或图片表格不保证。实验页面会明确显示该边界。
- 只绑定 `127.0.0.1:8787`，没有公网入口和用户系统。
- 上传、工作目录和产物位于本项目 `data/`，不会读写 read-paper 数据库。

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
./run.sh
```

## 验证

```bash
uv sync --frozen
uv run --frozen pytest -q
```

本项目以及依赖的 BabelDOC 均按 AGPL-3.0-only 发布。
