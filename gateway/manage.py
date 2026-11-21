"""Admin operations used by the web UI.

Every mutation edits the raw YAML mapping, atomically writes it to disk, then
rebuilds the runtime pools while carrying over per-key statistics.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from typing import Any

import yaml

from .config import GatewayConfig, parse_config
from .keypool import KeyPool, mask_key

log = logging.getLogger("gateway.manage")


# --------------------------------------------------------------------------- #
# Core apply
# --------------------------------------------------------------------------- #
def persist(state: Any) -> None:
    path = state.config.config_path
    if not path:
        raise RuntimeError("No config path configured")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(state.raw_config, fh, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)


def apply(state: Any) -> GatewayConfig:
    """Persist raw config and rebuild runtime state (keeping key stats)."""
    persist(state)
    previous: dict[tuple[str, str], Any] = {
        (name, key.value): key
        for name, pool in state.pools.items()
        for key in pool.keys
    }
    new_config = parse_config(state.raw_config, state.config.config_path)
    new_pools: dict[str, KeyPool] = {}
    for name, pool_cfg in new_config.pools.items():
        pool = KeyPool(pool_cfg)
        for key in pool.keys:
            old = previous.get((name, key.value))
            if old is not None:
                key.total_requests = old.total_requests
                key.total_successes = old.total_successes
                key.total_failures = old.total_failures
                key.consecutive_failures = old.consecutive_failures
                key.last_used = old.last_used
                key.last_error = old.last_error
                key.cooldown_until = old.cooldown_until
                key.cooldown_reason = old.cooldown_reason
                if not key.disabled:
                    key.disabled = old.disabled
        new_pools[name] = pool

    state.config = new_config
    state.pools = new_pools
    log.info("Config applied: %d pools, %d routes", len(new_config.pools), len(new_config.routes))
    return new_config


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #
def ui_state(state: Any) -> dict[str, Any]:
    cfg = state.config
    stats_pools = [pool.stats() for pool in state.pools.values()]
    pools = []
    for name, pool in state.pools.items():
        pcfg = cfg.pools[name]
        pools.append({
            "name": name,
            "provider": pcfg.provider,
            "base_url": pcfg.base_url,
            "models": pcfg.models,
            "selection": pcfg.selection,
            "keys": [{
                "id": key.id,
                "masked": mask_key(key.value),
                "disabled": key.disabled,
                "available": key.available(),
                "cooldown_remaining": round(max(0.0, key.cooldown_until - __import__("time").time())),
                "cooldown_reason": key.cooldown_reason,
                "total_requests": key.total_requests,
                "total_successes": key.total_successes,
                "total_failures": key.total_failures,
            } for key in pool.keys],
        })
    routes = {
        alias: {"pools": route.pools, "model": route.model_override}
        for alias, route in cfg.routes.items()
    }
    cd = cfg.cooldown
    return {
        "server": {
            "host": cfg.host,
            "port": cfg.port,
            "log_level": cfg.log_level,
            "request_timeout": cfg.request_timeout,
            "max_retries": cfg.max_retries,
            "selection": cfg.selection,
            "cooldown": {
                "rate_limit": cd.rate_limit,
                "quota": cd.quota,
                "auth_error": cd.auth_error,
                "server_error": cd.server_error,
                "max_backoff": cd.max_backoff,
            },
            "config_path": cfg.config_path,
        },
        "tokens": {
            "client": [_token_view(t) for t in cfg.auth_tokens],
            "admin": [_token_view(t) for t in cfg.admin_tokens],
        },
        "pools": pools,
        "routes": routes,
        "models": sorted({
            m for p in cfg.pools.values() for m in p.models
        } | set(cfg.routes.keys())),
        "stats": {
            "keys": sum(p["total_keys"] for p in stats_pools),
            "available": sum(p["available_keys"] for p in stats_pools),
            "cooling_down": sum(p["cooling_down"] for p in stats_pools),
            "disabled": sum(p["disabled_keys"] for p in stats_pools),
            "requests": sum(p["total_requests"] for p in stats_pools),
            "successes": sum(p["total_successes"] for p in stats_pools),
            "failures": sum(p["total_failures"] for p in stats_pools),
        },
    }


# --------------------------------------------------------------------------- #
# Mutators
# --------------------------------------------------------------------------- #
def _pool_raw(state: Any, name: str) -> dict[str, Any]:
    pools = state.raw_config.setdefault("pools", {})
    if name not in pools:
        raise KeyError(f"Unknown pool: {name}")
    return pools[name]


def _find_key(state: Any, pool_name: str, key_id: str) -> Any:
    pool = state.pools.get(pool_name)
    if pool is None:
        raise KeyError(f"Unknown pool: {pool_name}")
    key = pool.get(key_id)
    if key is None:
        raise KeyError(f"Unknown key: {key_id}")
    return key


def add_keys(state: Any, pool_name: str, keys: list[str]) -> int:
    cleaned = [str(k).strip() for k in keys if str(k).strip()]
    if not cleaned:
        raise ValueError("No keys supplied")
    pool_raw = _pool_raw(state, pool_name)
    current = pool_raw.setdefault("keys", [])
    current.extend(cleaned)
    apply(state)
    return len(cleaned)


def remove_key(state: Any, pool_name: str, key_id: str) -> None:
    key = _find_key(state, pool_name, key_id)
    pool_raw = _pool_raw(state, pool_name)
    keys = pool_raw.get("keys", [])
    if key.value not in keys:
        raise ValueError("Key is not stored as a literal in config (env/file reference)")
    keys.remove(key.value)
    disabled = [v for v in pool_raw.get("disabled_keys", []) if v != key.value]
    if disabled:
        pool_raw["disabled_keys"] = disabled
    else:
        pool_raw.pop("disabled_keys", None)
    apply(state)


def reorder_keys(state: Any, pool_name: str, order: list[str]) -> None:
    pool = state.pools.get(pool_name)
    if pool is None:
        raise KeyError(f"Unknown pool: {pool_name}")
    by_id = {k.id: k.value for k in pool.keys}
    if set(order) != set(by_id) or len(order) != len(by_id):
        raise ValueError("order must contain every key id exactly once")
    _pool_raw(state, pool_name)["keys"] = [by_id[kid] for kid in order]
    apply(state)


def set_key_disabled(state: Any, pool_name: str, key_id: str, disabled: bool) -> None:
    key = _find_key(state, pool_name, key_id)
    pool_raw = _pool_raw(state, pool_name)
    values = [v for v in pool_raw.get("disabled_keys", []) if v != key.value]
    if disabled:
        values.append(key.value)
    if values:
        pool_raw["disabled_keys"] = values
    else:
        pool_raw.pop("disabled_keys", None)
    apply(state)


def upsert_pool(state: Any, data: dict[str, Any]) -> None:
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("Pool name is required")
    provider = str(data.get("provider", "")).lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError("provider must be openai or anthropic")
    base_url = str(data.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url is required")
    models = [str(m).strip() for m in data.get("models", []) if str(m).strip()]
    pools = state.raw_config.setdefault("pools", {})
    entry = pools.setdefault(name, {})
    entry.update({
        "provider": provider,
        "base_url": base_url,
        "models": models,
        "selection": str(data.get("selection", "least_used")),
    })
    if data.get("default_model"):
        entry["default_model"] = str(data["default_model"])
    else:
        entry.pop("default_model", None)
    apply(state)


def delete_pool(state: Any, name: str) -> None:
    pools = state.raw_config.setdefault("pools", {})
    if name not in pools:
        raise KeyError(f"Unknown pool: {name}")
    del pools[name]
    routes = state.raw_config.setdefault("routes", {})
    for alias in list(routes):
        route = routes[alias]
        if isinstance(route, str):
            route = {"pool": route}
        if "failover" in route:
            remaining = [p for p in route["failover"] if p != name]
            if remaining:
                route["failover"] = remaining
            else:
                del routes[alias]
        elif route.get("pool") == name:
            del routes[alias]
    apply(state)


def upsert_route(state: Any, alias: str, pools: list[str], model: str | None) -> None:
    alias = str(alias).strip()
    pools = [str(p) for p in pools if str(p).strip()]
    if not alias or not pools:
        raise ValueError("Alias and at least one pool are required")
    known = state.raw_config.setdefault("pools", {})
    for name in pools:
        if name not in known:
            raise KeyError(f"Unknown pool: {name}")
    routes = state.raw_config.setdefault("routes", {})
    if len(pools) > 1:
        routes[alias] = {"failover": pools}
    else:
        entry = {"pool": pools[0]}
        if model:
            entry["model"] = str(model)
        routes[alias] = entry
    apply(state)


def delete_route(state: Any, alias: str) -> None:
    routes = state.raw_config.setdefault("routes", {})
    if alias not in routes:
        raise KeyError(f"Unknown route: {alias}")
    del routes[alias]
    apply(state)


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _token_view(token: str) -> dict[str, str]:
    return {"id": _token_id(token), "masked": mask_key(token)}


def add_token(state: Any, kind: str, token: str | None = None) -> str:
    field = "auth_tokens" if kind == "client" else "admin_tokens"
    if field not in ("auth_tokens", "admin_tokens"):
        raise ValueError("kind must be client or admin")
    token = token or f"sk-gw-{secrets.token_urlsafe(24)}"
    server = state.raw_config.setdefault("server", {})
    tokens = server.setdefault(field, [])
    if token not in tokens:
        tokens.append(token)
    apply(state)
    return token


def delete_token(state: Any, kind: str, token_id: str) -> None:
    field = "auth_tokens" if kind == "client" else "admin_tokens"
    server = state.raw_config.setdefault("server", {})
    tokens = server.get(field, [])
    match = next((t for t in tokens if _token_id(str(t)) == token_id), None)
    if match is None:
        raise KeyError("Token not found")
    tokens.remove(match)
    apply(state)


def update_settings(state: Any, data: dict[str, Any]) -> None:
    server = state.raw_config.setdefault("server", {})
    defaults = state.raw_config.setdefault("defaults", {})
    field_map = {
        "host": server, "port": server, "log_level": server,
        "request_timeout": server,
        "max_retries": defaults, "selection": defaults,
    }
    for key, target in field_map.items():
        if key in data:
            value = data[key]
            if key == "port":
                value = int(value)
            elif key in ("request_timeout", "max_retries"):
                value = float(value) if key == "request_timeout" else int(value)
            target[key] = value
    if "cooldown" in data and isinstance(data["cooldown"], dict):
        cd = defaults.setdefault("cooldown", {})
        for key, value in data["cooldown"].items():
            if key in ("rate_limit", "quota", "auth_error", "server_error", "max_backoff"):
                cd[key] = float(value)
    apply(state)
