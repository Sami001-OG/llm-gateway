"""Key pool management: availability tracking, selection strategies, cooldowns."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

from .config import CooldownConfig, PoolConfig

log = logging.getLogger("gateway.keypool")


def mask_key(value: str) -> str:
    """Return a log-safe representation of a secret key."""
    if len(value) <= 10:
        return value[:2] + "…" + value[-2:]
    return value[:6] + "…" + value[-4:]


def _parse_retry_after(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(str(value))
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


@dataclass
class KeyState:
    id: str
    value: str
    pool: str
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    consecutive_failures: int = 0
    total_requests: int = 0
    total_successes: int = 0
    total_failures: int = 0
    last_used: float = 0.0
    last_error: str = ""
    disabled: bool = False

    def available(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return not self.disabled and now >= self.cooldown_until

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "id": self.id,
            "key": mask_key(self.value),
            "pool": self.pool,
            "available": self.available(now),
            "disabled": self.disabled,
            "cooldown_remaining": max(0.0, self.cooldown_until - now),
            "cooldown_reason": self.cooldown_reason,
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "last_used": self.last_used,
            "last_error": self.last_error[:200],
        }


_QUOTA_MARKERS = (
    "insufficient_quota",
    "quota exceeded",
    "exceeded your current quota",
    "rate limit reached for organization",
    "billing",
    "usage limit",
    "spending limit",
    "credit balance",
    "out of credits",
    "payment required",
    "free tier limit",
    "daily limit",
)


class KeyPool:
    """A set of interchangeable credentials for one provider."""

    def __init__(self, config: PoolConfig):
        self.config = config
        self.name = config.name
        disabled = set(config.disabled_keys)
        self.keys: list[KeyState] = [
            KeyState(
                id=f"{config.name}#{i}",
                value=value,
                pool=config.name,
                disabled=value in disabled,
                cooldown_reason="disabled_by_config" if value in disabled else "",
            )
            for i, value in enumerate(config.keys)
        ]
        self._rr_cursor = 0
        self._lock_available = False  # set by tests if needed

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #
    def select(self, exclude: set[str] | None = None) -> KeyState | None:
        """Pick an available key. Synchronous + await-free => atomic in the event loop."""
        exclude = exclude or set()
        now = time.time()
        candidates = [k for k in self.keys if k.available(now) and k.id not in exclude]
        if not candidates:
            return None

        strategy = self.config.selection
        if strategy == "random":
            chosen = random.choice(candidates)
        elif strategy == "round_robin":
            # Start at cursor and wrap around, honouring availability.
            n = len(self.keys)
            for offset in range(n):
                idx = (self._rr_cursor + offset) % n
                k = self.keys[idx]
                if k.available(now) and k.id not in exclude:
                    self._rr_cursor = (idx + 1) % n
                    chosen = k
                    break
            else:  # pragma: no cover - defensive
                return None
        else:  # least_used (default)
            min_used = min(k.total_requests for k in candidates)
            tied = [k for k in candidates if k.total_requests == min_used]
            chosen = random.choice(tied)

        chosen.total_requests += 1
        chosen.last_used = now
        return chosen

    # ------------------------------------------------------------------ #
    # Outcome reporting
    # ------------------------------------------------------------------ #
    def mark_success(self, key: KeyState) -> None:
        key.total_successes += 1
        key.consecutive_failures = 0
        key.cooldown_until = 0.0
        key.cooldown_reason = ""
        key.last_error = ""

    def mark_failure(
        self,
        key: KeyState,
        status: int,
        headers: dict[str, str] | None,
        text: str,
    ) -> None:
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        body = (text or "")[:1000]
        low = body.lower()
        cd: CooldownConfig = self.config.cooldown
        key.total_failures += 1
        key.consecutive_failures += 1
        key.last_error = body[:300] or f"HTTP {status}"

        if status in (401, 403):
            key.disabled = True
            key.cooldown_reason = "invalid_credentials"
            key.cooldown_until = time.time() + cd.auth_error
            log.warning(
                "[%s] key %s returned %s -> DISABLED (re-enable via /admin)",
                self.name, mask_key(key.value), status,
            )
            return

        if status == 429:
            retry_after = _parse_retry_after(headers.get("retry-after"))
            seconds = retry_after if retry_after is not None else cd.rate_limit
            reason = "rate_limited"
        elif status == 402 or any(marker in low for marker in _QUOTA_MARKERS):
            seconds = cd.quota
            reason = "quota_exhausted"
        elif status == 0 or status >= 500 or status in (408, 409, 425):
            backoff = cd.server_error * (2 ** max(0, key.consecutive_failures - 1))
            seconds = min(backoff, cd.max_backoff)
            reason = f"upstream_error_{status or 'network'}"
        else:
            seconds = cd.server_error
            reason = f"http_{status}"

        key.cooldown_until = time.time() + max(0.0, seconds)
        key.cooldown_reason = reason
        log.warning(
            "[%s] key %s -> %s, cooling down %.0fs (%s)",
            self.name, mask_key(key.value), status or "network", seconds, reason,
        )

    # ------------------------------------------------------------------ #
    # Admin helpers
    # ------------------------------------------------------------------ #
    def get(self, key_id: str) -> KeyState | None:
        return next((k for k in self.keys if k.id == key_id), None)

    def set_disabled(self, key_id: str, disabled: bool) -> bool:
        key = self.get(key_id)
        if not key:
            return False
        key.disabled = disabled
        if not disabled:
            key.cooldown_until = 0.0
            key.cooldown_reason = ""
            key.consecutive_failures = 0
        return True

    def set_cooldown(self, key_id: str, seconds: float, reason: str = "manual") -> bool:
        key = self.get(key_id)
        if not key:
            return False
        key.cooldown_until = time.time() + max(0.0, seconds)
        key.cooldown_reason = reason
        return True

    def stats(self) -> dict[str, Any]:
        now = time.time()
        available = sum(1 for k in self.keys if k.available(now))
        return {
            "name": self.name,
            "provider": self.config.provider,
            "selection": self.config.selection,
            "models": self.config.models,
            "total_keys": len(self.keys),
            "available_keys": available,
            "cooling_down": sum(
                1 for k in self.keys if not k.disabled and k.cooldown_until > now
            ),
            "disabled_keys": sum(1 for k in self.keys if k.disabled),
            "total_requests": sum(k.total_requests for k in self.keys),
            "total_successes": sum(k.total_successes for k in self.keys),
            "total_failures": sum(k.total_failures for k in self.keys),
            "keys": [k.to_dict() for k in self.keys],
        }
