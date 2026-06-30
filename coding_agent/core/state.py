"""
Agent State - 状态管理

参考 Claude Code 的 append-only durable state 设计
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from enum import Enum


class MessageRole(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """工具调用"""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """工具执行结果"""
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    """消息"""
    role: MessageRole
    content: str | list[dict[str, Any]] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_result: ToolResult | None = None
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 调用格式"""
        result: dict[str, Any] = {"role": self.role.value}
        
        if self.content is not None:
            result["content"] = self.content
        
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments)
                    }
                }
                for tc in self.tool_calls
            ]
        
        if self.tool_result:
            result["tool_call_id"] = self.tool_result.tool_call_id
            result["content"] = self.tool_result.content
        
        return result


@dataclass
class AgentState:
    """
    Agent 状态
    
    参考 Claude Code 的 append-only 设计：
    - messages 是追加式的，不会修改已有消息
    - metadata 存储会话级别的信息
    - turn_count 跟踪当前轮次
    """
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    turn_count: int = 0
    max_turns: int = 100  # 最大轮次，防止无限循环
    created_at: float = field(default_factory=time.time)
    session_id: str | None = None
    
    def add_message(self, message: Message) -> None:
        """追加消息（append-only）"""
        self.messages.append(message)
    
    def add_user_message(self, content: str) -> None:
        """添加用户消息"""
        self.add_message(Message(
            role=MessageRole.USER,
            content=content
        ))
    
    def add_assistant_message(self, content: str, tool_calls: list[ToolCall] | None = None) -> None:
        """添加助手消息"""
        self.add_message(Message(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=tool_calls
        ))
    
    def add_tool_result(self, tool_call_id: str, content: str, is_error: bool = False) -> None:
        """添加工具执行结果"""
        self.add_message(Message(
            role=MessageRole.TOOL,
            tool_result=ToolResult(
                tool_call_id=tool_call_id,
                content=content,
                is_error=is_error
            )
        ))
    
    def get_context_messages(self) -> list[dict[str, Any]]:
        """获取用于 API 调用的消息列表"""
        return [msg.to_dict() for msg in self.messages]
    
    def should_stop(self) -> bool:
        """检查是否应该停止"""
        return self.turn_count >= self.max_turns
    
    def increment_turn(self) -> None:
        """增加轮次计数"""
        self.turn_count += 1
    
    def get_token_estimate(self) -> int:
        """估算当前 token 数（粗略）。计入正文、工具调用参数与工具结果。"""
        total = 0
        for msg in self.messages:
            if msg.content:
                if isinstance(msg.content, str):
                    total += len(msg.content) // 4  # 粗略估算
                elif isinstance(msg.content, list):
                    for item in msg.content:
                        if isinstance(item, dict) and "text" in item:
                            total += len(item["text"]) // 4
            # 工具调用参数
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += (len(tc.name) + len(json.dumps(tc.arguments))) // 4
            # 工具结果（通常是最大的 token 消耗来源）
            if msg.tool_result and msg.tool_result.content:
                total += len(msg.tool_result.content) // 4
        return total
