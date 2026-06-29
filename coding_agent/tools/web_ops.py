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


def register_web_tools(registry: Any = None) -> None:
    """注册 web 工具。"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(WebFetchTool())
