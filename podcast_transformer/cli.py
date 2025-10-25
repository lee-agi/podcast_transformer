"""Command line interface for extracting structured YouTube transcripts.

The module provides a CLI entry point `run` that accepts a YouTube URL and
optional Azure OpenAI diarization support to enrich transcripts with speaker
labels. The default behaviour fetches caption segments with timestamps and
emits JSON to stdout.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from collections import defaultdict
from typing import (
    Any,
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
from urllib.parse import parse_qs, urlparse


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

MAX_WAV_DURATION_SECONDS = 3600.0
MAX_WAV_SIZE_BYTES = 100 * 1024 * 1024
AUDIO_SEGMENT_SECONDS = 1800.0
WAV_FRAME_CHUNK_SIZE = 32_768
ESTIMATED_TOKENS_PER_SECOND = 4.0
PROGRESS_BAR_WIDTH = 30


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
        "--azure-diarization",
        action="store_true",
        help="Call Azure OpenAI diarization to annotate speakers.",
    )
    parser.add_argument(
        "--azure-summary",
        action="store_true",
        help="调用 Azure GPT-5 对 ASR 结果进行翻译与总结。",
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
    parser.add_argument(
        "--check-cache",
        action="store_true",
        help="Inspect cached files for the provided URL and exit.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty print JSON output for readability.",
    )

    args = parser.parse_args(argv)

    _load_dotenv_if_present(os.getenv("PODCAST_TRANSFORMER_DOTENV"))

    if args.clean_cache:
        cache_directory = _resolve_video_cache_dir(args.url)
        if os.path.isdir(cache_directory):
            shutil.rmtree(cache_directory)

    if args.check_cache:
        cache_directory = _resolve_video_cache_dir(args.url)
        exists = os.path.isdir(cache_directory)
        files = sorted(os.listdir(cache_directory)) if exists else []
        audio_path = os.path.join(cache_directory, "audio.wav")
        payload = {
            "cache": {
                "path": cache_directory,
                "exists": exists,
                "files": files,
                "audio_wav_exists": os.path.exists(audio_path),
            }
        }
        indent = 2 if args.pretty else None
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=indent)
        sys.stdout.write("\n")
        return 0

    fallback_languages = args.fallback_languages or [args.language]
    known_speaker_pairs = _parse_known_speakers(args.known_speakers)
    known_speaker_names = args.known_speaker_names or None

    transcript_segments: Optional[
        List[MutableMapping[str, float | str]]
    ] = None
    transcript_error: Optional[RuntimeError] = None

    try:
        transcript_segments = fetch_transcript_with_metadata(
            video_url=args.url,
            language=args.language,
            fallback_languages=fallback_languages,
        )
    except RuntimeError as exc:
        transcript_error = exc
        if not args.azure_diarization:
            raise

    diarization_segments: Optional[List[MutableMapping[str, float | str]]] = None
    azure_payload: Optional[MutableMapping[str, Any]] = None

    if args.azure_diarization:
        azure_payload = perform_azure_diarization(
            video_url=args.url,
            language=args.language,
            max_speakers=args.max_speakers,
            known_speakers=known_speaker_pairs,
            known_speaker_names=known_speaker_names,
        )
        diarization_segments = azure_payload.get("speakers") or []
        if not transcript_segments:
            transcript_segments = azure_payload.get("transcript")
        if not transcript_segments:
            if transcript_error is not None:
                raise transcript_error
            raise RuntimeError("Azure OpenAI 未返回可用的转写结果。")

    if not transcript_segments:
        raise RuntimeError(
            "未能获取字幕数据。请确认视频是否启用字幕，或使用 --azure-diarization 以启用 Azure 转写。"
        )

    merged_segments = merge_segments_with_speakers(
        transcript_segments, diarization_segments
    )

    summary_text: Optional[str] = None
    if args.azure_summary:
        summary_text = generate_translation_summary(merged_segments)

    payload: Union[List[MutableMapping[str, float | str]], MutableMapping[str, Any]]
    if summary_text is not None:
        payload = {
            "segments": merged_segments,
            "summary": summary_text,
        }
    else:
        payload = merged_segments

    indent = 2 if args.pretty else None
    json.dump(payload, sys.stdout, indent=indent, ensure_ascii=False)
    if indent:
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


def perform_azure_diarization(
    video_url: str,
    language: str,
    max_speakers: Optional[int] = None,
    known_speakers: Optional[List[Tuple[str, str]]] = None,
    known_speaker_names: Optional[Sequence[str]] = None,
) -> MutableMapping[str, Any]:
    """Use Azure OpenAI GPT-4o diarization to identify speaker segments."""

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
        try:
            with open(segment_path, "rb") as audio_file:
                request_kwargs: MutableMapping[str, Any] = {
                    "model": deployment,
                    "file": audio_file,
                    "response_format": "diarized_json",
                    "language": language,
                    "chunking_strategy": chunking_strategy_config,
                    "extra_body": request_extra_body,
                }
                if request_known_names:
                    request_kwargs["known_speaker_names"] = request_known_names

                response = client.audio.transcriptions.create(**request_kwargs)
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

        response_payload = _coerce_response_to_dict(response)
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
        produced_tokens += _estimate_tokens_from_transcript(transcript_segments)
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
            raise RuntimeError("Azure OpenAI 未返回说话人分段信息。")

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


def _extract_diarization_segments(
    payload: MutableMapping[str, Any]
) -> List[MutableMapping[str, float | str]]:
    """Extract diarization segments from verbose JSON payload."""

    candidates: List[List[MutableMapping[str, Any]]] = []

    for key in ("segments", "words", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            segment_list = [item for item in value if isinstance(item, MutableMapping)]
            if segment_list:
                candidates.append(segment_list)

    diarization = payload.get("diarization")
    if isinstance(diarization, MutableMapping):
        nested = diarization.get("segments")
        if isinstance(nested, list):
            segment_list = [item for item in nested if isinstance(item, MutableMapping)]
            if segment_list:
                candidates.append(segment_list)

    data_entries = payload.get("data")
    if isinstance(data_entries, list):
        for entry in data_entries:
            if not isinstance(entry, MutableMapping):
                continue
            entry_segments = entry.get("segments")
            if isinstance(entry_segments, list):
                segment_list = [
                    item for item in entry_segments if isinstance(item, MutableMapping)
                ]
                if segment_list:
                    candidates.append(segment_list)

    normalized: List[MutableMapping[str, float | str]] = []
    seen = set()

    for candidate in candidates:
        for raw_segment in candidate:
            segment = _normalize_segment_entry(raw_segment)
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

    candidates: List[List[MutableMapping[str, Any]]] = []

    for key in ("segments", "utterances", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            segment_list = [item for item in value if isinstance(item, MutableMapping)]
            if segment_list:
                candidates.append(segment_list)

    diarization = payload.get("diarization")
    if isinstance(diarization, MutableMapping):
        for key in ("segments", "utterances"):
            nested = diarization.get(key)
            if isinstance(nested, list):
                segment_list = [item for item in nested if isinstance(item, MutableMapping)]
                if segment_list:
                    candidates.append(segment_list)

    data_entries = payload.get("data")
    if isinstance(data_entries, list):
        for entry in data_entries:
            if not isinstance(entry, MutableMapping):
                continue
            for key in ("segments", "utterances"):
                nested = entry.get(key)
                if isinstance(nested, list):
                    segment_list = [
                        item for item in nested if isinstance(item, MutableMapping)
                    ]
                    if segment_list:
                        candidates.append(segment_list)

    transcript_segments: List[MutableMapping[str, float | str]] = []
    seen = set()

    for candidate in candidates:
        for raw_segment in candidate:
            normalized = _normalize_transcript_entry(raw_segment)
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

    raw_start = segment.get("start")
    if raw_start is None:
        raw_start = segment.get("offset")
    raw_end = segment.get("end")
    raw_duration = segment.get("duration")

    if raw_start is None:
        return None

    start = _to_seconds(raw_start)
    end = _to_seconds(raw_end) if raw_end is not None else None

    if end is None and raw_duration is not None:
        end = start + _to_seconds(raw_duration)

    if end is None:
        end = start

    speaker = (
        segment.get("speaker")
        or segment.get("speaker_label")
        or segment.get("speakerId")
        or segment.get("speaker_id")
    )

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

    text = segment.get("text") or segment.get("display_text")
    if not isinstance(text, str) or not text.strip():
        return None

    raw_start = segment.get("start")
    if raw_start is None:
        raw_start = segment.get("offset")
    raw_end = segment.get("end")
    raw_duration = segment.get("duration")

    if raw_start is None:
        return None

    start = _to_seconds(raw_start)
    end = _to_seconds(raw_end) if raw_end is not None else None

    if end is None and raw_duration is not None:
        end = start + _to_seconds(raw_duration)

    if end is None:
        end = start

    entry: MutableMapping[str, float | str] = {
        "start": float(start),
        "end": float(end),
        "text": text.strip(),
    }

    speaker = (
        segment.get("speaker")
        or segment.get("speaker_label")
        or segment.get("speakerId")
        or segment.get("speaker_id")
    )
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


def generate_translation_summary(
    segments: Sequence[MutableMapping[str, Any]],
    prompt: Optional[str] = None,
) -> str:
    """Call Azure GPT-5 to translate and summarize ASR segments."""

    if not segments:
        raise RuntimeError("无法生成翻译摘要：缺少 ASR 结果。")

    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not azure_key or not azure_endpoint:
        raise RuntimeError(
            "Azure OpenAI 凭据缺失。请设置 AZURE_OPENAI_API_KEY与 AZURE_OPENAI_ENDPOINT。"
        )

    summary_api_version = (
        os.getenv("AZURE_OPENAI_SUMMARY_API_VERSION") or "2025-01-01-preview"
    )
    deployment = os.getenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT") or "llab-gpt-5"

    try:
        from openai import AzureOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "openai 库未安装。请执行 `pip install openai`."
        ) from exc

    instruction = prompt or SUMMARY_PROMPT
    timeline = _format_segments_for_summary(segments)
    user_message = "原始 ASR 片段如下：\n" + timeline

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

    return _extract_summary_text(response)


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
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


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

    video_id = extract_video_id(video_url)
    if not video_id:
        raise RuntimeError(f"Unable to determine video id for caching: {video_url}")

    base_dir = os.getenv("PODCAST_TRANSFORMER_CACHE_DIR")
    if base_dir:
        cache_base = base_dir
    else:
        home_dir = os.path.expanduser("~")
        cache_base = os.path.join(home_dir, ".cache", "podcast_transformer")

    video_dir = os.path.join(cache_base, video_id)
    os.makedirs(video_dir, exist_ok=True)
    return video_dir


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

    if not needs_split:
        if existing:
            return existing
        return [wav_path]

    if existing:
        return existing

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

    user_agent = os.getenv("PODCAST_TRANSFORMER_YTDLP_UA", DEFAULT_YTDLP_USER_AGENT)

    try:
        from yt_dlp.utils import std_headers  # type: ignore[import-error]
    except Exception:  # pragma: no cover - fallback when utils missing
        std_headers = {}

    http_headers: MutableMapping[str, str] = dict(std_headers or {})
    http_headers["User-Agent"] = user_agent
    http_headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    http_headers.setdefault("Referer", "https://www.youtube.com/")

    ydl_opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(directory, "audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "http_headers": http_headers,
    }

    cookie_path = os.getenv("PODCAST_TRANSFORMER_YTDLP_COOKIES")
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
        if _should_try_android_fallback(exc, ydl_opts.get("cookiefile")):
            fallback_opts = _build_android_fallback_options(ydl_opts)
            try:
                audio_path = _download_with_ytdlp(yt_dlp, video_url, fallback_opts)
            except DownloadError as fallback_exc:  # pragma: no cover - depends on network
                raise RuntimeError(
                    "yt_dlp 无法下载音频，请确认 URL 可访问，"
                    "或提供有效的 cookie（设置 PODCAST_TRANSFORMER_YTDLP_COOKIES）。"
                ) from fallback_exc
        else:
            raise RuntimeError(
                "yt_dlp 无法下载音频，请确认 URL 可访问，"
                "或提供有效的 cookie（设置 PODCAST_TRANSFORMER_YTDLP_COOKIES）。"
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
    """Return True when 403 occurs without cookie support."""

    if cookiefile:
        return False

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

    fallback_options.pop("cookiefile", None)

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
