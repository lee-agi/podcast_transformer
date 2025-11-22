# any2summary

`any2summary` 是一个命令行工具，用于从 YouTube 视频中抽取字幕，并可选地调用 Azure OpenAI `gpt-4o-transcribe-diarize` 服务进行说话人分离。输出为包含时间戳、文本与说话人信息的 JSON。

## 功能特性

- 基于 `youtube-transcript-api` 获取时间戳精确到秒的字幕段。
- 使用 `yt_dlp` + `ffmpeg` 下载并转换音频，并提交至 Azure OpenAI 语音转写接口。
- 支持 B 站等多站点视频音频提取，按照 URL 主机自动设置 Referer 与缓存目录结构。
- 内置 Android 客户端回退逻辑，即使未配置 cookie 也能规避常见的 403 Forbidden 下载失败。
- 自动检测超长（超过 Azure 1,500 秒限制）或超大音频并切分成多个片段，逐段提交 Azure，并通过流式返回实时刷新进度条。
- 通过 `gpt-4o-transcribe-diarize` 返回的说话人分段信息，将不同说话人合并入字幕。
- 兼容 Azure OpenAI `diarized_json` 响应中 `response.output[*].content` 等多层嵌套结构，自动提取 `segments`/`chunks` 字段，即使 YouTube 无字幕也能利用 Azure 转写结果产出文本。
- 支持通过 `--azure-streaming/--no-azure-streaming` 控制 Azure 转写是否流式执行；启用时会实时刷新进度条，必要时可设置 `ANY2SUMMARY_DEBUG_PAYLOAD=1` 输出原始 chunk 便于排查。
- 摘要 Markdown 会自动复制一份到 `ANY2SUMMARY_OUTBOX_DIR`（默认 `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault/010 outbox`），便于在 Obsidian 等笔记库中直接浏览。
- 当 Azure 暂未返回说话人分段时，会退回空说话人列表并继续使用已有字幕，避免 CLI 直接失败。
- 若视频已提供带时间轴的字幕，默认直接复用字幕并跳过 Azure 说话人分离，避免冗余的音频下载与 ASR 调用；如需强制执行可添加 `--force-azure-diarization`。
- 可选调用 Azure GPT-5 Pro（默认部署 `gpt-5-pro`），通过 Responses API 翻译与总结 ASR 片段，可使用 `--summary-prompt-file` 定制提示词。
- 当视频元数据缺失领域信息时，自动调用 Azure GPT-5 基于生成的摘要推断领域标签，避免默认落入“通用”。
- 支持通过 `--summary-prompt-file` 指定外部 Prompt 配置文件，无需改动代码即可调整摘要策略。
- 摘要结果以标准 Markdown 格式输出，包含封面、目录与时间轴表格，并自动写入缓存目录的 `summary.md` 文件。
- 对不支持音频下载的网页文章，会自动抓取正文段落与站点图标，将原始 HTML、解析后的纯文本及元数据缓存为 `article_raw.html`、`article_content.txt`、`article_metadata.json`，并无缝接入摘要流程（覆盖测试见 `test/test_cli_article.py`），摘要默认使用面向文章的专用 Prompt，并可通过 `--article-summary-prompt-file` 单独配置；即便同时提供 `--summary-prompt-file` 或启用 `--azure-diarization`，文章模式也会忽略这些音频配置以防误用。
- 针对 Apple Podcasts（`podcasts.apple.com`）等音频播客链接，即使未显式添加 `--azure-diarization` 也会自动启用 Azure 说话人分离流程并下载音频，只要 Azure 凭据可用即可沿用默认 `prompts/summary_prompt.txt` 生成摘要。
- `--url` 支持以逗号分隔多个链接，CLI 会并发执行完整流程，并按输入顺序逐条输出 JSON 结果；若个别链接失败，会在标准错误流提示 `[URL] 错误信息`。
- 自动加载工作目录或 `ANY2SUMMARY_DOTENV` 指向的 `.env` 文件，简化凭据管理（仍向下兼容历史的 `PODCAST_TRANSFORMER_*` 环境变量，若需平滑迁移可逐步替换为 `ANY2SUMMARY_*`）。
- 提供 `--clean-cache` 与 `--check-cache` 选项，方便排查与清理缓存。
- 命令行输出 JSON，可通过 `--pretty` 选项进行格式化。

## 安装依赖

如已发布至 PyPI，可直接执行：

```bash
pip install any2summary
```

若从源码安装，可在 `any2summary` 目录下运行：

```bash
pip install .
```

或使用传统方式：

```bash
pip install youtube-transcript-api yt-dlp openai
# 若网络环境需要代理，请额外安装 httpx[socks] 以支持 SOCKS 代理
pip install "httpx[socks]"
```

或者执行项目提供的 `setup_and_run.sh` 脚本，它会自动创建虚拟环境并安装必要依赖。

