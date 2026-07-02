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


def test_mcp_lists_configured_servers(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.config.mcp_servers = {"fs": {"command": ["mcp-fs"]},
                                    "web": {"url": "http://x/mcp"}}
        proto._mcp_clients = []
        await proto._handle_command_action("mcp")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "fs" in text and "web" in text and "2 configured" in text
    asyncio.run(main())


def test_mcp_empty_is_helpful(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.config.mcp_servers = {}
        await proto._handle_command_action("mcp")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "No MCP servers" in text
    asyncio.run(main())


def test_hooks_lists_configured(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.config.hooks = {"pre_tool_use": [{"command": "x"}, {"command": "y"}]}
        await proto._handle_command_action("hooks")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "pre_tool_use" in text and "2 command" in text
    asyncio.run(main())


def test_doctor_static_renders_report(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        # doctor reads these off config; give it a healthy-ish shape
        proto.config.api_key = "sk-test"
        proto.config.model = "claude-opus-4-8"
        proto.config.api_base_url = "http://x/v1"
        proto.config.protocol = "anthropic"
        proto.config.extra_headers = {"Authorization": "Bearer x"}
        await proto._handle_command_action("doctor")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        # the rendered report has the doctor banner + a tally line
        assert "doctor" in text.lower()
        assert "ok" in text.lower()
        # a bracketed-suffix model would be flagged; a clean one must not be
        assert "illegal suffix" not in text
    asyncio.run(main())


def test_doctor_flags_bracket_model(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.config.api_key = "sk-test"
        proto.config.model = "claude-opus-4-8[1m]"
        proto.config.api_base_url = "http://x/v1"
        proto.config.protocol = "anthropic"
        proto.config.extra_headers = {}
        await proto._handle_command_action("doctor")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "suffix" in text.lower()  # the [1m] marker is called out
    asyncio.run(main())


def test_permissions_shows_current_mode(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.config.auto_approve = False
        await proto._handle_command_action("permissions")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "ask" in text.lower() and "confirmed" in text.lower()
    asyncio.run(main())


def test_permissions_toggle_persists(monkeypatch, tmp_path):
    async def main():
        # redirect the global config dir so the persist write hits tmp
        monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path))
        proto = _make_protocol(monkeypatch)
        proto.config.auto_approve = False
        await proto._handle_command_action("permissions:auto")
        assert proto.config.auto_approve is True
        # config_updated event fired + result mentions the new mode
        assert any(t == "config_updated" and d.get("auto_approve") is True
                   for t, d in proto._events)
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "auto" in text.lower() and "saved" in text.lower()
        # persisted to disk
        import json
        cfg = json.loads((tmp_path / "config.json").read_text())
        assert cfg["auto_approve"] is True
        # toggle back to ask
        proto._events.clear()
        await proto._handle_command_action("permissions:ask")
        assert proto.config.auto_approve is False
        cfg = json.loads((tmp_path / "config.json").read_text())
        assert cfg["auto_approve"] is False
    asyncio.run(main())


def test_status_reports_session_and_tokens(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.config.auto_approve = False
        proto.model_client.total_prompt_tokens = 120
        proto.model_client.total_completion_tokens = 30
        # give it an agent_loop with a permission policy (plan-mode off)
        proto.agent_loop = type("AL", (), {})()
        proto.agent_loop.permission_policy = type("P", (), {"plan_mode": False})()
        await proto._handle_command_action("status")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "Session status" in text
        assert "120 in / 30 out" in text
        assert "approval: ask" in text
        assert "plan-mode" not in text  # off → not shown
    asyncio.run(main())


def test_plan_renders_when_set_and_empty_otherwise(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        # no metadata → "No plan set yet."
        proto.state = type("S", (), {"session_id": "s", "turn_count": 0, "metadata": {}})()
        await proto._handle_command_action("plan")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "No plan set yet" in text
        # with a plan → rendered steps
        proto._events.clear()
        proto.state.metadata = {"plan": [{"step": "write tests", "status": "pending"},
                                         {"step": "ship it", "status": "in_progress"}]}
        await proto._handle_command_action("plan")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "write tests" in text and "ship it" in text
    asyncio.run(main())


def test_plan_mode_toggles_policy_and_handoff(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        proto.state = type("S", (), {"session_id": "s", "turn_count": 0,
                                     "metadata": {"plan": [{"step": "x", "status": "done"}]}})()
        proto.agent_loop = type("AL", (), {})()
        proto.agent_loop.permission_policy = type("P", (), {"plan_mode": False})()
        # turn ON
        await proto._handle_command_action("plan_mode")
        assert proto.agent_loop.permission_policy.plan_mode is True
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "ON" in text
        # turn OFF → injects a one-shot handoff note
        proto._events.clear()
        await proto._handle_command_action("plan_mode")
        assert proto.agent_loop.permission_policy.plan_mode is False
        assert "pending_handoff" in proto.state.metadata
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "OFF" in text
    asyncio.run(main())


def test_config_shows_redacted(monkeypatch, tmp_path):
    async def main():
        monkeypatch.setenv("CODING_AGENT_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(
            '{"api_key": "sk-supersecret", "model": "claude-opus-4-8"}')
        proto = _make_protocol(monkeypatch)
        await proto._handle_command_action("config:")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "claude-opus-4-8" in text
        assert "sk-supersecret" not in text  # redacted
    asyncio.run(main())


def _agent_loop_stub():
    """Minimal agent_loop for /agent tests: config, tool-filter setter, policy."""
    class AL:
        def __init__(self):
            self.config = type("Cfg", (), {"system_prompt": "base"})()
            self.permission_policy = type("P", (), {"plan_mode": False})()
            self._filter = "unset"
        def set_tool_filter(self, f):
            self._filter = f
    return AL()


def test_agent_switch_applies_profile(monkeypatch, tmp_path):
    async def main():
        monkeypatch.chdir(tmp_path)
        d = tmp_path / ".coding-agent" / "agents"
        d.mkdir(parents=True)
        (d / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: careful reviewer\n"
            "model: claude-sonnet-5\n---\nYou review code carefully.")
        proto = _make_protocol(monkeypatch)
        proto.state = type("S", (), {"session_id": "s", "turn_count": 0, "metadata": {}})()
        proto.agent_loop = _agent_loop_stub()
        proto.model_client.model = "old"
        await proto._handle_command_action("agent:reviewer")
        # profile applied: prompt, model (+ model_changed event), metadata
        assert proto.agent_loop.config.system_prompt == "You review code carefully."
        assert proto.config.model == "claude-sonnet-5"
        assert proto.model_client.model == "claude-sonnet-5"
        assert proto.state.metadata["active_agent"] == "reviewer"
        assert any(t == "model_changed" for t, _ in proto._events)
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "reviewer" in text and "careful reviewer" in text
    asyncio.run(main())


def test_agent_switch_unknown_is_friendly(monkeypatch, tmp_path):
    async def main():
        monkeypatch.chdir(tmp_path)
        proto = _make_protocol(monkeypatch)
        proto.state = type("S", (), {"session_id": "s", "turn_count": 0, "metadata": {}})()
        proto.agent_loop = _agent_loop_stub()
        await proto._handle_command_action("agent:nope")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "not found" in text and "/agents" in text
    asyncio.run(main())


def test_setup_emits_open_setup_event(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)
        await proto._handle_command_action("setup")
        kinds = [t for t, _ in proto._events]
        assert "open_setup" in kinds  # TUI reopens the wizard on this
        assert "command_result" in kinds
    asyncio.run(main())


def test_compact_suppresses_stream_and_reports_reduction(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)

        # A fake context manager whose compact() calls _call_model (like the real
        # layer-2 summary) and shrinks the token estimate.
        calls = {"suppressed_during": None}

        class _CM:
            async def compact(self, state, model_call_fn):
                # capture whether streaming is suppressed at call time
                calls["suppressed_during"] = getattr(proto, "_suppress_stream", False)
                await model_call_fn([{"role": "user", "content": "summarize"}], [])
                state._tok = 50  # shrink

        class _State:
            def __init__(self): self._tok = 500
            def get_token_estimate(self): return self._tok
        proto.state = _State()
        proto.agent_loop = type("AL", (), {"context_manager": _CM()})()

        # stub the model client so _call_model streams a chunk (which must be
        # suppressed) and returns a response
        class _MC:
            async def complete(self, ctx, tools, on_text_delta=None,
                               on_reasoning_delta=None, stream=True):
                if on_text_delta is not None:
                    on_text_delta("SECRET SUMMARY TEXT")
                return {"content": "ok", "tool_calls": [], "usage": {}}
        proto.model_client = _MC()

        await proto._handle_command_action("compact")

        # streaming was suppressed while compact ran → no stream_text leaked
        assert calls["suppressed_during"] is True
        assert not any(t == "stream_text" for t, _ in proto._events), \
            "compaction summary must not stream into the transcript"
        # flag restored afterwards
        assert getattr(proto, "_suppress_stream", False) is False
        # receipt reports the reduction
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "compacted" in text.lower() and "500" in text and "50" in text
    asyncio.run(main())


def test_compact_noop_receipt_when_nothing_reclaimed(monkeypatch):
    async def main():
        proto = _make_protocol(monkeypatch)

        class _CM:
            async def compact(self, state, model_call_fn):
                pass  # under threshold → no change

        class _State:
            def get_token_estimate(self): return 100
        proto.state = _State()
        proto.agent_loop = type("AL", (), {"context_manager": _CM()})()
        proto.model_client = type("MC", (), {})()

        await proto._handle_command_action("compact")
        text = next(d["text"] for t, d in proto._events if t == "command_result")
        assert "already compact" in text.lower()
    asyncio.run(main())
