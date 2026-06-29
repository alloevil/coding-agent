"""
工具基类 - 可插拔工具系统

参考 Claude Code 的工具设计：
- 每个工具有 name, description, parameters
- 支持权限检查
- 支持 hook 事件
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
from enum import Enum


class ToolPermission(Enum):
    """工具权限级别"""
    READ = "read"          # 只读操作，自动允许
    WRITE = "write"        # 写操作，需要确认
    EXECUTE = "execute"    # 执行命令，需要确认
    DANGEROUS = "dangerous"  # 危险操作，需要明确确认


@dataclass
class ToolParameter:
    """工具参数定义"""
    name: str
    type: str  # "string", "integer", "boolean", "array", "object"
    description: str
    required: bool = True
    enum: list[str] | None = None
    default: Any = None


@dataclass
class ToolDefinition:
    """工具定义（用于 API 调用）"""
    name: str
    description: str
    parameters: dict[str, Any]
    permission: ToolPermission = ToolPermission.READ


class Tool(ABC):
    """
    工具基类
    
    参考 Claude Code 的工具设计：
    - 每个工具有明确的 name 和 description
    - parameters 定义工具接受的参数
    - execute 方法执行实际逻辑
    - permission 定义权限级别
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述"""
        pass
    
    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """工具参数的 JSON Schema"""
        pass
    
    @property
    def permission(self) -> ToolPermission:
        """工具权限级别"""
        return ToolPermission.READ
    
    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """
        执行工具
        
        Args:
            **kwargs: 工具参数
        
        Returns:
            执行结果的字符串表示
        
        Raises:
            ToolExecutionError: 执行失败时抛出
        """
        pass
    
    def get_definition(self) -> ToolDefinition:
        """获取工具定义"""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            permission=self.permission
        )
    
    def get_openai_function(self) -> dict[str, Any]:
        """获取 OpenAI function calling 格式"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class ToolExecutionError(Exception):
    """工具执行错误"""
    def __init__(self, tool_name: str, message: str, original_error: Exception | None = None):
        self.tool_name = tool_name
        self.original_error = original_error
        super().__init__(f"Tool '{tool_name}' execution failed: {message}")


# Hook 系统（参考 Claude Code 的 27 个 hook 事件）
class HookEvent(Enum):
    """Hook 事件类型"""
    PRE_TOOL_USE = "pre_tool_use"      # 工具执行前
    POST_TOOL_USE = "post_tool_use"    # 工具执行后
    PRE_MODEL_CALL = "pre_model_call"  # 模型调用前
    POST_MODEL_CALL = "post_model_call"  # 模型调用后
    ON_ERROR = "on_error"              # 错误发生时
    ON_COMPACT = "on_compact"          # Context 压缩时


@dataclass
class HookContext:
    """Hook 上下文"""
    event: HookEvent
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    error: Exception | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Hook 类型
Hook = Callable[[HookContext], bool | None]
# 返回 True 表示阻止操作，None 或 False 表示允许
