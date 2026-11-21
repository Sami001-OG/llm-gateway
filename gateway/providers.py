"""Outbound provider calls and OpenAI <-> Anthropic wire-format translation.

The gateway exposes an OpenAI-compatible API. Requests aimed at a pool whose
provider is "anthropic" are translated on the way in and back out again, so
clients never need to know which provider served a given request.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("gateway.providers")


# --------------------------------------------------------------------------- #
# Result containers
# --------------------------------------------------------------------------- #
@dataclass
class Failure:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""


@dataclass
class SuccessJSON:
    status: int
    data: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SuccessStream:
    status: int
    stream: AsyncIterator[bytes]
    media_type: str = "text/event-stream"
    headers: dict[str, str] = field(default_factory=dict)


ProviderResult = Failure | SuccessJSON | SuccessStream


# --------------------------------------------------------------------------- #
# Translation helpers (OpenAI -> Anthropic)
# --------------------------------------------------------------------------- #
def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    """Convert OpenAI multimodal content into Anthropic content blocks."""
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
        return blocks
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    try:
                        header, b64 = url.split(",", 1)
                        media_type = header[5:].split(";")[0] or "image/png"
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        })
                    except ValueError:
                        log.warning("Malformed data: image_url dropped")
    return blocks


def openai_to_anthropic(payload: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    for msg in payload.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            text = _content_to_text(content)
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": _content_to_text(content),
                }],
            })
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = _content_to_blocks(content)
            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {}) or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            messages.append({"role": "assistant", "content": blocks})
            continue

        # user / anything else
        messages.append({"role": "user", "content": _content_to_blocks(content)})

    out: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": int(
            payload.get("max_completion_tokens")
            or payload.get("max_tokens")
            or 4096
        ),
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if payload.get("stream"):
        out["stream"] = True
    # temperature/top_p/top_k were REMOVED in Anthropic API version 2026-10-21,
    # and on Claude 4.7+ even older versions reject non-default values with 400.
    # Drop them rather than forwarding a guaranteed error.
    if "temperature" in payload or "top_p" in payload:
        log.debug("Dropping temperature/top_p: unsupported by current Claude models")
    if payload.get("stop"):
        stop = payload["stop"]
        out["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if payload.get("tools"):
        out["tools"] = [
            {
                "name": (t.get("function") or {}).get("name", ""),
                "description": (t.get("function") or {}).get("description", ""),
                "input_schema": (t.get("function") or {}).get("parameters")
                or {"type": "object", "properties": {}},
            }
            for t in payload["tools"]
            if isinstance(t, dict)
        ]
    if payload.get("tool_choice") is not None:
        tc = payload["tool_choice"]
        if isinstance(tc, str):
            out["tool_choice"] = {
                "auto": {"type": "auto"},
                "none": {"type": "none"},
                "required": {"type": "any"},
            }.get(tc, {"type": "auto"})
        elif isinstance(tc, dict) and tc.get("type") == "function":
            out["tool_choice"] = {
                "type": "tool",
                "name": (tc.get("function") or {}).get("name", ""),
            }
    return out


# --------------------------------------------------------------------------- #
# Translation helpers (Anthropic -> OpenAI)
# --------------------------------------------------------------------------- #
_STOP_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def anthropic_to_openai(data: dict[str, Any], model: str) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content", []) or []:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = data.get("usage", {}) or {}
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _STOP_MAP.get(data.get("stop_reason"), "stop"),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _sse_frame(
    delta: dict[str, Any],
    msg_id: str,
    model: str,
    finish: Any = None,
    usage: dict[str, int] | None = None,
    role_already_sent: bool = False,
) -> tuple[str, bool]:
    if not role_already_sent:
        delta = {"role": "assistant", **delta}
    choice: dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": finish}
    obj: dict[str, Any] = {
        "id": msg_id or "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        obj["usage"] = usage
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n", True


async def anthropic_stream_to_openai(
    resp: httpx.Response, requested_model: str
) -> AsyncIterator[bytes]:
    """Translate Anthropic's SSE event stream into OpenAI chat.completion chunks."""
    msg_id = ""
    model = requested_model
    in_tokens = 0
    out_tokens = 0
    finish_reason: Any = "stop"
    role_sent = False
    tool_index = 0
    active_tool: dict[str, Any] | None = None

    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            ev = json.loads(raw)
        except (TypeError, ValueError):
            continue
        etype = ev.get("type")

        if etype == "message_start":
            message = ev.get("message", {}) or {}
            msg_id = message.get("id", msg_id)
            model = message.get("model", model)
            in_tokens = (message.get("usage") or {}).get("input_tokens", 0)
            frame, role_sent = _sse_frame({}, msg_id, model, role_already_sent=role_sent)
            yield frame.encode("utf-8")

        elif etype == "content_block_start":
            block = ev.get("content_block", {}) or {}
            if block.get("type") == "tool_use":
                active_tool = {
                    "index": tool_index,
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                }
                frame, role_sent = _sse_frame(
                    {"tool_calls": [{
                        "index": tool_index,
                        "id": active_tool["id"],
                        "type": "function",
                        "function": {"name": active_tool["name"], "arguments": ""},
                    }]},
                    msg_id, model, role_already_sent=role_sent,
                )
                yield frame.encode("utf-8")
                tool_index += 1

        elif etype == "content_block_delta":
            delta = ev.get("delta", {}) or {}
            if delta.get("type") == "text_delta":
                frame, role_sent = _sse_frame(
                    {"content": delta.get("text", "")},
                    msg_id, model, role_already_sent=role_sent,
                )
                yield frame.encode("utf-8")
            elif delta.get("type") == "input_json_delta" and active_tool is not None:
                frame, role_sent = _sse_frame(
                    {"tool_calls": [{
                        "index": active_tool["index"],
                        "function": {"arguments": delta.get("partial_json", "")},
                    }]},
                    msg_id, model, role_already_sent=role_sent,
                )
                yield frame.encode("utf-8")

        elif etype == "content_block_stop":
            if active_tool is not None:
                active_tool = None

        elif etype == "message_delta":
            md = ev.get("delta", {}) or {}
            out_tokens = (ev.get("usage") or {}).get("output_tokens", out_tokens)
            if md.get("stop_reason"):
                finish_reason = _STOP_MAP.get(md["stop_reason"], "stop")

        elif etype == "message_stop":
            frame, role_sent = _sse_frame(
                {}, msg_id, model, finish=finish_reason,
                usage={
                    "prompt_tokens": in_tokens,
                    "completion_tokens": out_tokens,
                    "total_tokens": in_tokens + out_tokens,
                },
                role_already_sent=role_sent,
            )
            yield frame.encode("utf-8")

    yield b"data: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# Reverse translation: Anthropic request -> OpenAI request (for /v1/messages
