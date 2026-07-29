"""Serve the API with granian.

granian rather than uvicorn: it is a Rust HTTP server, so the per-request
overhead outside the database query is smaller, and it runs multiple workers
without a separate process manager.
"""

from __future__ import annotations

import os


def main() -> int:
    from granian import Granian
    from granian.constants import Interfaces

    Granian(
        target="tns_mirror_api.app:create_app",
        factory=True,
        # Binding to all interfaces is the point of a container. The service is
        # read-only and rate-limited; put it behind your own ingress to
        # restrict who reaches it.
        # Waived in security/waivers.yml as BANDIT-B104-api-bind, with an
        # expiry that fails CI when it passes.
        address=os.environ.get("TNS_API_HOST", "0.0.0.0"),  # noqa: S104  # nosec B104
        port=int(os.environ.get("TNS_API_PORT", "8000")),
        interface=Interfaces.ASGI,
        workers=int(os.environ.get("TNS_API_WORKERS", "1")),
        log_level=os.environ.get("TNS_API_LOG_LEVEL", "info"),
    ).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
