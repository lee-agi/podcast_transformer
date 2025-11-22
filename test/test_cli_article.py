"""测试 CLI 处理网页内容的能力。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from any2summary import cli


def _decode_json_stream(raw: str) -> list[Any]:
    decoder = json.JSONDecoder()
    items: list[Any] = []
    index = 0
    length = len(raw)
    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        obj, offset = decoder.raw_decode(raw, index)
        items.append(obj)
        index = offset
    return items


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

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))
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

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

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
        assert prompt == cli._load_default_article_prompt()
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


def test_cli_article_custom_prompt_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """文章模式应优先使用 --article-summary-prompt-file 指定的 Prompt。"""

    target_url = "https://www.example.com/posts/demo"
    article_prompt_path = tmp_path / "article_prompt.txt"
    custom_prompt_text = "# 自定义文章 Prompt"
    article_prompt_path.write_text(custom_prompt_text, encoding="utf-8")

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    def fake_fetch_transcript(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("unable to fetch transcript")

    def fail_azure(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover - 防御
        raise AssertionError("should not call azure diarization")

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "perform_azure_diarization", fail_azure)

    bundle = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "First paragraph."},
        ],
        "metadata": {
            "title": "Article",
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
        "metadata": {"title": "Article"},
        "file_base": "article",
    }

    def fake_generate_summary(
        segments: list[dict[str, Any]],
        video_url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        assert prompt == custom_prompt_text
        return summary_payload

    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate_summary)

    exit_code = cli.run(
        [
            "--url",
            target_url,
            "--language",
            "en",
            "--azure-summary",
            "--article-summary-prompt-file",
            str(article_prompt_path),
        ]
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)


def test_cli_article_ignores_summary_prompt_file_for_articles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """文章模式下应忽略 --summary-prompt-file，默认走文章 Prompt。"""

    target_url = "https://www.example.com/posts/prompt"
    summary_prompt_path = tmp_path / "summary_prompt.txt"
    summary_prompt_text = "# 自定义音频 Prompt"
    summary_prompt_path.write_text(summary_prompt_text, encoding="utf-8")

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    def fake_fetch_transcript(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("unable to fetch transcript")

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)

    bundle = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Article paragraph."},
        ],
        "metadata": {
            "title": "Article",
            "webpage_url": target_url,
        },
        "raw_html_path": str(tmp_path / "prompt.html"),
        "content_path": str(tmp_path / "prompt.txt"),
        "metadata_path": str(tmp_path / "prompt.json"),
        "icon_path": str(tmp_path / "prompt.png"),
    }

    (tmp_path / "prompt.html").write_text("raw", encoding="utf-8")
    (tmp_path / "prompt.txt").write_text("text", encoding="utf-8")
    (tmp_path / "prompt.json").write_text("{}", encoding="utf-8")
    (tmp_path / "prompt.png").write_bytes(b"icon")

    monkeypatch.setattr(
        cli, "fetch_article_assets", lambda url: bundle if url == target_url else None
    )

    summary_payload = {
        "summary_markdown": "# Summary\n",
        "timeline_markdown": "## Timeline\n",
        "metadata": {"title": "Article"},
        "file_base": "article",
    }

    def fake_generate_summary(
        segments: list[dict[str, Any]],
        video_url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        assert prompt == cli._load_default_article_prompt()
        assert video_url == target_url
        assert segments == bundle["segments"]
        return summary_payload

    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate_summary)

    exit_code = cli.run(
        [
            "--url",
            target_url,
            "--language",
            "en",
            "--azure-summary",
            "--summary-prompt-file",
            str(summary_prompt_path),
        ]
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)


def test_cli_article_uses_default_article_prompt_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target_url = "https://www.example.com/posts/default-prompt"
    default_prompt_path = tmp_path / "article_prompt.txt"
    default_prompt_text = "# 默认文章 Prompt"
    default_prompt_path.write_text(default_prompt_text, encoding="utf-8")

    monkeypatch.setattr(cli, "DEFAULT_ARTICLE_PROMPT_PATH", default_prompt_path)
    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    def fake_fetch_transcript(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("unable to fetch transcript")

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)

    bundle = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Article paragraph."},
        ],
        "metadata": {
            "title": "Article",
            "webpage_url": target_url,
        },
        "raw_html_path": str(tmp_path / "default.html"),
        "content_path": str(tmp_path / "default.txt"),
        "metadata_path": str(tmp_path / "default.json"),
        "icon_path": str(tmp_path / "default.png"),
    }

    (tmp_path / "default.html").write_text("raw", encoding="utf-8")
    (tmp_path / "default.txt").write_text("text", encoding="utf-8")
    (tmp_path / "default.json").write_text("{}", encoding="utf-8")
    (tmp_path / "default.png").write_bytes(b"icon")

    monkeypatch.setattr(
        cli, "fetch_article_assets", lambda url: bundle if url == target_url else None
    )

    summary_payload = {
        "summary_markdown": "# Summary\n",
        "timeline_markdown": "## Timeline\n",
        "metadata": {"title": "Article"},
        "file_base": "article",
    }

    captured_prompt: Dict[str, Any] = {}

    def fake_generate_summary(
        segments: list[dict[str, Any]],
        video_url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        captured_prompt["prompt"] = prompt
        return summary_payload

    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate_summary)

    exit_code = cli.run(
        [
            "--url",
            target_url,
            "--language",
            "en",
            "--azure-summary",
        ]
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)
    assert captured_prompt["prompt"] == default_prompt_text


def test_cli_article_disables_azure_diarization_when_article_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """即便启用 Azure diarization，也应在文章模式下短路并使用文章 Prompt。"""

    target_url = "https://www.example.com/posts/azure"
    summary_prompt_path = tmp_path / "summary_prompt.txt"
    summary_prompt_path.write_text("# 语音 Prompt", encoding="utf-8")

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    def fake_fetch_transcript(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("unable to fetch transcript")

    def fail_diarization(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("article mode should not invoke Azure diarization")

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "perform_azure_diarization", fail_diarization)

    bundle = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Article paragraph."},
        ],
        "metadata": {
            "title": "Article",
            "webpage_url": target_url,
        },
        "raw_html_path": str(tmp_path / "azure.html"),
        "content_path": str(tmp_path / "azure.txt"),
        "metadata_path": str(tmp_path / "azure.json"),
        "icon_path": str(tmp_path / "azure.png"),
    }

    (tmp_path / "azure.html").write_text("raw", encoding="utf-8")
    (tmp_path / "azure.txt").write_text("text", encoding="utf-8")
    (tmp_path / "azure.json").write_text("{}", encoding="utf-8")
    (tmp_path / "azure.png").write_bytes(b"icon")

    monkeypatch.setattr(
        cli, "fetch_article_assets", lambda url: bundle if url == target_url else None
    )

    summary_payload = {
        "summary_markdown": "# Summary\n",
        "timeline_markdown": "## Timeline\n",
        "metadata": {"title": "Article"},
        "file_base": "article",
    }

    def fake_generate_summary(
        segments: list[dict[str, Any]],
        video_url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        assert prompt == cli._load_default_article_prompt()
        assert metadata == bundle["metadata"]
        return summary_payload

    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate_summary)

    exit_code = cli.run(
        [
            "--url",
            target_url,
            "--language",
            "en",
            "--force-azure-diarization",
            "--azure-summary",
            "--summary-prompt-file",
            str(summary_prompt_path),
        ]
    )

    assert exit_code == 0
    json.loads(capsys.readouterr().out)


def test_cli_podcast_url_prefers_media_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Apple Podcasts 链接应触发音频流程并使用 SUMMARY_PROMPT。"""

    target_url = (
        "https://podcasts.apple.com/cn/podcast/demo/id1634356920?i=1000726193755"
    )

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    def fake_fetch_transcript(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("no captions")

    azure_called = {"count": 0}

    def fake_diarization(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        azure_called["count"] += 1
        return {
            "speakers": [],
            "transcript": [
                {"start": 0.0, "end": 1.0, "text": "Podcast intro."},
            ],
        }

    def fail_article(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("podcast URL should not fetch article assets")

    summary_payload = {
        "summary_markdown": "# Summary\n",
        "timeline_markdown": "## Timeline\n",
        "metadata": {"title": "Podcast"},
        "file_base": "podcast",
    }

    def fake_generate_summary(
        segments: list[dict[str, Any]],
        video_url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        assert prompt != cli._load_default_article_prompt()
        assert video_url == target_url
        assert segments[0]["text"] == "Podcast intro."
        return summary_payload

    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    monkeypatch.setattr(cli, "perform_azure_diarization", fake_diarization)
    monkeypatch.setattr(cli, "fetch_article_assets", fail_article)
    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate_summary)

    exit_code = cli.run(["--url", target_url, "--azure-summary"])

    assert exit_code == 0
    assert azure_called["count"] == 1
    json.loads(capsys.readouterr().out)


def test_cli_handles_multiple_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 应支持多个 URL 并保证各自输出。"""

    urls = [
        "https://example.com/article-1",
        "https://example.com/article-2",
    ]

    transcripts = {
        urls[0]: [
            {"start": 0.0, "end": 1.0, "text": "First article."},
        ],
        urls[1]: [
            {"start": 0.0, "end": 1.5, "text": "Second article."},
        ],
    }

    def fake_fetch_transcript(video_url: str, language: str, fallback_languages: List[str]):
        assert language == "en"
        assert fallback_languages == ["en"]
        return [dict(segment) for segment in transcripts[video_url]]

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    def fake_article_assets(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("no article")

    monkeypatch.setattr(cli, "fetch_article_assets", fake_article_assets)

    def fail_diarization(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("azure failed")

    monkeypatch.setattr(cli, "perform_azure_diarization", fail_diarization)

    exit_code = cli.run([
        "--url",
        ",".join(urls),
        "--language",
        "en",
    ])

    assert exit_code == 0
    captured = capsys.readouterr()
    payloads = _decode_json_stream(captured.out)
    assert len(payloads) == 2
    payload_first, payload_second = payloads
    assert payload_first[0]["text"] == "First article."
    assert payload_second[0]["text"] == "Second article."


def test_cli_multiple_urls_continues_on_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """某个 URL 失败时应继续处理其它 URL 并报告错误。"""

    success_url = "https://example.com/good"
    fail_url = "https://example.com/bad"

    def fake_fetch_transcript(video_url: str, language: str, fallback_languages: List[str]):
        if video_url == success_url:
            return [{"start": 0.0, "end": 1.0, "text": "Good."}]
        raise RuntimeError("transcript unavailable")

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "fetch_transcript_with_metadata", fake_fetch_transcript)
    def fake_article_assets_fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("no article")

    monkeypatch.setattr(cli, "fetch_article_assets", fake_article_assets_fail)

    def fail_diarization(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("azure failed")

    monkeypatch.setattr(cli, "perform_azure_diarization", fail_diarization)

    exit_code = cli.run([
        "--url",
        f"{success_url},{fail_url}",
        "--language",
        "en",
    ])

    assert exit_code == 1
    captured = capsys.readouterr()
    payloads = _decode_json_stream(captured.out)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload[0]["text"] == "Good."
    assert "transcript unavailable" in captured.err
