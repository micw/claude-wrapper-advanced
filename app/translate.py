"""Übersetzung OpenAI <-> CLI: History flatten, Tool-Mapping, Modell-Mapping."""
import json
import logging
import uuid

from .config import settings

log = logging.getLogger("translate")

# Anthropic stop_reason -> OpenAI finish_reason
_STOP_MAP = {"max_tokens": "length", "tool_use": "tool_calls",
             "end_turn": "stop", "stop_sequence": "stop"}


def finish_from_stop(stop_reason, default="stop"):
    return _STOP_MAP.get(stop_reason or "", default)


def _text(content) -> str:
    """content kann str, None oder eine Liste von Parts (multimodal) sein."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                # Bilder etc. werden vorerst ignoriert.
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def messages_to_prompt(messages) -> str:
    """Gesamte OpenAI-History zu EINEM Prompt flatten (die CLI antwortet sonst auf jede user-Message).

    Frühere Tool-Interaktionen werden als Text dargestellt (die CLI akzeptiert keine
    injizierten tool_use/tool_result-Blöcke). Das Modell vertraut diesen Text-Ergebnissen.
    """
    id_to_name = {}
    has_image = False
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                id_to_name[tc.get("id")] = (tc.get("function") or {}).get("name")
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image"):
                    has_image = True
    if has_image:
        log.warning("Request enthält Bild-Parts — werden aktuell ignoriert (kein Vision-Support).")

    lines = []
    for m in messages:
        role = m.get("role")
        c = _text(m.get("content"))
        if role == "system":
            lines.append("[System instructions]\n" + c)
        elif role == "user":
            lines.append("User: " + c)
        elif role == "assistant":
            if c:
                lines.append("Assistant: " + c)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                lines.append(
                    f'Assistant: [called tool {fn.get("name")} with arguments {fn.get("arguments")}]'
                )
        elif role == "tool":
            name = id_to_name.get(m.get("tool_call_id"), "tool")
            lines.append(f"Tool {name} returned: {c}")
        else:
            if c:
                lines.append(c)

    preamble = (
        "You are the assistant in a conversation exposed through an OpenAI-compatible API. "
        "Lines like 'Tool X returned: ...' are outputs of tools that were ALREADY executed — "
        "trust those results and do NOT call the same tool again for the same result. "
        "Call an available tool only when you need new information it provides.\n\n"
    )
    closing = "\n\nRespond to the latest message now."
    return preamble + "\n".join(lines) + closing


def openai_tools_to_mcp(tools):
    """OpenAI tools[] -> MCP tools/list-Format."""
    out = []
    for t in tools or []:
        if t.get("type") != "function":
            continue
        f = t.get("function") or {}
        if not f.get("name"):
            continue
        out.append({
            "name": f["name"],
            "description": f.get("description", ""),
            "inputSchema": f.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def tooluse_to_toolcalls(tool_use_blocks):
    """Native tool_use-Blöcke -> OpenAI tool_calls (Präfix mcp__t__ entfernen, args als JSON-String)."""
    out = []
    for b in tool_use_blocks:
        name = b.get("name", "")
        if name.startswith("mcp__t__"):
            name = name[len("mcp__t__"):]
        out.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
            },
        })
    return out


# Eingehende Reasoning-Effort-Werte (OpenAI + OpenRouter) -> CLI --effort-Level.
# Die CLI kennt nur low|medium|high|xhigh|max; none/minimal auf low abbilden.
_EFFORT_MAP = {
    "none": "low", "minimal": "low", "low": "low",
    "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max",
}


def map_effort(body):
    """Client-getriebenes Reasoning-Effort aus dem Request-Body -> CLI-Effort-Level.

    OpenRouter (reasoning.effort) wird bevorzugt, OpenAI (reasoning_effort) ist Fallback.
    Rückgabe: 'low'|'medium'|'high'|'xhigh'|'max' oder None (dann greift der Env-Default).
    """
    r = body.get("reasoning")
    val = r.get("effort") if isinstance(r, dict) else None
    if val is None:
        val = body.get("reasoning_effort")
    if not isinstance(val, str):
        return None
    return _EFFORT_MAP.get(val.strip().lower())


def map_model(m: str) -> str:
    """OpenAI-Modell-ID -> CLI --model. Unbekanntes -> Default."""
    if not m:
        return settings.default_model
    if m in settings.known_models:
        return m
    if m.startswith("claude-") or m.endswith("]"):  # volle Namen / opus[1m] durchreichen
        return m
    return settings.default_model
