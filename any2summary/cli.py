"""Command line interface for extracting structured YouTube transcripts.

The module provides a CLI entry point `run` that accepts a YouTube URL and
optional Azure OpenAI diarization support to enrich transcripts with speaker
labels. The default behaviour fetches caption segments with timestamps and
emits JSON to stdout.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import datetime
from html.parser import HTMLParser
from collections import defaultdict
from importlib import metadata
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import ParseResult, parse_qs, urljoin, urlparse


DEFAULT_YTDLP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
ANDROID_YTDLP_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Mobile Safari/537.36"
)

AZURE_AUDIO_LIMIT_SECONDS = 1500.0
MAX_WAV_DURATION_SECONDS = AZURE_AUDIO_LIMIT_SECONDS
MAX_WAV_SIZE_BYTES = 100 * 1024 * 1024
AUDIO_SEGMENT_SECONDS = 1400.0
WAV_FRAME_CHUNK_SIZE = 32_768
ESTIMATED_TOKENS_PER_SECOND = 4.0
PROGRESS_BAR_WIDTH = 30
READING_WORDS_PER_MINUTE = 300
DEFAULT_OUTBOX_DIR = (
    "/Users/clzhang/Library/Mobile Documents/"
    "iCloud~md~obsidian/Documents/Obsidian Vault/010 outbox"
)
PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"
DEFAULT_SUMMARY_PROMPT_PATH = PROMPTS_ROOT / "summary_prompt.txt"
DEFAULT_ARTICLE_PROMPT_PATH = PROMPTS_ROOT / "article_prompt.txt"

def _getenv(*keys: str) -> Optional[str]:
    """Return the first defined environment variable among keys."""

    for key in keys:
        value = os.getenv(key)
        if value is not None:
            return value
    return None


try:
    __CLI_VERSION = metadata.version("any2summary")
except metadata.PackageNotFoundError:
    try:
        __CLI_VERSION = metadata.version("podcast-transformer")
    except metadata.PackageNotFoundError:
        __CLI_VERSION = "0.0.0"


_TIME_START_KEYS = (
    "start",
    "start_time",
    "startTime",
    "start_seconds",
    "start_second",
    "offset",
    "begin",
    "from",
    "start_time_ms",
    "startTimeMs",
    "start_timeMillis",
    "start_ms",
    "startMillis",
    "startMilliseconds",
    "startMs",
)
_TIME_END_KEYS = (
    "end",
    "end_time",
    "endTime",
    "end_seconds",
    "end_second",
    "stop",
    "to",
    "finish",
    "offset_end",
    "end_time_ms",
    "endTimeMs",
    "end_timeMillis",
    "end_ms",
    "endMillis",
    "endMilliseconds",
    "endMs",
)
_TIME_DURATION_KEYS = (
    "duration",
    "duration_s",
    "duration_seconds",
    "duration_ms",
    "durationMillis",
    "durationMilliseconds",
    "durationMs",
    "length",
)
_TIME_CONTAINER_KEYS = (
    "timestamp",
    "timestamps",
    "time",
    "timing",
    "time_range",
    "timeRange",
    "range",
    "span",
    "offsets",
    "offset",
    "start_end",
    "startEnd",
)
_TEXT_VALUE_KEYS = (
    "text",
    "content",
    "value",
    "utterance",
    "sentence",
    "caption",
)
_TEXT_COLLECTION_KEYS = (
    "alternatives",
    "parts",
    "lines",
    "tokens",
    "elements",
    "chunks",
    "segments",
    "items",
    "words",
    "content",
    "sentences",
)
_SPEAKER_KEYS = (
    "speaker",
    "speaker_label",
    "speakerId",
    "speaker_id",
    "speaker_tag",
    "speakerTag",
    "label",
    "name",
)

_MEDIA_HOST_SUFFIXES = (
    "youtu.be",
    "youtube.com",
    "bilibili.com",
    "soundcloud.com",
    "music.apple.com",
    "podcasts.apple.com",
    "podcast.apple.com",
    "spotify.com",
    "open.spotify.com",
    "podcasters.spotify.com",
)
_FORCED_AUDIO_HOST_SUFFIXES = (
    "podcasts.apple.com",
    "podcast.apple.com",
)
_MEDIA_PATH_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".flac")


SUMMARY_PROMPT = '''
你是一个可以帮助用户完成AI相关文章翻译和总结的助手。

你需要完成如下任务：
1. 如果原始是youtube资源或播客音频资源，要保留视频或音频中的时间线，可以大概没5分钟或将一个主题合并成一段。将非中文字幕，先翻译成中文，千万不要省略或遗漏任何信息，仅可以删掉一些无意义的口语表达，比如uh、yeah等。
2. 如果内容很长，可以先给出Abstract和Keypoints，同时根据“主题”做分段，每段的标题即为提取的“主题”，每段最好不要超过300字。如果是多人对话，每段以`说话人`的姓名开始，按不同的`说话人`分段。
3. 将你认为重要的、insightful、非共识的内容markdown加粗标识，以便阅读，但加粗内容不宜太多。


注意：
1. 始终用第一人称翻译，不要用转述的方式。
2. 专业词汇和人名可以不翻译，例如`agent`、`llm`、`Sam`可以不翻译，或后面加上原始词，比如费曼图（Feynman diagram）。
3. 输出格式可参考（但不必一定遵守）：`摘要：1-3句话，可包含一些非共识的insight。\n<主题1>（00:00:00 - 00:05:03）\n<说话人名1>：<xxx>。\n<说话人名2>：<xxx>。\n<主题2>（00:05:03 - 00:09:52）\n<说话人名2>：<xxx>。\n<说话人名1>：<xxx>。......`。`<>`中是需要填充的内容。
'''


ARTICLE_SUMMARY_PROMPT = '''
你是一个英文文章翻译和总结智能体。你的任务是分析用户提供的文章，自动抓取原文（包括关键图表等）。

你的输出包含:
## 总结
最好不超过5句话总结文章的核心观点。
## 要点
1. 要点最好以层次化、结构化的方式展现。
2. 每一个要点必须是一个观点/结论/事实。
3. 要点之间最好有逻辑关系，当然要以忠实于原文为基础。
## 翻译要求
1. 如果是非中文，先将非关键信息翻译成中文，关键表达最好不翻译，若要翻译务必要附带上原始表达，以保留最准确的原始语言所传达的信息。
2. 专业词汇和人名不要翻译，例如`agent`、`llm`、`Sam`不要翻译，或后面加上原始词，比如费曼图（Feynman diagram）。
3. 翻译过程中千万不要压缩、省略或遗漏任何原始语言传达的信息，仅可以删掉一些无意义的口语表达或广告内容，比如uh、yeah等。
4. 将你认为重要的、insightful、非共识的内容markdown加粗标识，以便阅读，但加粗内容不宜太多。
'''


DOMAIN_PROMPT = (
    "你将收到一段中文摘要，请根据内容判断最贴切的领域标签。"
    "直接输出一个中文标签，避免解释或补充说明。"
)


def _load_dotenv_if_present(explicit_path: Optional[str] = None) -> None:
    """Load environment variables from a dotenv file when available."""

    candidates: List[str] = []
    if explicit_path and explicit_path.strip():
        candidates.append(explicit_path.strip())
    else:
        candidates.append(".env")

    for candidate in candidates:
        path = candidate
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" not in stripped:
                        continue
                    key, value = stripped.split("=", 1)
                    key = key.strip()
                    if not key or key in os.environ:
                        continue
                    cleaned = value.strip().strip('"').strip("'")
                    os.environ[key] = cleaned
        except OSError as exc:  # pragma: no cover - filesystem failure
            raise RuntimeError(f"读取 dotenv 文件失败：{path}") from exc
        break


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Entrypoint for the CLI.

    Args:
        argv: Sequence of command line arguments excluding the program name.

    Returns:
        Process exit code, 0 on success, non-zero on failure.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Download YouTube captions, enrich with optional Azure OpenAI "
            "diarization, and emit structured JSON output."
        )
    )
    parser.add_argument("--url", required=True, help="YouTube video URL")
    parser.add_argument(
        "--language",
        default="en",
        help="Primary language code for caption retrieval (default: en)",
    )
    parser.add_argument(
        "--fallback-language",
        dest="fallback_languages",
        action="append",
        help=(
            "Additional language codes to try if the primary language is not "
            "available. Can be supplied multiple times."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"any2summary {__CLI_VERSION}",
        help="显示版本信息并退出。",
    )
    parser.add_argument(
        "--azure-streaming",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否以流式模式调用 Azure 转写（默认启用，可用 --no-azure-streaming 关闭）",
    )
    parser.add_argument(
        "--force-azure-diarization",
        action="store_true",
        help="即使字幕可用也强制调用 Azure 说话人分离（文章链接不支持）。",
    )
    parser.add_argument(
        "--azure-summary",
        action="store_true",
        help="调用 Azure GPT-5 对 ASR 结果进行翻译与总结。",
    )
    parser.add_argument(
        "--summary-prompt-file",
        type=str,
        dest="summary_prompt_file",
        help="自定义 Azure 摘要系统 Prompt 的配置文件路径。",
    )
    parser.add_argument(
        "--article-summary-prompt-file",
        type=str,
        dest="article_summary_prompt_file",
        help="针对网页文章的专用 Prompt 文件路径，仅在 --azure-summary 时生效。",
    )
    parser.add_argument(
        "--max-speakers",
        type=int,
        default=None,
        help="Optional upper bound for speaker count during diarization.",
    )
    parser.add_argument(
        "--known-speaker",
        dest="known_speakers",
        action="append",
        help=(
            "Known speaker reference in the form name=path/to/audio.wav. "
            "Can be supplied multiple times to improve diarization labeling."
        ),
    )
    parser.add_argument(
        "--known-speaker-name",
        dest="known_speaker_names",
        action="append",
        help=(
            "Known speaker name without reference audio. Can be supplied "
            "multiple times to hint Azure diarization results."
        ),
    )
    parser.add_argument(
        "--clean-cache",
        action="store_true",
        help="Remove cached artifacts for the provided URL before processing.",
    )

    args = parser.parse_args(argv)

    _load_dotenv_if_present(
        _getenv("ANY2SUMMARY_DOTENV", "PODCAST_TRANSFORMER_DOTENV")
    )

    raw_urls = [item.strip() for item in args.url.split(",") if item and item.strip()]
    if not raw_urls:
        raise RuntimeError("--url 参数不能为空。")

    if len(raw_urls) == 1:
        args.url = raw_urls[0]
        return _run_single(args)

    return _run_multiple(args, raw_urls)


def _run_multiple(args: argparse.Namespace, urls: Sequence[str]) -> int:
    clones = [_clone_args(args, url) for url in urls]
    max_workers = max(1, min(len(clones), os.cpu_count() or len(clones)))

    results: List[Tuple[int, str, int, str, Optional[str]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map: Dict[concurrent.futures.Future[Tuple[int, str, Optional[BaseException]]], Tuple[int, str]] = {}
        for index, clone in enumerate(clones):
            future = executor.submit(_run_single_with_capture, clone)
            future_map[future] = (index, clone.url)

        for future in concurrent.futures.as_completed(future_map):
            index, url = future_map[future]
            try:
                exit_code, output, error = future.result()
            except Exception as exc:  # pragma: no cover - defensive
                exit_code = 1
                output = ""
                error = str(exc)
            error_message = str(error) if error else None
            results.append((index, url, exit_code, output, error_message))

    results.sort(key=lambda item: item[0])
    final_exit_code = 0
    for _, url, exit_code, output, error_message in results:
        if output:
            if output.endswith("\n"):
                sys.stdout.write(output)
            else:
                sys.stdout.write(output + "\n")
        if error_message:
            sys.stderr.write(f"[{url}] {error_message}\n")
        if exit_code != 0:
            final_exit_code = 1

    return final_exit_code


def _run_single_with_capture(args: argparse.Namespace) -> Tuple[int, str, Optional[str]]:
    buffer = io.StringIO()
    error_message: Optional[str] = None
    try:
        with contextlib.redirect_stdout(buffer):
            exit_code = _run_single(args)
    except Exception as exc:  # pragma: no cover - error path
        exit_code = 1
        error_message = str(exc)
    return exit_code, buffer.getvalue(), error_message


def _clone_args(args: argparse.Namespace, url: str) -> argparse.Namespace:
    cloned = copy.deepcopy(args)
    cloned.url = url
    return cloned


def _run_single(args: argparse.Namespace) -> int:
    auto_force_azure = _should_force_azure_transcription(args.url)

    force_azure = bool(args.force_azure_diarization or auto_force_azure)

    if args.summary_prompt_file and not args.azure_summary:
        raise RuntimeError(
            "--summary-prompt-file 仅能与 --azure-summary 搭配使用。"
        )

    if args.article_summary_prompt_file and not args.azure_summary:
        raise RuntimeError(
            "--article-summary-prompt-file 仅能与 --azure-summary 搭配使用。"
        )

    if args.clean_cache:
        cache_directory = _resolve_video_cache_dir(args.url)
        if os.path.isdir(cache_directory):
            shutil.rmtree(cache_directory)

    fallback_languages = args.fallback_languages or [args.language]
    known_speaker_pairs = _parse_known_speakers(args.known_speakers)
    known_speaker_names = args.known_speaker_names or None

    transcript_segments: Optional[
        List[MutableMapping[str, float | str]]
    ] = None
    transcript_error: Optional[RuntimeError] = None
    article_bundle: Optional[Mapping[str, Any]] = None

    try:
        transcript_segments = fetch_transcript_with_metadata(
            video_url=args.url,
            language=args.language,
            fallback_languages=fallback_languages,
        )
    except RuntimeError as exc:
        transcript_error = exc

    if transcript_segments is None and _is_probable_article_url(args.url):
        article_bundle = _maybe_fetch_article_assets(args.url)
        if article_bundle is not None:
            transcript_segments = [
                dict(segment)
                for segment in article_bundle.get("segments", [])
                if isinstance(segment, Mapping)
            ]
            if transcript_segments:
                transcript_error = None
            else:
                article_bundle = None

    diarization_segments: Optional[List[MutableMapping[str, float | str]]] = None
    azure_payload: Optional[MutableMapping[str, Any]] = None

    should_use_azure = False
    if article_bundle is not None:
        should_use_azure = False
    else:
        transcripts_available = bool(transcript_segments)
        has_speaker_constraints = bool(
            args.max_speakers is not None
            or known_speaker_pairs
            or known_speaker_names
        )
        if force_azure:
            should_use_azure = True
        elif not transcripts_available:
            should_use_azure = True
        elif has_speaker_constraints:
            should_use_azure = True

    if should_use_azure:
        try:
            azure_payload = perform_azure_diarization(
                video_url=args.url,
                language=args.language,
                max_speakers=args.max_speakers,
                known_speakers=known_speaker_pairs,
                known_speaker_names=known_speaker_names,
                streaming=args.azure_streaming,
            )
        except RuntimeError:
            if _is_probable_article_url(args.url):
                article_bundle = _maybe_fetch_article_assets(args.url)
            if article_bundle is not None:
                transcript_segments = [
                    dict(segment)
                    for segment in article_bundle.get("segments", [])
                    if isinstance(segment, Mapping)
                ]
                if transcript_segments:
                    transcript_error = None
                    should_use_azure = False
                else:
                    article_bundle = None
            if should_use_azure:
                if transcript_error is not None:
                    raise transcript_error
                raise
        else:
            diarization_segments = azure_payload.get("speakers") or []
            if not transcript_segments:
                transcript_segments = azure_payload.get("transcript")
            if not transcript_segments:
                if transcript_error is not None:
                    raise transcript_error
                raise RuntimeError("Azure OpenAI 未返回可用的转写结果。")

    if (
        not transcript_segments
        and article_bundle is None
        and _is_probable_article_url(args.url)
    ):
        article_bundle = _maybe_fetch_article_assets(args.url)
        if article_bundle is not None:
            transcript_segments = [
                dict(segment)
                for segment in article_bundle.get("segments", [])
                if isinstance(segment, Mapping)
            ]
            if transcript_segments:
                transcript_error = None
            else:
                article_bundle = None

    if not transcript_segments:
        if transcript_error is not None:
            raise transcript_error
        raise RuntimeError(
            "未能获取字幕数据。请确认视频是否启用字幕，或配置 Azure OpenAI 凭据（可搭配 --force-azure-diarization 强制调用 Azure 转写）。"
        )

    merged_segments = merge_segments_with_speakers(
        transcript_segments, diarization_segments
    )

    summary_bundle: Optional[MutableMapping[str, Any]] = None
    summary_paths: Optional[Mapping[str, str]] = None
    article_metadata = article_bundle.get("metadata") if article_bundle else None
    if args.azure_summary:
        custom_prompt: Optional[str] = None
        if article_bundle is not None:
            if args.article_summary_prompt_file:
                custom_prompt = _load_summary_prompt_file(
                    args.article_summary_prompt_file
                )
            else:
                custom_prompt = _load_default_article_prompt()
        elif args.summary_prompt_file:
            custom_prompt = _load_summary_prompt_file(args.summary_prompt_file)
        else:
            custom_prompt = _load_default_summary_prompt()
        summary_bundle = generate_translation_summary(
            merged_segments,
            args.url,
            prompt=custom_prompt,
            metadata=article_metadata,
        )
        summary_paths = _write_summary_documents(
            args.url,
            summary_bundle.get("summary_markdown", ""),
            summary_bundle.get("timeline_markdown", ""),
            summary_bundle.get("file_base", "summary"),
        )

    payload: Union[List[MutableMapping[str, float | str]], MutableMapping[str, Any]]
    if summary_bundle is not None:
        payload = {
            "segments": merged_segments,
            "summary": summary_bundle.get("summary_markdown"),
            "timeline": summary_bundle.get("timeline_markdown"),
            "summary_metadata": summary_bundle.get("metadata"),
        }
        if summary_paths is not None:
            payload["summary_path"] = summary_paths.get("summary")
            payload["timeline_path"] = summary_paths.get("timeline")
            payload["summary_paths"] = summary_paths
        if "total_words" in summary_bundle:
            payload["total_words"] = summary_bundle["total_words"]
        if "estimated_minutes" in summary_bundle:
            payload["estimated_minutes"] = summary_bundle["estimated_minutes"]
    else:
        payload = merged_segments

    if isinstance(payload, MutableMapping) and article_bundle is not None:
        payload["article_metadata"] = article_bundle.get("metadata")
        payload["article_assets"] = {
            "raw_html_path": article_bundle.get("raw_html_path"),
            "content_path": article_bundle.get("content_path"),
            "metadata_path": article_bundle.get("metadata_path"),
            "icon_path": article_bundle.get("icon_path"),
        }

    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")

    return 0


def fetch_transcript_with_metadata(
    video_url: str, language: str, fallback_languages: Iterable[str]
) -> List[MutableMapping[str, float | str]]:
    """Fetch caption segments for a YouTube video.

    Args:
        video_url: URL of the YouTube video.
        language: Preferred language code.
        fallback_languages: Iterable of language codes to try sequentially.

    Returns:
        List of dictionaries containing `start`, `end`, and `text` keys.

    Raises:
        RuntimeError: If captions cannot be retrieved.
    """

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "youtube_transcript_api is required to fetch captions. Install it "
            "via `pip install youtube-transcript-api`."
        ) from exc

    video_id = extract_video_id(video_url)
    if not video_id:
        raise RuntimeError(f"Unable to parse video id from URL: {video_url}")

    language_preferences: List[str] = []
    seen_codes = set()
    for code in [language, *fallback_languages]:
        if code and code not in seen_codes:
            language_preferences.append(code)
            seen_codes.add(code)

    segments = None
    transcript = None
    available_languages: List[str] = []

    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        except Exception as exc:  # pragma: no cover - network failure paths
            raise RuntimeError(f"Failed to list transcripts: {exc}")

        for code in language_preferences:
            try:
                transcript = transcript_list.find_transcript([code])
                break
            except Exception:
                continue

        available_languages = [
            item.language_code for item in transcript_list if hasattr(item, "language_code")
        ]

        if transcript is None and language_preferences:
            target_language = language_preferences[0]
            for candidate in transcript_list:
                try:
                    translated = candidate.translate(target_language)
                    segments = translated.fetch()
                    transcript = translated
                    break
                except Exception:
                    continue

        if transcript is None:
            for candidate in transcript_list:
                try:
                    segments = candidate.fetch()
                    transcript = candidate
                    break
                except Exception:
                    continue

        if transcript is None:
            maybe_plain_segments = []
            for candidate in transcript_list:
                try:
                    maybe_plain_segments = candidate.fetch()
                    transcript = candidate
                    break
                except Exception:
                    continue
            if transcript is not None and maybe_plain_segments:
                segments = maybe_plain_segments

        if segments is None and transcript is not None:
            try:
                segments = transcript.fetch()
            except Exception as exc:  # pragma: no cover - network failure paths
                raise RuntimeError(f"Failed to fetch transcript: {exc}")

        if segments is None:
            message = (
                "No transcript available after attempting preferences: "
                f"{language_preferences}. Available languages: {available_languages}."
            )
            raise RuntimeError(
                message
                + " 请使用 --fallback-language 指定可用语言，或确认视频未限制字幕访问。"
            )
    else:  # pragma: no cover - compatibility path for older versions
        for code in language_preferences:
            try:
                segments = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=[code]
                )
                break
            except Exception:
                continue

        if segments is None:
            raise RuntimeError(
                "No transcript available in requested languages: "
                f"{language_preferences}"
            )

    normalized_segments: List[MutableMapping[str, float | str]] = []
    for segment in segments:
        start = float(segment.get("start", 0.0))
        duration = float(segment.get("duration", 0.0))
        end = start + duration
        normalized_segments.append(
            {
                "start": start,
                "end": end,
                "text": segment.get("text", ""),
            }
        )

    return normalized_segments


def _segments_have_timestamps(
    segments: Sequence[MutableMapping[str, float | str]]
) -> bool:
    """Return True when transcript segments contain usable timeline data."""

    if not segments:
        return False

    for segment in segments:
        start = segment.get("start")
        end = segment.get("end")
        try:
            float(start)  # type: ignore[arg-type]
            float(end)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if float(end) < float(start):
            return False
    return True


def perform_azure_diarization(
    video_url: str,
    language: str,
    max_speakers: Optional[int] = None,
    known_speakers: Optional[List[Tuple[str, str]]] = None,
    known_speaker_names: Optional[Sequence[str]] = None,
    streaming: bool = True,
) -> MutableMapping[str, Any]:
    """Use Azure OpenAI GPT-4o diarization to identify speaker segments.

    Args:
        video_url: Source video URL.
        language: Preferred language code.
        max_speakers: Optional limit on distinct speaker labels.
        known_speakers: Optional list of (name, audio_path) tuples for voice hints.
        known_speaker_names: Optional speaker name hints without audio samples.
        streaming: Whether to enable Azure streaming responses for progress updates.

    Returns:
        A mapping containing `speakers` and `transcript` lists.
    """

    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_api_version = os.getenv("AZURE_OPENAI_API_VERSION") or "2025-03-01-preview"
    deployment = (
        os.getenv("AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT")
        or "gpt-4o-transcribe-diarize"
    )
    if not azure_key or not azure_endpoint:
        raise RuntimeError(
            "Azure OpenAI 凭据缺失。请设置 AZURE_OPENAI_API_KEY与 AZURE_OPENAI_ENDPOINT。"
        )

    cache_directory = _resolve_video_cache_dir(video_url)
    cache_path = _diarization_cache_path(cache_directory)
    cached_payload = _load_cached_diarization(cache_path)
    if cached_payload is not None:
        return cached_payload

    try:
        from openai import AzureOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "openai 库未安装。请执行 `pip install openai`."
        ) from exc

    wav_path = _prepare_audio_cache(video_url)
    segment_paths = _ensure_audio_segments(wav_path)
    if not segment_paths:
        raise RuntimeError(
            "音频缓存文件不存在或生成失败，请确认 ffmpeg 可用。"
        )

    segment_durations: List[float] = []
    for path in segment_paths:
        duration = max(_get_wav_duration(path), 0.0)
        if duration <= 0.0:
            try:
                file_size = os.path.getsize(path)
            except OSError:
                file_size = 0
            if file_size > 0:
                duration = file_size / 32_000.0
        if duration <= 0.0:
            duration = 1.0
        segment_durations.append(duration)

    total_audio_duration = sum(segment_durations)
    if total_audio_duration <= 0.0:
        total_audio_duration = float(len(segment_paths))

    total_estimated_tokens = _estimate_total_tokens(
        segment_paths, segment_durations
    )
    processed_duration = 0.0
    produced_tokens = 0.0
    segments_done = 0
    total_segments = len(segment_paths)

    client = AzureOpenAI(
        api_key=azure_key,
        api_version=azure_api_version,
        azure_endpoint=azure_endpoint,
    )
    openai_module = sys.modules.get("openai")
    bad_request_error: Optional[type[BaseException]]
    if openai_module is not None:
        bad_request_error = getattr(openai_module, "BadRequestError", None)
    else:
        bad_request_error = None

    extra_body = _build_extra_body(known_speakers)
    chunking_strategy_config: Dict[str, Any] = {"type": "auto"}
    request_extra_body: MutableMapping[str, Any] = dict(extra_body)
    request_extra_body["chunking_strategy"] = chunking_strategy_config

    request_known_names: List[str] = []
    extra_names = extra_body.get("known_speaker_names")
    if isinstance(extra_names, list):
        request_known_names.extend(str(name) for name in extra_names)
    if known_speaker_names:
        for name in known_speaker_names:
            if not isinstance(name, str):
                continue
            stripped = name.strip()
            if not stripped:
                continue
            if stripped not in request_known_names:
                request_known_names.append(stripped)

    if total_segments > 0:
        _update_progress_bar(
            0.0,
            _format_progress_detail(
                processed_duration,
                total_audio_duration,
                produced_tokens,
                total_estimated_tokens,
                segments_done,
                total_segments,
            ),
        )

    aggregated_diarization: List[MutableMapping[str, float | str]] = []
    aggregated_transcript: List[MutableMapping[str, float | str]] = []
    segment_offset = 0.0

    for index, segment_path in enumerate(segment_paths, start=1):
        segment_duration = (
            segment_durations[index - 1]
            if 0 <= index - 1 < len(segment_durations)
            else 0.0
        )
        stream_tokens = 0.0

        try:
            with open(segment_path, "rb") as audio_file:
                request_kwargs: MutableMapping[str, Any] = {
                    "model": deployment,
                    "file": audio_file,
                    "response_format": "diarized_json",
                    "language": language,
                    "chunking_strategy": "auto" 
                }
                if request_known_names:
                    request_kwargs["known_speaker_names"] = request_known_names

                def _handle_stream_chunk(payload: MutableMapping[str, Any]) -> None:
                    nonlocal stream_tokens
                    tokens = _extract_usage_tokens(payload)
                    if tokens is None:
                        return
                    stream_tokens = max(stream_tokens, float(tokens))
                    ratio = _compute_progress_ratio(
                        processed_duration,
                        total_audio_duration,
                        produced_tokens + stream_tokens,
                        total_estimated_tokens,
                        segments_done,
                        total_segments,
                    )
                    _update_progress_bar(
                        ratio,
                        _format_progress_detail(
                            processed_duration,
                            total_audio_duration,
                            produced_tokens + stream_tokens,
                            total_estimated_tokens,
                            segments_done,
                            total_segments,
                        ),
                    )

                response = client.audio.transcriptions.create(
                    **request_kwargs, stream=streaming
                )
        except Exception as exc:  # pragma: no cover - depends on API behaviour
            if (
                bad_request_error is not None
                and isinstance(exc, bad_request_error)
            ):
                message = _extract_openai_error_message(exc)
                raise RuntimeError(
                    "Azure OpenAI 调用失败："
                    f"{message}。请尝试使用 --clean-cache 重新生成音频，并确认 ffmpeg 可用。"
                ) from exc
            raise

        response_payload = _consume_transcription_response(
            response,
            on_chunk=_handle_stream_chunk if streaming else None,
        )
        if not streaming:
            ratio = _compute_progress_ratio(
                processed_duration,
                total_audio_duration,
                produced_tokens,
                total_estimated_tokens,
                segments_done,
                total_segments,
            )
            _update_progress_bar(
                ratio,
                _format_progress_detail(
                    processed_duration,
                    total_audio_duration,
                    produced_tokens,
                    total_estimated_tokens,
                    segments_done,
                    total_segments,
                ),
            )
        if _getenv("ANY2SUMMARY_DEBUG_PAYLOAD", "PODCAST_TRANSFORMER_DEBUG_PAYLOAD"):
            debug_name = f"debug_payload_{index}.json"
            debug_path = os.path.join(cache_directory, debug_name)
            try:
                with open(debug_path, "w", encoding="utf-8") as handle:
                    json.dump(response_payload, handle, ensure_ascii=False, indent=2)
            except OSError:
                pass
        diarization_segments = _extract_diarization_segments(response_payload)
        transcript_segments = _extract_transcript_segments(response_payload)

        if not diarization_segments:
            if transcript_segments:
                diarization_segments = [
                    {
                        "start": float(item.get("start", 0.0)),
                        "end": float(item.get("end", item.get("start", 0.0))),
                        "speaker": item.get("speaker", "Unknown"),
                    }
                    for item in transcript_segments
                ]
            else:
                processed_duration = min(
                    total_audio_duration, processed_duration + max(segment_duration, 0.0)
                )
                segments_done += 1
                ratio = _compute_progress_ratio(
                    processed_duration,
                    total_audio_duration,
                    produced_tokens,
                    total_estimated_tokens,
                    segments_done,
                    total_segments,
                )
                _update_progress_bar(
                    ratio,
                    _format_progress_detail(
                        processed_duration,
                        total_audio_duration,
                        produced_tokens,
                        total_estimated_tokens,
                        segments_done,
                        total_segments,
                    ),
                )
                continue

        diarization_with_offset = _offset_segments(diarization_segments, segment_offset)
        transcript_with_offset = _offset_segments(transcript_segments, segment_offset)

        aggregated_diarization.extend(diarization_with_offset)
        aggregated_transcript.extend(transcript_with_offset)

        max_end = _max_segment_end(diarization_with_offset, transcript_with_offset)
        if segment_duration <= 0.0:
            segment_duration = max(_get_wav_duration(segment_path), 0.0)
        if segment_duration > 0:
            segment_offset = max(segment_offset + segment_duration, max_end)
        else:
            segment_offset = max(segment_offset, max_end)

        processed_duration = min(
            total_audio_duration, processed_duration + max(segment_duration, 0.0)
        )
        segment_tokens = _estimate_tokens_from_transcript(transcript_segments)
        if stream_tokens > 0:
            segment_tokens = max(segment_tokens, stream_tokens)
        produced_tokens += segment_tokens
        segments_done += 1

        ratio = _compute_progress_ratio(
            processed_duration,
            total_audio_duration,
            produced_tokens,
            total_estimated_tokens,
            segments_done,
            total_segments,
        )
        _update_progress_bar(
            ratio,
            _format_progress_detail(
                processed_duration,
                total_audio_duration,
                produced_tokens,
                total_estimated_tokens,
                segments_done,
                total_segments,
            ),
        )

    if total_segments > 0:
        final_ratio = _compute_progress_ratio(
            processed_duration,
            total_audio_duration,
            produced_tokens,
            total_estimated_tokens,
            segments_done,
            total_segments,
        )
        if final_ratio < 1.0:
            _update_progress_bar(
                1.0,
                _format_progress_detail(
                    total_audio_duration,
                    total_audio_duration,
                    max(produced_tokens, total_estimated_tokens),
                    total_estimated_tokens,
                    total_segments,
                    total_segments,
                ),
            )

    if not aggregated_diarization:
        if aggregated_transcript:
            aggregated_diarization = [
                {
                    "start": float(item.get("start", 0.0)),
                    "end": float(item.get("end", item.get("start", 0.0))),
                    "speaker": item.get("speaker", "Unknown"),
                }
                for item in aggregated_transcript
            ]
        else:
            empty_payload: MutableMapping[str, Any] = {
                "speakers": [],
                "transcript": [],
            }
            _write_diarization_cache(cache_path, empty_payload)
            return empty_payload

    if max_speakers and max_speakers > 0:
        aggregated_diarization = _limit_speaker_count(
            aggregated_diarization, max_speakers
        )

    aggregated_diarization.sort(key=lambda item: item["start"])
    aggregated_transcript.sort(key=lambda item: item.get("start", 0.0))
    transcript_segments = aggregated_transcript
    diarization_segments = aggregated_diarization
    merged_entries: List[MutableMapping[str, float | str]] = []
    for entry in diarization_segments:
        if not merged_entries:
            merged_entries.append(dict(entry))
            continue
        previous = merged_entries[-1]
        if (
            previous.get("speaker") == entry.get("speaker")
            and abs(float(previous.get("end", 0.0)) - float(entry.get("start", 0.0))) < 0.2
        ):
            previous["end"] = max(
                float(previous.get("end", 0.0)), float(entry.get("end", 0.0))
            )
        else:
            merged_entries.append(dict(entry))

    result_payload: MutableMapping[str, Any] = {
        "speakers": merged_entries,
        "transcript": transcript_segments,
    }
    _write_diarization_cache(cache_path, result_payload)

    return result_payload


def _load_cached_diarization(
    cache_path: str,
) -> Optional[MutableMapping[str, Any]]:
    """Load diarization results from cache when available."""

    if not os.path.exists(cache_path):
        return None

    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, MutableMapping):
        return None

    speakers = payload.get("speakers")
    transcript = payload.get("transcript")
    if not isinstance(speakers, list) or not isinstance(transcript, list):
        return None

    return payload


def _write_diarization_cache(
    cache_path: str, payload: MutableMapping[str, Any]
) -> None:
    """Persist diarization payload to cache, ignoring failures."""

    directory = os.path.dirname(cache_path)
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=directory,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False)
            temp_path = handle.name
        os.replace(temp_path, cache_path)
    except OSError:
        try:
            if "temp_path" in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass


def _coerce_response_to_dict(response: object) -> MutableMapping[str, Any]:
    """Convert Azure OpenAI response to a dictionary."""

    if response is None:
        return {}
    if isinstance(response, MutableMapping):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()  # type: ignore[return-value]
    if hasattr(response, "to_dict"):
        return response.to_dict()  # type: ignore[call-arg]
    if hasattr(response, "__dict__"):
        return {
            key: value
            for key, value in response.__dict__.items()
            if not key.startswith("_")
        }
    return {}


def _iter_nested_mappings(
    payload: Mapping[str, Any]
) -> Iterator[MutableMapping[str, Any]]:
    """Yield nested mappings discovered within the payload."""

    if not isinstance(payload, MutableMapping):
        return

    stack: List[MutableMapping[str, Any]] = [payload]
    seen_ids: set[int] = set()

    while stack:
        current = stack.pop()
        identifier = id(current)
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        yield current
        for value in current.values():
            if isinstance(value, MutableMapping):
                stack.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, MutableMapping):
                        stack.append(item)


_ISO_8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def _parse_time_string(value: str) -> Optional[float]:
    """Parse textual time representation into seconds."""

    text = value.strip()
    if not text:
        return None

    lower = text.lower()

    match = _ISO_8601_DURATION_RE.match(text)
    if match:
        total = 0.0
        days = match.group("days")
        hours = match.group("hours")
        minutes = match.group("minutes")
        seconds = match.group("seconds")
        if days:
            total += float(days) * 86_400.0
        if hours:
            total += float(hours) * 3_600.0
        if minutes:
            total += float(minutes) * 60.0
        if seconds:
            total += float(seconds)
        return total if total > 0.0 else 0.0

    if ":" in text:
        parts = text.split(":")
        try:
            parts = [float(part) for part in parts]
        except ValueError:
            return None
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60.0 + part
        return seconds

    if lower.endswith("ms"):
        numeric = lower[:-2].strip()
        try:
            return float(numeric) / 1000.0
        except ValueError:
            return None
    if lower.endswith("s") and lower[-2:] != "ms":
        numeric = lower[:-1].strip()
        try:
            return float(numeric)
        except ValueError:
            return None

    try:
        return float(text)
    except ValueError:
        return None


def _coerce_time_value(value: Any, key_hint: Optional[str] = None) -> Optional[float]:
    """Convert a raw value into seconds, respecting millisecond hints."""

    if isinstance(value, (list, tuple, set)):
        for item in value:
            coerced = _coerce_time_value(item, key_hint)
            if coerced is not None:
                return coerced
        return None

    if isinstance(value, Mapping):
        seconds_component = None
        nanos_component = None

        if "seconds" in value:
            seconds_component = _coerce_time_value(value.get("seconds"), "seconds")
        if "nanos" in value:
            nanos_component = _coerce_time_value(value.get("nanos"), "nanos")
        if "nanoSeconds" in value:
            nanos_component = _coerce_time_value(value.get("nanoSeconds"), "nanos")
        if seconds_component is not None or nanos_component is not None:
            total = seconds_component or 0.0
            if nanos_component is not None:
                total += nanos_component / 1_000_000_000
            return total

        units = value.get("unit") or value.get("units")
        unit_value = value.get("value")
        if unit_value is not None:
            numeric = _coerce_time_value(unit_value, units or key_hint)
            if numeric is not None:
                if units:
                    lowered = str(units).lower()
                    if "ms" in lowered or "millis" in lowered:
                        return numeric / 1000.0
                return numeric

        candidate_keys = (
            "start",
            "end",
            "offset",
            "offset_seconds",
            "offsetSeconds",
            "offset_ms",
            "offsetMs",
            "offset_millis",
            "offsetMillis",
            "time",
            "time_seconds",
            "timeMillis",
            "time_ms",
            "milliseconds",
            "millis",
            "ms",
        )
        for candidate in candidate_keys:
            if candidate in value:
                nested = _coerce_time_value(value.get(candidate), candidate)
                if nested is not None:
                    return nested

        # Explore nested dictionaries to find any numeric field.
        for nested_value in value.values():
            nested = _coerce_time_value(nested_value, key_hint)
            if nested is not None:
                return nested

        return None

    if isinstance(value, str):
        return _parse_time_string(value)

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if key_hint:
        lower = str(key_hint).lower()
        if "nano" in lower:
            return numeric / 1_000_000_000
        if "ms" in lower or "millis" in lower:
            numeric /= 1000.0

    return _to_seconds(numeric)


def _find_time_value(obj: Any, keys: Sequence[str], visited: Optional[set[int]] = None) -> Optional[float]:
    """Recursively search for a time value using candidate keys."""

    if visited is None:
        visited = set()

    if isinstance(obj, Mapping):
        identifier = id(obj)
        if identifier in visited:
            return None
        visited.add(identifier)

        for key in keys:
            if key not in obj:
                continue
            value = obj[key]
            if isinstance(value, Mapping):
                nested = _find_time_value(value, keys, visited)
                if nested is not None:
                    return nested
            else:
                coerced = _coerce_time_value(value, key)
                if coerced is not None:
                    return coerced

        for container_key in _TIME_CONTAINER_KEYS:
            value = obj.get(container_key)
            if value is None:
                continue
            nested = _find_time_value(value, keys, visited)
            if nested is not None:
                return nested

    elif isinstance(obj, list):
        for item in obj:
            nested = _find_time_value(item, keys, visited)
            if nested is not None:
                return nested

    return None


def _extract_time_range(segment: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Extract start and end seconds from a diarization/transcript segment."""

    start = _find_time_value(segment, _TIME_START_KEYS)
    end = _find_time_value(segment, _TIME_END_KEYS)

    if start is not None and end is None:
        duration = _find_time_value(segment, _TIME_DURATION_KEYS)
        if duration is not None:
            end = start + duration

    if start is None and end is not None:
        duration = _find_time_value(segment, _TIME_DURATION_KEYS)
        if duration is not None:
            start = end - duration

    if start is None:
        duration = _find_time_value(segment, _TIME_DURATION_KEYS)
        if duration is not None:
            start = 0.0
            end = duration if end is None else end

    if start is not None and end is not None and end < start:
        end = start

    return start, end