# calls that get routed to an OpenAI pool)
# --------------------------------------------------------------------------- #
def _anthropic_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def anthropic_to_openai_request(payload: dict[str, Any], upstream_model: str) -> dict[str, Any]:
    system = payload.get("system")
    messages: list[dict[str, Any]] = []
    if system:
        system_text = _anthropic_content_to_text(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    for msg in payload.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "assistant":
            text = _anthropic_content_to_text(content)
            tool_calls = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                        },
                    })
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            continue

        # user (or tool_result inside user content)
        texts: list[str] = []
        for block in content if isinstance(content, list) else [content]:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    texts.append(block)
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_result":
                tool_content = block.get("content", "")
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _anthropic_content_to_text(tool_content),
                })
            elif btype == "image":
                source = block.get("source", {}) or {}
                if source.get("type") == "base64":
                    media = source.get("media_type", "image/png")
                    data = source.get("data", "")
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
                        ],
                    })
        if texts:
            messages.append({"role": "user", "content": "\n".join(texts)})

    out: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": int(payload.get("max_tokens", 4096)),
    }
    if payload.get("stream"):
        out["stream"] = True
    if "temperature" in payload:
        out["temperature"] = payload["temperature"]
    if "top_p" in payload:
        out["top_p"] = payload["top_p"]
    if payload.get("stop_sequences"):
        out["stop"] = list(payload["stop_sequences"])
    if payload.get("tools"):
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for t in payload["tools"] if isinstance(t, dict)
        ]
    if payload.get("tool_choice") is not None:
        tc = payload["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "tool":
            out["tool_choice"] = {
                "type": "function",
                "function": {"name": tc.get("name", "")},
            }
        elif isinstance(tc, dict) and tc.get("type") in ("any", "auto"):
            out["tool_choice"] = "required" if tc.get("type") == "any" else "auto"
        elif isinstance(tc, dict) and tc.get("type") == "none":
            out["tool_choice"] = "none"
    return out


_REVERSE_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def openai_to_anthropic_response(data: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {}) or {}
    blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {}) or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": args,
        })

    usage = data.get("usage", {}) or {}
    return {
        "id": data.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": _REVERSE_STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _iter_sse_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    buf = b""
    async for chunk in chunks:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace").rstrip("\r")
    if buf.strip():
        yield buf.decode("utf-8", "replace").rstrip("\r")


def _anthropic_event(event_type: str, data: dict[str, Any]) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


async def openai_stream_to_anthropic(
    chunks: AsyncIterator[bytes], model: str
) -> AsyncIterator[bytes]:
    """Turn OpenAI chat.completion SSE chunks into Anthropic Messages SSE events."""
    msg_id = ""
    text_started = False
    tools: dict[int, dict[str, Any]] = {}
    finish_reason = "end_turn"
    in_tokens = 0
    out_tokens = 0

    message_started = False

    def start_event() -> bytes:
        return _anthropic_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id or "msg_gateway",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    async for line in _iter_sse_lines(chunks):
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        msg_id = event.get("id", msg_id)
        if not message_started:
            yield start_event()
            message_started = True
        if event.get("usage"):
            in_tokens = event["usage"].get("prompt_tokens", in_tokens)
            out_tokens = event["usage"].get("completion_tokens", out_tokens)
        for choice in event.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}
            text = delta.get("content")
            if text:
                if not text_started:
                    yield _anthropic_event("content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    })
                    text_started = True
                yield _anthropic_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                })
            for tc in delta.get("tool_calls", []) or []:
                idx = tc.get("index", 0)
                fn = tc.get("function", {}) or {}
                if idx not in tools:
                    tools[idx] = {
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "index": idx,
                    }
                    yield _anthropic_event("content_block_start", {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tools[idx]["id"],
                            "name": tools[idx]["name"],
                            "input": {},
                        },
                    })
                if fn.get("arguments"):
                    yield _anthropic_event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    })
            if choice.get("finish_reason"):
                finish_reason = _REVERSE_STOP_MAP.get(choice["finish_reason"], "end_turn")

    if not message_started:
        yield start_event()

    if text_started:
        yield _anthropic_event("content_block_stop", {
            "type": "content_block_stop", "index": 0
        })
    for idx in tools:
        yield _anthropic_event("content_block_stop", {
            "type": "content_block_stop", "index": idx
        })
    yield _anthropic_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": finish_reason, "stop_sequence": None},
        "usage": {"output_tokens": out_tokens},
    })
    yield _anthropic_event("message_stop", {"type": "message_stop"})


