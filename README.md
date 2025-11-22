# any2summary

`any2summary` 是一个面向播客、视频与网页文章的命令行工具，可在本地一次性完成“下载/转写 → 说话人分离 → 摘要导出”整条链路。CLI 默认输出结构化 JSON，并在启用 Azure 摘要后生成带封面、目录与时间轴表格的 Markdown，帮助你把长内容快速同步到知识库或笔记工具，大幅提高知识获取效率。

## 适用场景
- **YouTube / Bilibili / Spotify / Apple Podcasts**：提取字幕，必要时下载音频并调用 Azure OpenAI `gpt-4o-transcribe-diarize` 获得说话人标签。
- **网页文章/文档**：无法下载音频时自动落到文章模式，抓取正文与站点元数据，再调用专用 Prompt 生成总结。
- **批量链接**：`--url` 支持逗号分隔多个链接，CLI 会并发处理并按输入顺序输出结果，方便批量整理内容。

## 功能总览
- `youtube-transcript-api` + `yt_dlp` + `ffmpeg` 负责字幕/音频获取，自动处理 Referer、User-Agent 与 Android 回退逻辑，规避 403 失败。
- 音频超出 Azure 1,500 秒限制时，自动切分为 ≤1,400 秒的 WAV 片段并依序上传，处理进度可通过 Azure 流式输出实时刷新。
- Azure 说话人分离结果与原字幕自动对齐，若 Azure 返回空结果会回退到已有字幕，避免流程中断。
- 无字幕或音频专用链接会自动触发 Azure 转写，若想在字幕已存在时也使用 Azure，可显式添加 `--force-azure-diarization`。
- `--azure-summary` 会调用 Azure GPT-5（Responses API 或 Chat 完成）生成 Markdown 摘要，并另存到 `ANY2SUMMARY_OUTBOX_DIR`（默认指向 Obsidian outbox）。
- 文章模式（`fetch_article_assets`）会缓存 `article_raw.html`、`article_content.txt`、`article_metadata.json` 并套用 `ARTICLE_SUMMARY_PROMPT`；可用 `--article-summary-prompt-file` 单独调参。
- `--clean-cache` 用于排查缓存；`ANY2SUMMARY_DOTENV` 允许自动加载 `.env` 并兼容历史 `PODCAST_TRANSFORMER_*` 变量。
- CLI 输出默认使用缩进 JSON，批量模式会顺序打印多个完整 JSON 文档，便于直接复制或通过流式解析消费。

## 快速开始

### 先决条件
- Python 3.10+
- `ffmpeg`（macOS 可 `brew install ffmpeg`，Linux/Windows 参考官方文档）
- 可访问 YouTube / 目标站点与 Azure OpenAI 的网络环境（如需代理，可在 `setup_and_run.sh` 中自定义 `http_proxy/https_proxy`）
- Azure OpenAI 资源及部署（若需说话人分离或摘要）

### 安装方式
1. **PyPI**（推荐）：`pip install any2summary`
2. **源码安装**：`cd any2summary && pip install .`
3. **手动安装依赖**：`pip install youtube-transcript-api yt-dlp openai "httpx[socks]"`
4. **一键脚本**：`cd any2summary && ./setup_and_run.sh --help`（脚本会创建 `.venv`、安装依赖并在开头设置代理变量）

### 最小示例
```bash
python -m any2summary.cli \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en
```
- 默认只抓取字幕并输出 JSON；若目标无字幕会自动调用 Azure 转写，如需无论是否存在字幕都强制使用 Azure 说话人分离，请添加 `--force-azure-diarization`。
- 支持在 `--url` 中填入多个逗号分隔的链接，CLI 会自动并发处理。

### 示例脚本
```bash
./run_example.sh "https://www.youtube.com/watch?v=<video-id>"
```
脚本会加载同目录下 `.env` 并调用 `setup_and_run.sh`，适合快速验证 Azure 凭据是否配置正确。

## CLI 参数速查

