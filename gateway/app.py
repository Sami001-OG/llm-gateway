"""Starlette ASGI application for the LLM gateway."""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import yaml
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import ui_api
from .config import GatewayConfig, RouteConfig, load_config
from .keypool import KeyPool, KeyState
from .providers import (
    Failure,
    SuccessJSON,
    SuccessStream,
    anthropic_to_openai_request,
    call_responses,
    call_upstream,
    openai_to_anthropic,
)

log = logging.getLogger("gateway.app")


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    pool: str
    model_override: str | None = None


class GatewayState:
    def __init__(self, config: GatewayConfig):
        self.config = config
        self.pools: dict[str, KeyPool] = {
            name: KeyPool(pool_cfg) for name, pool_cfg in config.pools.items()
        }
        self.client: httpx.AsyncClient | None = None
        self.raw_config: dict[str, Any] = {}
        if config.config_path:
            try:
                with open(config.config_path, "r", encoding="utf-8") as fh:
                    self.raw_config = yaml.safe_load(fh) or {}
            except OSError as exc:  # pragma: no cover
                log.warning("Could not read raw config: %s", exc)

    def start(self) -> None:
        limits = httpx.Limits(max_connections=512, max_keepalive_connections=128)
        self.client = httpx.AsyncClient(limits=limits, follow_redirects=True)
        log.info("HTTP client ready (max 512 connections)")

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def reload(self, path: str | None = None) -> int:
        path = path or self.config.config_path
        new_config = load_config(path)
        with open(path, "r", encoding="utf-8") as fh:
            self.raw_config = yaml.safe_load(fh) or {}
        self.config = new_config
        self.pools = {name: KeyPool(p) for name, p in new_config.pools.items()}
        return len(new_config.pools)

    # ------------------------------------------------------------------ #
    # Routing
    # ------------------------------------------------------------------ #
    def resolve(self, model: str | None) -> list[Candidate]:
        """Map a public model name to an ordered list of (pool, upstream model)."""
        if not model:
            return []
        route: RouteConfig | None = self.config.routes.get(model)
        if route:
            if route.model_override:
                return [Candidate(route.pools[0], route.model_override)]
            result = []
            for name in route.pools:
                pool_cfg = self.config.pools.get(name)
                if not pool_cfg:
                    continue
                upstream = self._upstream_model(pool_cfg.models, pool_cfg.default_model, model, None)
                if upstream:
                    result.append(Candidate(name, upstream))
            return result

        # No explicit route: any pool that declares support for this model.
        result = []
        for name, pool_cfg in self.config.pools.items():
            if model in pool_cfg.models:
                upstream = self._upstream_model(pool_cfg.models, pool_cfg.default_model, model, None)
                if upstream:
                    result.append(Candidate(name, upstream))
        return result

    @staticmethod
    def _upstream_model(
        pool_models: list[str],
        default_model: str | None,
        requested: str,
        override: str | None,
    ) -> str | None:
        if override:
            return override
        if requested in pool_models:
            return requested
        return default_model or (pool_models[0] if pool_models else None)

    def list_models(self) -> list[str]:
        names: set[str] = set()
        names.update(self.config.routes.keys())
        for pool in self.config.pools.values():
            names.update(pool.models)
        return sorted(names)


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #
class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, state: GatewayState):
        super().__init__(app)
        self.state = state

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path in ("/health", "/ready", "/", "/ui", "/ui/"):
            return await call_next(request)

        is_admin = path.startswith("/admin")
        expected = self.state.config.admin_tokens if is_admin else self.state.config.auth_tokens
        if expected:
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else header
            token = token or request.headers.get("x-api-key", "")
            if token not in expected:
                return JSONResponse(
                    {"error": {"message": "Invalid or missing API key", "type": "auth_error"}},
                    status_code=401,
                )
        return await call_next(request)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_PASSTHROUGH_HEADERS = {
    "x-request-id", "x-should-retry", "retry-after", "anthropic-workspace-id",
}


