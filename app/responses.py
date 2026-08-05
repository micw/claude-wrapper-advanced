"""OpenAI Responses API (/v1/responses) — Übersetzung auf unsere bestehende Pipeline.

Bewusst KEIN zweiter Prompt-Bau: die Responses-`input`-Items werden auf die OpenAI-Chat-Form
gemappt, danach läuft alles durch messages_to_prompt() wie beim Chat-Endpunkt. Damit gelten
History-Flattening, Bild-Handling und die Cache-Block-Struktur unverändert für beide Endpunkte.

Zustandslos: `store` wird ignoriert (wir speichern ohnehin nichts), `previous_response_id`
abgelehnt — stillschweigend zu antworten hieße, mit halbem Kontext zu antworten.
"""
import json
import time
import uuid

from .config import settings


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------- Request -> intern
def _content_parts(parts):
    """Responses-content-Parts -> OpenAI-Chat-Parts (Text + Bild)."""
    out = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text"):
            out.append({"type": "text", "text": p.get("text", "")})
        elif t == "input_image":
            # Responses liefert die URL als String (OpenWebUI: data-URI direkt).
            url = p.get("image_url") or p.get("url")
            out.append({"type": "image_url", "image_url": {"url": url}})
        elif t == "image_url":                       # Kulanz: Chat-Form durchreichen
            out.append(p)
    return out


def input_to_messages(body):
    """Responses-Request -> OpenAI-messages[]. Wirft ValueError bei Nicht-Unterstütztem."""
    if body.get("previous_response_id"):
        raise ValueError(
            "previous_response_id is not supported: this endpoint is stateless and keeps no "
            "conversation state. Send the full conversation in `input` "
            "(Open WebUI: leave ENABLE_RESPONSES_API_STATEFUL off, which is the default)."
        )
    if body.get("background"):
        raise ValueError(
            "background is not supported: responses are not stored, so there would be nothing "
            "to poll for. Use stream=true to follow a long turn instead."
        )

    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    data = body.get("input")
    if isinstance(data, str):                        # Kurzform: nur ein User-Text
        messages.append({"role": "user", "content": data})
        return messages
    if not isinstance(data, list):
        raise ValueError("'input' must be a string or an array of input items")

    for item in data:
        if not isinstance(item, dict):
            continue
        t = item.get("type") or "message"             # ohne type: als message behandeln
        if t == "message":
            content = item.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            else:
                content = _content_parts(content or [])
            messages.append({"role": item.get("role") or "user", "content": content})
        elif t == "function_call":
            messages.append({"role": "assistant", "tool_calls": [{
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {"name": item.get("name") or "",
                             "arguments": item.get("arguments") or "{}"},
            }]})
        elif t == "function_call_output":
            out = item.get("output")
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
            })
        # reasoning-Items schickt der Client ggf. zurück — für uns gegenstandslos (redigiert).
    return messages


