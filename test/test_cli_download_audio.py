"""测试 download_audio_stream 的 yt-dlp 调用参数。"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Dict

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from podcast_transformer import cli


class FakeDownloadError(Exception):
    """模拟 yt_dlp 下载异常。"""


class FakeYoutubeDL:
    """记录传入选项的伪造 YoutubeDL。"""

    latest_options: Dict[str, Any] | None = None
    last_url: str | None = None

    def __init__(self, options: Dict[str, Any]):
        FakeYoutubeDL.latest_options = options
        self._options = options

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> Dict[str, Any]:
        FakeYoutubeDL.last_url = url
        assert download is True
        return {"id": "abc123", "title": "sample", "ext": "m4a"}

    def prepare_filename(self, info: Dict[str, Any]) -> str:
        template = self._options["outtmpl"]
        ext = info.get("ext", "m4a")
        path = template.replace("%(ext)s", ext)
        Path(path).touch()
        return path


@pytest.fixture(autouse=True)
def reset_fake_state() -> None:
    FakeYoutubeDL.latest_options = None
    FakeYoutubeDL.last_url = None


def test_download_audio_stream_injects_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """download_audio_stream 应传递 UA 和可选 cookie 以确保 URL 可下载。"""

    target_url = "https://www.youtube.com/watch?v=lXUZvyajciY&t=50s"
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    fake_module = ModuleType("yt_dlp")
    fake_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    utils_module.std_headers = {
        "Accept": "*/*",
        "Accept-Language": "es-ES,es;q=0.9",
    }
    fake_module.utils = utils_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils_module)
    monkeypatch.setenv("PODCAST_TRANSFORMER_YTDLP_COOKIES", str(cookie_file))

    result = cli.download_audio_stream(target_url, str(tmp_path))

    assert FakeYoutubeDL.last_url == target_url
    expected_path = os.path.join(tmp_path, "audio.m4a")
    assert result == expected_path

    options = FakeYoutubeDL.latest_options
    assert options is not None
    headers = options.get("http_headers")
    assert isinstance(headers, dict)
    assert headers.get("User-Agent", "").startswith("Mozilla/")
    assert headers.get("Accept") == "*/*"
    assert headers.get("Accept-Language") == "es-ES,es;q=0.9"
    assert headers.get("Referer") == "https://www.youtube.com/"
    assert options.get("cookiefile") == str(cookie_file)


def test_download_audio_stream_android_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """403 Forbidden 时应尝试 Android 客户端回退并成功下载。"""

    target_url = "https://www.youtube.com/watch?v=fake403"

    class FlakyYoutubeDL:
        """首次调用抛出 403，第二次成功的伪造 YoutubeDL。"""

        call_count = 0
        options_history: list[dict[str, Any]] = []

        def __init__(self, options: Dict[str, Any]):
            self._options = options
            FlakyYoutubeDL.options_history.append(options)

        def __enter__(self) -> FlakyYoutubeDL:
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def extract_info(self, url: str, download: bool) -> Dict[str, Any]:
            assert url == target_url
            assert download is True
            FlakyYoutubeDL.call_count += 1
            if FlakyYoutubeDL.call_count == 1:
                raise FakeDownloadError("ERROR: HTTP Error 403: Forbidden")
            return {"id": "resolved", "title": "ok", "ext": "webm"}

        def prepare_filename(self, info: Dict[str, Any]) -> str:
            template = self._options["outtmpl"]
            ext = info.get("ext", "webm")
            path = template.replace("%(ext)s", ext)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"data")
            return path

    fake_module = ModuleType("yt_dlp")
    fake_module.YoutubeDL = FlakyYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    utils_module.std_headers = {"Accept": "*/*"}
    fake_module.utils = utils_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils_module)
    monkeypatch.delenv("PODCAST_TRANSFORMER_YTDLP_COOKIES", raising=False)

    result = cli.download_audio_stream(target_url, str(tmp_path))

    assert result == os.path.join(tmp_path, "audio.webm")
    assert FlakyYoutubeDL.call_count == 2
    assert len(FlakyYoutubeDL.options_history) == 2

    first_opts = FlakyYoutubeDL.options_history[0]
    second_opts = FlakyYoutubeDL.options_history[1]

    assert not first_opts.get("extractor_args")

    extractor_args = second_opts.get("extractor_args")
    assert extractor_args is not None
    youtube_args = extractor_args.get("youtube")
    assert youtube_args is not None
    assert youtube_args.get("player_client") == ["android"]

    fallback_headers = second_opts.get("http_headers")
    assert fallback_headers is not None
    assert "Android" in fallback_headers.get("User-Agent", "")


def test_download_audio_stream_non_youtube_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """非 YouTube 站点应保持原始域名 Referer。"""

    target_url = "https://www.bilibili.com/video/BV1xx411c7mD"

    class RecorderYoutubeDL:
        last_options: dict[str, Any] | None = None

        def __init__(self, options: Dict[str, Any]):
            RecorderYoutubeDL.last_options = options
            self._options = options

        def __enter__(self) -> RecorderYoutubeDL:
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

        def extract_info(self, url: str, download: bool) -> Dict[str, Any]:
            assert url == target_url
            assert download is True
            return {"id": "bv1", "title": "demo", "ext": "mp3"}

        def prepare_filename(self, info: Dict[str, Any]) -> str:
            path = self._options["outtmpl"].replace("%(ext)s", info.get("ext", "mp3"))
            Path(path).write_bytes(b"data")
            return path

    fake_module = ModuleType("yt_dlp")
    fake_module.YoutubeDL = RecorderYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    utils_module.std_headers = {}
    fake_module.utils = utils_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils_module)

    result = cli.download_audio_stream(target_url, str(tmp_path))

    assert result == os.path.join(tmp_path, "audio.mp3")

    options = RecorderYoutubeDL.last_options
    assert options is not None
    headers = options.get("http_headers")
    assert isinstance(headers, dict)
    assert headers.get("Referer") == "https://www.bilibili.com/"
