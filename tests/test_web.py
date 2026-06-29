"""
测试 web_fetch 工具（httpx.MockTransport，无真实网络）
"""
import httpx
import pytest

from coding_agent.tools.web_ops import WebFetchTool, html_to_text


def _patch_client(monkeypatch, transport):
    real_init = httpx.AsyncClient.__init__

    def patched(self, *a, **k):
        k["transport"] = transport
        real_init(self, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def test_html_to_text_strips_tags_and_scripts():
    html = """<html><head><title>Doc</title><style>x{}</style></head>
    <body><h1>Hello</h1><script>evil()</script><p>World text</p></body></html>"""
    text, title = html_to_text(html)
    assert title == "Doc"
    assert "Hello" in text
    assert "World text" in text
    assert "evil()" not in text
    assert "x{}" not in text


@pytest.mark.asyncio
async def test_fetch_html_returns_text(monkeypatch):
    html = "<html><head><title>T</title></head><body><p>Body content here</p></body></html>"
    _patch_client(monkeypatch, httpx.MockTransport(
        lambda r: httpx.Response(200, content=html.encode(),
                                 headers={"content-type": "text/html"})))
    out = await WebFetchTool().execute(url="https://example.com/doc")
    assert "# T" in out
    assert "Body content here" in out
    assert "https://example.com/doc" in out


@pytest.mark.asyncio
async def test_fetch_rejects_non_http():
    out = await WebFetchTool().execute(url="ftp://x/y")
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_fetch_http_error(monkeypatch):
    _patch_client(monkeypatch, httpx.MockTransport(
        lambda r: httpx.Response(404, content=b"nope")))
    out = await WebFetchTool().execute(url="https://example.com/missing")
    assert "HTTP 404" in out


@pytest.mark.asyncio
async def test_fetch_plaintext(monkeypatch):
    _patch_client(monkeypatch, httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"raw plain text",
                                 headers={"content-type": "text/plain"})))
    out = await WebFetchTool().execute(url="https://example.com/file.txt")
    assert "raw plain text" in out


@pytest.mark.asyncio
async def test_fetch_truncates(monkeypatch):
    big = "<p>" + ("A" * 50000) + "</p>"
    _patch_client(monkeypatch, httpx.MockTransport(
        lambda r: httpx.Response(200, content=big.encode(),
                                 headers={"content-type": "text/html"})))
    out = await WebFetchTool().execute(url="https://example.com/big", max_chars=1000)
    assert "truncated at 1000 chars" in out