同时需要本地安装 `ffmpeg`，macOS 可通过 `brew install ffmpeg`，其他平台请参考官方安装说明。

## Azure 配置

1. 在 Azure 门户中创建 Azure OpenAI 资源，并为 `gpt-4o-transcribe-diarize` 模型部署一个实例。
2. 复制终结点、密钥以及 API 版本（未配置时默认 `2024-06-01`），并在命令行环境设置：

```bash
export AZURE_OPENAI_API_KEY="<your-key>"
export AZURE_OPENAI_ENDPOINT="<https://your-resource.openai.azure.com>"
# 可选：指定 API 版本与部署名称
export AZURE_OPENAI_API_VERSION="2025-03-01-preview"
export AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT="gpt-4o-transcribe-diarize"
# chunking_strategy 默认使用 "auto"；如需自定义可设置字符串或 JSON：
# export AZURE_OPENAI_CHUNKING_STRATEGY="server_vad"
# export AZURE_OPENAI_CHUNKING_STRATEGY='{"type": "server_vad", "threshold": 0.6}'
# 可选：配置 GPT-5 翻译/总结调用所用的 API 版本与部署名称
export AZURE_OPENAI_SUMMARY_API_VERSION="2025-01-01-preview"
export AZURE_OPENAI_SUMMARY_DEPLOYMENT="gpt-5-pro"
export AZURE_OPENAI_DOMAIN_DEPLOYMENT="gpt-5-pro"
# 可选：覆盖 Responses API 基础地址（默认 <AZURE_OPENAI_ENDPOINT>/openai/v1）
# export AZURE_OPENAI_RESPONSES_BASE_URL="https://your-resource.openai.azure.com/openai/v1"
```

可通过 `--max-speakers` 对说话人数量做最佳努力限制；超出时会将较小说话人映射到主要说话人之一。

> 提示：若执行 `./setup_and_run.sh` 并包含 `--azure-diarization` 选项，脚本会在运行前检查 `AZURE_OPENAI_API_KEY` 与 `AZURE_OPENAI_ENDPOINT` 是否已设置。

## 环境变量管理

仓库提供 `.env.example`。复制后补齐密钥，即可让 CLI 自动加载：

```bash
cp .env.example .env
```

也可设置 `ANY2SUMMARY_DOTENV=/path/to/.env` 指向自定义位置，`run` 函数会在执行时自动读取文件并填充缺失的环境变量（不会覆盖已存在的设置）。

## 使用方法

```bash
# 首次或重复运行均可，脚本会自动复用 .venv
./setup_and_run.sh \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --fallback-language zh-Hans \
  --clean-cache \
  --azure-diarization \
  --azure-streaming \
  --azure-summary \
  --summary-prompt-file /absolute/path/to/prompt.txt \
  --pretty
```

如果只需要字幕（无需说话人分离），省略 `--azure-diarization` 选项即可。若目标视频无所需语言字幕，可通过 `--fallback-language` 多次指定备用语言。当所有字幕均不可用时，启用 `--azure-diarization` 会自动调用 Azure OpenAI 完整转写与说话人分离。

默认情况下，即使添加了 `--azure-diarization`，只要成功获取带时间线的字幕段，CLI 就会直接输出字幕并跳过 Azure 调用；若需要强制执行 Azure 说话人分离，可附加 `--force-azure-diarization`，或提供 `--known-speaker`/`--known-speaker-name`/`--max-speakers` 参数以触发完整流程。

若希望辅助识别特定说话人，可附加 `--known-speaker 名称=音频路径` 选项（可多次指定），工具会自动将参照音频转为数据 URL 并传递给 Azure OpenAI。若仅有姓名提示，可使用 `--known-speaker-name 姓名` 多次指定（例如 `--known-speaker-name Alice --known-speaker-name Bob`），脚本会将所有姓名通过 `known_speaker_names` 参数直接发送给 Azure 接口，以提升说话人标签的准确率。

当 `yt_dlp` 遭遇无 cookie 时的 403 Forbidden，CLI 会自动改用 Android 客户端参数重新发起下载，并切换到移动端 User-Agent，以提升公开视频的成功率。

> 提示：非 YouTube URL 暂不具备内建字幕获取能力，若需转写请启用 `--azure-diarization` 以调用 Azure OpenAI。

当生成的 WAV 超过约 25 分钟（1,500 秒）或 100MB 时，CLI 会在缓存目录下自动生成不超过约 23 分钟的 `audio_partXXX.wav` 片段并依序调用 Azure，从而绕过 `audio duration ... is longer than 1500 seconds` 等超限报错；若缓存中仍存在旧的超长片段，工具会自动清理并重新切分。若 Azure 返回空的说话人/转写结果，CLI 会回退为空列表并继续后续流程，可结合原始字幕输出完成后续摘要或导出。

