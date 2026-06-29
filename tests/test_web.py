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


# ── web_search ──────────────────────────────────────────────────────────────

from coding_agent.tools.web_ops import WebSearchTool, parse_search_results, _decode_ddg_href

_DDG_HTML = """
<html><body>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html&rut=x">asyncio — Asynchronous I/O</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org">asyncio is a library to write concurrent code using async/await syntax.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync-io-python%2F&rut=y">Async IO in Python</a>
  <a class="result__snippet">A complete walkthrough of async IO.</a>
</div>
</body></html>
"""


def test_decode_ddg_href():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=abc"
    assert _decode_ddg_href(href) == "https://docs.python.org/3/"
    assert _decode_ddg_href("//example.com/x") == "https://example.com/x"
    assert _decode_ddg_href("") == ""


def test_parse_search_results():
    results = parse_search_results(_DDG_HTML)
    assert len(results) == 2
    assert results[0]["title"] == "asyncio — Asynchronous I/O"
    assert results[0]["url"] == "https://docs.python.org/3/library/asyncio.html"
    assert "concurrent code" in results[0]["snippet"]
    assert results[1]["url"] == "https://realpython.com/async-io-python/"


def test_parse_search_results_respects_limit():
    assert len(parse_search_results(_DDG_HTML, limit=1)) == 1


@pytest.mark.asyncio
async def test_web_search_formats_results(monkeypatch):
    _patch_client(monkeypatch, httpx.MockTransport(
        lambda r: httpx.Response(200, content=_DDG_HTML.encode())))
    out = await WebSearchTool().execute(query="python asyncio")
    assert "docs.python.org" in out
    assert "1. asyncio" in out
    assert "realpython.com" in out


@pytest.mark.asyncio
async def test_web_search_empty_query():
    out = await WebSearchTool().execute(query="")
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_web_search_no_results(monkeypatch):
    _patch_client(monkeypatch, httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"<html><body>nothing</body></html>")))
    out = await WebSearchTool().execute(query="zzzznomatch")
    assert "No results found" in out