| 参数 | 类型 / 默认 | 必填 | 说明 | 典型用途 |
| --- | --- | --- | --- | --- |
| `--url` | 字符串，支持逗号分隔多个链接 | ✔ | 待处理的视频、音频或文章链接；多链接会并发执行并按输入顺序输出 | 批量生成字幕/摘要 |
| `--language` | 字符串，默认 `en` |  | 优先使用的字幕或转写语言代码 | 控制字幕/摘要语言 |
| `--fallback-language` | 可重复，默认空 |  | 主语言缺失时依次尝试的语言列表 | 跨语言字幕容错 |
| `-V/--version` | 标志 |  | 打印版本信息并退出 | 诊断安装版本 |
| `--azure-streaming` / `--no-azure-streaming` | 布尔，默认启用 |  | 控制 Azure 转写是否流式返回；关闭后将整体等待 | 需要最小日志或非交互环境 |
| `--force-azure-diarization` | 标志 |  | 即使字幕可用也强制走 Azure 流程（文章链接会忽略该选项，Apple Podcasts 等音频源会自动开启） | 确保使用 Azure 结果 |
| `--azure-summary` | 标志 |  | 基于字幕/转写结果调用 Azure GPT-5 输出 Markdown 摘要并写入缓存 `summary.md` | 生成摘要/翻译稿 |
| `--summary-prompt-file` | 文件路径 |  | 自定义视频/音频摘要 Prompt（默认使用 `./prompts/summary_prompt.txt`） | 调整摘要风格 |
| `--article-summary-prompt-file` | 文件路径 |  | 自定义文章模式 Prompt，仅在网页文章且启用 `--azure-summary` 时生效（默认使用 `./prompts/article_prompt.txt`） | 独立优化文章摘要 |
| `--max-speakers` | 整数 |  | Azure 说话人分离的最大说话人数上限 | 会议/访谈设定说话人范围 |
| `--known-speaker` | `name=path.wav` 可重复 |  | 为 Azure 提供已知说话人的参考音频 | 精细标注常驻嘉宾 |
| `--known-speaker-name` | 字符串，可重复 |  | 只提供说话人姓名提示，无需音频 | 给 Azure 额外语义提示 |
| `--clean-cache` | 标志 |  | 开始前清理当前 URL 对应缓存目录 | 重新下载 / 排障 |

> **提示**：网页文章模式会忽略 `--summary-prompt-file` 与 `--force-azure-diarization`，始终使用文章专用 Prompt；Apple Podcasts 等音频源即使未显式添加 `--force-azure-diarization` 也会自动进入 Azure 流程。

## 环境变量与配置

| 变量 | 默认值 / 来源 | 说明 |
| --- | --- | --- |
| `ANY2SUMMARY_DOTENV` | 工作目录下 `.env` | 启动时自动加载的 `.env` 路径，兼容旧的 `PODCAST_TRANSFORMER_DOTENV` |
| `ANY2SUMMARY_CACHE_DIR` | `~/.cache/any2summary` | 自定义缓存目录；子目录会按域名或视频 ID 分类 |
| `ANY2SUMMARY_OUTBOX_DIR` | `~/Library/.../Obsidian Vault/010 outbox` | 摘要 Markdown 的额外副本输出目录，可指向任意笔记库 |
| `ANY2SUMMARY_YTDLP_UA` | 桌面版 Chrome UA | `yt_dlp` 下载时使用的 User-Agent；Android 回退会自动切换 |
| `ANY2SUMMARY_YTDLP_COOKIES` | 空 | 指向 cookies.txt，可提升需要登录的视频成功率 |
| `ANY2SUMMARY_DEBUG_PAYLOAD` | 空 | 设为非空后会在缓存目录生成 `debug_payload_*.json`，便于分析 Azure 原始响应 |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` | 无 | Azure OpenAI 凭据，调用转写、摘要、领域检测必需 |
| `AZURE_OPENAI_API_VERSION` | `2025-03-01-preview` | Azure Diarization API 版本 |
| `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` | `gpt-4o-transcribe-diarize` | 语音转写/说话人分离部署名 |
| `AZURE_OPENAI_SUMMARY_DEPLOYMENT` | `llab-gpt-5-pro` | 摘要模型部署名，可与 Responses API 搭配 |
| `AZURE_OPENAI_DOMAIN_DEPLOYMENT` | 同 `AZURE_OPENAI_SUMMARY_DEPLOYMENT` | 基于摘要反推领域标签时使用 |
| `AZURE_OPENAI_SUMMARY_API_VERSION` | `2025-01-01-preview` | 摘要（Chat Completions）API 版本 |
| `AZURE_OPENAI_USE_RESPONSES` | 取决于部署后缀 | 为 `1/true` 或部署名以 `-pro` 结尾时，摘要与领域检测改走 Responses API |
| `AZURE_OPENAI_RESPONSES_BASE_URL` | 由 `AZURE_OPENAI_ENDPOINT` 推导 | 自定义 Responses API Base URL，可用于多资源场景 |
| `AZURE_OPENAI_CHUNKING_STRATEGY` | `auto` | 传给 Azure 转写的 chunking 策略字符串/JSON |
| `ANY2SUMMARY_OUTBOX_DIR` | 见上 | 控制摘要副本复制路径；若为空则跳过复制 |
| 代理变量 | 由 `setup_and_run.sh` 统一导出 | `https_proxy=http://127.0.0.1:7890` 等，可根据实际情况修改脚本 |

## 典型工作流

### 1. 只需字幕/时间轴（无 Azure）
```bash
python -m any2summary.cli --url "https://youtu.be/<id>" --language zh
```
输出包含 `segments`（时间戳+文本）以及基础元数据，适合直接导入二次处理脚本。

### 2. 说话人分离 + 摘要
```bash
ANY2SUMMARY_DOTENV=./.env \
python -m any2summary.cli \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --force-azure-diarization \
  --azure-summary \
  --summary-prompt-file ./prompts/summary_prompt.txt \
  --known-speaker "Host=./samples/host.wav"
```
- 音频会缓存到 `~/.cache/any2summary/youtube/<video-id>/` 并切分上传。
- CLI 输出 JSON 将包含 `summary`（Markdown 路径）、`timeline` 等字段，并将 Markdown 复制到 `ANY2SUMMARY_OUTBOX_DIR`。