# --------------------------------------------------------------------------- #
# Upstream dispatch
# --------------------------------------------------------------------------- #
async def call_upstream(
    client: httpx.AsyncClient,
    provider: str,
    base_url: str,
    key: str,
    payload: dict[str, Any],
    timeout: float,
    stream: bool,
    response_format: str = "openai",   # "openai" | "anthropic"
    forward_headers: dict[str, str] | None = None,
) -> ProviderResult:
    if provider == "openai":
        return await _call_openai(
            client, base_url, key, payload, timeout, stream, response_format
        )
    if provider == "anthropic":
        return await _call_anthropic(
            client, base_url, key, payload, timeout, stream, response_format,
            forward_headers or {},
        )
    return Failure(0, {}, f"Unsupported provider: {provider}")


async def call_responses(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    payload: dict[str, Any],
    timeout: float,
    stream: bool,
) -> ProviderResult:
    """OpenAI Responses API pass-through (needed by current Codex releases,
    which no longer support wire_api = 'chat')."""
    return await _call_openai(
        client, base_url, key, payload, timeout, stream,
        response_format="openai", endpoint="responses",
    )


def _hdrs(status: int, resp: httpx.Response) -> dict[str, str]:
    result = {k: v for k, v in resp.headers.items()}
    result["x-gateway-upstream-status"] = str(status)
    return result


