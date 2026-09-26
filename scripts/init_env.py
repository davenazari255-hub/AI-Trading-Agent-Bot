#!/usr/bin/env python3
"""Create .env from .env.example with freshly generated local secrets.

The result keeps Bybit Demo Trading as the default and Live Trading disabled.
An existing .env is never overwritten.
"""

import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    env_path = ROOT / ".env"
    example_path = ROOT / ".env.example"
    if env_path.exists():
        print(".env already exists. It was not changed.")
        return 0
    if not example_path.exists():
        print(".env.example is missing.", file=sys.stderr)
        return 1

    master_key = base64.b64encode(secrets.token_bytes(32)).decode()
    db_password = secrets.token_urlsafe(24)

    lines = []
    for line in example_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MOOO_MASTER_KEY="):
            line = f"MOOO_MASTER_KEY={master_key}"
        lines.append(line.replace("change-me", db_password))

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    print("Created .env with a new master key and database password.")
    print("Default environment: Bybit Demo Trading. Live Trading is disabled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
