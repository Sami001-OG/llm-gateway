"""JSON API backing the web dashboard (all routes require an admin token)."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import manage

log = logging.getLogger("gateway.ui_api")


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise ValueError("Request body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def _ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data}, status_code=status)


def endpoint(fn: Callable) -> Callable:
    """Wrap a mutating handler with consistent error handling."""
    async def wrapped(request: Request) -> Response:
        state = request.app.state.gateway
        try:
            data = await _body(request)
            result = fn(state, data, request)
            if hasattr(result, "__await__"):
                result = await result
            return _ok(result)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("UI action failed")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return wrapped


async def state_view(request: Request) -> Response:
    state = request.app.state.gateway
    return _ok(manage.ui_state(state))


# ----- accounts ------------------------------------------------------------ #
@endpoint
def add_keys(state: Any, data: dict, request: Request) -> Any:
    keys = data.get("keys", data.get("key"))
    if isinstance(keys, str):
        keys = [line.strip() for line in keys.replace(",", "\n").splitlines()]
    return {"added": manage.add_keys(state, data["pool"], keys or [])}


@endpoint
def remove_key(state: Any, data: dict, request: Request) -> Any:
    manage.remove_key(state, data["pool"], data["key_id"])
    return {"removed": data["key_id"]}


@endpoint
def reorder_keys(state: Any, data: dict, request: Request) -> Any:
    manage.reorder_keys(state, data["pool"], data["order"])
    return {"order": data["order"]}


@endpoint
def toggle_key(state: Any, data: dict, request: Request) -> Any:
    manage.set_key_disabled(state, data["pool"], data["key_id"], bool(data["disabled"]))
    return {"key_id": data["key_id"], "disabled": bool(data["disabled"])}


# ----- pools / routes ------------------------------------------------------ #
@endpoint
def upsert_pool(state: Any, data: dict, request: Request) -> Any:
    manage.upsert_pool(state, data)
    return {"name": data["name"]}


@endpoint
def delete_pool(state: Any, data: dict, request: Request) -> Any:
    manage.delete_pool(state, data["pool"])
    return {"deleted": data["pool"]}


@endpoint
def upsert_route(state: Any, data: dict, request: Request) -> Any:
    manage.upsert_route(state, data["alias"], data["pools"], data.get("model"))
    return {"alias": data["alias"]}


@endpoint
def delete_route(state: Any, data: dict, request: Request) -> Any:
    manage.delete_route(state, data["alias"])
    return {"deleted": data["alias"]}


# ----- tokens -------------------------------------------------------------- #
@endpoint
def add_token(state: Any, data: dict, request: Request) -> Any:
    token = manage.add_token(state, data["kind"], data.get("token"))
    return {"kind": data["kind"], "token": token, "id": manage._token_id(token)}


@endpoint
def delete_token(state: Any, data: dict, request: Request) -> Any:
    manage.delete_token(state, data["kind"], data["token"])
    return {"deleted": True}


# ----- settings ------------------------------------------------------------ #
@endpoint
def update_settings(state: Any, data: dict, request: Request) -> Any:
    manage.update_settings(state, data)
    return {"settings": "saved"}


async def reload_config(request: Request) -> Response:
    state = request.app.state.gateway
    try:
        count = state.reload()
        return _ok({"pools": count})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/admin/ui/state", state_view, methods=["GET"]),
        Route("/admin/ui/keys/add", add_keys, methods=["POST"]),
        Route("/admin/ui/keys/remove", remove_key, methods=["POST"]),
        Route("/admin/ui/keys/reorder", reorder_keys, methods=["POST"]),
        Route("/admin/ui/keys/toggle", toggle_key, methods=["POST"]),
        Route("/admin/ui/pools", upsert_pool, methods=["POST"]),
        Route("/admin/ui/pools/delete", delete_pool, methods=["POST"]),
        Route("/admin/ui/routes", upsert_route, methods=["POST"]),
        Route("/admin/ui/routes/delete", delete_route, methods=["POST"]),
        Route("/admin/ui/tokens", add_token, methods=["POST"]),
        Route("/admin/ui/tokens/delete", delete_token, methods=["POST"]),
        Route("/admin/ui/settings", update_settings, methods=["POST"]),
        Route("/admin/ui/reload", reload_config, methods=["POST"]),
    ]
