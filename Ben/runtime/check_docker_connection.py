"""Stable Docker connection self-check.

Run with the project venv:

    .venv/Scripts/python.exe Ben/runtime/check_docker_connection.py

Exercises the full path: endpoint resolution -> connect with retries
-> ping -> healthcheck -> a real container round-trip (run + exec +
remove). Exits non-zero on failure so it can gate CI or agent startup.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the Ben package importable regardless of CWD.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from Ben.runtime.docker_connection import (  # noqa: E402
    DockerConnectionError,
    connect_docker,
    docker_healthcheck,
    resolve_docker_endpoint,
)


def main() -> int:
    endpoint = resolve_docker_endpoint()
    print(f"[1/4] endpoint: {endpoint.label}")

    try:
        client = connect_docker()
    except DockerConnectionError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print("[2/4] connect + ping: OK")

    health = docker_healthcheck(client)
    if not health["ok"]:
        print(f"[FAIL] healthcheck: {health['error']}")
        return 1
    print(
        "[3/4] healthcheck: OK "
        f"(server {health['server_version']}, api {health['api_version']}, "
        f"{health['containers_running']}/{health['containers_total']} containers running)"
    )

    # Real round-trip: create -> start -> wait -> logs -> remove.
    # (containers.run(remove=True) can race log collection on npipe and
    # return None, so do the steps explicitly.)
    image = "ghcr.io/usestrix/strix-sandbox:1.3.0"
    container = None
    try:
        if not client.images.list(name=image):
            print(f"      pulling {image} ...")
            repo_name, _, tag = image.partition(":")
            client.images.pull(repo_name, tag=tag or None)
        # entrypoint=[] 清空镜像内置 entrypoint，跳过 Caido/代理，直接执行 echo。
        container = client.containers.create(
            image,
            ["echo", "docker-round-trip-ok"],
            entrypoint=[],
        )
        container.start()
        rc = container.wait(timeout=60)["StatusCode"]
        result = (container.logs() or b"").decode().strip()
        if rc != 0:
            raise RuntimeError(f"container exited {rc}: {result}")
    except Exception as exc:  # noqa: BLE001 - report anything the daemon throws
        print(f"[FAIL] container round-trip: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
    assert result == "docker-round-trip-ok", result
    print(f"[4/4] container round-trip: OK ({result})")

    client.close()
    print("ALL OK - docker connection is stable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
