"""Configuration loading and validation for the LLM gateway.

Config is plain YAML (no pydantic dependency). Secrets can be supplied inline,
via environment variables (``${VAR}``), or from a file (``file:/path/to/file``
with one key per line).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

log = logging.getLogger("gateway.config")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class CooldownConfig:
    rate_limit: float = 60.0        # seconds after HTTP 429 (unless Retry-After)
    quota: float = 21600.0          # seconds after quota / billing exhaustion (6h)
    auth_error: float = 86400.0     # seconds after 401/403 (key also disabled)
    server_error: float = 10.0      # base seconds after 5xx / network errors
    max_backoff: float = 600.0      # cap for exponential server-error backoff

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CooldownConfig":
        raw = raw or {}
        return cls(
            rate_limit=float(raw.get("rate_limit", 60)),
            quota=float(raw.get("quota", 21600)),
            auth_error=float(raw.get("auth_error", 86400)),
            server_error=float(raw.get("server_error", 10)),
            max_backoff=float(raw.get("max_backoff", 600)),
        )


@dataclass
class PoolConfig:
    name: str
    provider: str                   # "openai" | "anthropic"
    base_url: str
    models: list[str]
    keys: list[str]
    selection: str = "least_used"   # least_used | round_robin | random
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    default_model: str | None = None
    timeout: float | None = None
    max_retries: int | None = None  # optional per-pool override
    disabled_keys: list[str] = field(default_factory=list)


@dataclass
class RouteConfig:
    # Either (pool, optional upstream model override) or an ordered failover list.
    pools: list[str]
    model_override: str | None = None


@dataclass
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    auth_tokens: list[str] = field(default_factory=list)
    admin_tokens: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    request_timeout: float = 120.0
    max_retries: int = 4
    selection: str = "least_used"
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    pools: dict[str, PoolConfig] = field(default_factory=dict)
    routes: dict[str, RouteConfig] = field(default_factory=dict)
    config_path: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _expand_env(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        return os.environ.get(name, "")

    return _ENV_RE.sub(repl, value)


def _load_keys(raw_keys: Any) -> list[str]:
    """Expand env vars and ``file:`` references into a flat list of keys."""
    if raw_keys is None:
        return []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]

    keys: list[str] = []
    for item in raw_keys:
        item = str(item).strip()
        if not item:
            continue
        if item.startswith("file:"):
            path = _expand_env(item[5:].strip())
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            keys.append(_expand_env(line))
                log.info("Loaded keys from %s", path)
            except OSError as exc:
                log.warning("Could not read key file %r: %s", path, exc)
        else:
            expanded = _expand_env(item)
            if expanded:
                keys.append(expanded)
    return keys


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


# --------------------------------------------------------------------------- #
# Public loader
# --------------------------------------------------------------------------- #
def load_config(path: str) -> GatewayConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return parse_config(raw, path)


def parse_config(raw: dict[str, Any], path: str = "") -> GatewayConfig:
    """Build a GatewayConfig from an already-parsed YAML mapping."""
    raw = raw or {}
    server = raw.get("server") or {}
    defaults = raw.get("defaults") or {}
    global_cd = CooldownConfig.from_dict(
        {**defaults.get("cooldown", {}), **(server.get("cooldown") or {})}
    )

    cfg = GatewayConfig(
        host=str(server.get("host", "0.0.0.0")),
        port=int(server.get("port", 8000)),
        auth_tokens=_as_list(server.get("auth_tokens")),
        admin_tokens=_as_list(server.get("admin_tokens")),
        log_level=str(server.get("log_level", "INFO")).upper(),
        request_timeout=float(server.get("request_timeout", defaults.get("timeout", 120))),
        max_retries=int(defaults.get("max_retries", 4)),
        selection=str(defaults.get("selection", "least_used")),
        cooldown=global_cd,
        config_path=path,
    )

    for name, pdata in (raw.get("pools") or {}).items():
        provider = str(pdata.get("provider", "")).lower()
        if provider not in ("openai", "anthropic"):
            raise ValueError(f"Pool {name!r}: unsupported provider {provider!r}")
        pool_cd = CooldownConfig.from_dict(pdata.get("cooldown"))
        # Fill missing fields from global defaults.
        for fld in ("rate_limit", "quota", "auth_error", "server_error", "max_backoff"):
            if pdata.get("cooldown") is None or fld not in (pdata.get("cooldown") or {}):
                setattr(pool_cd, fld, getattr(global_cd, fld))

        models = _as_list(pdata.get("models"))
        cfg.pools[str(name)] = PoolConfig(
            name=str(name),
            provider=provider,
            base_url=str(pdata.get("base_url", "")).rstrip("/"),
            models=models,
            keys=_load_keys(pdata.get("keys")),
            selection=str(pdata.get("selection", cfg.selection)),
            cooldown=pool_cd,
            default_model=pdata.get("default_model"),
            timeout=float(pdata["timeout"]) if pdata.get("timeout") else None,
            max_retries=int(pdata["max_retries"]) if pdata.get("max_retries") else None,
            disabled_keys=[str(v) for v in _as_list(pdata.get("disabled_keys"))],
        )
        if not cfg.pools[str(name)].base_url:
            raise ValueError(f"Pool {name!r}: base_url is required")

    for alias, rdata in (raw.get("routes") or {}).items():
        if isinstance(rdata, str):
            rdata = {"pool": rdata}
        failover = _as_list(rdata.get("failover"))
        if failover:
            cfg.routes[str(alias)] = RouteConfig(pools=failover)
        else:
            pool = rdata.get("pool")
            if not pool:
                raise ValueError(f"Route {alias!r}: needs 'pool' or 'failover'")
            cfg.routes[str(alias)] = RouteConfig(
                pools=[str(pool)], model_override=rdata.get("model")
            )

    log.info(
        "Config loaded: %d pool(s), %d route(s), %d total key(s)",
        len(cfg.pools),
        len(cfg.routes),
        sum(len(p.keys) for p in cfg.pools.values()),
    )
    return cfg