def _extract_speaker_label(segment: Mapping[str, Any]) -> Optional[str]:
    """Derive speaker label from various field aliases."""

    for key in _SPEAKER_KEYS:
        if key not in segment:
            continue
        value = segment.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        elif isinstance(value, Mapping):
            nested = _extract_speaker_label(value)
            if nested:
                return nested

    speaker_info = segment.get("speaker_info")
    if isinstance(speaker_info, Mapping):
        nested = _extract_speaker_label(speaker_info)
        if nested:
            return nested

    for alias in ("info", "metadata", "details", "properties"):
        nested_container = segment.get(alias)
        if isinstance(nested_container, Mapping):
            nested = _extract_speaker_label(nested_container)
            if nested:
                return nested
        elif isinstance(nested_container, list):
            for item in nested_container:
                if isinstance(item, Mapping):
                    nested = _extract_speaker_label(item)
                    if nested:
                        return nested

    return None


def _extract_text_value(obj: Any, visited: Optional[set[int]] = None) -> Optional[str]:
    """Collect textual content from nested structures."""

    if visited is None:
        visited = set()

    if isinstance(obj, str):
        stripped = obj.strip()
        return stripped or None

    if isinstance(obj, Mapping):
        identifier = id(obj)
        if identifier in visited:
            return None
        visited.add(identifier)

        for key in _TEXT_VALUE_KEYS:
            if key not in obj:
                continue
            candidate = _extract_text_value(obj[key], visited)
            if candidate:
                return candidate

        for key in _TEXT_COLLECTION_KEYS:
            if key not in obj:
                continue
            candidate = _extract_text_value(obj[key], visited)
            if candidate:
                return candidate

    if isinstance(obj, list):
        pieces: List[str] = []
        for item in obj:
            candidate = _extract_text_value(item, visited)
            if candidate:
                pieces.append(candidate)
        if pieces:
            return " ".join(pieces).strip()

    return None


