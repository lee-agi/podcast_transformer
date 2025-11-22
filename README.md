# any2summary

`any2summary` is a command-line toolkit that handles the entire pipeline for podcasts, videos, and long-form articles—download, transcription, optional Azure speaker diarization, and Markdown summarization—directly on your local machine. The CLI emits structured JSON by default, and when Azure summarization is enabled it also writes Markdown with a cover, table of contents, and timeline table so long-form content can drop into your note-taking system with minimal effort.

> 📘 Looking for the Simplified Chinese version? See `README.zh.md` in the project root. Both documents share the same structure and should stay in sync.

## Use Cases
- **YouTube / Bilibili / Spotify / Apple Podcasts**: fetch captions when available, or download audio plus run Azure OpenAI `gpt-4o-transcribe-diarize` for transcripts and speaker labels.
- **Web articles / documentation**: fall back to article mode when audio cannot be downloaded, capturing page text and metadata before summarization.
- **Batch processing**: pass a comma-separated list to `--url`; the CLI processes links concurrently and prints results in the original order.

## Feature Highlights
- `youtube-transcript-api` + `yt_dlp` + `ffmpeg` handle caption/audio retrieval with automatic Referer, User-Agent, and Android fallback tuning to avoid 403 errors.
- Audio longer than Azure’s 1,500-second limit is split into ≤1,400-second WAV chunks and uploaded sequentially; streaming mode refreshes progress in real time.
- Azure diarization results align with existing captions; when Azure returns empty segments the CLI falls back to the downloaded subtitles to keep the pipeline moving.
- Audio-only links or captionless videos automatically trigger the Azure transcription flow; add `--force-azure-diarization` to invoke Azure even when captions exist.
- `--azure-summary` calls Azure GPT-5 (Responses API or Chat Completions) to generate Markdown summaries and copies them into `ANY2SUMMARY_OUTBOX_DIR` (defaults to an Obsidian outbox folder).
- Article mode (`fetch_article_assets`) caches `article_raw.html`, `article_content.txt`, and `article_metadata.json`, then applies `ARTICLE_SUMMARY_PROMPT`; `--article-summary-prompt-file` overrides the default.
- `--clean-cache` clears cached artifacts for the current URL; `ANY2SUMMARY_DOTENV` automatically loads a `.env` file and remains compatible with legacy `PODCAST_TRANSFORMER_*` variables.
- CLI output is always indented JSON; in batch mode each job prints a separate JSON document, making it easy to stream-parse.

## Quick Start

### Prerequisites
- Python 3.10+
- `ffmpeg` (install via `brew install ffmpeg` on macOS or follow the official docs for other platforms)
- Network access to YouTube/your target site plus Azure OpenAI (adjust the proxy variables in `setup_and_run.sh` if needed)
- Azure OpenAI resource and deployments for transcription/summary features
- Environment variables configured from `.env.example` (copy to `.env`, fill in the required Azure credentials, and export via `ANY2SUMMARY_DOTENV` or your shell before running the CLI)

### Environment Variables
- Run `cp .env.example .env` (or copy the file to your preferred location) and replace the placeholder Azure values before executing any command.
- `ANY2SUMMARY_DOTENV` points to the `.env` path that should be auto-loaded; scripts like `run_example.sh` expect this file to exist.
- Keep `.env.example` up to date when new settings are required so teammates have a canonical reference.

### Installation Options
1. **PyPI (recommended):** `pip install any2summary`
2. **From source:** `cd any2summary && pip install .`
3. **Manual dependencies:** `pip install youtube-transcript-api yt-dlp openai "httpx[socks]"`
4. **Bootstrap script:** `cd any2summary && ./setup_and_run.sh --help` (creates `.venv`, installs deps, and exports proxy variables near the top)

### Minimal Example
```bash
python -m any2summary.cli \
  --url "https://www.youtube.com/watch?v=<video-id>" \
  --language en
```
- Captions are returned as JSON by default. When the target lacks captions, Azure transcription triggers automatically. Add `--force-azure-diarization` to invoke Azure even if captions already exist.
- Supply multiple comma-separated links in `--url` to process them concurrently while preserving order.

### Sample Script
```bash
./run_example.sh "https://www.youtube.com/watch?v=<video-id>"
```
The script loads `.env` located in the same directory and calls `setup_and_run.sh`, making it convenient to verify Azure credentials.

## CLI Reference