async def _call_openai(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    payload: dict[str, Any],
    timeout: float,
    stream: bool,
    response_format: str,
    endpoint: str = "chat/completions",
) -> ProviderResult:
    url = f"{base_url}/{endpoint}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    model = payload.get("model", "")
    try:
        if not stream:
            resp = await client.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code >= 400:
                return Failure(resp.status_code, _hdrs(resp.status_code, resp), resp.text)
            data = resp.json()
            if response_format == "anthropic":
                data = openai_to_anthropic_response(data, model)
            return SuccessJSON(resp.status_code, data, _hdrs(resp.status_code, resp))

        cm = client.stream("POST", url, headers=headers, json=payload, timeout=timeout)
        resp = await cm.__aenter__()
        if resp.status_code >= 400:
            body = await resp.aread()
            await cm.__aexit__(None, None, None)
            return Failure(resp.status_code, _hdrs(resp.status_code, resp), body.decode("utf-8", "replace"))

        async def raw_gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await cm.__aexit__(None, None, None)

        stream_out: AsyncIterator[bytes]
        if response_format == "anthropic":
            stream_out = openai_stream_to_anthropic(raw_gen(), model)
        else:
            stream_out = raw_gen()
        return SuccessStream(resp.status_code, stream_out, headers=_hdrs(resp.status_code, resp))
    except httpx.HTTPError as exc:
        log.debug("OpenAI upstream network error: %s", exc)
        return Failure(0, {}, str(exc))
    except Exception as exc:  # noqa: BLE001 - keep gateway alive on bad upstreams
        log.exception("OpenAI upstream error")
        return Failure(0, {}, str(exc))


async def _call_anthropic(
    client: httpx.AsyncClient,
    base_url: str,
    key: str,
    payload: dict[str, Any],
    timeout: float,
    stream: bool,
    response_format: str,
    forward_headers: dict[str, str] | None = None,
) -> ProviderResult:
    url = f"{base_url}/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    # Keep the client's anthropic-version/beta values; upstream needs the exact
    # capability set paired with the body fields Claude Code sent.
    for name, value in (forward_headers or {}).items():
        if name.startswith("anthropic-") and name != "anthropic-workspace-id":
            headers[name] = value
    body = payload  # already in Anthropic format
    model = body.get("model", "")
    try:
        if not stream:
            resp = await client.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code >= 400:
                return Failure(resp.status_code, _hdrs(resp.status_code, resp), resp.text)
            data = resp.json()
            if response_format == "openai":
                data = anthropic_to_openai(data, model)
            return SuccessJSON(resp.status_code, data, _hdrs(resp.status_code, resp))

        cm = client.stream("POST", url, headers=headers, json=body, timeout=timeout)
        resp = await cm.__aenter__()
        if resp.status_code >= 400:
            raw = await resp.aread()
            await cm.__aexit__(None, None, None)
            return Failure(resp.status_code, _hdrs(resp.status_code, resp), raw.decode("utf-8", "replace"))

        async def raw_gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await cm.__aexit__(None, None, None)

        stream_out: AsyncIterator[bytes]
        if response_format == "openai":
            translated = anthropic_stream_to_openai(resp, model)
            # Close the httpx stream when the translated generator finishes.
            async def translated_gen() -> AsyncIterator[bytes]:
                try:
                    async for frame in translated:
                        yield frame
                finally:
                    await cm.__aexit__(None, None, None)
            stream_out = translated_gen()
        else:
            stream_out = raw_gen()

        return SuccessStream(resp.status_code, stream_out, headers=_hdrs(resp.status_code, resp))
    except httpx.HTTPError as exc:
        log.debug("Anthropic upstream network error: %s", exc)
        return Failure(0, {}, str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("Anthropic upstream error")
        return Failure(0, {}, str(exc))
