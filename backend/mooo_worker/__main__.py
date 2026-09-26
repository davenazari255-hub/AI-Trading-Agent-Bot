"""Entry point: ``python -m mooo_worker``."""

import asyncio
import logging

from mooo_core.config import load_settings_or_exit
from mooo_worker.main import run_worker


def main() -> None:
    settings = load_settings_or_exit()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