### 3. 网页文章模式
```bash
python -m any2summary.cli \
  --url "https://example.com/blog/post" \
  --language zh \
  --azure-summary \
  --article-summary-prompt-file ./prompts/article_prompt.txt
```
- `fetch_article_assets` 会保存 `article_raw.html`、`article_content.txt`、`article_metadata.json`。
- 摘要始终使用文章 Prompt，忽略 `--summary-prompt-file` 与 `--force-azure-diarization`。

### 4. 并发处理多个链接
```bash
python -m any2summary.cli \
  --url "https://youtu.be/A1,https://podcasts.apple.com/episode/B2" \
  --azure-summary
```
- CLI 会按输入顺序输出两条 JSON；若其中某条失败，会在标准错误输出 `[URL] 错误信息`，其余任务继续完成。

## 缓存与文件结构
- 默认缓存位于 `~/.cache/any2summary/<host_or_id>/`：
  - `audio.*`：原始下载音频，拼接音频则以 `audio_partXXX.wav` 命名
  - `captions.json`：字幕片段
  - `segments.json`：Azure 转写合并结果
  - `summary.md`、`timeline.md`：摘要/时间轴 Markdown
  - `article_raw.html`/`article_content.txt`/`article_metadata.json`：文章模式产物
- `--clean-cache` 会在新任务开始前删除对应目录。
- 可通过设置 `ANY2SUMMARY_CACHE_DIR` 将缓存迁移至外置磁盘或共享目录。

## 深度定制与调试
- **Prompt 定制**：为不同来源维护独立 Prompt 文件，通过 `--summary-prompt-file` / `--article-summary-prompt-file` 切换。
- **默认 Prompt 管理**：直接编辑仓库 `prompts/summary_prompt.txt` 与 `prompts/article_prompt.txt` 即可修改 CLI 默认摘要风格，每次执行都会重新读取文件内容。
- **说话人优化**：利用 `--known-speaker` (name=wav) 或 `--known-speaker-name` 提供语义/音频提示提升 Azure 标签准确率。
- **Azure Streaming**：默认开启，若在 CI 环境不希望显示进度条，可添加 `--no-azure-streaming`。
- **Android 回退**：当 `yt_dlp` 遇到 403 时会自动切换至 Android UA；如站点需要 cookie，请设置 `ANY2SUMMARY_YTDLP_COOKIES`。
- **调试 payload**：把 `ANY2SUMMARY_DEBUG_PAYLOAD` 设为 `1` 后，可在缓存目录获取 `debug_payload_*.json` 观察 Azure 原始响应。
- **多 URL 策略**：内部使用 `ThreadPoolExecutor`，最大并发不超过 CPU 核心数；可通过分批调用控制资源占用。

## 脚本与 Docker

### setup_and_run.sh
- 负责创建 `.venv`、安装依赖并执行 CLI，脚本开头默认导出 `http_proxy/https_proxy/all_proxy` 到 `127.0.0.1:7890`，若端口不同请修改脚本后再运行。
- 支持 `./setup_and_run.sh --url <...> --azure-summary` 等完整 CLI 参数，适合日常使用或分享给非开发者。

### Docker
```bash
docker build -t any2summary ./any2summary
docker run --rm \
  --env-file ./any2summary/.env \
  -v "$HOME/.cache/any2summary:/app/.cache/any2summary" \
  any2summary \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en
```
- 记得通过 `--env-file` 传入 Azure 凭据，并挂载缓存目录避免重复下载。

## 测试
```bash
cd any2summary
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest test/test_cli.py test/test_cli_article.py

# 或在仓库根目录执行：
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest any2summary/test/
pytest test/ -q  # 回归与集成用例
```

## 常见问题
- **403 Forbidden / 无法下载音频**：确认 URL 可直接访问；若需登录，请提供 cookies (`ANY2SUMMARY_YTDLP_COOKIES`) 或使用 `setup_and_run.sh` 默认代理。
- **Azure 凭据错误**：确保 `.env` 或环境变量中包含 `AZURE_OPENAI_API_KEY`、`AZURE_OPENAI_ENDPOINT`，并在需要摘要时配置对应部署名。
- **音频过长**：工具会自动切分并重试；若缓存中存在旧的超长 WAV，可先执行 `--clean-cache`。
- **文章模式摘要为空**：请确认 `--azure-summary` 已启用且文章可正常访问；必要时提供自定义 `--article-summary-prompt-file`。
- **本地磁盘占用高**：定期清理 `ANY2SUMMARY_CACHE_DIR`，或结合 `--clean-cache` 针对性删除历史任务。

发布前请再次确认 README、测试命令与 Prompt 文件说明是否与当前 CLI 行为保持一致，避免用户在实际运行时遇到参数不匹配的问题。
