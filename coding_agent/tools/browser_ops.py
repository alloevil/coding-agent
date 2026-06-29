"""
浏览器控制工具集

使用 Playwright for Python 实现浏览器自动化：
- 按需启动，会话内复用浏览器实例
- 默认 headless chromium
- 截图返回 base64 编码的 PNG
- 可访问性快照
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any

from .base import Tool, ToolPermission, ToolExecutionError


# ── 浏览器实例管理 ──────────────────────────────────────────────

_browser_instance: Any = None
_browser_context: Any = None
_browser_lock = asyncio.Lock()

DEFAULT_TIMEOUT_MS = 30_000


async def _get_page(url: str | None = None, timeout: int = DEFAULT_TIMEOUT_MS) -> Any:
    """获取或创建浏览器页面，返回 (page, already_existed)"""
    global _browser_instance, _browser_context

    from playwright.async_api import async_playwright

    async with _browser_lock:
        if _browser_instance is None:
            pw = await async_playwright().start()
            _browser_instance = await pw.chromium.launch(headless=True)

        if _browser_context is None:
            _browser_context = await _browser_instance.new_context(
                viewport={"width": 1280, "height": 720},
            )

    page = await _browser_context.new_page()
    page.set_default_timeout(timeout)

    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    return page


async def _close_browser() -> str:
    """关闭浏览器实例"""
    global _browser_instance, _browser_context

    async with _browser_lock:
        if _browser_context is not None:
            await _browser_context.close()
            _browser_context = None
        if _browser_instance is not None:
            await _browser_instance.close()
            _browser_instance = None

    return "Browser closed"


# ── 工具定义 ────────────────────────────────────────────────────

class BrowserOpenTool(Tool):
    """打开 URL，返回页面标题和可访问性快照"""

    @property
    def name(self) -> str:
        return "browser_open"

    @property
    def description(self) -> str:
        return (
            "Open a URL in a headless browser. Returns the page title "
            "and an accessibility snapshot of the page structure."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                },
            },
            "required": ["url"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url")
        timeout = kwargs.get("timeout", 30) * 1000  # 转为毫秒

        if not url:
            raise ToolExecutionError(self.name, "url is required")

        try:
            page = await _get_page(url, timeout=timeout)
            title = await page.title()

            # 获取可访问性快照
            snapshot_text = await _build_snapshot(page)

            return (
                f"Opened: {url}\n"
                f"Title: {title}\n\n"
                f"Accessibility snapshot:\n{snapshot_text}"
            )
        except Exception as e:
            return f"Error opening URL '{url}': {e}"


class BrowserScreenshotTool(Tool):
    """截图并返回 base64 图片"""

    @property
    def name(self) -> str:
        return "browser_screenshot"

    @property
    def description(self) -> str:
        return (
            "Take a screenshot of the current page and return it as a "
            "base64-encoded PNG image for visual inspection."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full scrollable page (default: false)",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector to screenshot a specific element",
                },
            },
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        full_page = kwargs.get("full_page", False)
        selector = kwargs.get("selector")

        try:
            page = await _get_page()

            if selector:
                element = page.locator(selector)
                screenshot_bytes = await element.screenshot(type="png")
            else:
                screenshot_bytes = await page.screenshot(
                    type="png",
                    full_page=full_page,
                )

            b64_str = base64.b64encode(screenshot_bytes).decode("ascii")
            return f"Screenshot (base64 PNG, {len(screenshot_bytes)} bytes):\n{b64_str}"
        except Exception as e:
            return f"Error taking screenshot: {e}"


class BrowserClickTool(Tool):
    """点击页面元素"""

    @property
    def name(self) -> str:
        return "browser_click"

    @property
    def description(self) -> str:
        return "Click an element on the page by CSS selector or visible text."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": (
                        "CSS selector to click. Use 'text=...' prefix for text-based "
                        "matching (e.g., 'text=Submit')."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                },
            },
            "required": ["selector"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> str:
        selector = kwargs.get("selector")
        timeout = kwargs.get("timeout", 30) * 1000

        if not selector:
            raise ToolExecutionError(self.name, "selector is required")

        try:
            page = await _get_page()
            page.set_default_timeout(timeout)

            # text= 前缀由 Playwright 内置支持
            await page.locator(selector).click(timeout=timeout)
            return f"Clicked element: {selector}"
        except Exception as e:
            return f"Error clicking '{selector}': {e}"


class BrowserTypeTool(Tool):
    """在输入框中输入文本"""

    @property
    def name(self) -> str:
        return "browser_type"

    @property
    def description(self) -> str:
        return "Type text into an input field identified by CSS selector."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of the input element",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type into the element",
                },
                "clear": {
                    "type": "boolean",
                    "description": "Clear the field before typing (default: false)",
                },
                "submit": {
                    "type": "boolean",
                    "description": "Press Enter after typing (default: false)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                },
            },
            "required": ["selector", "text"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    async def execute(self, **kwargs: Any) -> str:
        selector = kwargs.get("selector")
        text = kwargs.get("text")
        clear = kwargs.get("clear", False)
        submit = kwargs.get("submit", False)
        timeout = kwargs.get("timeout", 30) * 1000

        if not selector:
            raise ToolExecutionError(self.name, "selector is required")
        if text is None:
            raise ToolExecutionError(self.name, "text is required")

        try:
            page = await _get_page()
            page.set_default_timeout(timeout)

            locator = page.locator(selector)

            if clear:
                await locator.fill(text, timeout=timeout)
            else:
                await locator.type(text, timeout=timeout)

            if submit:
                await locator.press("Enter", timeout=timeout)

            return f"Typed text into '{selector}'" + (" (submitted)" if submit else "")
        except Exception as e:
            return f"Error typing into '{selector}': {e}"


class BrowserEvaluateTool(Tool):
    """执行 JavaScript 代码"""

    @property
    def name(self) -> str:
        return "browser_evaluate"

    @property
    def description(self) -> str:
        return "Execute JavaScript code in the browser context and return the result."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression or code to evaluate",
                },
            },
            "required": ["expression"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.EXECUTE

    async def execute(self, **kwargs: Any) -> str:
        expression = kwargs.get("expression")

        if not expression:
            raise ToolExecutionError(self.name, "expression is required")

        try:
            page = await _get_page()
            result = await page.evaluate(expression)
            return f"Result: {result}"
        except Exception as e:
            return f"Error evaluating expression: {e}"


class BrowserSnapshotTool(Tool):
    """获取页面可访问性快照"""

    @property
    def name(self) -> str:
        return "browser_snapshot"

    @property
    def description(self) -> str:
        return (
            "Get an accessibility snapshot of the current page — "
            "a structured text representation of the page's interactive "
            "elements, headings, and content hierarchy."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ

    async def execute(self, **kwargs: Any) -> str:
        try:
            page = await _get_page()
            snapshot_text = await _build_snapshot(page)
            return f"Accessibility snapshot:\n{snapshot_text}"
        except Exception as e:
            return f"Error getting snapshot: {e}"


class BrowserCloseTool(Tool):
    """关闭浏览器"""

    @property
    def name(self) -> str:
        return "browser_close"

    @property
    def description(self) -> str:
        return "Close the browser and release all resources."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGEROUS

    async def execute(self, **kwargs: Any) -> str:
        return await _close_browser()


# ── 可访问性快照构建 ─────────────────────────────────────────────

async def _build_snapshot(page: Any) -> str:
    """
    构建页面可访问性快照的文本表示。
    优先使用 Playwright 的 accessibility.snapshot()，
    失败时回退到自定义 DOM 遍历。
    """
    try:
        snapshot = await page.accessibility.snapshot()
        if snapshot:
            return _format_ax_node(snapshot, indent=0)
    except Exception:
        pass

    # 回退：遍历关键 DOM 元素
    return await _fallback_dom_snapshot(page)


def _format_ax_node(node: dict[str, Any], indent: int) -> str:
    """递归格式化 accessibility tree node"""
    parts: list[str] = []
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    line = "  " * indent + f"[{role}]"
    if name:
        line += f' "{name}"'
    if value:
        line += f" = {value}"
    parts.append(line)

    for child in node.get("children", []):
        parts.append(_format_ax_node(child, indent + 1))

    return "\n".join(parts)


async def _fallback_dom_snapshot(page: Any) -> str:
    """DOM 遍历回退方案，提取交互元素和标题"""
    return await page.evaluate("""() => {
        const items = [];
        const selectors = 'h1, h2, h3, h4, h5, h6, a, button, input, textarea, select, [role="button"], [role="link"]';
        document.querySelectorAll(selectors).forEach(el => {
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute('role') || '';
            const text = (el.textContent || '').trim().slice(0, 80);
            const type = el.getAttribute('type') || '';
            const placeholder = el.getAttribute('placeholder') || '';
            const href = el.getAttribute('href') || '';
            let desc = `[${tag}`;
            if (role) desc += `, role=${role}`;
            if (type) desc += `, type=${type}`;
            desc += ']';
            if (text) desc += ` "${text}"`;
            if (placeholder) desc += ` placeholder="${placeholder}"`;
            if (href) desc += ` -> ${href}`;
            items.push(desc);
        });
        return items.join('\\n') || '(empty page)';
    }""")


# ── 注册 ────────────────────────────────────────────────────────

def register_browser_tools(registry: Any = None) -> None:
    """注册所有浏览器控制工具"""
    from .registry import get_registry

    reg = registry or get_registry()
    reg.register(BrowserOpenTool())
    reg.register(BrowserScreenshotTool())
    reg.register(BrowserClickTool())
    reg.register(BrowserTypeTool())
    reg.register(BrowserEvaluateTool())
    reg.register(BrowserSnapshotTool())
    reg.register(BrowserCloseTool())
