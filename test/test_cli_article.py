"""测试 CLI 处理网页内容的能力。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from podcast_transformer import cli


class _FakeResponse:
    """轻量级 HTTP 响应对象，模拟 httpx.Response。"""

    def __init__(self, text: str, content: bytes, headers: Dict[str, str] | None = None):
        self._text = text
        self.content = content
        self.headers = headers or {}
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    @property
    def text(self) -> str:
        return self._text


class _FakeHttpClient:
    """返回预置响应的伪造 httpx.Client。"""

    def __init__(self, responses: Dict[str, _FakeResponse]):
        self._responses = responses

    def __enter__(self) -> _FakeHttpClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        return False

    def get(self, url: str, headers: Dict[str, str] | None = None) -> _FakeResponse:
        if url not in self._responses:
            raise AssertionError(f"未模拟的 URL: {url}")
        return self._responses[url]


@pytest.fixture(name="article_html")
def _article_html_fixture() -> str:
    """返回用于测试的网页 HTML 内容。"""

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\">\n"
        "    <title>Effective Context Engineering for AI Agents</title>\n"
        "    <meta name=\"description\" content=\"A guide for large context.\">\n"
        "    <link rel=\"icon\" href=\"/static/icon.png\">\n"
        "  </head>\n"
        "  <body>\n"
        "    <main>\n"
        "      <h1>Effective Context Engineering for AI Agents</h1>\n"
        "      <p>First paragraph introducing the key concepts.</p>\n"
        "      <p>Second paragraph with more details.</p>\n"
        "    </main>\n"
        "  </body>\n"
        "</html>\n"
    )


def test_fetch_article_assets_writes_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, article_html: str
) -> None:
    """应下载网页、提取正文并缓存原始与解析后的内容。"""

    target_url = "https://www.example.com/posts/demo"
    favicon_url = "https://www.example.com/static/icon.png"
    icon_bytes = b"PNGDATA"

    responses = {
        target_url: _FakeResponse(
            article_html, article_html.encode("utf-8"), {"Content-Type": "text/html"}
        ),
        favicon_url: _FakeResponse("", icon_bytes, {"Content-Type": "image/png"}),
    }

    def fake_client_factory() -> _FakeHttpClient:
        return _FakeHttpClient(responses)

    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli, "_create_http_client", lambda: fake_client_factory(), raising=False
    )

    bundle = cli.fetch_article_assets(target_url)

    assert bundle["metadata"]["title"] == "Effective Context Engineering for AI Agents"
    assert bundle["metadata"]["description"] == "A guide for large context."

    raw_path = Path(bundle["raw_html_path"])
    content_path = Path(bundle["content_path"])
    metadata_path = Path(bundle["metadata_path"])
    icon_path = Path(bundle["icon_path"])

    assert raw_path.exists()
    assert content_path.exists()
    assert metadata_path.exists()
    assert icon_path.exists()

    assert raw_path.read_text(encoding="utf-8") == article_html
    content_text = content_path.read_text(encoding="utf-8")
    assert "First paragraph" in content_text
    assert "Second paragraph" in content_text

    segments = bundle["segments"]
    assert len(segments) == 2
    assert segments[0]["text"].startswith("First paragraph")
    assert segments[1]["start"] == pytest.approx(1.0)


def test_cli_processes_article_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 在网页模式下应直接调用摘要逻辑并输出段落内容。"""

    target_url = "https://www.example.com/posts/demo"

    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(tmp_path))

    def fake_fetch_transcript(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("unable to fetch transcript")

    def fail_azure(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - 防御
        raise AssertionError("should not call azure diarization")

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "perform_azure_diarization", fail_azure)

    bundle = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "First paragraph introducing the key concepts."},
            {"start": 1.0, "end": 2.0, "text": "Second paragraph with more details."},
        ],
        "metadata": {
            "title": "Effective Context Engineering for AI Agents",
            "webpage_url": target_url,
        },
        "raw_html_path": str(tmp_path / "article.html"),
        "content_path": str(tmp_path / "article.txt"),
        "metadata_path": str(tmp_path / "article.json"),
        "icon_path": str(tmp_path / "icon.png"),
    }

    (tmp_path / "article.html").write_text("raw", encoding="utf-8")
    (tmp_path / "article.txt").write_text("text", encoding="utf-8")
    (tmp_path / "article.json").write_text("{}", encoding="utf-8")
    (tmp_path / "icon.png").write_bytes(b"icon")

    monkeypatch.setattr(
        cli, "fetch_article_assets", lambda url: bundle if url == target_url else None
    )

    summary_payload = {
        "summary_markdown": "# Summary\n",
        "timeline_markdown": "## Timeline\n",
        "metadata": {"title": "Effective Context Engineering"},
        "file_base": "demo",
        "total_words": 42,
        "estimated_minutes": 1,
    }

    def fake_generate_summary(
        segments: list[dict[str, Any]],
        video_url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        assert video_url == target_url
        assert segments == bundle["segments"]
        assert metadata == bundle["metadata"]
        return summary_payload

    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate_summary)

    exit_code = cli.run(["--url", target_url, "--language", "en", "--azure-summary"])

    assert exit_code == 0

    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert payload["segments"][0]["text"].startswith("First paragraph")
    assert payload["summary"] == "# Summary\n"
    assert payload["summary_path"].endswith("demo_summary.md")
    assert os.path.exists(payload["summary_path"])

