"""针对 Azure diarization chunking 策略的单元测试。"""

from __future__ import annotations

import json
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List
import wave

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

sys.modules.pop("podcast_transformer", None)
sys.modules.pop("podcast_transformer.cli", None)

from podcast_transformer import cli

cli = importlib.reload(cli)


@pytest.fixture
def dummy_audio(tmp_path: Path) -> Path:
    file_path = tmp_path / "sample.wav"
    file_path.write_bytes(b"RIFF0000")
    return file_path


def test_perform_azure_diarization_uses_auto_chunking(
    monkeypatch: pytest.MonkeyPatch, dummy_audio: Path, tmp_path: Path
) -> None:
    """默认应发送 auto chunking 策略以满足 Azure 要求。"""

    # 确保环境变量存在
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")
    monkeypatch.delenv("AZURE_OPENAI_CHUNKING_STRATEGY", raising=False)

    known_speakers = [("Alice", str(dummy_audio))]

    # 替换依赖，捕获请求参数
    captured: Dict[str, Any] = {}

    def fake_prepare_audio_cache(_: str) -> str:
        return str(dummy_audio)

    def fake_create(**kwargs: Any) -> Dict[str, Any]:
        captured.update(kwargs)
        strategy = kwargs.get("chunking_strategy")
        if not isinstance(strategy, dict) or strategy.get("type") != "auto":
            raise RuntimeError("chunking_strategy must be dict with type auto")
        if kwargs.get("known_speaker_names") != ["Alice"]:
            raise RuntimeError("known_speaker_names must match provided speakers")
        segment = {"start": "0.0", "end": "1.0", "speaker": "Speaker 1"}
        return {
            "segments": [segment],
            "diarization": {"segments": [segment]},
        }

    class FakeTranscriptions:
        def __init__(self) -> None:
            self.create = fake_create

    class FakeAzureClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    fake_openai = ModuleType("openai")
    fake_openai.AzureOpenAI = FakeAzureClient  # type: ignore[attr-defined]

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "diarization.json"

    monkeypatch.setattr(cli, "_prepare_audio_cache", fake_prepare_audio_cache)
    monkeypatch.setattr(
        cli, "_resolve_video_cache_dir", lambda __: str(cache_dir)
    )
    monkeypatch.setattr(
        cli, "_diarization_cache_path", lambda directory: str(cache_file)
    )
    monkeypatch.setattr(cli, "_load_cached_diarization", lambda _: None)
    monkeypatch.setattr(cli, "_parse_known_speakers", lambda _: known_speakers)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = cli.perform_azure_diarization(
        "https://youtu.be/exampleid", "en", known_speakers=known_speakers
    )

    assert captured["chunking_strategy"] == {"type": "auto"}
    assert captured["extra_body"]["chunking_strategy"] == {"type": "auto"}
    assert result["speakers"][0]["speaker"] == "Speaker 1"