| Argument | Type / Default | Required | Description | Typical Usage |
| --- | --- | --- | --- | --- |
| `--url` | String, comma-separated | ✔ | Video/audio/article URLs; processed concurrently in the given order | Batch caption/summary export |
| `--language` | String, default `en` |  | Preferred language for captions/transcripts | Control transcript language |
| `--fallback-language` | Repeatable |  | Extra language codes to try when the primary one is missing | Cross-language resilience |
| `-V/--version` | Flag |  | Display version and exit | Verify installed version |
| `--azure-streaming` / `--no-azure-streaming` | Boolean, default on |  | Whether Azure transcription streams chunk updates | Minimize CLI noise or keep progress bars |
| `--force-azure-diarization` | Flag |  | Force Azure diarization even when captions are available (ignored for article links; automatically on for Apple Podcasts & similar audio URLs) | Ensure Azure results every time |
| `--azure-summary` | Flag |  | Use Azure GPT-5 to produce Markdown summaries/timelines saved to `summary.md` in cache | Generate polished summaries |
| `--summary-prompt-file` | Path |  | Custom prompt for audio/video summaries (defaults to `./prompts/summary_prompt.txt`) | Tailor summary tone |
| `--article-summary-prompt-file` | Path |  | Custom prompt for article mode when `--azure-summary` is enabled (defaults to `./prompts/article_prompt.txt`) | Tune article summarization |
| `--max-speakers` | Integer |  | Upper bound for Azure diarization speaker count | Interview/meeting constraints |
| `--known-speaker` | `name=path.wav`, repeatable |  | Provide reference audio clips to improve speaker labeling | Identify recurring hosts |
| `--known-speaker-name` | String, repeatable |  | Supply speaker names without audio samples | Give Azure semantic hints |
| `--clean-cache` | Flag |  | Remove cached artifacts for the current URL before processing | Force re-download/re-transcribe |

> **Notes:** Article mode ignores `--summary-prompt-file` and `--force-azure-diarization` to ensure web pages always use the article-specific prompt. Conversely, Apple Podcasts and similar audio sources automatically fall back to the Azure pipeline even without `--force-azure-diarization`.

## Environment Variables & Config

| Variable | Default / Source | Purpose |
| --- | --- | --- |
| `ANY2SUMMARY_DOTENV` | `.env` in working dir | Auto-loaded `.env`; also honors `PODCAST_TRANSFORMER_DOTENV` |
| `ANY2SUMMARY_CACHE_DIR` | `~/.cache/any2summary` | Override cache location (subdirectories keyed by host/video ID) |
| `ANY2SUMMARY_OUTBOX_DIR` | `~/Library/.../Obsidian Vault/010 outbox` | Destination for Markdown copies; set to disable or redirect |
| `ANY2SUMMARY_YTDLP_UA` | Desktop Chrome UA | Custom UA for `yt_dlp`; Android fallback overrides when needed |
| `ANY2SUMMARY_YTDLP_COOKIES` | Empty | Path to `cookies.txt` for login-only content |
| `ANY2SUMMARY_DEBUG_PAYLOAD` | Empty | If set, save `debug_payload_*.json` in cache directories |
| `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` | None | Required for all Azure features |
| `AZURE_OPENAI_API_VERSION` | `2025-03-01-preview` | Azure diarization API version |
| `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` | `gpt-4o-transcribe-diarize` | Transcription/dearization deployment name |
| `AZURE_OPENAI_SUMMARY_DEPLOYMENT` | `gpt-5-pro` | Summary model deployment |
| `AZURE_OPENAI_DOMAIN_DEPLOYMENT` | Uses summary deployment | Infers domain tags from summaries |
| `AZURE_OPENAI_SUMMARY_API_VERSION` | `2025-01-01-preview` | API version for Chat Completions mode |
| `AZURE_OPENAI_USE_RESPONSES` | Based on deployment suffix | Opt into Responses API (`1/true/yes` or `*-pro`) |
| `AZURE_OPENAI_RESPONSES_BASE_URL` | Derived from endpoint | Override Responses API base URL |
| `AZURE_OPENAI_CHUNKING_STRATEGY` | `auto` | Strategy string/JSON sent to Azure transcription |
| Proxy vars | Exported in `setup_and_run.sh` | Defaults to localhost:7890 for http/https/all_proxy |

## Typical Workflows

