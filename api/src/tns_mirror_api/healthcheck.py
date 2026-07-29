"""Container healthcheck: ``python -m tns_mirror_api.healthcheck``.

A module rather than an inline one-liner in the Dockerfile because it has to
honour ``TNS_API_ROOT_PATH`` — a service mounted at ``/tns`` does not answer
``/healthz``, and a healthcheck that quietly probes the wrong path reports an
unhealthy container that is working perfectly.

Deliberately probes ``/healthz`` and not ``/readyz``. Liveness answers "should
this container be restarted"; a stale mirror is not fixed by restarting, and
probing readiness for that decision would put the service in a restart loop
while it waits for a sync it cannot perform itself.

Uses ``http.client`` rather than ``urllib.request``: the target is a fixed
loopback address, so URL parsing and scheme handling buy nothing here and only
widen what this can be pointed at.
"""

from __future__ import annotations

import http.client
import os
import sys


def main() -> int:
    prefix = os.environ.get("TNS_API_ROOT_PATH", "").strip().strip("/")
    path = f"/{prefix}/healthz" if prefix else "/healthz"
    port = int(os.environ.get("TNS_API_PORT", "8000"))

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request("GET", path)
        status = connection.getresponse().status
    except OSError as exc:
        print(f"healthcheck failed for :{port}{path}: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if status != 200:
        print(f"healthcheck got HTTP {status} for :{port}{path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
