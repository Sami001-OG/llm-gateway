"""Self-contained integration tests using a local mock upstream server.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest

import httpx
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from gateway.app import create_app
from gateway.config import CooldownConfig, GatewayConfig, PoolConfig, RouteConfig, load_config

MOCK: dict = {
    "openai_calls": [],
    "anthropic_calls": [],
    "fail_keys": set(),
    "anthropic_bodies": [],
}

OPENAI_SSE = (
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":['
    '{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":['
    '{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    'data: [DONE]\n\n'
)

ANTHROPIC_SSE = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"id":"msg_s",'
    '"model":"claude-3-5-sonnet-20241022","usage":{"input_tokens":7}}}\n\n'
    'event: content_block_start\n'
    'data: {"type":"content_block_start","content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","delta":{"type":"text_delta",'
    '"text":"Hi from Claude"}}\n\n'
    'event: content_block_stop\n'
    'data: {"type":"content_block_stop"}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":4}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


# --------------------------------------------------------------------------- #
# Mock upstream
# --------------------------------------------------------------------------- #
async def mock_openai(request: Request) -> StreamingResponse | JSONResponse:
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else auth
    MOCK["openai_calls"].append(key)
    if key in MOCK["fail_keys"]:
        return JSONResponse(
            {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    body = await request.json()
    if body.get("stream"):
        return StreamingResponse(
            (chunk.encode() for chunk in [OPENAI_SSE]), media_type="text/event-stream"
        )
    return JSONResponse({
        "id": "chatcmpl-ok",
        "object": "chat.completion",
        "model": body.get("model", "gpt-4o"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello from GPT"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    })


async def mock_anthropic(request: Request) -> StreamingResponse | JSONResponse:
    key = request.headers.get("x-api-key", "")
    MOCK["anthropic_calls"].append(key)
    body = await request.json()
    MOCK["anthropic_bodies"].append(body)
    if key in MOCK["fail_keys"]:
        return JSONResponse(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "Slow down"}},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    if body.get("stream"):
        return StreamingResponse(
            (chunk.encode() for chunk in [ANTHROPIC_SSE]), media_type="text/event-stream"
        )
    return JSONResponse({
        "id": "msg_ok",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "claude-3-5-sonnet-20241022"),
        "content": [{"type": "text", "text": "Hello from Claude"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })


async def mock_openai_responses(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    key = auth[7:] if auth.lower().startswith("bearer ") else auth
    MOCK["openai_calls"].append(key)
    if key in MOCK["fail_keys"]:
        return JSONResponse({"error": {"message": "Rate limit", "code": "rate_limit_error"}},
                            status_code=429, headers={"Retry-After": "1"})
    body = await request.json()
    return JSONResponse({
        "id": "resp_ok",
        "object": "response",
        "status": "completed",
        "model": body.get("model", ""),
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello from Responses"}]}],
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    })


_mock_app = Starlette(routes=[
    Route("/openai/v1/chat/completions", mock_openai, methods=["POST"]),
    Route("/openai/v1/responses", mock_openai_responses, methods=["POST"]),
    Route("/anthropic/v1/messages", mock_anthropic, methods=["POST"]),
])


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_mock_server() -> tuple[int, threading.Thread]:
    port = _free_port()
    config = uvicorn.Config(_mock_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return port, thread
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Mock upstream failed to start")


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #
def build_config(base_url: str, n_keys: int = 3) -> GatewayConfig:
    cd = CooldownConfig(rate_limit=1, quota=2, auth_error=2, server_error=1, max_backoff=5)
    return GatewayConfig(
        auth_tokens=["client-token"],
        admin_tokens=["admin-token"],
        log_level="ERROR",
        max_retries=4,
        selection="least_used",
        cooldown=cd,
        pools={
            "openai_main": PoolConfig(
                name="openai_main",
                provider="openai",
                base_url=f"{base_url}/openai/v1",
                models=["gpt-4o"],
                keys=[f"sk-oai-{i}" for i in range(n_keys)],
                cooldown=cd,
            ),
            "anthropic_main": PoolConfig(
                name="anthropic_main",
                provider="anthropic",
                base_url=f"{base_url}/anthropic/v1",
                models=["claude-3-5-sonnet-20241022"],
                keys=["sk-ant-0"],
                cooldown=cd,
            ),
        },
        routes={
            "gpt-4o": RouteConfig(pools=["openai_main"]),
            "claude": RouteConfig(
                pools=["anthropic_main"], model_override="claude-3-5-sonnet-20241022"
            ),
            "auto": RouteConfig(pools=["openai_main", "anthropic_main"]),
        },
    )


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port, cls.thread = start_mock_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    async def asyncSetUp(self) -> None:
        MOCK["openai_calls"].clear()
        MOCK["anthropic_calls"].clear()
        MOCK["anthropic_bodies"].clear()
        MOCK["fail_keys"].clear()
        cfg = build_config(self.base)
        tmpdir = "/data/data/com.termux/files/usr/tmp/opencode"
        os.makedirs(tmpdir, exist_ok=True)
        cfg.config_path = os.path.join(tmpdir, f"ui_state_{id(self)}.yaml")
        raw = {
            "server": {
                "host": "0.0.0.0", "port": 8000,
                "auth_tokens": ["client-token"], "admin_tokens": ["admin-token"],
                "log_level": "ERROR", "request_timeout": 120,
            },
            "defaults": {
                "max_retries": 4, "selection": "least_used",
                "cooldown": {"rate_limit": 1, "quota": 2, "auth_error": 2,
                             "server_error": 1, "max_backoff": 5},
            },
            "pools": {
                "openai_main": {
                    "provider": "openai", "base_url": f"{self.base}/openai/v1",
                    "models": ["gpt-4o"],
                    "keys": [f"sk-oai-{i}" for i in range(3)],
                },
                "anthropic_main": {
                    "provider": "anthropic", "base_url": f"{self.base}/anthropic/v1",
                    "models": ["claude-3-5-sonnet-20241022"],
                    "keys": ["sk-ant-0"],
                },
            },
            "routes": {
                "gpt-4o": {"pool": "openai_main"},
                "claude": {"pool": "anthropic_main",
                           "model": "claude-3-5-sonnet-20241022"},
                "auto": {"failover": ["openai_main", "anthropic_main"]},
            },
        }
        with open(cfg.config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False)
        self.app = create_app(cfg)
        self.app.state.gateway.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://gateway"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.gateway.stop()

    def post(self, model: str = "gpt-4o", **extra) -> httpx.AsyncClient:
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        payload.update(extra)
        return self.client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer client-token"},
        )

    # ----- tests ------------------------------------------------------- #
    async def test_auth_required(self) -> None:
        resp = await self.client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status_code, 401)
        resp2 = await self.post()
        self.assertEqual(resp2.status_code, 200)

    async def test_openai_non_streaming(self) -> None:
        resp = await self.post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello from GPT")
        self.assertEqual(data["object"], "chat.completion")

    async def test_rotation_avoids_failing_key(self) -> None:
        MOCK["fail_keys"].add("sk-oai-0")
        for _ in range(4):
            resp = await self.post()
            self.assertEqual(resp.status_code, 200)
        calls = MOCK["openai_calls"]
        self.assertEqual(calls.count("sk-oai-0"), 1)  # tried once, then cooled down
        self.assertGreaterEqual(len(calls), 4)

    async def test_least_used_distribution(self) -> None:
        for _ in range(12):
            resp = await self.post()
            self.assertEqual(resp.status_code, 200)
        calls = MOCK["openai_calls"]
        for i in range(3):
            self.assertEqual(calls.count(f"sk-oai-{i}"), 4)

    async def test_anthropic_translation(self) -> None:
        resp = await self.post(
            "claude",
            messages=[
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Hello"},
            ],
            temperature=0.5,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello from Claude")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        upstream = MOCK["anthropic_bodies"][0]
        self.assertEqual(upstream["system"], "Be brief.")
        self.assertEqual(upstream["messages"][0], {"role": "user", "content": [
            {"type": "text", "text": "Hello"}
        ]})
        # temperature/top_p are removed in Anthropic API 2026-10-21 and rejected
        # on current Claude models, so the gateway must drop them.
        self.assertNotIn("temperature", upstream)
        self.assertEqual(upstream["max_tokens"], 4096)

    async def test_openai_streaming(self) -> None:
        resp = await self.post(stream=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue("text/event-stream" in resp.headers["content-type"])
        self.assertIn("[DONE]", resp.text)
        self.assertIn("Hi", resp.text)

    async def test_anthropic_streaming_translation(self) -> None:
        resp = await self.post("claude", stream=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Hi from Claude", resp.text)
        self.assertIn("[DONE]", resp.text)
        self.assertIn("chat.completion.chunk", resp.text)

    def post_anthropic(self, model: str = "claude", **extra):
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": "Be helpful.",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        payload.update(extra)
        return self.client.post(
            "/v1/messages",
            json=payload,
            headers={"x-api-key": "client-token", "anthropic-version": "2023-06-01"},
        )

    async def test_anthropic_endpoint_native_pool(self) -> None:
        resp = await self.post_anthropic("claude")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["content"][0]["text"], "Hello from Claude")
        self.assertEqual(data["stop_reason"], "end_turn")
        self.assertEqual(data["role"], "assistant")

    async def test_anthropic_endpoint_routed_to_openai_pool(self) -> None:
        resp = await self.post_anthropic("gpt-4o")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["content"][0]["text"], "Hello from GPT")
        self.assertEqual(data["stop_reason"], "end_turn")
        self.assertTrue(any(k.startswith("sk-oai-") for k in MOCK["openai_calls"]))

    async def test_anthropic_endpoint_streaming_to_openai_pool(self) -> None:
        resp = await self.post_anthropic("gpt-4o", stream=True)
        self.assertEqual(resp.status_code, 200)
        text = resp.text
        self.assertIn("event: message_start", text)
        self.assertIn("event: content_block_delta", text)
        self.assertIn("event: message_stop", text)
        self.assertIn("Hi", text)

    async def test_anthropic_endpoint_streaming_native_pool(self) -> None:
        resp = await self.post_anthropic("claude", stream=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("event: message_start", resp.text)
        self.assertIn("Hi from Claude", resp.text)

    async def test_responses_api_for_codex(self) -> None:
        before = len(MOCK["openai_calls"])
        resp = await self.client.post(
            "/v1/responses",
            json={"model": "gpt-4o", "input": "Hello"},
            headers={"Authorization": "Bearer client-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["object"], "response")
        self.assertEqual(len(MOCK["openai_calls"]), before + 1)

    async def test_admin_stats_masks_keys(self) -> None:
        await self.post()
        resp = await self.client.get(
            "/admin/stats", headers={"Authorization": "Bearer admin-token"}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["totals"]["keys"], 4)
        self.assertEqual(data["totals"]["requests"], 1)
        blob = json.dumps(data)
        self.assertNotIn("sk-oai-0", blob)
        self.assertNotIn("sk-ant-0", blob)

    async def test_admin_cooldown_and_enable(self) -> None:
        admin = {"Authorization": "Bearer admin-token"}
        resp = await self.client.post(
            "/admin/keys/openai_main/openai_main%230/cooldown",
            json={"seconds": 60, "reason": "test"},
            headers=admin,
        )
        self.assertEqual(resp.status_code, 200)
        stats = (await self.client.get("/admin/stats", headers=admin)).json()
        key0 = next(
            k for p in stats["pools"] if p["name"] == "openai_main"
            for k in p["keys"] if k["id"] == "openai_main#0"
        )
        self.assertFalse(key0["available"])

        resp = await self.client.post(
            "/admin/keys/openai_main/openai_main%230/enable", headers=admin
        )
        self.assertEqual(resp.status_code, 200)
        stats = (await self.client.get("/admin/stats", headers=admin)).json()
        key0 = next(
            k for p in stats["pools"] if p["name"] == "openai_main"
            for k in p["keys"] if k["id"] == "openai_main#0"
        )
        self.assertTrue(key0["available"])

    async def test_all_keys_cooling_returns_503(self) -> None:
        admin = {"Authorization": "Bearer admin-token"}
        for i in range(3):
            await self.client.post(
                f"/admin/keys/openai_main/openai_main%23{i}/cooldown",
                json={"seconds": 60, "reason": "test"},
                headers=admin,
            )
        resp = await self.post()
        self.assertEqual(resp.status_code, 503)

    async def test_models_endpoint(self) -> None:
        resp = await self.client.get(
            "/v1/models", headers={"Authorization": "Bearer client-token"}
        )
        self.assertEqual(resp.status_code, 200)
        ids = [m["id"] for m in resp.json()["data"]]
        self.assertIn("gpt-4o", ids)
        self.assertIn("auto", ids)


class UITests(unittest.IsolatedAsyncioTestCase):
    """Web dashboard shell + its admin JSON API."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port, cls.thread = start_mock_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    async def asyncSetUp(self) -> None:
        cfg = build_config(self.base)
        cfg.pools = {k: v for k, v in cfg.pools.items() if k == "openai_main"}
        cfg.routes = {"gpt-4o": cfg.routes["gpt-4o"]}
        tmpdir = "/data/data/com.termux/files/usr/tmp/opencode"
        os.makedirs(tmpdir, exist_ok=True)
        cfg.config_path = os.path.join(tmpdir, f"ui_test_{id(self)}.yaml")
        raw = {
            "server": {"auth_tokens": ["client-token"], "admin_tokens": ["admin-token"]},
            "pools": {
                "openai_main": {
                    "provider": "openai", "base_url": f"{self.base}/openai/v1",
                    "models": ["gpt-4o"],
                    "keys": [f"sk-oai-{i}" for i in range(3)],
                },
            },
            "routes": {"gpt-4o": {"pool": "openai_main"}},
        }
        with open(cfg.config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False)
        self.app = create_app(cfg)
        self.app.state.gateway.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://gateway"
        )
        self.admin = {"Authorization": "Bearer admin-token"}

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.gateway.stop()

    async def test_dashboard_shell_public(self) -> None:
        r = await self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Gateway Control Center", r.text)
        self.assertIn("Endpoints", r.text)

    async def test_ui_state_requires_admin(self) -> None:
        self.assertEqual((await self.client.get("/admin/ui/state")).status_code, 401)
        r = await self.client.get("/admin/ui/state", headers=self.admin)
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(len(data["pools"]), 1)
        self.assertEqual(len(data["pools"][0]["keys"]), 3)
        self.assertNotIn("sk-oai-0", json.dumps(data))

    async def test_add_reorder_remove_key(self) -> None:
        r = await self.client.post(
            "/admin/ui/keys/add", json={"pool": "openai_main", "keys": "sk-new-1\nsk-new-2"},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"]["added"], 2)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        ids = [k["id"] for k in state["pools"][0]["keys"]]
        self.assertEqual(len(ids), 5)
        order = list(reversed(ids))
        r = await self.client.post(
            "/admin/ui/keys/reorder", json={"pool": "openai_main", "order": order},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        self.assertEqual(len(state["pools"][0]["keys"]), 5)
        # IDs are positional, so verify the persisted literal order instead.
        with open(self.app.state.gateway.config.config_path, encoding="utf-8") as fh:
            saved = yaml.safe_load(fh)
        self.assertEqual(
            saved["pools"]["openai_main"]["keys"],
            ["sk-new-2", "sk-new-1", "sk-oai-2", "sk-oai-1", "sk-oai-0"],
        )

        r = await self.client.post(
            "/admin/ui/keys/remove",
            json={"pool": "openai_main", "key_id": order[0]}, headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)

    async def test_toggle_key_persists(self) -> None:
        r = await self.client.post(
            "/admin/ui/keys/toggle",
            json={"pool": "openai_main", "key_id": "openai_main#0", "disabled": True},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        self.assertTrue(state["pools"][0]["keys"][0]["disabled"])
        with open(self.app.state.gateway.config.config_path, encoding="utf-8") as fh:
            saved = yaml.safe_load(fh)
        self.assertIn("sk-oai-0", saved["pools"]["openai_main"]["disabled_keys"])

    async def test_route_crud(self) -> None:
        r = await self.client.post(
            "/admin/ui/routes",
            json={"alias": "auto", "pools": ["openai_main"], "model": "gpt-4o"},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        self.assertEqual(state["routes"]["auto"]["pools"], ["openai_main"])
        r = await self.client.post(
            "/admin/ui/routes/delete", json={"alias": "auto"}, headers=self.admin
        )
        self.assertEqual(r.status_code, 200)

    async def test_token_create_and_revoke(self) -> None:
        r = await self.client.post(
            "/admin/ui/tokens", json={"kind": "client", "token": None}, headers=self.admin
        )
        self.assertEqual(r.status_code, 200)
        token = r.json()["data"]["token"]
        tid = r.json()["data"]["id"]
        self.assertTrue(token.startswith("sk-gw-"))
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        self.assertEqual(len(state["tokens"]["client"]), 2)
        r = await self.client.post(
            "/admin/ui/tokens/delete", json={"kind": "client", "token": tid},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        self.assertEqual(len(state["tokens"]["client"]), 1)

    async def test_provider_presets(self) -> None:
        r = await self.client.get("/admin/ui/presets", headers=self.admin)
        self.assertEqual(r.status_code, 200)
        presets = r.json()["data"]
        self.assertGreaterEqual(len(presets), 6)
        ids = {p["id"] for p in presets}
        self.assertTrue({"openai", "anthropic", "groq", "gemini", "ollama"}.issubset(ids))
        for p in presets:
            self.assertTrue(p["base_url"].startswith("http"))

    async def test_quick_add_pool_with_keys(self) -> None:
        r = await self.client.post(
            "/admin/ui/pools",
            json={
                "name": "groq_free", "provider": "openai",
                "base_url": "https://api.groq.com/openai/v1",
                "models": ["llama-3.3-70b-versatile"],
                "keys": "gsk_test_a\ngsk_test_b",
            },
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        names = [p["name"] for p in state["pools"]]
        self.assertIn("groq_free", names)
        groq = next(p for p in state["pools"] if p["name"] == "groq_free")
        self.assertEqual(len(groq["keys"]), 2)

    async def test_settings_update(self) -> None:
        r = await self.client.post(
            "/admin/ui/settings",
            json={"max_retries": 7, "selection": "round_robin",
                  "cooldown": {"rate_limit": 99}},
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200)
        state = (await self.client.get("/admin/ui/state", headers=self.admin)).json()["data"]
        self.assertEqual(state["server"]["max_retries"], 7)
        self.assertEqual(state["server"]["selection"], "round_robin")
        self.assertEqual(state["server"]["cooldown"]["rate_limit"], 99)


class ConfigTests(unittest.TestCase):
    def test_yaml_env_and_file_expansion(self) -> None:
        tmpdir = "/data/data/com.termux/files/usr/tmp/opencode"
        os.makedirs(tmpdir, exist_ok=True)
        keyfile = os.path.join(tmpdir, "test_keys.txt")
        with open(keyfile, "w", encoding="utf-8") as fh:
            fh.write("# comment\nsk-from-file\n\n")
        os.environ["TEST_GW_KEY"] = "sk-from-env"
        cfgpath = os.path.join(tmpdir, "test_config.yaml")
        with open(cfgpath, "w", encoding="utf-8") as fh:
            fh.write(
                "server:\n"
                "  auth_tokens: [tok]\n"
                "pools:\n"
                "  p1:\n"
                "    provider: openai\n"
                "    base_url: https://api.openai.com/v1\n"
                "    models: [gpt-4o]\n"
                "    keys:\n"
                "      - ${TEST_GW_KEY}\n"
                f"      - file:{keyfile}\n"
            )
        cfg = load_config(cfgpath)
        self.assertEqual(cfg.pools["p1"].keys, ["sk-from-env", "sk-from-file"])
        self.assertEqual(cfg.port, 8000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
