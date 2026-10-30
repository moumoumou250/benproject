"""Stable Docker daemon connection.

Resolution order for the endpoint: ``DOCKER_HOST`` env var, else the
current docker context, else the platform default socket (npipe on
Windows, unix socket elsewhere).

On top of plain resolution this module adds the pieces a long-running
agent needs for a *stable* connection:

- explicit API timeouts, so a wedged daemon cannot hang us forever
- a post-connect ``ping()`` verification (a constructed client is not
  proof the daemon answers)
- retry with exponential backoff around connect+ping, since the daemon
  (esp. Docker Desktop) may still be warming up when we first call
- :func:`wait_for_docker` to poll until the daemon is ready
- :func:`docker_healthcheck` returning a diagnostics dict

All knobs are env-tunable:

- ``STRIX_DOCKER_TIMEOUT``        per-call API timeout seconds (default 60)
- ``STRIX_DOCKER_CONNECT_RETRIES`` connect+ping attempts (default 4)
- ``STRIX_DOCKER_RETRY_DELAY``    initial backoff seconds (default 1.0, x2 per retry)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import docker  # type: ignore[import-untyped, unused-ignore]
from docker.context import ContextAPI  # type: ignore[import-untyped, unused-ignore]
from docker.context.config import (  # type: ignore[import-untyped, unused-ignore]
    get_current_context_name,
)
from docker.errors import DockerException  # type: ignore[import-untyped, unused-ignore]
from requests.exceptions import RequestException


logger = logging.getLogger(__name__)


DEFAULT_CONTEXT = "default"

# Transient failures worth retrying: daemon warming up, socket briefly
# unavailable, HTTP layer hiccups. DockerException covers APIError and
# the SDK's own connection wrapper; RequestException covers the
# requests/urllib3 layer underneath (ConnectionError, Timeout...).
_RETRYABLE = (DockerException, RequestException, OSError)


@dataclass(frozen=True)
class DockerEndpoint:
    """Where strix talks to Docker and how that address was chosen."""

    host: str | None
    source: str
    tls: Any = None

    @property
    def label(self) -> str:
        return f"{self.host} ({self.source})" if self.host else self.source


class DockerConnectionError(RuntimeError):
    """The daemon at ``endpoint`` could not be reached."""

    def __init__(self, endpoint: DockerEndpoint, cause: BaseException) -> None:
        self.endpoint = endpoint
        self.cause = root_cause(cause)
        super().__init__(f"Cannot connect to Docker at {endpoint.label}: {self.detail}")

    @property
    def detail(self) -> str:
        text = str(self.cause).strip()
        name = type(self.cause).__name__
        return text if name in text else f"{name}: {text}" if text else name


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (not an int)", name, raw)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (not a number)", name, raw)
        return default


def resolve_docker_endpoint(environ: dict[str, str] | None = None) -> DockerEndpoint:
    """``DOCKER_HOST``, else the current docker context, else the SDK default."""
    env = os.environ if environ is None else environ
    host = env.get("DOCKER_HOST", "").strip()
    if host:
        return DockerEndpoint(host, "DOCKER_HOST")

    name = get_current_context_name()
    if name == DEFAULT_CONTEXT:
        return DockerEndpoint(None, "default socket")

    source = f"docker context '{name}'"
    try:
        context = ContextAPI.get_context(name)
    except DockerException as exc:
        raise DockerConnectionError(DockerEndpoint(None, source), exc) from exc
    if context is None:
        missing = DockerException(f"{source} is selected but does not exist")
        raise DockerConnectionError(DockerEndpoint(None, source), missing)
    return DockerEndpoint(context.Host, source, context.TLSConfig)


def _build_client(endpoint: DockerEndpoint, timeout: int) -> Any:
    """Construct a client for ``endpoint`` with an explicit API timeout.

    Without ``timeout`` the SDK defaults to 60s on some paths and *no
    limit* on others; we set it uniformly so a hung daemon surfaces as
    an error instead of a frozen agent.
    """
    if endpoint.source == "DOCKER_HOST" or endpoint.host is None:
        # from_env also reads DOCKER_TLS_VERIFY and DOCKER_CERT_PATH.
        return docker.from_env(timeout=timeout)
    return docker.DockerClient(base_url=endpoint.host, tls=endpoint.tls or False, timeout=timeout)


def connect_docker(
    *,
    timeout: int | None = None,
    retries: int | None = None,
    retry_delay: float | None = None,
    verify: bool = True,
) -> Any:
    """Return a verified ``docker.DockerClient`` or raise DockerConnectionError.

    The client is constructed for the resolved endpoint, then — unless
    ``verify=False`` — proven alive with ``ping()``. Transient failures
    are retried with exponential backoff (``retry_delay`` * 2**attempt).

    ``retries`` counts total attempts, so the default of 4 means
    ~7s worst-case before giving up (1+2+4s of backoff between tries).
    """
    endpoint = resolve_docker_endpoint()
    timeout = timeout if timeout is not None else _env_int("STRIX_DOCKER_TIMEOUT", 60)
    attempts = retries if retries is not None else _env_int("STRIX_DOCKER_CONNECT_RETRIES", 4)
    delay = retry_delay if retry_delay is not None else _env_float("STRIX_DOCKER_RETRY_DELAY", 1.0)
    attempts = max(1, attempts)

    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        client = None
        try:
            client = _build_client(endpoint, timeout)
            if verify:
                client.ping()
            if attempt > 1:
                logger.info("Docker at %s reachable on attempt %d", endpoint.label, attempt)
            return client
        except _RETRYABLE as exc:
            last_exc = exc
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - teardown must not mask the real error
                    pass
            if attempt == attempts:
                break
            wait = delay * (2 ** (attempt - 1))
            logger.warning(
                "Docker at %s unreachable (attempt %d/%d), retrying in %.1fs: %s",
                endpoint.label, attempt, attempts, wait, root_cause(exc),
            )
            time.sleep(wait)

    assert last_exc is not None
    raise DockerConnectionError(endpoint, last_exc) from last_exc


def wait_for_docker(deadline_seconds: float = 30.0, poll_interval: float = 1.0) -> Any:
    """Block until the daemon answers ``ping()`` or the deadline passes.

    For startup sequencing: Docker Desktop can take tens of seconds to
    boot its VM. Returns a verified client on success; raises
    DockerConnectionError on timeout.
    """
    endpoint = resolve_docker_endpoint()
    deadline = time.monotonic() + deadline_seconds
    last_exc: BaseException | None = None
    while True:
        try:
            client = _build_client(endpoint, timeout=_env_int("STRIX_DOCKER_TIMEOUT", 60))
            client.ping()
            return client
        except _RETRYABLE as exc:
            last_exc = exc
            if time.monotonic() >= deadline:
                raise DockerConnectionError(endpoint, last_exc) from last_exc
            time.sleep(poll_interval)


def docker_healthcheck(client: Any | None = None) -> dict[str, Any]:
    """Diagnostics snapshot: reachability, versions, daemon info.

    Pass an existing client or let it build (and close) its own. Never
    raises for a *reachable* endpoint; on failure returns ``ok=False``
    with the error detail so callers can render one status line.
    """
    endpoint = resolve_docker_endpoint()
    own_client = client is None
    try:
        if own_client:
            client = _build_client(endpoint, timeout=_env_int("STRIX_DOCKER_TIMEOUT", 60))
        version = client.version()
        info = client.info()
        return {
            "ok": True,
            "endpoint": endpoint.label,
            "server_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "os": info.get("OperatingSystem"),
            "containers_running": info.get("ContainersRunning"),
            "containers_total": info.get("Containers"),
            "images": info.get("Images"),
        }
    except _RETRYABLE as exc:
        cause = root_cause(exc)
        return {
            "ok": False,
            "endpoint": endpoint.label,
            "error": f"{type(cause).__name__}: {cause}",
        }
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def root_cause(exc: BaseException) -> BaseException:
    """The innermost exception: the SDK wraps the OSError in requests/urllib3 errors."""
    current = exc
    seen: list[BaseException] = []
    while not any(current is s for s in seen):
        seen.append(current)
        nested = [arg for arg in current.args if isinstance(arg, BaseException)]
        following = current.__cause__ or current.__context__ or (nested[-1] if nested else None)
        if following is None:
            break
        current = following
    return current
