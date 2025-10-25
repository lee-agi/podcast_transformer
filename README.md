# podcast_transformer

`podcast_transformer` 是一个命令行工具，用于从 YouTube 视频中抽取字幕，并可选地调用 Azure OpenAI `gpt-4o-transcribe-diarize` 服务进行说话人分离。输出为包含时间戳、文本与说话人信息的 JSON。

## 功能特性

- 基于 `youtube-transcript-api` 获取时间戳精确到秒的字幕段。
- 使用 `yt_dlp` + `ffmpeg` 下载并转换音频，并提交至 Azure OpenAI 语音转写接口。
- 支持 B 站等多站点视频音频提取，按照 URL 主机自动设置 Referer 与缓存目录结构。
- 内置 Android 客户端回退逻辑，即使未配置 cookie 也能规避常见的 403 Forbidden 下载失败。
- 自动检测超长或超大音频并切分成多个片段，逐段提交 Azure，避免单次上传超过限制。
- 通过 `gpt-4o-transcribe-diarize` 返回的说话人分段信息，将不同说话人合并入字幕。
- 可选调用 Azure GPT-5，根据定制 system prompt 翻译与总结 ASR 片段。
- 摘要结果以标准 Markdown 格式输出，包含封面、目录与时间轴表格，并自动写入缓存目录的 `summary.md` 文件。
- 自动加载工作目录或 `PODCAST_TRANSFORMER_DOTENV` 指向的 `.env` 文件，简化凭据管理。
- 提供 `--clean-cache` 与 `--check-cache` 选项，方便排查与清理缓存。
- 命令行输出 JSON，可通过 `--pretty` 选项进行格式化。

## 安装依赖

如已发布至 PyPI，可直接执行：

```bash
pip install podcast-transformer
```

若从源码安装，可在 `podcast_transformer` 目录下运行：

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
export AZURE_OPENAI_SUMMARY_DEPLOYMENT="llab-gpt-5"
```

可通过 `--max-speakers` 对说话人数量做最佳努力限制；超出时会将较小说话人映射到主要说话人之一。

> 提示：若执行 `./setup_and_run.sh` 并包含 `--azure-diarization` 选项，脚本会在运行前检查 `AZURE_OPENAI_API_KEY` 与 `AZURE_OPENAI_ENDPOINT` 是否已设置。

## 环境变量管理

仓库提供 `.env.example`。复制后补齐密钥，即可让 CLI 自动加载：

```bash
cp .env.example .env
```

也可设置 `PODCAST_TRANSFORMER_DOTENV=/path/to/.env` 指向自定义位置，`run` 函数会在执行时自动读取文件并填充缺失的环境变量（不会覆盖已存在的设置）。

## 使用方法

```bash
# 首次或重复运行均可，脚本会自动复用 .venv
./setup_and_run.sh \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --fallback-language zh-Hans \
  --clean-cache \
  --azure-diarization \
  --azure-summary \
  --known-speaker Alice=/absolute/path/to/alice.wav \
  --known-speaker Bob=/absolute/path/to/bob.wav \
  --known-speaker-name Charlie \
  --max-speakers 2 \
  --pretty
