"""针对 Azure 说话人分离回退逻辑的测试。"""

from __future__ import annotations

import json
import sys
import types
import wave
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from any2summary import cli


def test_consume_transcription_response_collects_stream_data() -> None:
    """流式响应应累积所有 chunk 便于后续解析。"""

    chunks = [
        {"type": "transcript.segment", "segments": [{"start": 0, "end": 1}]},
        {"type": "transcript.text.done", "text": "Final"},
    ]

    result = cli._consume_transcription_response(iter(chunks))

    assert "data" in result
    assert result["data"] == chunks


def _write_silent_wav(path: Path, duration_seconds: float, sample_rate: int = 8000) -> None:
    """生成指定时长的静音 WAV 文件。"""

    total_frames = int(duration_seconds * sample_rate)
    if total_frames <= 0:
        total_frames = sample_rate
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * total_frames)


class _DummyTranscriptions:
    def create(self, **kwargs):
        return {}


class _DummyAudio:
    def __init__(self) -> None:
        self.transcriptions = _DummyTranscriptions()


class _DummyAzureOpenAI:
    def __init__(self, **_: object) -> None:
        self.audio = _DummyAudio()


def test_perform_azure_diarization_handles_empty_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Azure 返回空响应时应当返回空结果而非抛错。"""

    wav_path = tmp_path / "audio.wav"
    _write_silent_wav(wav_path, duration_seconds=1.0)

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.com")

    module = types.SimpleNamespace(
        AzureOpenAI=_DummyAzureOpenAI,
        BadRequestError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "openai", module)

    monkeypatch.setattr(cli, "_prepare_audio_cache", lambda _: str(wav_path))
    monkeypatch.setattr(cli, "_ensure_audio_segments", lambda __: [str(wav_path)])
    monkeypatch.setattr(cli, "_resolve_video_cache_dir", lambda _: str(tmp_path))

    result = cli.perform_azure_diarization("https://youtu.be/example", "en")

    assert result == {"speakers": [], "transcript": []}


def test_cli_azure_diarization_handles_nested_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """当 Azure 响应嵌套在 response.output 时也应提取字幕。"""

    wav_path = tmp_path / "audio.wav"
    _write_silent_wav(wav_path, duration_seconds=1.0)

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.com")

    stream_events = [
        {
            "diarization": {
                "segments": [
                    {
                        "timestamps": {
                            "start": {"seconds": 0},
                            "end": {"seconds": 1},
                        },
                        "speaker": {"label": "Speaker 1"},
                    },
                    {
                        "timestamps": {
                            "start": {"offset_seconds": 1},
                            "end": {"offsetMillis": 2000},
                        },
                        "speaker": {"info": {"name": "Speaker 2"}},
                    },
                ]
            }
        },
        {
            "transcript": {
                "chunks": [
                    {
                        "content": [{"type": "text", "value": "Hello"}],
                        "timestamps": {"start_time_ms": 0, "end_time_ms": 1000},
                        "speaker": {"label": "Speaker 1"},
                    },
                    {
                        "content": [{"type": "text", "value": "World"}],
                        "timing": {"from": "PT1S", "to": "PT2S"},
                        "speaker": {"details": {"label": "Speaker 2"}},
                    },
                ]
            }
        },
        {"type": "transcript.text.done", "text": "Hello World"},
    ]

    captured: dict[str, object] = {}

    class _StreamingTranscriptions:
        def __init__(self) -> None:
            self.events = stream_events

        def create(self, **kwargs):
            captured["stream"] = kwargs.get("stream")
            captured["chunking_strategy"] = kwargs.get("chunking_strategy")
            if kwargs.get("stream"):
                return iter(self.events)
            return self.events[-1]

    class _Client(_DummyAzureOpenAI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.audio.transcriptions = _StreamingTranscriptions()

    module = types.SimpleNamespace(
        AzureOpenAI=_Client,
        BadRequestError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "openai", module)

    def fake_fetch_transcript(*_args, **_kwargs):
        raise RuntimeError(
            "No transcript available in requested languages: ['en']"
        )

    progress_updates: list[float] = []

    def record_progress(ratio: float, _detail: str | None = None) -> None:
        progress_updates.append(ratio)

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "_prepare_audio_cache", lambda *_: str(wav_path))
    monkeypatch.setattr(cli, "_ensure_audio_segments", lambda *_: [str(wav_path)])
    monkeypatch.setattr(cli, "_resolve_video_cache_dir", lambda *_: str(tmp_path))
    monkeypatch.setattr(cli, "_update_progress_bar", record_progress)

    exit_code = cli.run([
        "--url",
        "https://youtu.be/nested",
        "--azure-diarization",
    ])

    assert exit_code == 0

    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line.strip()]
    assert lines, "Expected JSON payload in CLI output"
    payload = json.loads(lines[-1])
    assert payload[0]["text"] == "Hello"
    assert payload[0]["speaker"] == "Speaker 1"
    assert payload[1]["text"] == "World"
    assert payload[1]["speaker"] == "Speaker 2"
    assert progress_updates, "Streaming 模式下应当触发进度更新"
    assert captured["stream"] is True
