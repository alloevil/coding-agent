"""
会话持久化 - SQLite

参考 OpenCode 的 SQLite 持久化设计
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..core.state import AgentState, Message, MessageRole, ToolCall, ToolResult


class SessionStore:
    """
    会话存储
    
    使用 SQLite 持久化会话历史
    """
    
    def __init__(self, db_path: str = "~/.coding-agent/sessions.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        """初始化数据库"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    updated_at REAL,
                    metadata TEXT
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    tool_calls TEXT,
                    tool_result TEXT,
                    timestamp REAL,
                    metadata TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            
            conn.commit()
    
    def create_session(self, metadata: dict[str, Any] | None = None) -> str:
        """创建新会话"""
        import uuid
        session_id = str(uuid.uuid4())
        now = time.time()
        
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at, metadata) VALUES (?, ?, ?, ?)",
                (session_id, now, now, json.dumps(metadata or {}))
            )
            conn.commit()
        
        return session_id
    
    def save_state(self, session_id: str, state: AgentState) -> None:
        """保存状态到数据库"""
        with sqlite3.connect(str(self.db_path)) as conn:
            # 更新会话时间
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (time.time(), session_id)
            )
            
            # 删除旧消息
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,)
            )
            
            # 插入新消息
            for msg in state.messages:
                tool_calls_json = None
                if msg.tool_calls:
                    tool_calls_json = json.dumps([
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments
                        }
                        for tc in msg.tool_calls
                    ])
                
                tool_result_json = None
                if msg.tool_result:
                    tool_result_json = json.dumps({
                        "tool_call_id": msg.tool_result.tool_call_id,
                        "content": msg.tool_result.content,
                        "is_error": msg.tool_result.is_error
                    })
                
                conn.execute(
                    """INSERT INTO messages 
                       (session_id, role, content, tool_calls, tool_result, timestamp, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        msg.role.value,
                        msg.content if isinstance(msg.content, str) else json.dumps(msg.content),
                        tool_calls_json,
                        tool_result_json,
                        msg.timestamp,
                        json.dumps(msg.metadata)
                    )
                )
            
            conn.commit()
    
    def load_state(self, session_id: str) -> AgentState | None:
        """从数据库加载状态"""
        with sqlite3.connect(str(self.db_path)) as conn:
            # 检查会话是否存在
            cursor = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?",
                (session_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            metadata = json.loads(row[0]) if row[0] else {}
            
            # 加载消息
            cursor = conn.execute(
                """SELECT role, content, tool_calls, tool_result, timestamp, metadata
                   FROM messages WHERE session_id = ? ORDER BY id""",
                (session_id,)
            )
            
            messages = []
            for row in cursor.fetchall():
                role = MessageRole(row[0])
                content = row[1]
                tool_calls_json = row[2]
                tool_result_json = row[3]
                timestamp = row[4]
                msg_metadata = json.loads(row[5]) if row[5] else {}
                
                # 解析 tool_calls
                tool_calls = None
                if tool_calls_json:
                    tc_data = json.loads(tool_calls_json)
                    tool_calls = [
                        ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                        for tc in tc_data
                    ]
                
                # 解析 tool_result
                tool_result = None
                if tool_result_json:
                    tr_data = json.loads(tool_result_json)
                    tool_result = ToolResult(
                        tool_call_id=tr_data["tool_call_id"],
                        content=tr_data["content"],
                        is_error=tr_data["is_error"]
                    )
                
                messages.append(Message(
                    role=role,
                    content=content,
                    tool_calls=tool_calls,
                    tool_result=tool_result,
                    timestamp=timestamp,
                    metadata=msg_metadata
                ))
            
            return AgentState(
                messages=messages,
                metadata=metadata,
                session_id=session_id
            )
    
    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """列出最近的会话"""
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                """SELECT id, created_at, updated_at, metadata 
                   FROM sessions ORDER BY updated_at DESC LIMIT ?""",
                (limit,)
            )
            
            sessions = []
            for row in cursor.fetchall():
                sessions.append({
                    "id": row[0],
                    "created_at": row[1],
                    "updated_at": row[2],
                    "metadata": json.loads(row[3]) if row[3] else {}
                })
            
            return sessions
    
    def set_title(self, session_id: str, title: str) -> None:
        """把标题写入会话的 metadata（复用现有 JSON 列，无需迁移 schema）。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                "SELECT metadata FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return
            meta = json.loads(row[0]) if row[0] else {}
            meta["title"] = title
            conn.execute(
                "UPDATE sessions SET metadata = ? WHERE id = ?",
                (json.dumps(meta), session_id),
            )
            conn.commit()

    def delete_session(self, session_id: str) -> None:
        """删除会话"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
