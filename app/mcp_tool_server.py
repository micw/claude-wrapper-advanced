#!/usr/bin/env python3
"""MCP-stdio-Server, der 'das Tool' ist: bei tools/call ANTWORTET ER NICHT (stall).

Dadurch pausiert die CLI am Tool-Call; der Wrapper liest den nativen tool_use aus
dem Output-Stream und beendet den Turn (Kill bei one-shot, Interrupt bei Reuse).
Die exponierten Tools kommen pro Request über die Env-Variable TOOLS_JSON rein.
"""
import json
import os
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    try:
        tools = json.loads(os.environ.get("TOOLS_JSON", "[]"))
    except Exception:
        tools = []

    for line in sys.stdin:  # blockiert bis Input; endet, wenn die CLI stirbt (stdin schließt)
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        method = m.get("method")
        mid = m.get("id")
        if method == "initialize":
            params = m.get("params") or {}
            send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "t", "version": "0.1.0"},
                },
            })
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            # STALL: absichtlich keine Antwort.
            pass
        # notifications & alles andere ignorieren


if __name__ == "__main__":
    main()