def _extract_diarization_segments(
    payload: MutableMapping[str, Any]
) -> List[MutableMapping[str, float | str]]:
    """Extract diarization segments from verbose JSON payload."""

    normalized: List[MutableMapping[str, float | str]] = []
    seen = set()

    for mapping in _iter_nested_mappings(payload):
        segment = _normalize_segment_entry(mapping)
        if segment is None:
            continue
        fingerprint = (
            round(segment.get("start", 0.0), 3),
            round(segment.get("end", 0.0), 3),
            segment.get("speaker"),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(segment)

    if normalized:
        return normalized

    return []


def _extract_transcript_segments(
    payload: MutableMapping[str, Any]
) -> List[MutableMapping[str, float | str]]:
    """Extract transcript segments with text from payload."""

    transcript_segments: List[MutableMapping[str, float | str]] = []
    seen = set()

    for mapping in _iter_nested_mappings(payload):
        normalized = _normalize_transcript_entry(mapping)
        if normalized is None:
            continue
        fingerprint = (
            round(normalized.get("start", 0.0), 3),
            round(normalized.get("end", 0.0), 3),
            normalized.get("text"),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        transcript_segments.append(normalized)

    transcript_segments.sort(key=lambda item: item.get("start", 0.0))
    return transcript_segments


def _normalize_segment_entry(
    segment: MutableMapping[str, Any]
) -> Optional[MutableMapping[str, float | str]]:
    """Normalize a diarization segment into start/end/speaker fields."""

    if not isinstance(segment, Mapping):
        return None

    start, end = _extract_time_range(segment)
    if start is None:
        return None

    if end is None:
        end = start

    speaker = _extract_speaker_label(segment)
    if speaker is None:
        return None

    return {
        "start": float(start),
        "end": float(end),
        "speaker": str(speaker),
    }


def _normalize_transcript_entry(
    segment: MutableMapping[str, Any]
) -> Optional[MutableMapping[str, float | str]]:
    """Normalize transcript segment ensuring text exists."""

    if not isinstance(segment, Mapping):
        return None

    text = _extract_text_value(segment)
    if not text:
        return None

    start, end = _extract_time_range(segment)
    if start is None:
        return None

    if end is None:
        end = start

    entry: MutableMapping[str, float | str] = {
        "start": float(start),
        "end": float(end),
        "text": text.strip(),
    }

    speaker = _extract_speaker_label(segment)
    if speaker is not None:
        entry["speaker"] = str(speaker)

    return entry


def _to_seconds(value: Any) -> float:
    """Convert a value that may be in seconds or ticks to seconds."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0.0

    if numeric > 1_000_000:  # heuristically treat as 100-ns ticks
        return numeric / 10_000_000
    return numeric


def _limit_speaker_count(
    segments: List[MutableMapping[str, float | str]], max_speakers: int
) -> List[MutableMapping[str, float | str]]:
    """Remap segments to ensure the number of speakers does not exceed limit."""

    identified = [
        segment.get("speaker")
        for segment in segments
        if isinstance(segment.get("speaker"), str)
    ]
    unique_speakers = {speaker for speaker in identified if speaker is not None}
    if len(unique_speakers) <= max_speakers:
        return segments

    durations: defaultdict[str, float] = defaultdict(float)
    for segment in segments:
        speaker = segment.get("speaker")
        if not isinstance(speaker, str):
            continue
        duration = max(
            0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0))
        )
        durations[speaker] += duration

    ranked_speakers = [
        speaker for speaker, _ in sorted(durations.items(), key=lambda item: item[1], reverse=True)
    ]
    allowed = ranked_speakers[:max_speakers]
    if not allowed:
        return segments

    totals = {speaker: durations.get(speaker, 0.0) for speaker in allowed}
    mapping: MutableMapping[str, str] = {}
    remapped: List[MutableMapping[str, float | str]] = []

    for segment in segments:
        speaker = segment.get("speaker")
        if speaker is None or speaker in allowed:
            remapped.append(segment)
            continue
        if speaker not in mapping:
            target = min(allowed, key=lambda value: totals.get(value, 0.0))
            mapping[speaker] = target
        target = mapping[speaker]
        updated = dict(segment)
        updated["speaker"] = target
        duration = max(
            0.0, float(updated.get("end", 0.0)) - float(updated.get("start", 0.0))
        )
        totals[target] = totals.get(target, 0.0) + duration
        remapped.append(updated)

    return remapped



def _extract_openai_error_message(exc: Exception) -> str:
    """Extract a user-friendly message from an OpenAI exception."""

    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    text = str(exc)
    return text.strip() or "Unknown Azure OpenAI error"


def _load_summary_prompt_file(path: str) -> str:
    """Load custom summary prompt content from a file."""

    absolute_path = os.path.abspath(path)
    if not os.path.isfile(absolute_path):
        raise RuntimeError(f"摘要 Prompt 配置文件不存在：{path}")

    try:
        with open(absolute_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise RuntimeError(f"读取摘要 Prompt 配置文件失败：{path}") from exc

    if not content.strip():
        raise RuntimeError(f"摘要 Prompt 配置文件内容为空：{path}")

    return content


def _load_prompt_with_fallback(path: Path | str, fallback: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return fallback


def _load_default_summary_prompt() -> str:
    return _load_prompt_with_fallback(DEFAULT_SUMMARY_PROMPT_PATH, SUMMARY_PROMPT)


def _load_default_article_prompt() -> str:
    return _load_prompt_with_fallback(
        DEFAULT_ARTICLE_PROMPT_PATH, ARTICLE_SUMMARY_PROMPT
    )


def generate_translation_summary(
    segments: Sequence[MutableMapping[str, Any]],
    video_url: str,
    prompt: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> MutableMapping[str, Any]:
    """Call Azure GPT-5 to translate and summarize ASR segments."""

    if not segments:
        raise RuntimeError("无法生成翻译摘要：缺少 ASR 结果。")

    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not azure_key or not azure_endpoint:
        raise RuntimeError(
            "Azure OpenAI 凭据缺失。请设置 AZURE_OPENAI_API_KEY与 AZURE_OPENAI_ENDPOINT。"
        )

    deployment = os.getenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT") or "llab-gpt-5-pro"

    instruction = prompt or _load_default_summary_prompt()
    timeline = _format_segments_for_summary(segments)
    user_message = "原始 ASR 片段如下：\n" + timeline

    use_responses = deployment.endswith("-pro") or str(
        os.getenv("AZURE_OPENAI_USE_RESPONSES", "")
    ).lower() in {"1", "true", "yes"}

    if use_responses:
        base_url = os.getenv("AZURE_OPENAI_RESPONSES_BASE_URL")
        if not base_url:
            base_url = _build_responses_base_url(azure_endpoint)

        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "openai 库未安装。请执行 `pip install openai`."
            ) from exc

        client = OpenAI(base_url=base_url.rstrip("/"), api_key=azure_key)
        response = client.responses.create(
            model=deployment,
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": instruction}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_message}],
                },
            ],
            max_output_tokens=16384,
        )
        raw_summary = _extract_responses_text(response)
    else:
        summary_api_version = (
            os.getenv("AZURE_OPENAI_SUMMARY_API_VERSION") or "2025-01-01-preview"
        )

        try:
            from openai import AzureOpenAI
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "openai 库未安装。请执行 `pip install openai`."
            ) from exc

        client = AzureOpenAI(
            api_key=azure_key,
            api_version=summary_api_version,
            azure_endpoint=azure_endpoint,
        )

        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": user_message},
            ],
            max_completion_tokens=16384,
        )
        raw_summary = _extract_summary_text(response)

    if metadata is not None:
        video_metadata = dict(metadata)
    else:
        video_metadata = _fetch_video_metadata(video_url)
    return _compose_summary_documents(segments, raw_summary, video_metadata, video_url)


def _format_segments_for_summary(
    segments: Sequence[MutableMapping[str, Any]]
) -> str:
    """Format segments into timeline text for summarization prompt."""

    lines = []
    for segment in sorted(
        segments, key=lambda item: float(item.get("start", 0.0))
    ):
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        start = _format_timestamp(segment.get("start", 0.0))
        end = _format_timestamp(segment.get("end", segment.get("start", 0.0)))
        speaker = segment.get("speaker")
        if isinstance(speaker, str) and speaker.strip():
            line = f"{start} - {end} | {speaker.strip()}: {text.strip()}"
        else:
            line = f"{start} - {end} | {text.strip()}"
        lines.append(line)

    if not lines:
        raise RuntimeError("无法生成翻译摘要：ASR 结果中缺少有效文本。")

    return "\n".join(lines)


def _normalize_domain_tag(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""

    first_line = text.splitlines()[0].strip()
    first_line = re.sub(r"^[\s\-\*\u2022\d\.、]+", "", first_line)

    if ":" in first_line or "：" in first_line:
        parts = re.split(r"[:：]", first_line, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            first_line = parts[1].strip()

    for separator in ("，", ",", "。", ";", "；"):
        if separator in first_line:
            first_line = first_line.split(separator, 1)[0].strip()

    if " " in first_line:
        first_line = first_line.split(" ", 1)[0].strip()

    return first_line[:32].strip()


def _infer_domain_from_summary(raw_summary: str) -> Optional[str]:
    text = str(raw_summary or "").strip()
    if not text:
        return None

    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not azure_key or not azure_endpoint:
        return None

    deployment = (
        os.getenv("AZURE_OPENAI_DOMAIN_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT")
        or "llab-gpt-5-pro"
    )

    use_responses = deployment.endswith("-pro") or str(
        os.getenv("AZURE_OPENAI_USE_RESPONSES", "")
    ).lower() in {"1", "true", "yes"}

    try:
        if use_responses:
            base_url = os.getenv("AZURE_OPENAI_RESPONSES_BASE_URL")
            if not base_url:
                base_url = _build_responses_base_url(azure_endpoint)

            from openai import OpenAI

            client = OpenAI(base_url=base_url.rstrip("/"), api_key=azure_key)
            response = client.responses.create(
                model=deployment,
                input=[
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": DOMAIN_PROMPT}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                ],
                max_output_tokens=1024,
            )
            content = _extract_responses_text(response)
        else:
            summary_api_version = (
                os.getenv("AZURE_OPENAI_SUMMARY_API_VERSION") or "2025-01-01-preview"
            )

            from openai import AzureOpenAI

            client = AzureOpenAI(
                api_key=azure_key,
                api_version=summary_api_version,
                azure_endpoint=azure_endpoint,
            )
            response = client.chat.completions.create(
                model=deployment,
                messages=[
                    {"role": "system", "content": DOMAIN_PROMPT},
                    {"role": "user", "content": text},
                ],
                max_completion_tokens=1024,
            )
            content = _extract_summary_text(response)
    except Exception:  # pragma: no cover - depends on runtime environment
        return None

    candidate = _normalize_domain_tag(content)
    if not candidate:
        return None
    return candidate


def _compose_summary_documents(
    segments: Sequence[MutableMapping[str, Any]],
    raw_summary: str,
    video_metadata: Mapping[str, Any],
    video_url: str,
) -> MutableMapping[str, Any]:
    if not raw_summary or not raw_summary.strip():
        raise RuntimeError("Azure GPT-5 摘要结果为空。")

    sorted_segments = sorted(
        (dict(segment) for segment in segments),
        key=lambda item: float(item.get("start", 0.0)),
    )

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if sorted_segments:
        starts = [float(item.get("start", 0.0)) for item in sorted_segments]
        ends = [float(item.get("end", item.get("start", 0.0))) for item in sorted_segments]
        earliest = min(starts)
        latest = max(ends)
        total_duration = max(0.0, latest - earliest)
    else:
        total_duration = 0.0

    formatted_duration = _format_timestamp(total_duration)
    speakers = sorted(
        {
            str(item.get("speaker", "")).strip()
            for item in sorted_segments
            if str(item.get("speaker", "")).strip()
        }
    )

    title_raw = re.sub(r'[:\/\\`]', '', str(video_metadata.get("title") or ""))
    title = str(title_raw or "未知标题").strip() or "未知标题"
    publish_date_raw = str(video_metadata.get("upload_date") or "").strip()
    publish_date = _format_publish_date(publish_date_raw)

    source_url = str(video_metadata.get("webpage_url") or video_url)
    domain = _extract_domain(video_metadata)
    if domain == "通用":
        generated_domain = _infer_domain_from_summary(raw_summary)
        if generated_domain:
            domain = generated_domain

    total_words = _count_words(raw_summary)
    estimated_minutes = max(1, math.ceil(total_words / READING_WORDS_PER_MINUTE))

    year, month = _derive_year_month(publish_date_raw, generated_at)
    year_month_code = f"{year}-M{month}"
    heading = f"【{domain}】{title}-{year_month_code}"
    file_base = _sanitize_filename_base(heading)

    summary_lines: List[str] = []
    summary_lines.append(f"# {heading}")
    summary_lines.append("")
    summary_lines.append("## 封面")
    summary_lines.append(f"- 标题：{title}")
    summary_lines.append(f"- 链接：{source_url}")
    summary_lines.append(f"- 发布日期：{publish_date}")
    summary_lines.append(f"- 总字数：{total_words}")
    summary_lines.append(f"- 预估阅读时长：约 {estimated_minutes} 分钟")
    summary_lines.append(f"- 生成时间：{generated_at}")
    summary_lines.append(f"- 覆盖时长：{formatted_duration}")

    summary_lines.append("")
    summary_lines.append(raw_summary.strip())
    summary_lines.append("")
    summary_lines.extend(_build_exchange_footer())

    timeline_lines: List[str] = []
    timeline_lines.append(f"# {heading}")
    timeline_lines.append("")
    timeline_lines.append("## 封面")
    timeline_lines.append(f"- 标题：{title}")
    timeline_lines.append(f"- 链接：{source_url}")
    timeline_lines.append(f"- 发布日期：{publish_date}")
    timeline_lines.append(f"- 总字数：{total_words}")
    timeline_lines.append(f"- 预估阅读时长：约 {estimated_minutes} 分钟")
    timeline_lines.append(f"- 生成时间：{generated_at}")
    timeline_lines.append(f"- 覆盖时长：{formatted_duration}")
    if speakers:
        timeline_lines.append(f"- 识别说话人：{', '.join(speakers)}")

    timeline_lines.append("")
    timeline_lines.append("## 时间轴")
    timeline_lines.append("| 序号 | 起始 | 结束 | 时长 | 说话人 | 文本 |")
    timeline_lines.append("| --- | --- | --- | --- | --- | --- |")

    for index, segment in enumerate(sorted_segments, start=1):
        start_seconds = float(segment.get("start", 0.0))
        end_seconds = float(segment.get("end", start_seconds))
        duration_seconds = max(0.0, end_seconds - start_seconds)
        speaker = str(segment.get("speaker", "")).strip() or "-"
        text = str(segment.get("text", ""))
        cell_text = _sanitize_markdown_cell(text)
        timeline_lines.append(
            "| {idx} | {start} | {end} | {duration} | {speaker} | {text} |".format(
                idx=index,
                start=_format_timestamp(start_seconds),
                end=_format_timestamp(end_seconds),
                duration=_format_timestamp(duration_seconds),
                speaker=_sanitize_markdown_cell(speaker),
                text=cell_text,
            )
        )

    metadata = {
        "generated_at": generated_at,
        "title": title,
        "url": source_url,
        "publish_date": publish_date,
        "total_words": total_words,
        "estimated_minutes": estimated_minutes,
        "duration": formatted_duration,
        "speakers": speakers,
        "domain": domain,
        "year": year,
        "month": month,
    }

    return {
        "summary_markdown": "\n".join(summary_lines),
        "timeline_markdown": "\n".join(timeline_lines),
        "metadata": metadata,
        "total_words": total_words,
        "estimated_minutes": estimated_minutes,
        "file_base": file_base,
        "heading": heading,
    }


def _build_exchange_footer() -> List[str]:
    weekday_map = {
        0: "周一",
        1: "周二",
        2: "周三",
        3: "周四",
        4: "周五",
        5: "周六",
        6: "周日",
    }

    now = datetime.now()
    zh_weekday = weekday_map.get(now.weekday(), "周?")
    formatted_date = f"{now.strftime('%Y-%m-%d')}_{zh_weekday}"

    return [
        "## 欢迎交流与合作",
        "目前主要兴趣是探索agent的落地，想进一步交流可加微信（cleezhang），一些[自我介绍](https://lee-agi.github.io/85ed64eda0/)。",
        "",
        f"> 本文发表于 {formatted_date}。",
    ]


def _build_responses_base_url(endpoint: str) -> str:
    normalized = endpoint.rstrip("/")
    if normalized.endswith("/openai/v1"):
        return normalized
    return f"{normalized}/openai/v1"


def _extract_responses_text(response: object) -> str:
    candidate = getattr(response, "output_text", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()

    data = None
    if isinstance(response, MutableMapping):
        data = response.get("output") or response.get("data") or response.get("choices")
    else:
        data = getattr(response, "output", None) or getattr(response, "data", None)

    texts: List[str] = []

    if isinstance(data, list):
        for item in data:
            content = None
            if isinstance(item, MutableMapping):
                content = item.get("content")
            else:
                content = getattr(item, "content", None)

            if isinstance(content, list):
                for block in content:
                    text_value = None
                    if isinstance(block, MutableMapping):
                        text_value = block.get("text")
                    else:
                        text_value = getattr(block, "text", None)
                    if isinstance(text_value, str) and text_value.strip():
                        texts.append(text_value.strip())
            elif isinstance(content, str) and content.strip():
                texts.append(content.strip())

    if texts:
        return "\n".join(texts).strip()

    raise RuntimeError("Azure GPT-5 未返回可用的摘要结果。")


def _sanitize_markdown_cell(value: str) -> str:
    sanitized = value.replace("|", "\\|").replace("\n", " ").strip()
    return sanitized or "-"


def _extract_summary_text(response: object) -> str:
    """Extract summary content from Azure chat completion response."""

    choices = None
    if isinstance(response, MutableMapping):
        choices = response.get("choices")
    elif hasattr(response, "choices"):
        choices = getattr(response, "choices")

    if not isinstance(choices, list) or not choices:
        raise RuntimeError("Azure GPT-5 未返回可用的摘要结果。")

    first = choices[0]
    if isinstance(first, MutableMapping):
        message = first.get("message")
    else:
        message = getattr(first, "message", None)

    if isinstance(message, MutableMapping):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)

    if isinstance(content, str) and content.strip():
        return content.strip()

    raise RuntimeError("Azure GPT-5 摘要结果为空。")


def _write_summary_documents(
    video_url: str,
    summary_markdown: str,
    timeline_markdown: str,
    file_base: str,
) -> Mapping[str, str]:
    if not summary_markdown or not summary_markdown.strip():
        raise RuntimeError("无法写入空的摘要 Markdown 内容。")
    if not timeline_markdown or not timeline_markdown.strip():
        raise RuntimeError("无法写入空的时间轴 Markdown 内容。")

    directory = _resolve_video_cache_dir(video_url)
    summary_filename = f"{file_base}_summary.md"
    timeline_filename = f"{file_base}_timeline.md"
    summary_path = _ensure_unique_markdown_path(directory, summary_filename)
    timeline_path = _ensure_unique_markdown_path(directory, timeline_filename)

    try:
        with open(summary_path, "w", encoding="utf-8") as summary_file:
            summary_file.write(summary_markdown)
        with open(timeline_path, "w", encoding="utf-8") as timeline_file:
            timeline_file.write(timeline_markdown)
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise RuntimeError(
            f"写入摘要/时间轴 Markdown 文件失败：{summary_path}, {timeline_path}"
        ) from exc

    result: Dict[str, str] = {
        "summary": summary_path,
        "timeline": timeline_path,
    }

    outbox_summary = _copy_file_to_outbox(summary_path)
    if outbox_summary is not None:
        result["outbox_summary"] = outbox_summary

    return result


def _ensure_unique_markdown_path(directory: str, filename: str) -> str:
    base_path = Path(directory)
    base_path.mkdir(parents=True, exist_ok=True)
    candidate = base_path / filename
    if not candidate.exists():
        return str(candidate)

    stem = candidate.stem
    suffix = candidate.suffix or ".md"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    attempt = 1
    while True:
        if attempt == 1:
            new_name = f"{stem}_{timestamp}{suffix}"
        else:
            new_name = f"{stem}_{timestamp}_{attempt}{suffix}"
        candidate = base_path / new_name
        if not candidate.exists():
            return str(candidate)
        attempt += 1


def _copy_file_to_outbox(source_path: str) -> Optional[str]:
    """Copy a generated file to the configured outbox directory."""

    outbox_dir = _getenv("ANY2SUMMARY_OUTBOX_DIR", "PODCAST_TRANSFORMER_OUTBOX_DIR")
    if not outbox_dir:
        outbox_dir = DEFAULT_OUTBOX_DIR
    if not outbox_dir:
        return None

    try:
        resolved_dir = os.path.expanduser(outbox_dir)
        os.makedirs(resolved_dir, exist_ok=True)
        target_path = os.path.join(resolved_dir, os.path.basename(source_path))
        shutil.copyfile(source_path, target_path)
        return target_path
    except OSError:
        return None


def _fetch_video_metadata(video_url: str) -> Mapping[str, Any]:
    metadata: Dict[str, Any] = {
        "title": None,
        "webpage_url": video_url,
        "upload_date": "",
    }

    try:
        import yt_dlp
    except ModuleNotFoundError:
        return metadata

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "nocheckcertificate": True,
    }

    try:  # pragma: no cover - depends on network availability
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception:
        return metadata

    if isinstance(info, Mapping):
        for key in ("title", "webpage_url", "upload_date"):
            if key in info:
                metadata[key] = info.get(key)

    return metadata


def _count_words(text: str) -> int:
    if not isinstance(text, str):
        return 0

    stripped = text.strip()
    if not stripped:
        return 0

    latin_tokens = re.findall(r"[A-Za-z0-9]+", stripped)
    chinese_tokens = re.findall(r"[\u4e00-\u9fff]", stripped)

    total = len(latin_tokens) + len(chinese_tokens)
    if total == 0:
        compact = re.sub(r"\s", "", stripped)
        total = len(compact)

    return max(total, 0)


def _format_timestamp(value: Any) -> str:
    """Format numeric seconds into HH:MM:SS.mmm."""

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 0.0

    if seconds < 0:
        seconds = 0.0

    total_milliseconds = int(round(seconds * 1000))
    hours = total_milliseconds // 3_600_000
    minutes = (total_milliseconds // 60_000) % 60
    secs = (total_milliseconds // 1000) % 60
    milliseconds = total_milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_publish_date(raw: str) -> str:
    if raw and len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    if raw and re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return "未知日期"


def _derive_year_month(publish_date_raw: str, generated_at: str) -> Tuple[str, str]:
    if publish_date_raw and publish_date_raw.isdigit() and len(publish_date_raw) == 8:
        return publish_date_raw[:4], publish_date_raw[4:6]

    match = re.match(r"(\d{4})-(\d{2})", publish_date_raw)
    if match:
        return match.group(1), match.group(2)

    if re.match(r"\d{4}-\d{2}-\d{2}", generated_at):
        return generated_at[:4], generated_at[5:7]

    return "1970", "01"


def _extract_domain(video_metadata: Mapping[str, Any]) -> str:
    domain = ""
    candidates = []
    categories = video_metadata.get("categories")
    if isinstance(categories, list):
        candidates.extend(categories)
    category = video_metadata.get("category")
    if category is not None:
        candidates.append(category)
    tags = video_metadata.get("tags")
    if isinstance(tags, list):
        candidates.extend(tags)

    for candidate in candidates:
        if isinstance(candidate, str):
            name = candidate.strip()
            if name:
                domain = name
                break

    domain = domain or "通用"
    return domain


def _sanitize_filename_base(text: str) -> str:
    sanitized = re.sub(r"[\\/:*?\"<>|]", "", text)
    sanitized = sanitized.replace(" ", "")
    return sanitized or "summary"


def _build_extra_body(
    known_speakers: Optional[List[Tuple[str, str]]]
) -> MutableMapping[str, Any]:
    if not known_speakers:
        return {}

    names: List[str] = []
    references: List[str] = []

    for name, path in known_speakers:
        data_url = _to_data_url(path)
        names.append(name)
        references.append(data_url)

    return {
        "known_speaker_names": names,
        "known_speaker_references": references,
    }


def _to_data_url(path: str) -> str:
    if not os.path.exists(path):
        raise RuntimeError(f"Known speaker reference file not found: {path}")
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("utf-8")
    return "data:audio/wav;base64," + encoded


def _parse_known_speakers(
    raw_values: Optional[Iterable[str]]
) -> Optional[List[Tuple[str, str]]]:
    if not raw_values:
        return None

    parsed: List[Tuple[str, str]] = []
    for item in raw_values:
        if not item:
            continue
        if "=" not in item:
            raise RuntimeError(
                f"Known speaker entry '{item}' must follow name=path format."
            )
        name, path = item.split("=", 1)
        name = name.strip()
        path = path.strip()
        if not name or not path:
            raise RuntimeError(
                f"Known speaker entry '{item}' is invalid; name/path cannot be empty."
            )
        parsed.append((name, path))
    return parsed or None


def _resolve_video_cache_dir(video_url: str) -> str:
    """Resolve the cache directory for a given video URL."""

    parsed_url = urlparse(video_url)
    hostname = (parsed_url.hostname or "unknown-host").lower()
    host_segment = hostname.replace(".", "_") or "unknown-host"

    video_id = extract_video_id(video_url)
    if video_id:
        cache_key = os.path.join("youtube", video_id)
    else:
        hasher = hashlib.sha256()
        hasher.update(video_url.strip().encode("utf-8"))
        digest = hasher.hexdigest()[:16]
        cache_key = os.path.join(host_segment, digest)

    base_dir = _getenv("ANY2SUMMARY_CACHE_DIR", "PODCAST_TRANSFORMER_CACHE_DIR")
    if base_dir:
        cache_base = base_dir
    else:
        home_dir = os.path.expanduser("~")
        cache_base = os.path.join(home_dir, ".cache", "any2summary")

    video_dir = os.path.join(cache_base, cache_key)
    os.makedirs(video_dir, exist_ok=True)
    return video_dir


def _create_http_client():
    """Return an httpx.Client instance for fetching web content."""

    try:
        import httpx
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError("httpx 库未安装。请执行 `pip install httpx`." ) from exc

    return httpx.Client(follow_redirects=True, timeout=30.0)


def _normalize_article_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "")
    return normalized.strip()


class _ArticleHTMLParser(HTMLParser):
    """Lightweight HTML parser to extract article metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: List[str] = []
        self.in_heading = False
        self.heading_parts: List[str] = []
        self.current_paragraph_tag: Optional[str] = None
        self.current_paragraph_parts: List[str] = []
        self.paragraphs: List[str] = []
        self.description: Optional[str] = None
        self.icon_href: Optional[str] = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return

        if tag_lower == "title":
            self.in_title = True
            return

        if tag_lower in {"p", "li"}:
            if self.current_paragraph_tag is None:
                self.current_paragraph_tag = tag_lower
                self.current_paragraph_parts = []
            return

        if tag_lower == "h1":
            self.in_heading = True
            self.heading_parts = []
            return

        if tag_lower == "meta":
            attr_dict = {key.lower(): value for key, value in attrs if value is not None}
            content = attr_dict.get("content")
            if not content:
                return
            name = (attr_dict.get("name") or "").lower()
            prop = (attr_dict.get("property") or "").lower()
            if name == "description" or prop in {"og:description", "twitter:description"}:
                self.description = content
            return

        if tag_lower == "link":
            attr_dict = {key.lower(): value for key, value in attrs if value is not None}
            rel_value = (attr_dict.get("rel") or "").lower()
            href = attr_dict.get("href")
            if href and "icon" in rel_value:
                self.icon_href = href
            return

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return

        if tag_lower == "title":
            self.in_title = False
            return

        if tag_lower == self.current_paragraph_tag:
            text = _normalize_article_text("".join(self.current_paragraph_parts))
            if text:
                self.paragraphs.append(text)
            self.current_paragraph_tag = None
            self.current_paragraph_parts = []
            return

        if tag_lower == "h1":
            self.in_heading = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.in_heading:
            self.heading_parts.append(data)
        if self.current_paragraph_tag is not None:
            self.current_paragraph_parts.append(data)


def _parse_article_html(html_text: str) -> Mapping[str, Any]:
    parser = _ArticleHTMLParser()
    parser.feed(html_text)
    parser.close()

    title = _normalize_article_text("".join(parser.title_parts))
    if not title and parser.heading_parts:
        title = _normalize_article_text("".join(parser.heading_parts))

    paragraphs: List[str] = []
    for text in parser.paragraphs:
        normalized = _normalize_article_text(text)
        if normalized:
            paragraphs.append(normalized)

    return {
        "title": title,
        "description": _normalize_article_text(parser.description or ""),
        "icon_href": parser.icon_href,
        "paragraphs": paragraphs,
    }


def _infer_icon_extension(icon_url: str, content_type: Optional[str]) -> str:
    path = urlparse(icon_url).path
    extension = os.path.splitext(path)[1]
    if extension:
        return extension

    if content_type:
        lowered = content_type.lower()
        if "png" in lowered:
            return ".png"
        if "jpeg" in lowered or "jpg" in lowered:
            return ".jpg"
        if "svg" in lowered:
            return ".svg"
        if "gif" in lowered:
            return ".gif"
    return ".ico"


def _download_article_icon(
    client: Any, icon_href: Optional[str], page_url: str, cache_dir: str
) -> Optional[str]:
    if not icon_href:
        return None

    icon_url = urljoin(page_url, icon_href)
    try:
        response = client.get(
            icon_url,
            headers={
                "User-Agent": DEFAULT_YTDLP_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": page_url,
            },
        )
        response.raise_for_status()
    except Exception:  # pragma: no cover - 网络异常
        return None

    extension = _infer_icon_extension(icon_url, response.headers.get("Content-Type"))
    filename = f"favicon{extension}"
    icon_path = os.path.join(cache_dir, filename)
    try:
        with open(icon_path, "wb") as icon_file:
            icon_file.write(response.content)
    except OSError:  # pragma: no cover - 文件系统异常
        return None

    return icon_path


def fetch_article_assets(video_url: str) -> MutableMapping[str, Any]:
    """Fetch article content and metadata for non-audio webpages."""

    if not video_url.lower().startswith(("http://", "https://")):
        raise RuntimeError("仅支持通过 HTTP/HTTPS 访问的网页链接。")

    cache_dir = _resolve_video_cache_dir(video_url)
    os.makedirs(cache_dir, exist_ok=True)

    with _create_http_client() as client:
        response = client.get(
            video_url,
            headers={
                "User-Agent": DEFAULT_YTDLP_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        response.raise_for_status()
        html_text = response.text

        if not html_text or not html_text.strip():
            raise RuntimeError("网页内容为空，无法解析正文。")
        parsed = _parse_article_html(html_text)
        icon_path = _download_article_icon(
            client, parsed.get("icon_href"), video_url, cache_dir
        )

    raw_html_path = os.path.join(cache_dir, "article_raw.html")
    with open(raw_html_path, "w", encoding="utf-8") as raw_file:
        raw_file.write(html_text)

    paragraph_texts = list(parsed.get("paragraphs", []))

    if not paragraph_texts:
        raise RuntimeError("未能从网页中提取正文文本。")

    segments: List[MutableMapping[str, float | str]] = []
    for index, paragraph in enumerate(paragraph_texts):
        start = float(index)
        end = float(index + 1)
        segments.append({"start": start, "end": end, "text": paragraph})

    content_path = os.path.join(cache_dir, "article_content.txt")
    with open(content_path, "w", encoding="utf-8") as content_file:
        content_file.write("\n\n".join(paragraph_texts))

    metadata: Dict[str, Any] = {
        "title": parsed.get("title") or paragraph_texts[0][:80],
        "description": parsed.get("description", ""),
        "webpage_url": video_url,
        "source_type": "article",
    }

    metadata_path = os.path.join(cache_dir, "article_metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
        json.dump(metadata, metadata_file, ensure_ascii=False, indent=2)

    return {
        "segments": segments,
        "metadata": metadata,
        "raw_html_path": raw_html_path,
        "content_path": content_path,
        "metadata_path": metadata_path,
        "icon_path": icon_path,
        "cache_dir": cache_dir,
    }


def _maybe_fetch_article_assets(video_url: str) -> Optional[Mapping[str, Any]]:
    try:
        bundle = fetch_article_assets(video_url)
    except RuntimeError:
        return None

    segments = bundle.get("segments")
    if not isinstance(segments, list) or not segments:
        return None

    return bundle


def _diarization_cache_path(directory: str) -> str:
    """Return the cache file path for diarization payload."""

    return os.path.join(directory, "diarization.json")


def _prepare_audio_cache(video_url: str) -> str:
    """Prepare cached audio WAV for a video, avoiding redundant downloads."""

    video_dir = _resolve_video_cache_dir(video_url)

    wav_path = os.path.join(video_dir, "audio.wav")
    if os.path.exists(wav_path):
        return wav_path

    raw_path = _find_cached_raw_audio(video_dir)
    if raw_path is None:
        raw_path = download_audio_stream(video_url, video_dir)

    if raw_path.endswith(".wav"):
        if os.path.abspath(raw_path) == os.path.abspath(wav_path):
            return wav_path
        shutil.copyfile(raw_path, wav_path)
        return wav_path

    convert_audio_to_wav(raw_path, wav_path)
    return wav_path


def _find_cached_raw_audio(directory: str) -> Optional[str]:
    """Locate previously downloaded audio file in directory (non-WAV)."""

    if not os.path.isdir(directory):
        return None

    for name in os.listdir(directory):
        lower = name.lower()
        if lower.startswith("audio.") and not lower.endswith(".wav"):
            return os.path.join(directory, name)
    return None


def _ensure_audio_segments(wav_path: str) -> List[str]:
    """Ensure large WAV files are split into manageable segments."""

    if not os.path.exists(wav_path):
        return []

    file_size = os.path.getsize(wav_path)
    duration = _get_wav_duration(wav_path)
    needs_split = (
        duration > float(MAX_WAV_DURATION_SECONDS)
        or file_size > int(MAX_WAV_SIZE_BYTES)
    )

    directory = os.path.dirname(wav_path)
    base_name = os.path.splitext(os.path.basename(wav_path))[0]
    existing = _list_existing_segments(directory, base_name)

    if existing:
        requires_resplit = False
        for segment_path in existing:
            if _get_wav_duration(segment_path) > AUDIO_SEGMENT_SECONDS:
                requires_resplit = True
                break
        if requires_resplit:
            for segment_path in existing:
                with contextlib.suppress(OSError):
                    os.remove(segment_path)
            existing = []
        else:
            return existing

    if not needs_split:
        return [wav_path]

    return _split_wav_file(wav_path, directory, base_name)


def _list_existing_segments(directory: str, base_name: str) -> List[str]:
    """Return sorted list of previously split WAV segments."""

    if not os.path.isdir(directory):
        return []

    prefix = f"{base_name}_part"
    segments: List[str] = []
    for name in sorted(os.listdir(directory)):
        if not name.startswith(prefix):
            continue
        if not name.lower().endswith(".wav"):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            segments.append(path)
    return segments


def _split_wav_file(
    wav_path: str, directory: str, base_name: str
) -> List[str]:
    """Split WAV file into multiple segments using wave module."""

    segment_paths: List[str] = []

    try:
        with wave.open(wav_path, "rb") as source:
            params = source.getparams()
            frame_rate = source.getframerate() or 16000
            frames_per_segment = int(AUDIO_SEGMENT_SECONDS * frame_rate)
            if frames_per_segment <= 0:
                frames_per_segment = frame_rate
            frames_per_chunk = max(WAV_FRAME_CHUNK_SIZE, frame_rate)

            total_frames = source.getnframes()
            frames_remaining = total_frames
            segment_index = 0

            while frames_remaining > 0:
                segment_index += 1
                segment_path = os.path.join(
                    directory, f"{base_name}_part{segment_index:03d}.wav"
                )
                with wave.open(segment_path, "wb") as destination:
                    destination.setparams(params)
                    frames_to_write = min(frames_per_segment, frames_remaining)
                    written = 0

                    while written < frames_to_write:
                        frames_to_read = min(
                            frames_per_chunk, frames_to_write - written
                        )
                        frame_bytes = source.readframes(frames_to_read)
                        if not frame_bytes:
                            break
                        destination.writeframes(frame_bytes)
                        written += frames_to_read

                    frames_remaining -= written

                if os.path.exists(segment_path) and os.path.getsize(segment_path) > 0:
                    segment_paths.append(segment_path)

            if not segment_paths:
                return [wav_path]

    except (OSError, wave.Error):
        return [wav_path]

    return segment_paths


def _get_wav_duration(wav_path: str) -> float:
    """Return duration in seconds for given WAV file."""

    try:
        with wave.open(wav_path, "rb") as handle:
            frames = handle.getnframes()
            frame_rate = handle.getframerate()
    except (OSError, wave.Error):
        return 0.0

    if frame_rate <= 0:
        return 0.0

    return frames / float(frame_rate)


def _estimate_total_tokens(
    segment_paths: Sequence[str],
    durations: Optional[Sequence[float]] = None,
) -> float:
    """Estimate expected tokens based on segment durations."""

    total_tokens = 0.0
    for index, path in enumerate(segment_paths):
        if durations is not None and index < len(durations):
            duration = durations[index]
        else:
            duration = max(_get_wav_duration(path), 0.0)
            if duration <= 0.0:
                try:
                    file_size = os.path.getsize(path)
                except OSError:
                    file_size = 0
                if file_size > 0:
                    duration = file_size / 32_000.0
        total_tokens += max(duration, 0.0) * ESTIMATED_TOKENS_PER_SECOND
    return max(total_tokens, 1.0)


def _estimate_tokens_from_transcript(
    segments: Iterable[MutableMapping[str, float | str]]
) -> float:
    """Approximate token count from transcript segments."""

    total_chars = 0
    segment_count = 0
    for segment in segments:
        text = segment.get("text", "")
        if isinstance(text, str):
            total_chars += len(text)
        segment_count += 1
    if total_chars == 0 and segment_count == 0:
        return 0.0
    if total_chars == 0:
        total_chars = segment_count * 16
    return max(total_chars / 4.0, float(segment_count))


def _update_progress_bar(ratio: float, detail: str) -> None:
    """Render a simple textual progress bar to stdout."""

    ratio = min(max(ratio, 0.0), 1.0)
    filled = int(PROGRESS_BAR_WIDTH * ratio)
    bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
    sys.stdout.write(
        f"\r[{bar}] {ratio * 100:5.1f}% {detail[:80]}"
    )
    sys.stdout.flush()
    if ratio >= 1.0:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _compute_progress_ratio(
    processed_duration: float,
    total_duration: float,
    produced_tokens: float,
    total_tokens: float,
    segments_done: int,
    total_segments: int,
) -> float:
    """Combine duration、token与片段比值，得到整体进度。"""

    if total_duration <= 0:
        duration_ratio = 0.0
    else:
        duration_ratio = min(max(processed_duration / total_duration, 0.0), 1.0)

    if total_tokens <= 0:
        token_ratio = duration_ratio
    else:
        token_ratio = min(max(produced_tokens / total_tokens, 0.0), 1.0)

    if total_segments <= 0:
        segment_ratio = duration_ratio
    else:
        segment_ratio = min(max(segments_done / total_segments, 0.0), 1.0)

    combined = 0.5 * duration_ratio + 0.3 * token_ratio + 0.2 * segment_ratio
    return min(max(combined, 0.0), 1.0)


def _format_progress_detail(
    processed_duration: float,
    total_duration: float,
    produced_tokens: float,
    total_tokens: float,
    segments_done: int,
    total_segments: int,
) -> str:
    """Return user-friendly progress detail string."""

    total_minutes = total_duration / 60.0 if total_duration > 0 else 0.0
    processed_minutes = processed_duration / 60.0
    return (
        f"Azure diarization {segments_done}/{total_segments} "
        f"{processed_minutes:.1f}m/{total_minutes:.1f}m "
        f"tokens≈{int(produced_tokens)}/{int(max(total_tokens, 1.0))}"
    )


def _offset_segments(
    segments: Iterable[MutableMapping[str, float | str]], offset: float
) -> List[MutableMapping[str, float | str]]:
    """Return new segment list with applied time offset."""

    adjusted: List[MutableMapping[str, float | str]] = []
    for segment in segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        shifted = dict(segment)
        shifted["start"] = start + offset
        shifted["end"] = end + offset
        adjusted.append(shifted)
    return adjusted


def _max_segment_end(
    diarization: Iterable[MutableMapping[str, float | str]],
    transcript: Iterable[MutableMapping[str, float | str]],
) -> float:
    """Return maximum end timestamp across provided segments."""

    max_end = 0.0
    for collection in (diarization, transcript):
        for segment in collection:
            end = float(segment.get("end", segment.get("start", 0.0)))
            if end > max_end:
                max_end = end
    return max_end


def _consume_transcription_response(
    response: Any,
    on_chunk: Optional[Callable[[MutableMapping[str, Any]], None]] = None,
) -> MutableMapping[str, Any]:
    collected: List[MutableMapping[str, Any]] = []

    def _record(payload: MutableMapping[str, Any]) -> None:
        if not payload:
            return
        collected.append(payload)
        if on_chunk:
            on_chunk(payload)

    if isinstance(response, MutableMapping):
        payload = _coerce_response_to_dict(response)
        if payload:
            _record(payload)
        if len(collected) > 1:
            enriched = dict(payload)
            enriched.setdefault("data", collected)
            return enriched
        return payload

    if isinstance(response, Iterable) and not isinstance(response, (str, bytes)):
        final_payload: MutableMapping[str, Any] = {}
        for item in response:
            payload = _coerce_response_to_dict(item)
            if not payload:
                continue
            final_payload = payload
            _record(payload)

        if not collected:
            return final_payload

        result = dict(final_payload) if final_payload else {}
        result.setdefault("data", collected)
        return result

    payload = _coerce_response_to_dict(response)
    if payload:
        _record(payload)
    return payload


def _extract_usage_tokens(payload: Mapping[str, Any]) -> Optional[float]:
    candidates: List[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        candidates.append(payload)
        response_obj = payload.get("response")
        if isinstance(response_obj, Mapping):
            candidates.append(response_obj)

    for candidate in candidates:
        usage = candidate.get("usage") if isinstance(candidate, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        for key in ("output_tokens", "total_tokens", "completion_tokens"):
            tokens = usage.get(key)
            if tokens is None:
                continue
            try:
                return float(tokens)
            except (TypeError, ValueError):
                continue
    return None


def _is_youtube_hostname(hostname: str) -> bool:
    if not hostname:
        return False
    if hostname in {"youtu.be", "youtube.com"}:
        return True
    if hostname.endswith(".youtube.com"):
        return True
    return hostname == "www.youtube.com"


def _matches_host_suffix(hostname: str, suffix: str) -> bool:
    hostname = hostname.lower()
    check = suffix.lower().lstrip(".")
    if not check:
        return False
    return hostname == check or hostname.endswith(f".{check}")


def _is_media_source_url(video_url: str) -> bool:
    parsed = urlparse(video_url)
    hostname = (parsed.hostname or "").lower()
    if hostname:
        for suffix in _MEDIA_HOST_SUFFIXES:
            if _matches_host_suffix(hostname, suffix):
                return True
    path = parsed.path.lower()
    for extension in _MEDIA_PATH_EXTENSIONS:
        if path.endswith(extension):
            return True
    return False


def _is_probable_article_url(video_url: str) -> bool:
    """Heuristically determine whether a URL points to a webpage article."""

    return not _is_media_source_url(video_url)


def _should_force_azure_transcription(video_url: str) -> bool:
    """Return True when URL belongs to audio sources that require Azure pipeline."""

    parsed = urlparse(video_url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    for suffix in _FORCED_AUDIO_HOST_SUFFIXES:
        if _matches_host_suffix(hostname, suffix):
            return True
    return False


def _default_referer_for_url(parsed: ParseResult, is_youtube: bool) -> str:
    if is_youtube:
        return "https://www.youtube.com/"
    scheme = parsed.scheme or "https"
    hostname = parsed.hostname or ""
    if hostname:
        return f"{scheme}://{hostname}/"
    return parsed.geturl() or "https://www.youtube.com/"


def download_audio_stream(video_url: str, directory: str) -> str:
    """Download the best available audio stream using yt_dlp."""

    try:
        import yt_dlp
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "yt_dlp is required to download audio. Install it via "
            "`pip install yt-dlp`."
        ) from exc

    os.makedirs(directory, exist_ok=True)

    user_agent = _getenv(
        "ANY2SUMMARY_YTDLP_UA",
        "PODCAST_TRANSFORMER_YTDLP_UA",
    ) or DEFAULT_YTDLP_USER_AGENT
    parsed_url = urlparse(video_url)
    hostname = (parsed_url.hostname or "").lower()
    is_youtube = _is_youtube_hostname(hostname)
    referer = _default_referer_for_url(parsed_url, is_youtube)

    try:
        from yt_dlp.utils import std_headers  # type: ignore[import-error]
    except Exception:  # pragma: no cover - fallback when utils missing
        std_headers = {}

    http_headers: MutableMapping[str, str] = dict(std_headers or {})
    http_headers["User-Agent"] = user_agent
    http_headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    http_headers.setdefault("Referer", referer)

    ydl_opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(directory, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "http_headers": http_headers,
    }

    cookie_path = _getenv("ANY2SUMMARY_YTDLP_COOKIES", "PODCAST_TRANSFORMER_YTDLP_COOKIES")
    if cookie_path is not None:
        cookie_path = cookie_path.strip()
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path

    try:
        from yt_dlp.utils import DownloadError  # type: ignore[import-error]
    except Exception:  # pragma: no cover - defensive fallback
        DownloadError = getattr(yt_dlp, "DownloadError", RuntimeError)

    try:
        audio_path = _download_with_ytdlp(yt_dlp, video_url, ydl_opts)
    except DownloadError as exc:  # pragma: no cover - depends on network
        if is_youtube and _should_try_android_fallback(exc, ydl_opts.get("cookiefile")):
            fallback_opts = _build_android_fallback_options(ydl_opts)
            try:
                audio_path = _download_with_ytdlp(yt_dlp, video_url, fallback_opts)
            except DownloadError as fallback_exc:  # pragma: no cover - depends on network
                raise RuntimeError(
                    "yt_dlp 无法下载音频，请确认 URL 可访问，"
                    "或提供有效的 cookie（设置 ANY2SUMMARY_YTDLP_COOKIES"
                    " 或 PODCAST_TRANSFORMER_YTDLP_COOKIES）。"
                ) from fallback_exc
        else:
            raise RuntimeError(
                "yt_dlp 无法下载音频，请确认 URL 可访问，"
                "或提供有效的 cookie（设置 ANY2SUMMARY_YTDLP_COOKIES"
                " 或 PODCAST_TRANSFORMER_YTDLP_COOKIES）。"
            ) from exc

    if not os.path.exists(audio_path):
        raise RuntimeError("Audio download failed; file not found.")

    return audio_path


def _download_with_ytdlp(
    yt_dlp_module: Any, video_url: str, options: Mapping[str, Any]
) -> str:
    """Execute yt_dlp with supplied options and return downloaded path."""

    with yt_dlp_module.YoutubeDL(options) as ydl:
        info = ydl.extract_info(video_url, download=True)
        audio_path = ydl.prepare_filename(info)
    return audio_path


def _should_try_android_fallback(
    exc: BaseException, cookiefile: Optional[str]
) -> bool:
    """Return True when 403 occurs; cookies no longer prevent fallback."""

    _ = cookiefile  # legacy parameter retained for compatibility
    message = str(exc)
    if message and ("403" in message or "Forbidden" in message):
        return True

    exc_info = getattr(exc, "exc_info", None)
    if exc_info and len(exc_info) > 1 and exc_info[1] is not None:
        nested_message = str(exc_info[1])
        if "403" in nested_message or "Forbidden" in nested_message:
            return True

    return False


def _build_android_fallback_options(base_options: Mapping[str, Any]) -> Dict[str, Any]:
    """Clone yt_dlp options and inject Android headers and args."""

    fallback_options = dict(base_options)

    headers = dict(base_options.get("http_headers", {}))
    headers["User-Agent"] = ANDROID_YTDLP_USER_AGENT
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Referer", "https://www.youtube.com/")
    fallback_options["http_headers"] = headers

    extractor_args: Dict[str, Any] = {}
    youtube_args: Dict[str, Any] = {}

    if "extractor_args" in base_options:
        original = base_options["extractor_args"]
        if isinstance(original, Mapping):
            extractor_args.update(original)
            youtube_original = original.get("youtube")
            if isinstance(youtube_original, Mapping):
                youtube_args.update(youtube_original)

    youtube_args["player_client"] = ["android"]
    youtube_args.setdefault("player_skip", ["configs"])
    extractor_args["youtube"] = youtube_args
    fallback_options["extractor_args"] = extractor_args

    return fallback_options


def convert_audio_to_wav(source_path: str, target_path: str) -> None:
    """Convert audio to single-channel 16kHz WAV using ffmpeg."""

    command = [
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        target_path,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError("ffmpeg is required to convert audio; install it first.") from exc
    except subprocess.CalledProcessError as exc:  # pragma: no cover
        raise RuntimeError(f"ffmpeg failed to convert audio: {exc.stderr}")


def merge_segments_with_speakers(
    transcript_segments: Iterable[MutableMapping[str, float | str]],
    diarization_segments: Optional[Iterable[MutableMapping[str, float | str]]],
) -> List[MutableMapping[str, float | str]]:
    """Merge transcript segments with diarization metadata.

    Args:
        transcript_segments: Iterable of transcript dictionaries containing
            `start`, `end`, and `text` keys.
        diarization_segments: Iterable of diarization dictionaries containing
            `start`, `end`, and `speaker` keys or None.

    Returns:
        List of transcript dictionaries enhanced with a `speaker` key when
        diarization data is supplied.
    """

    diarization_list = list(diarization_segments or [])
    merged: List[MutableMapping[str, float | str]] = []

    for segment in transcript_segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        best_speaker = determine_best_speaker(start, end, diarization_list)

        enriched = dict(segment)
        if best_speaker is not None:
            enriched["speaker"] = best_speaker
        merged.append(enriched)

    return merged


def determine_best_speaker(
    start: float,
    end: float,
    diarization_segments: Sequence[MutableMapping[str, float | str]],
) -> Optional[str]:
    """Determine the speaker label with the greatest overlap."""

    best_label: Optional[str] = None
    best_overlap = 0.0

    for segment in diarization_segments:
        diar_start = float(segment.get("start", 0.0))
        diar_end = float(segment.get("end", diar_start))
        label = segment.get("speaker")
        if diar_end <= start or diar_start >= end:
            continue
        overlap = min(diar_end, end) - max(diar_start, start)
        if overlap > best_overlap:
            best_overlap = overlap
            if isinstance(label, str):
                best_label = label

    return best_label


def extract_video_id(video_url: str) -> Optional[str]:
    """Extract YouTube video identifier from a URL."""

    parsed = urlparse(video_url)
    if parsed.hostname in {"youtu.be"}:
        return parsed.path.lstrip("/") or None

    if parsed.hostname in {"www.youtube.com", "youtube.com", "m.youtube.com"}:
        query = parse_qs(parsed.query)
        video_ids = query.get("v")
        if video_ids:
            return video_ids[0]

        if parsed.path.startswith("/embed/"):
            return parsed.path.split("/", maxsplit=2)[2]

    return None


def main() -> int:  # pragma: no cover - convenience wrapper
    """Console script entry point."""

    return run()


if __name__ == "__main__":  # pragma: no cover - manual execution
    sys.exit(main())