def tools_to_openai(tools):
    """Responses-Tools (flach) -> OpenAI-Chat-Form, die openai_tools_to_mcp() erwartet."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") != "function":
            continue                                  # Built-in-Tools (web_search etc.) können wir nicht
        if isinstance(t.get("function"), dict):        # schon geschachtelt -> unverändert
            out.append(t)
            continue
        out.append({"type": "function", "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {"type": "object", "properties": {}},
        }})
    return out


# ---------------------------------------------------------------- intern -> Response
def usage_obj(usage, thinking_tokens=0, cost=None):
    """Unser Chat-usage -> Responses-Namen (Clients normalisieren beide, wir liefern das native).

    reasoning_tokens ist die Summe der `estimated_tokens` aus den thinking_deltas — eine Schätzung
    der CLI, kein abgerechneter Wert. Deshalb auf output_tokens gedeckelt: ein Teilwert, der größer
    ist als die Gesamtsumme, wäre offensichtlich kaputt.
    """
    u = usage or {}
    ptd = u.get("prompt_tokens_details") or {}
    out_tokens = u.get("completion_tokens", 0)
    obj = {
        "input_tokens": u.get("prompt_tokens", 0),
        "input_tokens_details": {"cached_tokens": ptd.get("cached_tokens", 0)},
        "output_tokens": out_tokens,
        "output_tokens_details": {"reasoning_tokens": min(thinking_tokens or 0, out_tokens)},
        "total_tokens": u.get("total_tokens", 0),
    }
    if cost is not None:                      # wie beim Chat-Endpunkt (OpenRouter-Stil)
        obj["cost"] = cost
    return obj


def message_item(text, item_id=None):
    return {"type": "message", "id": item_id or new_id("msg"), "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": text,
                                                "annotations": []}]}


def function_call_item(tc, item_id=None):
    fn = tc.get("function") or {}
    return {"type": "function_call", "id": item_id or new_id("fc"),
            "call_id": tc.get("id"), "name": fn.get("name"),
            "arguments": fn.get("arguments", "{}"), "status": "completed"}


def status_from_stop(stop_reason):
    """Anthropic stop_reason -> (Responses-status, incomplete_details).

    Ohne diese Abbildung meldet eine abgeschnittene Antwort `completed` — der Client hielte ein
    halbes Ergebnis für vollständig. Der Chat-Endpunkt macht dasselbe über finish_reason=length.
    """
    if stop_reason == "max_tokens":
        return "incomplete", {"reason": "max_output_tokens"}
    return "completed", None


def envelope(rid, model, output, *, status="completed", usage=None, thinking_tokens=0,
             cost=None, error=None, tools=None, tool_choice=None, incomplete_details=None):
    """Das Response-Objekt selbst — identisch für non-streaming und response.completed."""
    obj = {
        "id": rid, "object": "response", "created_at": int(time.time()),
        "status": status, "model": model, "output": output,
        # Angefragte Werte zurückspiegeln statt sie zu erfinden. parallel_tool_calls ist dagegen
        # tatsächlich immer False: die CLI emittiert einen tool_use pro Turn.
        "parallel_tool_calls": False,
        "tool_choice": tool_choice if tool_choice is not None else "auto",
        "tools": tools if tools is not None else [],
        "usage": usage_obj(usage, thinking_tokens, cost),
    }
    if incomplete_details:
        obj["incomplete_details"] = incomplete_details
    if error:
        obj["error"] = error
    return obj


class ThinkingSummary:
    """Fortschritt als reasoning-Item mit EINER summary-Part, die ersetzt wird.

    Clients ersetzen die Part an ihrem summary_index (`item['summary'][i] = part`), statt sie
    anzuhängen wie beim Chat-Endpunkt. Deshalb aktualisiert sich hier eine Zeile in place — und
    ein kurzes Intervall ist sinnvoll statt einer Textwand.
    """

    def __init__(self, output_index, interval=None):
        self.output_index = output_index
        self.interval = settings.thinking_interval_responses if interval is None else interval
        self.item_id = new_id("rs")
        self.tokens = 0
        self.last = None
        self.opened = False

    @staticmethod
    def _fmt(n):
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    def _part(self):
        return {"type": "summary_text", "text": f"Thinking… · {self._fmt(self.tokens)} tokens"}

    def update(self, est_tokens, now):
        """Fortschritt verbuchen -> Liste von Events (leer, wenn gedrosselt)."""
        self.tokens += est_tokens or 0
        if self.last is not None and now - self.last < self.interval:
            return []
        self.last = now
        if not self.opened:
            self.opened = True
            return [
                ("response.output_item.added", {
                    "output_index": self.output_index,
                    "item": {"type": "reasoning", "id": self.item_id, "summary": []}}),
                ("response.reasoning_summary_part.added", {
                    "output_index": self.output_index, "summary_index": 0, "part": self._part()}),
            ]
        # .done ERSETZT die Part an summary_index -> Anzeige aktualisiert sich in place.
        return [("response.reasoning_summary_part.done", {
            "output_index": self.output_index, "summary_index": 0, "part": self._part()})]

    def close(self):
        if not self.opened:
            return []
        return [("response.output_item.done", {
            "output_index": self.output_index,
            "item": {"type": "reasoning", "id": self.item_id, "status": "completed",
                     "summary": [self._part()]}})]