### 1. Captions + timeline only (no Azure)
```bash
python -m any2summary.cli --url "https://youtu.be/<id>" --language zh
```
Emits `segments` with timestamps and text—ideal for additional scripting or downstream tooling.

### 2. Speaker diarization + summary
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
- Audio is cached under `~/.cache/any2summary/youtube/<video-id>/` and split when needed.
- JSON output includes inline `summary`/`timeline` plus `summary_path` pointing to Markdown files; a copy is placed under `ANY2SUMMARY_OUTBOX_DIR`.

### 3. Article mode
```bash
python -m any2summary.cli \
  --url "https://example.com/blog/post" \
  --language zh \
  --azure-summary \
  --article-summary-prompt-file ./prompts/article_prompt.txt
```
- `fetch_article_assets` stores `article_raw.html`, `article_content.txt`, and `article_metadata.json`.
- The workflow always applies the article-specific prompt and ignores `--summary-prompt-file` / `--force-azure-diarization`.

### 4. Multiple URLs in parallel
```bash
python -m any2summary.cli \
  --url "https://youtu.be/A1,https://podcasts.apple.com/episode/B2" \
  --azure-summary
```
- Each job prints a JSON block in the original order; failures are reported to stderr as `[URL] error message` without stopping remaining tasks.

## Cache Layout
- Default cache root: `~/.cache/any2summary/<host_or_id>/`, containing:
  - `audio.*`: downloaded audio (split files named `audio_partXXX.wav`)
  - `captions.json`: caption segments
  - `segments.json`: merged Azure transcripts
  - `summary.md`, `timeline.md`: Markdown exports
  - `article_raw.html` / `article_content.txt` / `article_metadata.json`: article mode artifacts
- `--clean-cache` wipes the directory before processing.
- Set `ANY2SUMMARY_CACHE_DIR` to relocate caches to another drive or shared path.

## Advanced Customization & Debugging
- **Prompt overrides:** keep dedicated prompt files per source type and pass them via `--summary-prompt-file` / `--article-summary-prompt-file`.
- **Default prompt management:** editing `prompts/summary_prompt.txt` or `prompts/article_prompt.txt` immediately updates the CLI’s built-in behavior.
- **Speaker accuracy:** use `--known-speaker name=sample.wav` or `--known-speaker-name` hints to improve Azure labels.
- **Azure streaming:** enabled by default; disable with `--no-azure-streaming` in CI or log-sensitive environments.
- **Android fallback:** `yt_dlp` automatically retries with Android settings on YouTube 403 errors; provide cookies through `ANY2SUMMARY_YTDLP_COOKIES` for gated content.
- **Payload debugging:** set `ANY2SUMMARY_DEBUG_PAYLOAD=1` to dump raw Azure responses as JSON in the cache folder.
- **Batch throughput:** a `ThreadPoolExecutor` caps concurrency at CPU count; split large batches manually if you need throttling.

## Scripts & Docker

### setup_and_run.sh
- Creates `.venv`, installs dependencies, and exports proxy variables (`http_proxy/https_proxy/all_proxy` to `127.0.0.1:7890` by default). Edit the script to match your proxy port.
- Accepts the full CLI argument list (e.g., `./setup_and_run.sh --url <...> --azure-summary`) and is suitable for teammates who prefer shell scripts over Python invocations.

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
- Pass Azure credentials via `--env-file` and mount the cache directory to avoid repeated downloads/transcriptions.

## Testing
```bash
cd any2summary
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest test/test_cli.py test/test_cli_article.py

# From the repo root:
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest any2summary/test/
pytest test/ -q  # regression + integration suites
```

## FAQ
- **403 Forbidden / audio download fails**: verify the URL is publicly accessible; for login-required content, provide cookies via `ANY2SUMMARY_YTDLP_COOKIES` or rely on the default proxy in `setup_and_run.sh`.
- **Azure credential errors**: ensure `.env` or environment vars define `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT`, and set deployment names when summaries are required.
- **Audio too long**: the CLI auto-splits WAV files and retries; if stale oversized files linger, run with `--clean-cache` first.
- **Empty article summaries**: confirm `--azure-summary` is enabled and the article is reachable; provide a custom `--article-summary-prompt-file` if necessary.
- **Disk usage**: periodically clean `ANY2SUMMARY_CACHE_DIR` or combine it with `--clean-cache` on old tasks.

Before publishing, verify that README updates, sample commands, and prompt descriptions align with the current CLI behavior to avoid mismatches for new users.
