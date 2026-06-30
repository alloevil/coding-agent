# Agent Protocol — Go TUI 对接规范

Python agent core 通过 **stdin/stdout JSON** 与 Go TUI 通信。

## 启动

```bash
python3 -m coding_agent.protocol
```

Agent 启动后自动从 `MODEL_API_KEY` 环境变量读取 API key，输出 `ready` 事件。

## 协议格式

每条消息一行 JSON（`\n` 分隔）。

### TUI → Agent（请求）

| type | 说明 | 字段 |
|------|------|------|
| `user_input` | 用户输入 | `content`, `session_id`(可选) |
| `permission_response` | 权限确认 | `approved`: bool |
| `new_session` | 新建会话 | — |
| `set_auto_approve` | 设置自动批准 | `value`: bool |
| `list_sessions` | 列出会话 | — |
| `init` | 初始化配置(首行) | `api_key`, `model`, `api_base_url`, `auto_approve` |
| `interrupt` | 中断当前执行 | — |

### Agent → TUI（事件）

| type | 说明 | 关键字段 |
|------|------|----------|
| `ready` | 就绪 | `model`, `tools`, `auto_approve` |
| `thinking` | 思考中 | `turn` |
| `stream_text` | 流式文本 | `text` |
| `assistant_message` | 完整消息 | `content` |
| `tool_call` | 工具调用 | `id`, `name`, `arguments` |
| `tool_result` | 工具结果 | `id`, `result`, `is_error` |
| `permission_request` | 需要确认 | `tool_name`, `arguments` |
| `permission_request_event` | 同上(内部) | `id`, `name`, `arguments`, `permission` |
| `error` | 错误 | `error` |
| `done` | 完成 | `turns` |
| `compacting` | 压缩中 | — |
| `session_state` | 会话状态 | `session_id`, `turn_count` |
| `config_updated` | 配置更新 | `auto_approve` |
| `sessions_list` | 会话列表 | `sessions` |
| `interrupted` | 工具被中断 | `id`, `tool_name`, `partial_result` |
| `retrying` | 工具重试中 | `id`, `tool_name`, `attempt`, `max_retries`, `delay` |
| `rollback` | 工具已回滚 | `tool_name`, `result` |

## 交互流程

```
TUI                              Agent
 |                                 |
 |--- init (可选) ---------------->|
 |<-- ready -----------------------|
 |                                 |
 |--- user_input ----------------->|
 |<-- thinking --------------------|
 |<-- stream_text (逐字) ----------|
 |<-- tool_call -------------------|
 |<-- permission_request ----------|  (auto_approve=false 时)
 |--- permission_response -------->|
 |<-- tool_result -----------------|
 |<-- stream_text (逐字) ----------|
 |<-- done ------------------------|
 |<-- session_state ---------------|
```

### 中断流程

```
TUI                              Agent
 |                                 |
 |--- user_input ----------------->|
 |<-- thinking --------------------|
 |<-- tool_call (耗时工具) --------|
 |                                 |
 |--- interrupt ------------------>|  (Ctrl+C / 发送 interrupt)
 |<-- interrupted -----------------|  (工具返回部分结果)
 |<-- tool_result -----------------|  (result = "...[Interrupted by user]")
 |<-- done ------------------------|  (状态保留，可继续)
 |                                 |
 |--- user_input (继续) ---------->|  (上下文不丢失)
 |<-- thinking --------------------|
 |<-- ... -------------------------|
```

### 重试流程

```
TUI                              Agent
 |                                 |
 |<-- tool_call -------------------|
 |<-- retrying --------------------|  (attempt=1/3, delay=1s)
 |<-- retrying --------------------|  (attempt=2/3, delay=2s)
 |<-- tool_result -----------------|  (成功 或 最终失败)
```

### 回滚流程

```
TUI                              Agent
 |                                 |
 |<-- tool_call (file_write) ------|
 |<-- tool_result (成功) ----------|
 |                                 |
 |--- user_input (撤销) ---------->|
 |<-- tool_call (rollback_last) ---|
 |<-- rollback --------------------|
 |<-- tool_result (已回滚) --------|
```

## Go TUI 示例（Bubble Tea）

```go
cmd := exec.Command("python3", "-m", "coding_agent.protocol")
stdin, _ := cmd.StdinPipe()
stdout, _ := cmd.StdoutPipe()
cmd.Start()

// 读取事件
scanner := bufio.NewScanner(stdout)
for scanner.Scan() {
    var event map[string]interface{}
    json.Unmarshal(scanner.Bytes(), &event)
    
    switch event["type"] {
    case "stream_text":
        // 实时显示文本
        fmt.Print(event["text"])
    case "tool_call":
        // 显示工具调用
        fmt.Printf("🔧 %s\n", event["name"])
    case "permission_request":
        // 询问用户
        approved := askUser(event["tool_name"])
        resp, _ := json.Marshal(map[string]interface{}{
            "type": "permission_response",
            "approved": approved,
        })
        stdin.Write(append(resp, '\n'))
    case "done":
        // 完成
        fmt.Printf("\n✨ Done in %v turns\n", event["turns"])
    }
}

// 发送用户输入
input, _ := json.Marshal(map[string]interface{}{
    "type": "user_input",
    "content": "Create a hello world file",
})
stdin.Write(append(input, '\n'))
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `MODEL_API_KEY` | API key（必需） |
| `MODEL_BASE_URL` | API 端点（默认 `https://api.openai.com/v1`） |
| `MODEL_PRIMARY` | 模型名称（默认 `gpt-4`） |
