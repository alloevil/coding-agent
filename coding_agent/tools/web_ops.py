"""
web_fetch 工具 - 抓取 URL 并转为可读文本

参考 Claude Code / Codex 的 web fetch：把网页抓下来、剥离 HTML 标签、
返回限长的可读文本，供模型查文档 / issue / API 参考。

设计：
- 依赖已有的 httpx（不引入新依赖），HTML 剥离用 stdlib html.parser
- 限制响应大小与输出长度，避免撑爆 context
- 只读语义（READ 权限），但仍是对外请求——交由上层权限/沙箱裁决
- 跨主机重定向会被 httpx 跟随；超时与非 2xx 返回明确错误
"""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import httpx

from .base import Tool, ToolPermission

# 抓取上限
MAX_BYTES = 2 * 1024 * 1024
# 输出文本上限（再交给 registry 的 max_result_chars 兜底）
MAX_TEXT_CHARS = 20000
# 这些标签内的内容直接丢弃（脚本、样式等）
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
# 这些标签视为块级，转换时补换行
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "pre", "blockquote", "ul", "ol",
}


class _TextExtractor(HTMLParser):
    """把 HTML 抽成纯文本：丢弃脚本/样式，块级标签补换行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        # 标题在 <head> 内（head 属于跳过标签），需在 skip 检查之前捕获
        if self._in_title and self._title is None and data.strip():
            self._title = data.strip()
        if self._skip_depth > 0:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # 折叠多余空白：每行去尾空格，连续空行压成一行
        lines = [ln.rstrip() for ln in text.splitlines()]
        out: list[str] = []
        blank = False
        for ln in lines:
            if not ln.strip():
                if not blank:
                    out.append("")
                blank = True
            else:
                out.append(ln.strip())
                blank = False
        return "\n".join(out).strip()

    @property
    def title(self) -> str | None:
        return self._title


def html_to_text(html: str) -> tuple[str, str | None]:
    """返回 (正文文本, 标题)。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # 解析失败则退回原始内容（已是文本）
        return html, None
    return parser.get_text(), parser.title


class WebFetchTool(Tool):
    """抓取一个 URL，返回可读文本。"""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch a URL over HTTP(S) and return its readable text content "
            "(HTML is stripped to text). Use this to read documentation, API "
            "references, issues, or any web page. Returns title + text, capped "
            "in length. Not for authenticated/private endpoints."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (http:// or https://)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": f"Max characters of text to return (default {MAX_TEXT_CHARS})",
                },
            },
            "required": ["url"],
        }

    @property
    def permission(self) -> ToolPermission:
        # 只读，但属于对外请求；上层可按需要升级为需确认
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url")
        max_chars = kwargs.get("max_chars") or MAX_TEXT_CHARS
        if not url:
            return "Error: 'url' is required"
        if not url.startswith(("http://", "https://")):
            return f"Error: url must start with http:// or https:// (got: {url[:60]})"

        try:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": "coding-agent/0.2"})
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return f"Error: HTTP {e.response.status_code} fetching {url}"
        except httpx.TimeoutException:
            return f"Error: timed out fetching {url}"
        except httpx.HTTPError as e:
            return f"Error fetching {url}: {e}"

        content_type = resp.headers.get("content-type", "")
        raw = resp.content[:MAX_BYTES]
        try:
            body = raw.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeError):
            body = raw.decode("utf-8", errors="replace")

        if "html" in content_type.lower() or body.lstrip()[:1] == "<":
            text, title = html_to_text(body)
        else:
            text, title = body, None

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... (truncated at {max_chars} chars)"

        header = f"# {title}\n" if title else ""
        return f"{header}URL: {url}\n\n{text}"


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

# DuckDuckGo 的无 key HTML 端点（结果稳定、可解析）
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
MAX_RESULTS = 8


class _DDGResultParser(HTMLParser):
    """
    解析 DuckDuckGo HTML 结果页：
    - 结果标题链接 class="result__a"，href 形如
      //duckduckgo.com/l/?uddg=<urlencoded real url>
    - 摘要 class="result__snippet"
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._cur: dict[str, str] | None = None
        self._capture: str | None = None  # "title" | "snippet"

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        ad = dict(attrs)
        cls = ad.get("class", "") or ""
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "url": _decode_ddg_href(ad.get("href", "")), "snippet": ""}
            self._capture = "title"
        elif tag == "a" and "result__snippet" in cls:
            self._capture = "snippet"
        elif tag in ("div", "td") and "result__snippet" in cls:
            self._capture = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture == "title" and self._cur is not None:
            # 标题结束：把当前结果存入（snippet 可能稍后补）
            if self._cur not in self.results:
                self.results.append(self._cur)
            self._capture = None
        elif self._capture == "snippet" and tag in ("a", "div", "td"):
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture == "title" and self._cur is not None:
            self._cur["title"] += data
        elif self._capture == "snippet":
            # 归属到最近一个结果
            if self.results:
                self.results[-1]["snippet"] += data


def _decode_ddg_href(href: str) -> str:
    """从 DDG 跳转链接里解出真实 URL（uddg 参数）。"""
    from urllib.parse import urlparse, parse_qs, unquote
    if not href:
        return ""
    if "uddg=" in href:
        try:
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
        except Exception:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_search_results(html: str, limit: int = MAX_RESULTS) -> list[dict[str, str]]:
    """解析搜索结果页，返回 [{title,url,snippet}]。"""
    parser = _DDGResultParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    out = []
    for r in parser.results:
        title = " ".join(r["title"].split()).strip()
        url = r["url"].strip()
        snippet = " ".join(r["snippet"].split()).strip()
        if title and url:
            out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= limit:
            break
    return out


class WebSearchTool(Tool):
    """搜索网络，返回标题/URL/摘要列表（DuckDuckGo，无需 key）。"""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web and return a list of results (title, URL, snippet). "
            "Use this to find documentation, libraries, error messages, or recent "
            "information, then web_fetch a result URL to read it. Keyless "
            "(DuckDuckGo). Returns up to 8 results."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {
                    "type": "integer",
                    "description": f"Max results to return (default {MAX_RESULTS})",
                },
            },
            "required": ["query"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        limit = kwargs.get("max_results") or MAX_RESULTS
        if not query:
            return "Error: 'query' is required"

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.post(
                    DDG_HTML_URL,
                    data={"q": query},
                    headers={"User-Agent": "Mozilla/5.0 (compatible; coding-agent/0.2)"},
                )
                resp.raise_for_status()
        except httpx.TimeoutException:
            return f"Error: web search timed out for query: {query!r}"
        except httpx.HTTPError as e:
            return f"Error performing web search: {e}"

        results = parse_search_results(resp.text, limit=limit)
        if not results:
            return f"No results found for: {query!r}"

        lines = [f"Search results for {query!r}:\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}")
            if r["snippet"]:
                lines.append(f"   {r['snippet'][:200]}")
        return "\n".join(lines)


def register_web_tools(registry: Any = None) -> None:
    """注册 web 工具。"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(WebFetchTool())
    reg.register(WebSearchTool())
