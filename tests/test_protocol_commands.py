"""
测试 protocol.py 处理 slash 命令：/model /help 等在后端被拦截分发，
作为 command_result / model_changed 事件发回，而不是喂给 LLM（不启动 turn）。
"""
import asyncio


def _make_protocol(monkeypatch):
    from coding_agent import protocol as P

    for reg in ("register_file_tools", "register_shell_tools", "register_git_tools"):
        monkeypatch.setattr(P, reg, lambda *a, **k: None, raising=False)

    proto = P.AgentProtocol.__new__(P.AgentProtocol)
    proto.config = type("C", (), {"auto_approve": True, "model": "old-model",
                                  "protocol": "openai", "session_db_path": ":memory:"})()
    proto._turn_task = None
    proto.state = type("S", (), {"session_id": "s", "turn_count": 3})()
    proto.plan_tool = type("PT", (), {"bind_state": lambda self, s: None})()
    proto.session_store = type("SS", (), {
        "load_state": lambda self, sid: None,
        "create_session": lambda self: "s2",
    })()
    proto.tool_registry = type("TR", (), {"get_all_tools": lambda self: []})()
    proto.model_client = type("MC", (), {
        "total_prompt_tokens": 0, "total_completion_tokens": 0,
        "total_reasoning_tokens": 0, "cache_hit_rate": 0.0, "model": "old-model",
    })()
    proto._events = []
    proto._send_event = lambda t, d=None: proto._events.append((t, d or {}))
    return proto


def test_slash_command_not_run_as_turn(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        await proto.handle_request({"type": "user_input", "content": "/help"})
        # /help → command_result 事件；不应启动 turn
        assert proto._turn_task is None
        kinds = [t for t, _ in proto._events]
        assert "command_result" in kinds
    asyncio.run(main())


def test_slash_model_switches_and_emits_model_changed(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        await proto.handle_request({"type": "user_input", "content": "/model gpt-4o"})
        assert proto.config.model == "gpt-4o"
        assert proto.model_client.model == "gpt-4o"
        kinds = [t for t, _ in proto._events]
        assert "model_changed" in kinds
        # the model_changed event carries the new model
        mc_ev = next(d for t, d in proto._events if t == "model_changed")
        assert mc_ev["model"] == "gpt-4o"
    asyncio.run(main())


def test_bare_slash_model_reports_current(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        await proto.handle_request({"type": "user_input", "content": "/model"})
        # no arg → reports current model as a command_result, no switch
        assert proto.config.model == "old-model"
        texts = [d.get("text", "") for t, d in proto._events if t == "command_result"]
        assert any("old-model" in t for t in texts)
    asyncio.run(main())


def test_non_command_still_runs_turn(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        # a normal message must still start a turn (not be swallowed)
        started = {"v": False}

        async def fake_turn(content):
            started["v"] = True
        proto._run_turn = fake_turn
        await proto.handle_request({"type": "user_input", "content": "hello there"})
        await asyncio.sleep(0.01)
        assert started["v"] is True
    asyncio.run(main())


def test_bang_shell_runs_tool_and_emits_output(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)

        class FakeShell:
            async def execute(self, **kw):
                assert kw["command"] == "echo hi"
                return "hi\n"

        proto.tool_registry.get_tool = lambda name: FakeShell() if name == "shell_exec" else None
        # state 需要 add_user_message
        msgs = []
        proto.state = type("S", (), {
            "session_id": "s", "turn_count": 0,
            "add_user_message": lambda self, m: msgs.append(m),
        })()
        await proto.handle_request({"type": "user_input", "content": "!echo hi"})
        if proto._turn_task:
            await asyncio.wait_for(proto._turn_task, timeout=2)
        await asyncio.sleep(0.01)
        # shell_output 事件带命令和输出
        ev = next(d for t, d in proto._events if t == "shell_output")
        assert ev["command"] == "echo hi"
        assert "hi" in ev["output"]
        # 记入了上下文
        assert msgs and "echo hi" in msgs[0]
    asyncio.run(main())


def test_bare_bang_is_normal_message(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        started = {"v": False}

        async def fake_turn(content):
            started["v"] = True
        proto._run_turn = fake_turn
        # 单独一个 "!" 不是命令，照常走 turn
        await proto.handle_request({"type": "user_input", "content": "!"})
        await asyncio.sleep(0.01)
        assert started["v"] is True
    asyncio.run(main())


def test_rewind_pops_through_last_user_message(monkeypatch):
    async def main():
        from coding_agent.core.state import AgentState
        proto = _make_protocol(monkeypatch)
        st = AgentState()
        st.add_user_message("first")
        st.add_assistant_message("reply one")
        st.add_user_message("second ask")
        st.add_assistant_message("reply two")
        st.turn_count = 2
        proto.state = st
        await proto.handle_request({"type": "rewind"})
        # rewound 事件带被弹出的 user 文本
        ev = next(d for t, d in proto._events if t == "rewound")
        assert ev["text"] == "second ask"
        # 消息弹到最后一个 user 之前；turn 回退
        assert len(st.messages) == 2
        assert st.messages[-1].content == "reply one"
        assert st.turn_count == 1
    asyncio.run(main())


def test_rewind_on_empty_state_is_safe(monkeypatch):
    async def main():
        from coding_agent.core.state import AgentState
        proto = _make_protocol(monkeypatch)
        proto.state = AgentState()
        await proto.handle_request({"type": "rewind"})
        ev = next(d for t, d in proto._events if t == "rewound")
        assert ev["text"] == ""
    asyncio.run(main())


def test_slash_quit_emits_quit_event(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        await proto.handle_request({"type": "user_input", "content": "/exit"})
        kinds = [t for t, _ in proto._events]
        assert "quit" in kinds, "/exit must emit a quit event for the TUI to close"
        # /quit alias too
        proto._events.clear()
        await proto.handle_request({"type": "user_input", "content": "/quit"})
        assert "quit" in [t for t, _ in proto._events]
    asyncio.run(main())


def test_slash_resume_lists_sessions(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.session_store = type("SS", (), {
            "list_sessions": lambda self: [{"id": "s1"}],
        })()
        await proto.handle_request({"type": "user_input", "content": "/resume"})
        ev = next(d for t, d in proto._events if t == "sessions_list")
        assert ev["sessions"] == [{"id": "s1"}]
    asyncio.run(main())


def test_memory_add_and_show(monkeypatch, tmp_path):
    async def main():
        monkeypatch.chdir(tmp_path)  # ProjectMemoryManager(".") writes here
        proto = _make_protocol(monkeypatch)
        # add
        await proto.handle_request({"type": "user_input", "content": "/memory add build uses bazel"})
        assert any("Saved" in d.get("text", "") for t, d in proto._events if t == "command_result")
        # show reflects it
        proto._events.clear()
        await proto.handle_request({"type": "user_input", "content": "/memory"})
        shown = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "bazel" in shown
    asyncio.run(main())


def test_export_writes_markdown(monkeypatch, tmp_path):
    async def main():
        from coding_agent.core.state import AgentState
        monkeypatch.chdir(tmp_path)
        proto = _make_protocol(monkeypatch)
        st = AgentState(session_id="abc12345")
        st.add_user_message("do the thing")
        st.add_assistant_message("done")
        proto.state = st
        await proto.handle_request({"type": "user_input", "content": "/export out.md"})
        assert any("Exported" in d.get("text", "") for t, d in proto._events if t == "command_result")
        content = (tmp_path / "out.md").read_text()
        assert "do the thing" in content and "done" in content
    asyncio.run(main())