当缓存文件异常或想强制重新下载音频时，可追加 `--clean-cache` 选项，脚本会在调用任何外部服务前删除当前 URL 的缓存目录。

若仅需检查缓存内容，可使用：

```bash
./setup_and_run.sh --url "https://www.youtube.com/watch?v=<video-id>" --check-cache
```

命令会输出缓存路径、存在的文件列表以及 `audio.wav` 是否存在。

音频文件与转换后的 WAV 会缓存于 `~/.cache/any2summary/<video_id>/`（或通过设置 `ANY2SUMMARY_CACHE_DIR` 自定义位置）；对于非 YouTube 站点，会在缓存目录中包含域名与 URL 哈希前缀，重复执行时同样复用缓存，避免再次下载。

> 注意：部分视频仅提供其他语言字幕，脚本会尝试自动翻译为 `--language` 指定的语言；如仍无法获取，请使用 `--fallback-language` 指明可用的字幕语言代码（可在报错信息中查看）。

## GPT-5 翻译与总结

启用 `--azure-summary` 选项即可在原始字幕/转写结果基础上，调用 Azure GPT-5（默认部署名 `gpt-5`）生成翻译与总结，system prompt 详见 `SUMMARY_PROMPT`。命令示例：

```bash
./setup_and_run.sh \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --azure-streaming \
  --azure-summary \
  --summary-prompt-file ./prompts/summary_prompt.txt
```

输出 JSON 将新增 `summary` 字段，内容遵循以下约定：

- 始终保留原始时间线；若原文非中文，先翻译成中文。
- 长内容会包含 `Abstract`、`Keypoints` 与按主题分段的正文，每段不超过约 300 字。
- 多人对话按说话人分段，保持第一人称述说；专业名词可带原文注释。
- 同时会在对应视频缓存目录（例如 `~/.cache/any2summary/youtube/<video_id>/summary.md`）写入一份结构化 Markdown 文件，含封面标题、目录与时间轴表格；CLI 输出会额外返回 `summary_path` 方便调用方读取该文件。

若需调整文案风格，可通过 `AZURE_OPENAI_SUMMARY_DEPLOYMENT` 更换部署，或在调用 CLI 时提供 `--summary-prompt-file` 指向自定义 prompt 文件；保持为空时则回退到内置的 `SUMMARY_PROMPT`。

> 调试提示：若需检查 Azure 分段返回的原始数据，可在命令前设置 `ANY2SUMMARY_DEBUG_PAYLOAD=1`，缓存目录会生成 `debug_payload_*.json` 供分析。

### 处理网页文章

若传入的 URL 属于博客/文档网页且 `yt_dlp` 无法提取音频，CLI 会改为抓取网页正文及站点图标，并将内容拆分为段落 `segments` 供后续摘要使用；相关缓存文件位于对应缓存目录下的 `article_raw.html`、`article_content.txt` 与 `article_metadata.json`。此流程与 Azure 摘要无缝衔接，因此可直接对文章链接执行 `--azure-summary` 获取翻译与总结，默认采用专门为文章设计的 Prompt（可通过 `--article-summary-prompt-file` 指定单独的文章 Prompt；文章模式会忽略 `--summary-prompt-file` 与 `--azure-diarization`，后者仅用于视频/音频链接）。Apple Podcasts 等音频源会自动跳过此分支，直接下载音频并走 Azure 话者分离流程。

### 处理多个 URL

CLI 允许一次性处理多个视频或文章链接：

```bash
./setup_and_run.sh \
  --url "https://example.com/a,https://example.com/b" \
  --language en
```

多个任务会并发执行，并按照输入顺序逐条输出 JSON；若个别链接失败，会在标准错误流输出 `[URL] 错误信息`，其余链接继续完成。

## 示例脚本

快速体验可运行：

```bash
./run_example.sh "https://www.youtube.com/watch?v=<video-id>"
```

脚本会自动加载同目录下的 `.env` 并调用 `setup_and_run.sh`。

## Docker 部署

仓库提供 `Dockerfile`，可通过以下命令构建镜像：

```bash
docker build -t any2summary ./any2summary
```

运行时挂载缓存目录与 `.env`：

```bash
docker run --rm \
  --env-file ./any2summary/.env \
  -v "$HOME/.cache/any2summary:/app/.cache/any2summary" \
  any2summary \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --pretty
```

## 测试

项目根目录（即 `any2summary` 目录）执行：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest test/test_cli.py test/test_cli_article.py
```

若在其父目录运行，可将路径改为 `pytest any2summary/test/test_cli.py` 等价命令。

## 限制

- 需要网络访问 YouTube 以及 Azure OpenAI 服务。
- Azure OpenAI 识别质量受音频质量、部署规格与配额影响；可根据实际情况调整 `--max-speakers`。
- 当前实现依赖 `ffmpeg` 进行音频转码，若本地缺失需提前安装。
