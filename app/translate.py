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
                # Bild-Parts kommen separat als eigene Blöcke (siehe _images).
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


# Part-Typen, die ein Bild tragen: OpenAI Chat (image_url), OpenAI Responses
# (input_image) und die Anthropic-Form (image) für Clients, die sie direkt schicken.
_IMAGE_PART_TYPES = ("image_url", "input_image", "image")
# Vom Backend akzeptierte Bildformate — alles andere wird mit 400 abgelehnt.
_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MEDIA_ALIASES = {"image/jpg": "image/jpeg"}

# Nur base64/data-URI wird unterstützt — bewusst KEINE Remote-URLs:
#  - Das Backend selbst laden zu lassen (source.type "url") scheitert praktisch (robots.txt,
#    nicht öffentlich erreichbare Adressen) und kippt dann den KOMPLETTEN Request mit 400.
#  - Im Wrapper laden würde die Session nicht-deterministisch machen: die History wird jeden Turn
#    neu geschickt, also würde dieselbe URL pro Turn neu geladen — ändern sich die Bytes, bricht
#    der Cache-Prefix, und die Fetch-Latenz fällt jedes Mal an. Außerdem: ausgehende Requests auf
#    client-gelieferte URLs (SSRF-Fläche).
# Wer ein Bild per Link will, nimmt ein Web-Fetch-Tool des Clients — dann ist der Inhalt
# regulärer Teil der Session statt eines unsichtbaren Seiteneffekts.
_REMOTE_UNSUPPORTED = "remote image URLs are not supported, only inline (base64) images"


def _data_uri_source(url):
    """'data:image/png;base64,AAAA' -> Anthropic base64-source. (source|None, Grund)."""
    head, _, data = url.partition(",")
    if not data or "base64" not in head:
        return None, "only base64 data URIs are supported"
    return _base64_source(head[len("data:"):].split(";")[0].strip().lower(), data)


def _base64_source(media, data):
    """base64-Bild -> Anthropic source, normalisiert & gegen die Limits geprüft. (source|None, Grund)."""
    media = _MEDIA_ALIASES.get(media, media)
    if media not in _IMAGE_MEDIA_TYPES:
        return None, f"unsupported media type {media or '(none)'}"
    # Größe ohne Dekodieren abschätzen: base64 ist ~4/3 der Rohbytes.
    size = len(data) * 3 // 4
    if size > settings.max_image_bytes:
        return None, f"too large ({size // (1024 * 1024)} MB > {settings.max_image_mb:g} MB)"
    return {"type": "base64", "media_type": media, "data": data}, ""


