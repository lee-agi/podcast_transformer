"""Test suite for podcast_transformer CLI tool."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List

import pytest


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(PROJECT_ROOT)
if PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, PACKAGE_ROOT)

import podcast_transformer.cli as cli


def test_cli_outputs_transcript_with_speaker_annotations(monkeypatch, capsys):
    """CLI should merge transcript and speaker data into JSON output."""

    def fake_fetch_transcript(video_url: str, language: str, fallback_languages: List[str]):
        assert video_url == "https://youtu.be/example"
        assert language == "en"
        assert fallback_languages == ["en"]
        return [
            {"start": 0.0, "end": 2.5, "text": "Hello world."},
            {"start": 2.5, "end": 5.2, "text": "Second sentence."},
        ]

    def fake_perform_diarization(
        video_url: str,
        language: str,
        max_speakers: int | None = None,
        known_speakers=None,
        known_speaker_names=None,
    ):
        assert video_url == "https://youtu.be/example"
        assert language == "en"
        assert max_speakers is None
        assert known_speakers is None
        assert known_speaker_names is None
        return {
            "speakers": [
                {"start": 0.0, "end": 3.0, "speaker": "Speaker A"},
                {"start": 3.0, "end": 6.0, "speaker": "Speaker B"},
            ],
            "transcript": None,
        }

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "perform_azure_diarization", fake_perform_diarization)

    exit_code = cli.run([
        "--url",
        "https://youtu.be/example",
        "--language",
        "en",
        "--azure-diarization",
    ])

    assert exit_code == 0

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert payload[0]["speaker"] == "Speaker A"
    assert payload[1]["speaker"] == "Speaker B"
    assert payload[0]["start"] == 0.0
    assert payload[1]["end"] == 5.2


def test_cli_fallbacks_to_azure_transcription(monkeypatch, capsys):
    """When YouTube transcript is unavailable, Azure fallback should be used."""

    def fake_fetch_transcript(video_url: str, language: str, fallback_languages: List[str]):
        raise RuntimeError("No subtitles available")

    def fake_perform_diarization(
        video_url: str,
        language: str,
        max_speakers: int | None = None,
        known_speakers=None,
        known_speaker_names=None,
    ):
        return {
            "speakers": [
                {"start": 0.0, "end": 3.0, "speaker": "Speaker A"},
                {"start": 3.0, "end": 6.0, "speaker": "Speaker B"},
            ],
            "transcript": [
                {"start": 0.0, "end": 3.0, "text": "Hello world."},
                {"start": 3.0, "end": 6.0, "text": "Second sentence."},
            ],
        }

    monkeypatch.setattr(
        cli, "fetch_transcript_with_metadata", fake_fetch_transcript
    )
    monkeypatch.setattr(cli, "perform_azure_diarization", fake_perform_diarization)

    exit_code = cli.run([
        "--url",
        "https://youtu.be/example",
        "--language",
        "en",
        "--azure-diarization",
    ])

    assert exit_code == 0

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert payload[0]["text"] == "Hello world."
    assert payload[1]["speaker"] == "Speaker B"


def test_prepare_audio_uses_cache(monkeypatch, tmp_path):
    """Cached WAV file should bypass re-download and reconversion."""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(cache_dir))

    video_url = "https://youtu.be/example"
    video_id = cli.extract_video_id(video_url)
    assert video_id is not None

    video_dir = cache_dir / "youtube" / video_id
    video_dir.mkdir(parents=True)
    wav_path = video_dir / "audio.wav"
    wav_path.write_bytes(b"fake-audio")

    download_called = False
    convert_called = False

    def fake_download(url: str, directory: str) -> str:
        nonlocal download_called
        download_called = True
        return str(video_dir / "audio.m4a")

    def fake_convert(src: str, dst: str) -> None:
        nonlocal convert_called
        convert_called = True

    monkeypatch.setattr(cli, "download_audio_stream", fake_download)
    monkeypatch.setattr(cli, "convert_audio_to_wav", fake_convert)

    cached_path = cli._prepare_audio_cache(video_url)

    assert cached_path == str(wav_path)
    assert not download_called
    assert not convert_called


def test_resolve_video_cache_dir_non_youtube(monkeypatch, tmp_path):
    """非 YouTube URL 应使用稳定哈希目录。"""

    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(tmp_path))

    url = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    cache_dir = cli._resolve_video_cache_dir(url)

    assert cache_dir.startswith(str(tmp_path))
    assert "bilibili" in cache_dir
    assert os.path.isdir(cache_dir)


def test_build_extra_body(tmp_path):
    audio_file = tmp_path / "speaker.wav"
    audio_file.write_bytes(b"fake-audio")

    payload = cli._build_extra_body([("Agent", str(audio_file))])

    assert payload["known_speaker_names"] == ["Agent"]
    assert payload["known_speaker_references"][0].startswith("data:audio/wav;base64,")


def test_parse_known_speakers_invalid():
    with pytest.raises(RuntimeError):
        cli._parse_known_speakers(["invalid-format"])


def test_cli_clean_cache_removes_cached_directory(monkeypatch, tmp_path, capsys):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(cache_root))

    url = "https://www.youtube.com/watch?v=exampleid"
    video_dir = cache_root / "exampleid"
    video_dir.mkdir()
    (video_dir / "audio.wav").write_bytes(b"fake-audio")

    removed: dict[str, str] = {}

    def fake_rmtree(path: str, **kwargs):
        removed["path"] = path

    def fake_fetch_transcript(*_args, **_kwargs):
        return [{"start": 0.0, "end": 1.0, "text": "Hi"}]

    monkeypatch.setattr(cli.shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)

    exit_code = cli.run([
        "--url",
        url,
        "--clean-cache",
    ])

    assert exit_code == 0
    expected = cli._resolve_video_cache_dir(url)
    assert removed["path"] == expected
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload[0]["text"] == "Hi"


def test_cli_loads_env_file(monkeypatch, tmp_path):
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "AZURE_OPENAI_API_KEY=dotenv-key\nAZURE_OPENAI_ENDPOINT=https://example.invalid\n"
    )

    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.setenv("PODCAST_TRANSFORMER_DOTENV", str(dotenv_path))

    def fake_fetch_transcript(*_args, **_kwargs):
        return [{"start": 0.0, "end": 1.0, "text": "Hello"}]

    def fake_perform_diarization(*_args, **_kwargs):
        assert os.getenv("AZURE_OPENAI_API_KEY") == "dotenv-key"
        assert os.getenv("AZURE_OPENAI_ENDPOINT") == "https://example.invalid"
        return {
            "speakers": [{"start": 0.0, "end": 1.0, "speaker": "Speaker"}],
            "transcript": None,
        }

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "perform_azure_diarization", fake_perform_diarization)

    exit_code = cli.run(["--url", "https://youtu.be/env", "--azure-diarization"])

    assert exit_code == 0


def test_cli_check_cache_reports_status(monkeypatch, tmp_path, capsys):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(cache_root))

    url = "https://youtu.be/check"
    video_dir = Path(cli._resolve_video_cache_dir(url))
    audio_path = video_dir / "audio.wav"
    audio_path.write_bytes(b"audio")

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("fetch_transcript should not be called")

    def fail_diarization(*_args, **_kwargs):
        raise AssertionError("perform_azure_diarization should not be called")

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fail_fetch)
    monkeypatch.setattr(cli, "perform_azure_diarization", fail_diarization)

    exit_code = cli.run(["--url", url, "--check-cache"])

    assert exit_code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["cache"]["path"] == cli._resolve_video_cache_dir(url)
    assert payload["cache"]["audio_wav_exists"] is True
    assert "audio.wav" in payload["cache"]["files"]
