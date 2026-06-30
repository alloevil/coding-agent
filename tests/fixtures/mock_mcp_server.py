#!/usr/bin/env python3
"""最小 MCP stdio server（仅用于测试）：initialize / tools/list / tools/call。"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method"); rid = req.get("id")
        if method == "initialize":
            send({"jsonrpc":"2.0","id":rid,"result":{
                "protocolVersion":"2024-11-05","capabilities":{},
                "serverInfo":{"name":"mock","version":"1.0"}}})
        elif method == "notifications/initialized":
            pass  # notification, no response
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":rid,"result":{"tools":[
                {"name":"echo","description":"Echo back text",
                 "inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}},
                {"name":"add","description":"Add two numbers",
                 "inputSchema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}}}},
            ]}})
        elif method == "tools/call":
            p = req.get("params", {}); name = p.get("name"); args = p.get("arguments", {})
            if name == "echo":
                send({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":f"echo: {args.get('text','')}"}]}})
            elif name == "add":
                s = (args.get("a",0) or 0) + (args.get("b",0) or 0)
                send({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":str(s)}]}})
            else:
                send({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":"unknown"}],"isError":True}})
        else:
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":"method not found"}})

if __name__ == "__main__":
    main()