def _clean_headers(upstream: dict[str, str]) -> dict[str, str]:
    """Forward safe headers; drop hop-by-hop/content headers that Starlette sets."""
    return {
        k: v for k, v in upstream.items()
        if k.lower().startswith("x-gateway-")
        or k.lower().startswith("anthropic-ratelimit-")
        or k.lower() in _PASSTHROUGH_HEADERS
    }


def _error(
    status: int, message: str, detail: Any = None, anthropic: bool = False
) -> JSONResponse:
    if anthropic:
        payload: dict[str, Any] = {
            "type": "error",
            "error": {
                "type": "gateway_error",
                "message": message,
                "details": detail,
            },
        }
    else:
        payload = {
            "error": {
                "message": message,
                "type": "gateway_error",
                "code": status,
            }
        }
        if detail is not None:
            payload["error"]["detail"] = detail
    return JSONResponse(payload, status_code=status)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
async def ui_shell(request: Request) -> HTMLResponse:
    """Public HTML shell; all data comes from admin-token-protected endpoints."""
    path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


async def health(request: Request) -> JSONResponse:
    state: GatewayState = request.app.state.gateway
    available = sum(
        sum(1 for k in pool.keys if k.available())
        for pool in state.pools.values()
    )
    total = sum(len(pool.keys) for pool in state.pools.values())
    return JSONResponse({
        "status": "ok",
        "pools": len(state.pools),
        "keys_available": available,
        "keys_total": total,
    })


async def models(request: Request) -> JSONResponse:
    state: GatewayState = request.app.state.gateway
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 0,
                "owned_by": "gateway",
                # Claude Code gateway model discovery reads these two fields.
                "display_name": m,
                "description": "Routed across the configured account pool",
            }
            for m in state.list_models()
        ],
    })


