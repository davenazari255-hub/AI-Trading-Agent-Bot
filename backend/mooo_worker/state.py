"""In-memory worker state reported by the health endpoint and heartbeat."""

import time
from dataclasses import dataclass, field
from typing import Any

from mooo_core.config import BybitEnv, WorkerRole


@dataclass
class WorkerState:
    role: WorkerRole
    trading_environment: BybitEnv
    lock_held: bool = False
    started_at: float = field(default_factory=time.time)
    last_heartbeat_at: float | None = None

    @property
    def trading_enabled(self) -> bool:
        """New trading is allowed only for the trading role while it holds the worker lock."""
        return self.role is WorkerRole.TRADING and self.lock_held

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "worker",
            "role": self.role.value,
            "trading_environment": self.trading_environment.value,
            "lock_held": self.lock_held,
            "trading_enabled": self.trading_enabled,
            "uptime_s": round(time.time() - self.started_at, 1),
            "last_heartbeat_at": self.last_heartbeat_at,
        }
