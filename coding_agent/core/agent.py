"""
Agent Loop - 核心循环

参考 Claude Code 的 AsyncGenerator 模式：
async def* agentLoop(state): AsyncGenerator<AgentEvent>

9 步 pipeline：
1. Settings resolution
2. State init
3. Context assembly
4. Pre-model shapers (compaction)
5. Model call (streaming)
6. Tool dispatch
7. Permission gate
8. Tool execution
9. Stop condition

增强功能：
- 流式中断恢复（interrupt）
- 错误恢复与自动重试（retry）
- 工具执行回滚（rollback）
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncGenerator, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum

from .state import AgentState, Message, MessageRole, ToolCall
from .config import AgentConfig
from ..tools.registry import ToolRegistry, get_registry
from ..tools.base import ToolPermission
# 注意：ContextManager 在 __init__ 内延迟导入，避免
# context.manager <-> core.state <-> core.agent 的循环导入。

# 延迟导入避免循环依赖
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..memory.session import SessionStore


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

# 瞬态错误关键词（匹配到则重试）
TRANSIENT_ERROR_KEYWORDS: list[str] = [
    "timeout", "timed out", "ETIMEDOUT", "ECONNREFUSED", "ECONNRESET",
    "EPIPE", "network", "connection reset", "connection refused",
    "temporary", "try again", "resource temporarily unavailable",
    "rate limit", "too many requests", "429", "503", "502", "504",
    "busy", "overloaded", "unavailable",
]

# 永久错误关键词（匹配到则不重试）
PERMANENT_ERROR_KEYWORDS: list[str] = [
    "does not exist", "not found", "No such file", "permission denied",
    "syntax error", "invalid syntax", "type error", "name error",
    "value error", "attribute error", "key error", "index error",
    "not a directory", "is a directory", "read-only",
]


def _classify_error(error_msg: str) -> str:
    """
    分类错误为 transient 或 permanent。
    
    Returns:
        "transient" - 可重试的临时错误
        "permanent" - 不可重试的永久错误
        "unknown"   - 无法判断，保守不重试
    """
    lower = error_msg.lower()
    for kw in PERMANENT_ERROR_KEYWORDS:
        if kw.lower() in lower:
            return "permanent"
    for kw in TRANSIENT_ERROR_KEYWORDS:
        if kw.lower() in lower:
            return "transient"
    return "unknown"


# ---------------------------------------------------------------------------
# 回滚记录
# ---------------------------------------------------------------------------

@dataclass
class RollbackRecord:
    """工具执行回滚记录"""
    tool_name: str
    arguments: dict[str, Any]
    rollback_data: dict[str, Any]
    # rollback_data 语义取决于 tool_name：
    #   file_write: {"path": ..., "original_content": ... | None}
    #   file_edit:  {"path": ..., "old_text": ..., "new_text": ...}
    #   shell_exec: {"command": ..., "workdir": ...}


@dataclass
class RollbackLog:
    """回滚日志（最近 N 条）"""
    max_records: int = 50
    records: list[RollbackRecord] = field(default_factory=list)

    def add(self, record: RollbackRecord) -> None:
        self.records.append(record)
        if len(self.records) > self.max_records:
            self.records.pop(0)

    def pop_last(self) -> RollbackRecord | None:
        if self.records:
            return self.records.pop()
        return None

    @property
    def last(self) -> RollbackRecord | None:
        return self.records[-1] if self.records else None


# ---------------------------------------------------------------------------
# 重试配置
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    """重试策略配置"""
    max_retries: int = 3
    base_delay: float = 1.0          # 秒
    backoff_factor: float = 2.0       # 指数退避因子
    retry_on_keywords: list[str] = field(default_factory=lambda: list(TRANSIENT_ERROR_KEYWORDS))
    no_retry_on_keywords: list[str] = field(default_factory=lambda: list(PERMANENT_ERROR_KEYWORDS))


class AgentEvent(Enum):
    """Agent 事件类型"""
    THINKING = "thinking"              # 模型正在思考
    TOOL_CALL = "tool_call"            # 工具调用
    TOOL_RESULT = "tool_result"        # 工具结果
    ASSISTANT_MESSAGE = "assistant_message"  # 助手消息
    ERROR = "error"                    # 错误
    DONE = "done"                      # 完成
    COMPACTING = "compacting"          # 正在压缩
    PERMISSION_REQUEST = "permission_request"  # 权限确认请求
    INTERRUPTED = "interrupted"        # 工具执行被中断
    RETRYING = "retrying"              # 工具执行重试中
    ROLLBACK = "rollback"              # 工具回滚


@dataclass
class AgentEventData:
    """Agent 事件数据"""
    event: AgentEvent
    data: dict[str, Any]


# 权限确认回调类型
# 参数：tool_name, arguments -> True 允许, False 拒绝
PermissionHandler = Callable[[str, dict[str, Any]], Awaitable[bool]]


class AgentLoop:
    """
    Agent 核心循环
    
    参考 Claude Code 的设计：
    - 单一 queryLoop 处理所有接口
    - 流式事件输出
    - 支持权限检查
    - 支持 context 压缩
    """
    
    def __init__(
        self,
        config: AgentConfig,
        tool_registry: ToolRegistry | None = None,
        session_store: "SessionStore | None" = None,
        retry_config: RetryConfig | None = None,
        register_builtin_tools: bool = True,
        spawn_depth: int = 0,
    ):
        self.config = config
        self.tool_registry = tool_registry or get_registry()

        # 延迟导入避免循环依赖
        if session_store is None:
            from ..memory.session import SessionStore
            session_store = SessionStore(db_path=config.session_db_path)
        self.session_store = session_store

        from ..context.manager import ContextManager
        self.context_manager = ContextManager(max_tokens=config.max_context_tokens)

        # 模型调用函数（需要外部注入）
        self._model_call_fn: Any = None

        # 累计 token 用量查询（可选；返回 int）。用于 token 预算停止。
        self._token_usage_fn: Any = None

        # 权限确认回调
        self._permission_handler: PermissionHandler | None = None

        # 子代理深度追踪（0 = 根代理）
        self._spawn_depth: int = spawn_depth

        # ---- 中断恢复 ----
        self._interrupt_event = asyncio.Event()
        self._interrupted: bool = False

        # ---- 错误恢复 ----
        self.retry_config = retry_config or RetryConfig()

        # ---- 工具回滚 ----
        self.rollback_log = RollbackLog()

        # ---- 细粒度权限策略 ----
        from .permissions import PermissionPolicy
        self.permission_policy = PermissionPolicy.from_config(
            getattr(config, "permissions", None),
            auto_approve=config.auto_approve,
        )

        # 自动注册子代理工具 + 回滚工具。
        # 子代理传 register_builtin_tools=False，避免用自己的引用覆盖父代理
        # 已注册的 agent_spawn/agent_parallel（它们共享同一个 tool_registry）。
        if register_builtin_tools:
            from ..tools.agent_ops import register_agent_tools
            register_agent_tools(self.tool_registry, self)
            self._register_rollback_tool()

    def set_model_call_fn(self, fn: Any) -> None:
        """设置模型调用函数"""
        self._model_call_fn = fn

    def set_token_usage_fn(self, fn: Any) -> None:
        """设置累计 token 用量查询函数（返回 int），用于 token 预算停止。"""
        self._token_usage_fn = fn

    def _over_token_budget(self) -> bool:
        """是否已超出配置的 token 预算。"""
        budget = getattr(self.config, "max_total_tokens", 0) or 0
        if budget <= 0 or self._token_usage_fn is None:
            return False
        try:
            return self._token_usage_fn() >= budget
        except Exception:
            return False
    
    def set_permission_handler(self, handler: PermissionHandler) -> None:
        """设置权限确认回调"""
        self._permission_handler = handler
    
    async def run(
        self,
        state: AgentState,
        user_input: str | None = None
    ) -> AsyncGenerator[AgentEventData, None]:
        """
        运行 Agent Loop
        
        参考 Claude Code 的 AsyncGenerator 模式
        """
        # 1. 添加用户输入
        if user_input:
            state.add_user_message(user_input)
        
        # 重置中断状态
        self._interrupted = False
        self._interrupt_event.clear()
        
        # 2. 主循环
        while not state.should_stop():
            # token 预算停止：超出则结束本轮，避免成本失控
            if self._over_token_budget():
                yield AgentEventData(
                    event=AgentEvent.DONE,
                    data={"turns": state.turn_count, "reason": "token_budget_exceeded"},
                )
                break
            state.increment_turn()
            
            # 3. Context assembly
            context = self.context_manager.assemble_context(
                state, 
                self.config.system_prompt
            )
            
            # 4. Pre-model shapers (compaction)
            if self.context_manager.needs_compaction(state):
                yield AgentEventData(
                    event=AgentEvent.COMPACTING,
                    data={"message": "Compacting context..."}
                )
                from ..tools.base import HookEvent, HookContext
                await self.tool_registry.run_hooks(
                    HookEvent.ON_COMPACT,
                    HookContext(event=HookEvent.ON_COMPACT,
                                metadata={"tokens_before": state.get_token_estimate()}),
                )
                await self.context_manager.compact(state, self._model_call_fn)
                # 重新组装 context
                context = self.context_manager.assemble_context(
                    state,
                    self.config.system_prompt
                )
            
            # 5. Model call
            yield AgentEventData(
                event=AgentEvent.THINKING,
                data={"turn": state.turn_count}
            )
            
            try:
                response = await self._call_model(context)
            except Exception as e:
                yield AgentEventData(
                    event=AgentEvent.ERROR,
                    data={"error": str(e)}
                )
                break
            
            # 6. Parse response
            assistant_message = response.get("content", "")
            tool_calls_data = response.get("tool_calls", [])
            
            # 解析工具调用
            tool_calls = []
            if tool_calls_data:
                for tc in tool_calls_data:
                    tool_calls.append(ToolCall(
                        id=tc.get("id", str(uuid.uuid4())),
                        name=tc["function"]["name"],
                        arguments=json.loads(tc["function"]["arguments"])
                    ))
            
            # 添加助手消息
            state.add_assistant_message(assistant_message, tool_calls)
            
            # 发送助手消息事件
            if assistant_message:
                yield AgentEventData(
                    event=AgentEvent.ASSISTANT_MESSAGE,
                    data={"content": assistant_message}
                )
            
            # 7. Stop condition: no tool calls
            if not tool_calls:
                yield AgentEventData(
                    event=AgentEvent.DONE,
                    data={"turns": state.turn_count}
                )
                break
            
            # 8. Tool dispatch + execution
            #    连续的只读(自动放行)工具并发执行；其余串行、保持顺序。
            i = 0
            while i < len(tool_calls):
                # 收集一段连续的可并行(只读)调用
                group: list[ToolCall] = []
                while i < len(tool_calls) and self._is_parallelizable(tool_calls[i]):
                    group.append(tool_calls[i])
                    i += 1
                if len(group) >= 2:
                    async for ev in self._dispatch_parallel(group, state):
                        yield ev
                    continue
                if group:
                    # 只有一个可并行调用，按串行处理即可
                    async for ev in self._dispatch_one(group[0], state):
                        yield ev
                    continue
                # 非并行调用(需权限/写/执行/未知)，串行处理
                async for ev in self._dispatch_one(tool_calls[i], state):
                    yield ev
                i += 1

        # 保存会话
        if state.session_id:
            self.session_store.save_state(state.session_id, state)

    def _is_parallelizable(self, tool_call: ToolCall) -> bool:
        """工具是否可并发执行：存在、READ 权限、且策略判定为 ALLOW（不需询问）。"""
        from .permissions import Decision
        tool = self.tool_registry.get_tool(tool_call.name)
        if tool is None:
            return False
        if tool.permission != ToolPermission.READ:
            return False
        decision = self.permission_policy.decide(
            tool_call.name, tool_call.arguments, tool.permission
        )
        return decision == Decision.ALLOW

    async def _dispatch_one(
        self, tool_call: ToolCall, state: AgentState
    ) -> AsyncGenerator[AgentEventData, None]:
        """串行处理单个工具调用：事件 -> 权限门 -> 执行 -> 结果。"""
        yield AgentEventData(
            event=AgentEvent.TOOL_CALL,
            data={
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        )

        # 9. Permission gate（细粒度策略：DENY / ALLOW / ASK）
        from .permissions import Decision
        tool = self.tool_registry.get_tool(tool_call.name)
        if not tool:
            result = f"Error: Tool '{tool_call.name}' not found"
            is_error = True
        else:
            decision = self.permission_policy.decide(
                tool_call.name, tool_call.arguments, tool.permission
            )

            if decision == Decision.DENY:
                result = (
                    f"Permission denied by policy for '{tool_call.name}' "
                    f"(arguments: {tool_call.arguments})"
                )
                is_error = True
            elif decision == Decision.ASK:
                yield AgentEventData(
                    event=AgentEvent.PERMISSION_REQUEST,
                    data={
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                        "permission": tool.permission.value,
                    },
                )
                if self._permission_handler:
                    try:
                        approved = await self._permission_handler(
                            tool_call.name, tool_call.arguments
                        )
                    except Exception as e:
                        approved = False
                        yield AgentEventData(
                            event=AgentEvent.ERROR,
                            data={"error": f"Permission handler error: {e}"},
                        )
                else:
                    approved = False

                if approved:
                    result, is_error = "", False
                    async for item in self._recovery_events(tool_call, state):
                        if isinstance(item, tuple):
                            result, is_error = item
                        else:
                            yield item
                else:
                    result = f"Permission denied for tool '{tool_call.name}'"
                    is_error = True
            else:  # Decision.ALLOW
                result, is_error = "", False
                async for item in self._recovery_events(tool_call, state):
                    if isinstance(item, tuple):
                        result, is_error = item
                    else:
                        yield item

        state.add_tool_result(tool_call.id, result, is_error)
        yield AgentEventData(
            event=AgentEvent.TOOL_RESULT,
            data={"id": tool_call.id, "result": result, "is_error": is_error},
        )

    async def _dispatch_parallel(
        self, group: list[ToolCall], state: AgentState
    ) -> AsyncGenerator[AgentEventData, None]:
        """并发执行一组只读工具：先发全部 TOOL_CALL，并发执行，再按序发结果。"""
        for tc in group:
            yield AgentEventData(
                event=AgentEvent.TOOL_CALL,
                data={"id": tc.id, "name": tc.name, "arguments": tc.arguments},
            )

        # 并发执行（只读工具无副作用、不需回滚；不透传重试事件以避免交错）
        results = await asyncio.gather(
            *(self._execute_with_recovery(tc, state) for tc in group),
            return_exceptions=True,
        )

        for tc, res in zip(group, results):
            if isinstance(res, Exception):
                result, is_error = f"Error executing tool '{tc.name}': {res}", True
            else:
                result, is_error = res
            state.add_tool_result(tc.id, result, is_error)
            yield AgentEventData(
                event=AgentEvent.TOOL_RESULT,
                data={"id": tc.id, "result": result, "is_error": is_error},
            )
    
    async def _call_model(self, context: list[dict[str, Any]]) -> dict[str, Any]:
        """调用模型"""
        if not self._model_call_fn:
            raise RuntimeError("Model call function not set")

        # 获取工具定义
        tools = self.tool_registry.get_openai_functions()

        # PRE_MODEL_CALL hook
        from ..tools.base import HookEvent, HookContext
        await self.tool_registry.run_hooks(
            HookEvent.PRE_MODEL_CALL,
            HookContext(event=HookEvent.PRE_MODEL_CALL,
                        metadata={"message_count": len(context)}),
        )

        # 调用模型
        response = await self._model_call_fn(context, tools)

        # POST_MODEL_CALL hook
        await self.tool_registry.run_hooks(
            HookEvent.POST_MODEL_CALL,
            HookContext(event=HookEvent.POST_MODEL_CALL,
                        metadata={"has_tool_calls": bool(response.get("tool_calls"))}),
        )
        return response
    
    def _needs_permission(self, permission: ToolPermission) -> bool:
        """检查是否需要权限确认"""
        if self.config.auto_approve:
            return False
        
        # READ 权限自动允许
        if permission == ToolPermission.READ:
            return False
        
        # 其他权限需要确认
        return True
    
    # -----------------------------------------------------------------------
    # 流式中断恢复
    # -----------------------------------------------------------------------
    
    def interrupt(self) -> None:
        """
        中断当前工具执行。
        
        调用后：
        - 正在执行的工具会尽快返回部分结果 + "[Interrupted by user]"
        - AgentLoop 状态保持不变，可以继续接收新输入
        - 中断信号会在每次 run() 开始时自动清除
        """
        self._interrupted = True
        self._interrupt_event.set()
    
    def is_interrupted(self) -> bool:
        """检查是否处于中断状态"""
        return self._interrupted
    
    def clear_interrupt(self) -> None:
        """清除中断状态（通常不需要手动调用，run() 会自动清除）"""
        self._interrupted = False
        self._interrupt_event.clear()
    
    async def _wait_with_interrupt(self, coro: Any) -> Any:
        """
        等待协程，同时监听中断信号。
        
        如果中断信号在协程完成前触发，协程被取消并返回中断标记。
        """
        interrupt_task = asyncio.create_task(self._interrupt_event.wait())
        try:
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(coro), interrupt_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            # 取消未完成的任务
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            
            # 检查哪个先完成
            if interrupt_task in done:
                return "__INTERRUPTED__"
            
            # 返回协程结果
            for task in done:
                if task is not interrupt_task:
                    return task.result()
        except asyncio.CancelledError:
            return "__INTERRUPTED__"
    
    # -----------------------------------------------------------------------
    # 错误恢复与重试
    # -----------------------------------------------------------------------
    
    async def _execute_with_retry(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        event_cb: Callable[[AgentEventData], None] | None = None,
    ) -> tuple[str, bool]:
        """
        执行工具，带自动重试。

        Args:
            event_cb: 可选回调，重试发生时收到 RETRYING 事件（用于向 UI 实时通知）。

        Returns:
            (result_str, is_error)
        """
        last_error = ""
        cfg = self.retry_config
        
        for attempt in range(cfg.max_retries + 1):
            # 检查中断
            if self._interrupted:
                return "[Interrupted by user]", True
            
            try:
                result = await self._interruptible_execute(tool_name, arguments)
                if result == "__INTERRUPTED__":
                    return "[Interrupted by user]", True
                is_error = result.startswith("Error")
                
                if not is_error:
                    return result, False
                
                # 工具返回了错误字符串
                error_category = _classify_error(result)
                
                if error_category == "permanent":
                    return result, True
                
                # transient 或 unknown -> 可重试
                last_error = result
                
            except Exception as e:
                error_msg = str(e)
                error_category = _classify_error(error_msg)
                
                if error_category == "permanent":
                    return f"Error executing tool '{tool_name}': {error_msg}", True
                
                last_error = error_msg
            
            # 还有重试机会
            if attempt < cfg.max_retries:
                delay = cfg.base_delay * (cfg.backoff_factor ** attempt)
                if event_cb is not None:
                    event_cb(AgentEventData(
                        event=AgentEvent.RETRYING,
                        data={
                            "tool_name": tool_name,
                            "attempt": attempt + 1,
                            "max_retries": cfg.max_retries,
                            "delay": delay,
                            "error": last_error[:300],
                        },
                    ))
                await asyncio.sleep(delay)
        
        # 所有重试用完
        return f"Error executing tool '{tool_name}' after {cfg.max_retries + 1} attempts: {last_error}", True
    
    async def _interruptible_execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """
        执行工具，支持中断。
        
        如果中断信号已触发，立即返回 __INTERRUPTED__。
        否则正常执行工具。
        """
        if self._interrupted:
            return "__INTERRUPTED__"
        
        result = await self.tool_registry.execute_tool(tool_name, arguments)
        
        if self._interrupted:
            return result + "\n[Interrupted by user]"
        
        return result
    
    # -----------------------------------------------------------------------
    # 回滚记录
    # -----------------------------------------------------------------------
    
    async def _record_rollback_info(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """
        在执行 WRITE/EXECUTE 权限工具前，记录回滚信息。
        """
        rollback_data: dict[str, Any] = {}
        
        if tool_name == "file_write":
            path = arguments.get("path", "")
            try:
                from pathlib import Path
                p = Path(path)
                if p.exists():
                    rollback_data["original_content"] = p.read_text(encoding="utf-8")
                else:
                    rollback_data["original_content"] = None  # 文件是新建的
                rollback_data["path"] = path
            except Exception:
                rollback_data["path"] = path
                rollback_data["original_content"] = None
        
        elif tool_name == "file_edit":
            rollback_data["path"] = arguments.get("path", "")
            rollback_data["old_text"] = arguments.get("old_text", "")
            rollback_data["new_text"] = arguments.get("new_text", "")
        
        elif tool_name == "shell_exec":
            rollback_data["command"] = arguments.get("command", "")
            rollback_data["workdir"] = arguments.get("workdir")

        elif tool_name == "apply_patch":
            # 解析补丁，快照每个受影响文件的写前内容（add 记 None=新建）
            from pathlib import Path
            try:
                from ..tools.patch_ops import parse_patch
                ops = parse_patch(arguments.get("patch", ""))
                root = Path(arguments.get("root", "."))
                snapshots: dict[str, str | None] = {}
                for op in ops:
                    fp = root / op["path"]
                    key = str(fp)
                    if op["op"] == "add":
                        snapshots[key] = None  # 原本不存在
                    else:
                        try:
                            snapshots[key] = fp.read_text(encoding="utf-8")
                        except OSError:
                            snapshots[key] = None
                rollback_data["snapshots"] = snapshots
            except Exception:
                return  # 补丁无法解析则不记录（apply 也会失败）

        else:
            # 其他工具不记录回滚
            return
        
        self.rollback_log.add(RollbackRecord(
            tool_name=tool_name,
            arguments=arguments,
            rollback_data=rollback_data,
        ))
    
    def rollback_last(self) -> str:
        """
        回滚上一次工具执行。
        
        Returns:
            回滚结果描述
        """
        record = self.rollback_log.pop_last()
        if not record:
            return "Error: No tool execution to rollback"
        
        try:
            if record.tool_name == "file_write":
                path = record.rollback_data.get("path", "")
                original = record.rollback_data.get("original_content")
                from pathlib import Path
                p = Path(path)
                if original is None:
                    # 文件是新建的，删除
                    if p.exists():
                        p.unlink()
                        return f"Rolled back file_write: deleted '{path}'"
                    else:
                        return f"Rolled back file_write: '{path}' already doesn't exist"
                else:
                    # 恢复原内容
                    p.write_text(original, encoding="utf-8")
                    return f"Rolled back file_write: restored '{path}'"
            
            elif record.tool_name == "file_edit":
                path = record.rollback_data.get("path", "")
                old_text = record.rollback_data.get("old_text", "")
                new_text = record.rollback_data.get("new_text", "")
                from pathlib import Path
                p = Path(path)
                if not p.exists():
                    return f"Error: Cannot rollback file_edit, '{path}' does not exist"
                content = p.read_text(encoding="utf-8")
                if new_text in content:
                    content = content.replace(new_text, old_text, 1)
                    p.write_text(content, encoding="utf-8")
                    return f"Rolled back file_edit: reverted '{path}'"
                else:
                    return f"Error: Cannot rollback file_edit, new_text not found in '{path}'"
            
            elif record.tool_name == "shell_exec":
                command = record.rollback_data.get("command", "")
                return f"Cannot rollback shell_exec (command: {command}). Manual intervention may be required."

            elif record.tool_name == "apply_patch":
                from pathlib import Path
                snapshots = record.rollback_data.get("snapshots", {})
                restored, deleted = 0, 0
                for key, original in snapshots.items():
                    p = Path(key)
                    if original is None:
                        # 原本不存在 -> 删除新增的文件
                        if p.exists():
                            p.unlink()
                            deleted += 1
                    else:
                        p.write_text(original, encoding="utf-8")
                        restored += 1
                return (f"Rolled back apply_patch: restored {restored} file(s), "
                        f"deleted {deleted} added file(s)")

            else:
                return f"Error: Rollback not supported for tool '{record.tool_name}'"
        
        except Exception as e:
            return f"Error during rollback of '{record.tool_name}': {str(e)}"
    
    def _register_rollback_tool(self) -> None:
        """注册 rollback_last 工具到 registry"""
        from ..tools.base import Tool, ToolPermission as TP
        
        agent_ref = self
        
        class RollbackLastTool(Tool):
            @property
            def name(self) -> str:
                return "rollback_last"
            
            @property
            def description(self) -> str:
                return (
                    "Rollback the last WRITE/EXECUTE tool execution. "
                    "Supports file_write (restores original content or deletes new file), "
                    "file_edit (reverts the edit), and shell_exec (returns warning). "
                    "Each rollback is one-shot; calling again rolls back the next-to-last operation."
                )
            
            @property
            def parameters(self) -> dict[str, Any]:
                return {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            
            @property
            def permission(self) -> TP:
                return TP.WRITE
            
            async def execute(self, **kwargs: Any) -> str:
                return agent_ref.rollback_last()
        
        self.tool_registry.register(RollbackLastTool())
    
    # -----------------------------------------------------------------------
    # 带回滚 + 重试的执行入口
    # -----------------------------------------------------------------------

    async def _execute_with_recovery(
        self,
        tool_call: ToolCall,
        state: AgentState,
        event_cb: Callable[[AgentEventData], None] | None = None,
    ) -> tuple[str, bool]:
        """
        完整的工具执行流程：回滚记录 -> 中断检查 -> 重试执行。

        Args:
            event_cb: 可选回调，转发执行期间的事件（如 RETRYING）。

        Returns:
            (result_str, is_error)
        """
        tool = self.tool_registry.get_tool(tool_call.name)

        # 记录回滚信息（WRITE/EXECUTE 权限工具）
        if tool and tool.permission in (ToolPermission.WRITE, ToolPermission.EXECUTE):
            await self._record_rollback_info(tool_call.name, tool_call.arguments)

        # 带重试执行
        result, is_error = await self._execute_with_retry(
            tool_call.name,
            tool_call.arguments,
            event_cb=event_cb,
        )

        return result, is_error

    async def _recovery_events(
        self,
        tool_call: ToolCall,
        state: AgentState,
    ) -> AsyncGenerator[AgentEventData | tuple[str, bool], None]:
        """
        运行 _execute_with_recovery，并在执行期间实时 yield 中间事件
        （如 RETRYING）。最后 yield 一个 (result, is_error) 元组作为结果哨兵。

        用法：
            async for item in self._recovery_events(tc, state):
                if isinstance(item, tuple):
                    result, is_error = item
                else:
                    yield item  # 转发中间事件
        """
        queue: asyncio.Queue[AgentEventData] = asyncio.Queue()

        def _cb(ev: AgentEventData) -> None:
            queue.put_nowait(ev)

        task = asyncio.ensure_future(
            self._execute_with_recovery(tool_call, state, event_cb=_cb)
        )

        # 边执行边把队列里的事件吐出去
        while not task.done():
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=0.05)
                yield ev
            except asyncio.TimeoutError:
                continue

        # 排空剩余事件
        while not queue.empty():
            yield queue.get_nowait()

        result, is_error = await task
        yield (result, is_error)
