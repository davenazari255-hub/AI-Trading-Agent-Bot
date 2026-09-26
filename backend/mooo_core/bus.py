"""Redis keys and channels shared by the API Server and Agent Worker."""

from mooo_core.config import WorkerRole

COMMANDS_STREAM = "mooo:commands"
BACKTESTS_STREAM = "mooo:backtests"
EVENTS_CHANNEL = "mooo:events"
STATE_CHANNEL = "mooo:state"
MARKET_CHANNEL = "mooo:market"

WORKER_LOCK_KEY = "mooo:worker:lock"
WORKER_LOCK_TTL_MS = 15_000
WORKER_LOCK_REFRESH_S = 5.0

WORKER_HEARTBEAT_INTERVAL_S = 5.0
WORKER_HEARTBEAT_TTL_S = 60
WORKER_DOWN_AFTER_S = 20.0


def worker_heartbeat_key(role: WorkerRole) -> str:
    """Return the Redis key that holds the latest heartbeat for a worker role."""
    return f"mooo:worker:heartbeat:{role.value}"