async def count_tokens(request: Request) -> JSONResponse:
    """Anthropic's /v1/messages/count_tokens - rough estimate (no upstream call)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "Request body must be valid JSON", anthropic=True)
    text = json.dumps(body.get("messages", []), ensure_ascii=False)
    return JSONResponse({"input_tokens": max(1, len(text) // 4)})


async def chat_completions(request: Request) -> Response:
    """OpenAI-compatible endpoint (OpenCode, Codex, most agent frameworks)."""
    return await _handle_completion(request, api_format="openai")


async def anthropic_messages(request: Request) -> Response:
    """Anthropic-compatible endpoint (Claude Code and the Anthropic SDK)."""
    return await _handle_completion(request, api_format="anthropic")


async def responses_api(request: Request) -> Response:
    """OpenAI Responses API pass-through (required by current Codex releases)."""
    state: GatewayState = request.app.state.gateway
    started = time.monotonic()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "Request body must be valid JSON")
    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        return _error(400, "Missing required field: model")

    candidates = [
        c for c in state.resolve(model)
        if c.pool in state.config.pools
        and state.config.pools[c.pool].provider == "openai"
    ]
    if not candidates:
        return _error(
            400,
            "Responses API requires an OpenAI pool; no OpenAI candidate for this model",
        )

    assert state.client is not None, "HTTP client not started"
    tried: set[str] = set()
    last_failure: Failure | None = None
    for attempt in range(state.config.max_retries):
        candidate = candidates[attempt % len(candidates)]
        pool = state.pools[candidate.pool]
        key = pool.select(exclude=tried) or pool.select()
        if key is None:
            continue
        tried.add(key.id)

        payload = copy.deepcopy(body)
        payload["model"] = candidate.model_override or model
        pool_cfg = state.config.pools[pool.name]
        result = await call_responses(
            state.client, pool_cfg.base_url, key.value, payload,
            pool_cfg.timeout or state.config.request_timeout,
            bool(body.get("stream", False)),
        )
        if isinstance(result, (SuccessJSON, SuccessStream)):
            pool.mark_success(key)
            log.info(
                "OK  format=responses model=%s pool=%s key=%s %.2fs",
                model, pool.name, key.id, time.monotonic() - started,
            )
            return _success_response(result, model)
        last_failure = result
        pool.mark_failure(key, result.status, result.headers, result.text)

    return _error(503, "All candidate accounts are unavailable or rate-limited", {
        "attempts": len(tried),
        "last_upstream_status": last_failure.status if last_failure else None,
    })


async def _handle_completion(request: Request, api_format: str) -> Response:
    state: GatewayState = request.app.state.gateway
    started = time.monotonic()
    is_anthropic_client = api_format == "anthropic"

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "Request body must be valid JSON", anthropic=is_anthropic_client)

    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object", anthropic=is_anthropic_client)

    model = body.get("model")
    if not isinstance(model, str) or not model:
        return _error(400, "Missing required field: model", anthropic=is_anthropic_client)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error(
            400, "Missing required field: messages (non-empty list)",
            anthropic=is_anthropic_client,
        )

    stream = bool(body.get("stream", False))
    candidates = state.resolve(model)
    if not candidates:
        return _error(
            404,
            f"No pool configured for model {model!r}",
            {"available_models": state.list_models()},
            anthropic=is_anthropic_client,
        )

    assert state.client is not None, "HTTP client not started"
    max_retries = state.config.max_retries
    last_failure: Failure | None = None
    tried: set[str] = set()

    for attempt in range(max_retries):
        candidate = candidates[attempt % len(candidates)]
        pool = state.pools.get(candidate.pool)
        if pool is None:
            continue

        key: KeyState | None = pool.select(exclude=tried)
        if key is None:
            # This pool has no free keys (all cooling down / disabled). Try other pools.
            key = pool.select()
            if key is None:
                log.debug("Pool %s has no available keys", pool.name)
                continue
        tried.add(key.id)

        upstream_model = candidate.model_override or model
        payload = copy.deepcopy(body)
        payload["model"] = upstream_model

        pool_cfg = state.config.pools[pool.name]
        provider = pool_cfg.provider

        # Translate the incoming wire format into whatever the upstream speaks.
        if not is_anthropic_client:
            if provider == "anthropic":
                payload = openai_to_anthropic(payload, upstream_model)
            # OpenAI upstream: keep payload as-is.
        else:
            if provider == "openai":
                payload = anthropic_to_openai_request(payload, upstream_model)
            # Anthropic upstream: keep native Anthropic format.

        timeout = pool_cfg.timeout or state.config.request_timeout

        # Claude Code's gateway contract: forward anthropic-* headers verbatim.
        # New capabilities arrive as new header values, so never allowlist them.
        forward_headers: dict[str, str] = {}
        if is_anthropic_client:
            forward_headers = {
                name.lower(): value
                for name, value in request.headers.items()
                if name.lower().startswith("anthropic-")
            }

        result = await call_upstream(
            state.client, provider, pool_cfg.base_url, key.value,
            payload, timeout, stream,
            response_format="anthropic" if is_anthropic_client else "openai",
            forward_headers=forward_headers,
        )

        if isinstance(result, (SuccessJSON, SuccessStream)):
            pool.mark_success(key)
            elapsed = time.monotonic() - started
            log.info(
                "OK  format=%s model=%s pool=%s key=%s stream=%s %.2fs",
                api_format, model, pool.name, key.id, stream, elapsed,
            )
            # Response is already in the client's wire format (handled in providers.py).
            return _success_response(result, model)

        last_failure = result
        pool.mark_failure(key, result.status, result.headers, result.text)

    # Forward validation errors verbatim. Coding agents (notably Claude Code)
    # match the upstream's error wording to drop unsupported beta fields and
    # retry automatically; wrapping it in our own envelope breaks that recovery.
    if last_failure and last_failure.status in (400, 413, 422) and last_failure.text:
        try:
            raw_error = json.loads(last_failure.text)
        except (TypeError, ValueError):
            raw_error = None
        err_headers = _clean_headers(last_failure.headers)
        if is_anthropic_client:
            if isinstance(raw_error, dict) and raw_error.get("type") == "error":
                return JSONResponse(raw_error, status_code=last_failure.status, headers=err_headers)
            return JSONResponse(
                {"type": "error", "error": {
                    "type": "upstream_error", "message": last_failure.text[:1000]
                }},
                status_code=last_failure.status, headers=err_headers,
            )
        if isinstance(raw_error, dict) and "error" in raw_error:
            return JSONResponse(raw_error, status_code=last_failure.status, headers=err_headers)

    elapsed = time.monotonic() - started
    log.error("FAILED format=%s model=%s after %d attempts %.2fs",
              api_format, model, len(tried), elapsed)
    detail = {
        "attempts": len(tried),
        "last_upstream_status": last_failure.status if last_failure else None,
        "last_upstream_error": last_failure.text[:500] if last_failure else None,
    }
    return _error(
        503, "All candidate accounts are unavailable or rate-limited",
        detail, anthropic=is_anthropic_client,
    )


def _success_response(result: SuccessJSON | SuccessStream, model: str) -> Response:
    headers = _clean_headers(result.headers)
    headers["x-gateway-model"] = model
    if isinstance(result, SuccessStream):
        return StreamingResponse(
            result.stream,
            status_code=result.status,
            media_type=result.media_type,
            headers=headers,
        )
    return JSONResponse(result.data, status_code=result.status, headers=headers)


# ----- admin endpoints ----------------------------------------------------- #
async def admin_stats(request: Request) -> JSONResponse:
    state: GatewayState = request.app.state.gateway
    pools = [pool.stats() for pool in state.pools.values()]
    return JSONResponse({
        "pools": pools,
        "totals": {
            "pools": len(pools),
            "keys": sum(p["total_keys"] for p in pools),
            "available": sum(p["available_keys"] for p in pools),
            "cooling_down": sum(p["cooling_down"] for p in pools),
            "disabled": sum(p["disabled_keys"] for p in pools),
            "requests": sum(p["total_requests"] for p in pools),
            "successes": sum(p["total_successes"] for p in pools),
            "failures": sum(p["total_failures"] for p in pools),
        },
    })


async def admin_keys(request: Request) -> JSONResponse:
    state: GatewayState = request.app.state.gateway
    return JSONResponse({
        name: [k.to_dict() for k in pool.keys] for name, pool in state.pools.items()
    })


async def admin_key_action(request: Request) -> JSONResponse:
    state: GatewayState = request.app.state.gateway
    pool = state.pools.get(request.path_params["pool"])
    if pool is None:
        return _error(404, f"Unknown pool: {request.path_params['pool']}")
    key_id = request.path_params["key_id"]
    action = request.path_params["action"]

    if action in ("enable", "disable"):
        ok = pool.set_disabled(key_id, disabled=(action == "disable"))
        if not ok:
            return _error(404, f"Unknown key: {key_id}")
        return JSONResponse({"ok": True, "key": key_id, "action": action})

    if action == "cooldown":
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return _error(400, "Expected JSON body")
        seconds = float(data.get("seconds", 60))
        reason = str(data.get("reason", "manual"))
        if not pool.set_cooldown(key_id, seconds, reason):
            return _error(404, f"Unknown key: {key_id}")
        return JSONResponse({"ok": True, "key": key_id, "cooldown_seconds": seconds})

    return _error(404, f"Unknown action: {action}")


async def admin_reload(request: Request) -> JSONResponse:
    state: GatewayState = request.app.state.gateway
    try:
        count = state.reload()
    except Exception as exc:  # noqa: BLE001
        return _error(500, f"Reload failed: {exc}")
    return JSONResponse({"ok": True, "pools": count})


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(config: GatewayConfig) -> Starlette:
    state = GatewayState(config)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        state.start()
        try:
            yield
        finally:
            await state.stop()

    routes = [
        Route("/", ui_shell, methods=["GET"]),
        Route("/ui", ui_shell, methods=["GET"]),
        *ui_api.routes(),
        Route("/health", health, methods=["GET"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/messages", anthropic_messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
        Route("/v1/responses", responses_api, methods=["POST"]),
        Route("/admin/stats", admin_stats, methods=["GET"]),
        Route("/admin/keys", admin_keys, methods=["GET"]),
        Route(
            "/admin/keys/{pool}/{key_id}/{action}",
            admin_key_action,
            methods=["POST"],
        ),
        Route("/admin/reload", admin_reload, methods=["POST"]),
    ]

    app = Starlette(
        debug=False,
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(AuthMiddleware, state=state)],
    )
    app.state.gateway = state
    return app


def create_app_from_file(path: str) -> Starlette:
    return create_app(load_config(path))