def _image_source(part):
    """Bild-Part (OpenAI/Anthropic) -> Anthropic image.source. (source|None, Grund)."""
    src = part.get("source")
    if part.get("type") == "image" and isinstance(src, dict):   # bereits Anthropic-Form
        if src.get("type") == "base64":                          # normalisieren + gegen Limits prüfen
            return _base64_source((src.get("media_type") or "").lower(), src.get("data") or "")
        return None, _REMOTE_UNSUPPORTED                          # source.type "url" -> siehe unten
    url = part.get("image_url") or part.get("url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None, "no image data in the part"
    if url.startswith("data:"):
        return _data_uri_source(url)
    return None, _REMOTE_UNSUPPORTED


def _images(content):
    """Bild-Parts einer Message -> (Anthropic image-Blöcke, Hinweise auf Verworfenes).

    Verworfenes wird als Text angemerkt, damit das Modell nicht über ein Bild redet,
    das nie ankam (sonst: "ich sehe nur einen Datei-Verweis").
    """
    if not isinstance(content, list):
        return [], []
    blocks, notes = [], []
    for p in content:
        if not isinstance(p, dict) or p.get("type") not in _IMAGE_PART_TYPES:
            continue
        if len(blocks) >= settings.max_images:
            notes.append(f"[an image was dropped: more than {settings.max_images} images]")
            continue
        src, why = _image_source(p)
        if src:
            blocks.append({"type": "image", "source": src})
        else:
            log.warning("Bild-Part verworfen: %s", why)
            notes.append(f"[an image could not be passed through: {why}]")
    return blocks, notes


def messages_to_prompt(messages):
    """OpenAI-History -> CLI-User-content (die CLI antwortet sonst auf jede user-Message).

    Frühere Tool-Interaktionen werden als Text dargestellt (die CLI akzeptiert keine
    injizierten tool_use/tool_result-Blöcke). Das Modell vertraut diesen Text-Ergebnissen.

    Bild-Parts (OpenWebUI & Co. schicken beim Paste ein image_url mit data-URI) werden als
    native Anthropic image-Blöcke mitgesendet — die CLI reicht sie über stream-json durch.
    Sie stehen jeweils VOR dem Text ihrer Nachricht.

    Rückgabe:
      - cache_history AUS und keine Bilder: EIN flacher String.
      - cache_history AN : content-ARRAY mit je einem Block PRO Nachricht (bereits
        abgeschlossene Nachrichten bleiben so über Turns byte-stabil) + cache_control auf
        dem letzten Block. Anthropic cached blockweise -> die ganze append-stabile History
        wird inkrementell gecacht (nur der neue Turn ist fresh input). Empirisch verifiziert
        über 50 Turns: create/Turn bleibt konstant statt mit der History zu wachsen.
    """
    id_to_name = {}
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                id_to_name[tc.get("id")] = (tc.get("function") or {}).get("name")

    # Pro Nachricht EIN gerenderter Text (damit abgeschlossene Nachrichten stabile Blöcke bleiben)
    # plus deren Bild-Blöcke.
    msgs = []
    for m in messages:
        role = m.get("role")
        c = _text(m.get("content"))
        images, notes = _images(m.get("content"))
        parts = []
        if role == "system":
            parts.append("[System instructions]\n" + c)
        elif role == "user":
            parts.append("User: " + c)
        elif role == "assistant":
            if c:
                parts.append("Assistant: " + c)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                parts.append(
                    f'Assistant: [called tool {fn.get("name")} with arguments {fn.get("arguments")}]'
                )
        elif role == "tool":
            name = id_to_name.get(m.get("tool_call_id"), "tool")
            parts.append(f"Tool {name} returned: {c}")
        elif c:
            parts.append(c)
        parts += notes
        if parts or images:
            msgs.append(("\n".join(parts), images))

    preamble = (
        "You are the assistant in a conversation exposed through an OpenAI-compatible API. "
        "Lines like 'Tool X returned: ...' are outputs of tools that were ALREADY executed — "
        "trust those results and do NOT call the same tool again for the same result. "
        "Call an available tool only when you need new information it provides.\n\n"
    )
    closing = "\n\nRespond to the latest message now."

    n_images = sum(len(imgs) for _, imgs in msgs)
    if n_images:
        log.debug("Request enthält %d Bild-Block(s)", n_images)

    # Ohne Bilder UND ohne History-Caching bleibt es beim flachen String (unverändertes Verhalten).
    if not settings.cache_history and not n_images:
        return preamble + "\n".join(text for text, _ in msgs) + closing

    blocks = [{"type": "text", "text": preamble}]
    for text, images in msgs:
        blocks += images                              # Bild vor Text (Backend-Empfehlung)
        if text:
            blocks.append({"type": "text", "text": text + "\n"})  # Separator im Block halten (append-stabil)
    if settings.cache_history and len(blocks) > 1:    # cache_control auf den letzten (neuesten) Block
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": settings.cache_history_ttl}
    blocks.append({"type": "text", "text": closing})
    return blocks


class ThinkingProgress:
    """thinking_delta-Events -> gedrosselte Fortschrittszeile für reasoning_content.

    Die CLI redigiert den Denktext (`thinking` ist leer) und liefert nur `estimated_tokens` pro
    Event — als INKREMENT, nicht kumulativ (gemessen: 50, 200, 150, 150, 250, … über 18 Events).
    Wir summieren und zeigen den Stand; ohne das schweigt der Stream bei opus/xhigh minutenlang.

    Clients hängen reasoning-Deltas an (statt sie in place zu ersetzen), deshalb pro Update EINE
    Zeile und nur alle `thinking_interval` Sekunden.
    """

    def __init__(self, interval=None):
        self.tokens = 0
        self.last = None
        self.interval = settings.thinking_interval if interval is None else interval

    @staticmethod
    def _fmt(n):
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    def update(self, est_tokens, now):
        """Fortschritt verbuchen. Gibt die anzuzeigende Zeile zurück — oder None (gedrosselt)."""
        self.tokens += est_tokens or 0
        if self.last is not None and now - self.last < self.interval:
            return None
        prefix = "" if self.last is None else "\n"
        self.last = now
        return f"{prefix}Thinking… · {self._fmt(self.tokens)} tokens"


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
# Direkt exponierbare Effort-Stufen (für Modell-Varianten wie 'opus:max').
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]


def split_model_effort(m):
    """'opus:max' -> ('opus', 'max'). Suffix nur splitten, wenn es eine Effort-Stufe ist.

    Voller CLI-Name (claude-opus-4-8) oder 'opus[1m]' bleiben unangetastet, wenn kein
    gültiger Effort-Suffix dranhängt. Rückgabe: (base_model, effort|None).
    """
    if m and ":" in m:
        base, _, suffix = m.rpartition(":")
        eff = _EFFORT_MAP.get(suffix.strip().lower())
        if eff and base:
            return base, eff
    return m, None


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
