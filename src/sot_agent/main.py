"""Backend process entry point."""

from __future__ import annotations

import uvicorn

from .config import load_config


def run() -> None:
    settings = load_config()
    uvicorn.run(
        "sot_agent.app:create_default_app",
        factory=True,
        host=settings.l2.host,
        port=settings.l2.port,
        reload=False,
        timeout_graceful_shutdown=settings.l2.graceful_shutdown_timeout_seconds,
    )


if __name__ == "__main__":
    run()
