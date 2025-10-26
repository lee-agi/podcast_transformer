"""针对 Azure 说话人分离回退逻辑的测试。"""

from __future__ import annotations

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
