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

from podcast_transformer import cli


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

    nested_payload = {
        "response": {
            "output": [
                {
                    "type": "diarization",
                    "diarization": {
                        "segments": [
                            {"start": 0.0, "end": 1.0, "speaker": "Speaker 1"},
                            {"start": 1.0, "end": 2.0, "speaker": "Speaker 2"},
                        ]
                    },
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": "Hello",
                            "speaker": "Speaker 1",
                        },
                        {
                            "start": 1.0,
                            "end": 2.0,
                            "text": "World",
                            "speaker": "Speaker 2",
                        },
                    ],
                }
            ]
        }
    }

    class _Client(_DummyAzureOpenAI):
        pass

    module = types.SimpleNamespace(
        AzureOpenAI=_Client,
        BadRequestError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "openai", module)

    def fake_consume(response, on_chunk=None):
        if on_chunk is not None:
            on_chunk({"usage": {"output_tokens": 10}})
        return nested_payload

    def fake_fetch_transcript(*_args, **_kwargs):
        raise RuntimeError(
            "No transcript available in requested languages: ['en']"
        )

    monkeypatch.setattr(cli, "_consume_transcription_response", fake_consume)
    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "_prepare_audio_cache", lambda *_: str(wav_path))
    monkeypatch.setattr(cli, "_ensure_audio_segments", lambda *_: [str(wav_path)])
    monkeypatch.setattr(cli, "_resolve_video_cache_dir", lambda *_: str(tmp_path))

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