def test_perform_azure_diarization_passes_known_speaker_names(
    monkeypatch: pytest.MonkeyPatch, dummy_audio: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")

    captured: Dict[str, Any] = {}

    def fake_prepare_audio_cache(_: str) -> str:
        return str(dummy_audio)

    def fake_create(**kwargs: Any) -> Dict[str, Any]:
        captured.update(kwargs)
        if kwargs.get("known_speaker_names") != ["Alice", "Bob"]:
            raise RuntimeError("known_speaker_names 未正确传递")
        segment = {"start": "0.0", "end": "1.0", "speaker": "Speaker 1"}
        return {
            "segments": [segment],
            "diarization": {"segments": [segment]},
        }

    class FakeTranscriptions:
        def __init__(self) -> None:
            self.create = fake_create

    class FakeAzureClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    fake_openai = ModuleType("openai")
    fake_openai.AzureOpenAI = FakeAzureClient  # type: ignore[attr-defined]

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "diarization.json"

    monkeypatch.setattr(cli, "_prepare_audio_cache", fake_prepare_audio_cache)
    monkeypatch.setattr(
        cli, "_resolve_video_cache_dir", lambda __: str(cache_dir)
    )
    monkeypatch.setattr(
        cli, "_diarization_cache_path", lambda directory: str(cache_file)
    )
    monkeypatch.setattr(cli, "_load_cached_diarization", lambda _: None)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = cli.perform_azure_diarization(
        "https://youtu.be/exampleid",
        "en",
        known_speaker_names=["Alice", "Bob"],
    )

    assert result["speakers"][0]["speaker"] == "Speaker 1"


def test_perform_azure_diarization_wraps_bad_request_error(
    monkeypatch: pytest.MonkeyPatch, dummy_audio: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "diarization.json"

    def fake_prepare_audio_cache(_: str) -> str:
        return str(dummy_audio)

    class FakeBadRequestError(Exception):
        def __init__(self, message: str):
            super().__init__(message)
            self.response = None
            self.body = {"error": {"message": message}}

    def fake_create(**_kwargs: Any) -> Dict[str, Any]:
        raise FakeBadRequestError("Audio file might be corrupted or unsupported")

    class FakeTranscriptions:
        def __init__(self) -> None:
            self.create = fake_create

    class FakeAzureClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    fake_openai = ModuleType("openai")
    fake_openai.AzureOpenAI = FakeAzureClient  # type: ignore[attr-defined]
    fake_openai.BadRequestError = FakeBadRequestError  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "_prepare_audio_cache", fake_prepare_audio_cache)
    monkeypatch.setattr(
        cli, "_resolve_video_cache_dir", lambda __: str(cache_dir)
    )
    monkeypatch.setattr(
        cli, "_diarization_cache_path", lambda directory: str(cache_file)
    )
    monkeypatch.setattr(cli, "_load_cached_diarization", lambda _: None)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with pytest.raises(RuntimeError) as excinfo:
        cli.perform_azure_diarization("https://youtu.be/example", "en")

    message = str(excinfo.value)
    assert "Audio file might be corrupted" in message
    assert "--clean-cache" in message


def _write_silent_wav(path: Path, duration_seconds: float, sample_rate: int = 16000) -> None:
    frames = int(duration_seconds * sample_rate)
    if frames <= 0:
        frames = sample_rate
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


def test_perform_azure_diarization_offsets_segments_when_split(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """切分后的音频应多次调用 Azure 并正确偏移时间轴。"""

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_file = cache_dir / "diarization.json"

    base_audio = tmp_path / "audio.wav"
    _write_silent_wav(base_audio, duration_seconds=2.0)

    segment_paths = [tmp_path / "segment1.wav", tmp_path / "segment2.wav"]
    durations = {
        str(segment_paths[0]): 1.0,
        str(segment_paths[1]): 1.5,
    }
    for path, duration in durations.items():
        _write_silent_wav(Path(path), duration_seconds=duration)

    call_log: List[Dict[str, Any]] = []

    def fake_prepare_audio_cache(_: str) -> str:
        return str(base_audio)

    def fake_ensure_segments(_: str) -> List[str]:
        return [str(path) for path in segment_paths]

    def fake_duration(path: str) -> float:
        return durations[path]

    def fake_create(**kwargs: Any) -> Dict[str, Any]:
        call_log.append(kwargs)
        index = len(call_log)
        segment = {
            "start": 0.0,
            "end": 0.8,
            "speaker": f"Speaker {index}",
        }
        transcript = {
            "start": 0.0,
            "end": 0.8,
            "text": f"chunk-{index}",
        }
        return {
            "segments": [transcript],
            "diarization": {"segments": [segment]},
        }

    progress_updates: List[tuple[float, str]] = []

    def fake_progress(ratio: float, detail: str) -> None:
        progress_updates.append((ratio, detail))

    class FakeTranscriptions:
        def __init__(self) -> None:
            self.create = fake_create

    class FakeAzureClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    fake_openai = ModuleType("openai")
    fake_openai.AzureOpenAI = FakeAzureClient  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "_prepare_audio_cache", fake_prepare_audio_cache)
    monkeypatch.setattr(cli, "_ensure_audio_segments", fake_ensure_segments)
    monkeypatch.setattr(cli, "_get_wav_duration", fake_duration)
    monkeypatch.setattr(cli, "_resolve_video_cache_dir", lambda __: str(cache_dir))
    monkeypatch.setattr(
        cli, "_diarization_cache_path", lambda directory: str(cache_file)
    )
    monkeypatch.setattr(cli, "_load_cached_diarization", lambda _: None)
    monkeypatch.setattr(cli, "_update_progress_bar", fake_progress)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = cli.perform_azure_diarization("https://youtu.be/example", "en")

    assert len(call_log) == 2
    speakers = result["speakers"]
    assert pytest.approx(speakers[0]["start"], rel=1e-4) == 0.0
    assert pytest.approx(speakers[1]["start"], rel=1e-4) == durations[str(segment_paths[0])]
    transcript = result["transcript"]
    assert transcript[0]["text"] == "chunk-1"
    assert transcript[1]["text"] == "chunk-2"
    assert transcript[1]["start"] > transcript[0]["end"]
    assert progress_updates, "progress updates should be recorded"
    assert pytest.approx(progress_updates[0][0], rel=1e-4) == 0.0
    assert progress_updates[-1][0] == pytest.approx(1.0, rel=1e-3)
    assert all(0.0 <= ratio <= 1.0 for ratio, _ in progress_updates)


def test_perform_azure_diarization_uses_cached_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF0000")

    cached_payload = {
        "speakers": [{"start": 0.0, "end": 1.0, "speaker": "Speaker 1"}],
        "transcript": [{"start": 0.0, "end": 1.0, "text": "hello"}],
    }
    cache_path = tmp_path / "diarization.json"
    cache_path.write_text(json.dumps(cached_payload))

    def fake_prepare_audio_cache(_: str) -> str:
        return str(audio_path)

    monkeypatch.setattr(cli, "_prepare_audio_cache", fake_prepare_audio_cache)
    monkeypatch.setattr(
        cli, "_resolve_video_cache_dir", lambda __: str(tmp_path)
    )
    monkeypatch.setattr(
        cli, "_diarization_cache_path", lambda directory: str(cache_path)
    )

    fake_openai = ModuleType("openai")

    class FakeAzureClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("AzureOpenAI should not be instantiated when cache exists")

    fake_openai.AzureOpenAI = FakeAzureClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = cli.perform_azure_diarization("https://youtu.be/exampleid", "en")

    assert result == cached_payload
