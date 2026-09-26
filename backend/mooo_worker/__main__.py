"""Entry point: ``python -m mooo_worker``."""

import asyncio

from mooo_core.config import load_settings_or_exit
from mooo_core.log import configure_process_logging
from mooo_worker.main import run_worker


def main() -> None:
    settings = load_settings_or_exit()
    configure_process_logging(settings)
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    main()