```

如果只需要字幕（无需说话人分离），省略 `--azure-diarization` 选项即可。若目标视频无所需语言字幕，可通过 `--fallback-language` 多次指定备用语言。当所有字幕均不可用时，启用 `--azure-diarization` 会自动调用 Azure OpenAI 完整转写与说话人分离。

若希望辅助识别特定说话人，可附加 `--known-speaker 名称=音频路径` 选项（可多次指定），工具会自动将参照音频转为数据 URL 并传递给 Azure OpenAI。若仅有姓名提示，可使用 `--known-speaker-name 姓名` 多次指定（例如 `--known-speaker-name Alice --known-speaker-name Bob`），脚本会将所有姓名通过 `known_speaker_names` 参数直接发送给 Azure 接口，以提升说话人标签的准确率。

当 `yt_dlp` 遭遇无 cookie 时的 403 Forbidden，CLI 会自动改用 Android 客户端参数重新发起下载，并切换到移动端 User-Agent，以提升公开视频的成功率。

> 提示：非 YouTube URL 暂不具备内建字幕获取能力，若需转写请启用 `--azure-diarization` 以调用 Azure OpenAI。

当生成的 WAV 超过约 60 分钟或 100MB 时，CLI 会在缓存目录下自动生成 `audio_partXXX.wav` 片段并依序调用 Azure，从而绕过 `Audio file might be corrupted or unsupported` 等超限报错。

当缓存文件异常或想强制重新下载音频时，可追加 `--clean-cache` 选项，脚本会在调用任何外部服务前删除当前 URL 的缓存目录。

若仅需检查缓存内容，可使用：

```bash
./setup_and_run.sh --url "https://www.youtube.com/watch?v=<video-id>" --check-cache
```

命令会输出缓存路径、存在的文件列表以及 `audio.wav` 是否存在。

音频文件与转换后的 WAV 会缓存于 `~/.cache/podcast_transformer/<video_id>/`（或通过设置 `PODCAST_TRANSFORMER_CACHE_DIR` 自定义位置）；对于非 YouTube 站点，会在缓存目录中包含域名与 URL 哈希前缀，重复执行时同样复用缓存，避免再次下载。

> 注意：部分视频仅提供其他语言字幕，脚本会尝试自动翻译为 `--language` 指定的语言；如仍无法获取，请使用 `--fallback-language` 指明可用的字幕语言代码（可在报错信息中查看）。

## GPT-5 翻译与总结

启用 `--azure-summary` 选项即可在原始字幕/转写结果基础上，调用 Azure GPT-5（默认部署名 `llab-gpt-5`）生成翻译与总结，system prompt 详见 `SUMMARY_PROMPT`。命令示例：

```bash
./setup_and_run.sh \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --azure-summary
```

输出 JSON 将新增 `summary` 字段，内容遵循以下约定：

- 始终保留原始时间线；若原文非中文，先翻译成中文。
- 长内容会包含 `Abstract`、`Keypoints` 与按主题分段的正文，每段不超过约 300 字。
- 多人对话按说话人分段，保持第一人称述说；专业名词可带原文注释。
- 同时会在对应视频缓存目录（例如 `~/.cache/podcast_transformer/youtube/<video_id>/summary.md`）写入一份结构化 Markdown 文件，含封面标题、目录与时间轴表格；CLI 输出会额外返回 `summary_path` 方便调用方读取该文件。

若需调整文案风格，可通过 `AZURE_OPENAI_SUMMARY_DEPLOYMENT` 更换部署，或在调用 `generate_translation_summary` 时传入自定义 prompt。

## 示例脚本

快速体验可运行：

```bash
./run_example.sh "https://www.youtube.com/watch?v=<video-id>"
```

脚本会自动加载同目录下的 `.env` 并调用 `setup_and_run.sh`。

## Docker 部署

仓库提供 `Dockerfile`，可通过以下命令构建镜像：

```bash
docker build -t podcast-transformer ./podcast_transformer
```

运行时挂载缓存目录与 `.env`：

```bash
docker run --rm \
  --env-file ./podcast_transformer/.env \
  -v "$HOME/.cache/podcast_transformer:/app/.cache/podcast_transformer" \
  podcast-transformer \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en \
  --pretty
```

## 测试

项目根目录（即 `podcast_transformer` 目录）执行：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest test/test_cli.py
```

若在其父目录运行，可将路径改为 `pytest podcast_transformer/test/test_cli.py`。

## 限制

- 需要网络访问 YouTube 以及 Azure OpenAI 服务。
- Azure OpenAI 识别质量受音频质量、部署规格与配额影响；可根据实际情况调整 `--max-speakers`。
- 当前实现依赖 `ffmpeg` 进行音频转码，若本地缺失需提前安装。
