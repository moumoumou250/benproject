"""Grok Build subscription auth: reuse the Grok CLI login and its chat proxy.

``LLM_MODEL=grok`` or ``grok/<model>`` sends Responses API calls to
``https://cli-chat-proxy.grok.com/v1`` with the bearer token in
``~/.grok/auth.json`` (the file ``grok login`` writes). That token is an
auth.x.ai OAuth access token, not an ``xai-`` API key, so it is not valid on
``api.x.ai``. Ben refreshes it in place when it is near expiry and leaves the
rest of the CLI record alone.

The Cursor account must be on Privacy Mode (no training). Legacy (no storage)
is rejected by the proxy before any completion is produced. Settings → Privacy.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterator

    from openai import AsyncOpenAI


logger = logging.getLogger(__name__)

PROXY_BASE_URL = os.environ.get(
    "GROK_CLI_CHAT_PROXY_BASE_URL", "https://cli-chat-proxy.grok.com/v1"
)
TOKEN_URL = "https://auth.x.ai/oauth2/token"  # noqa: S105  # nosec B105 - URL, not a secret
DEFAULT_MODEL = "grok-4.6"
SUBSCRIPTION_PREFIX = "grok/"

_TOKEN_TIMEOUT = 30
_EXPIRY_SKEW_S = 300
_DEFAULT_TTL_S = 6 * 60 * 60

AUTH_PATH = Path.home() / ".grok" / "auth.json"
VERSION_PATH = Path.home() / ".grok" / "version.json"
_FALLBACK_CLIENT_VERSION = "1.0.77"

_refresh_lock = threading.Lock()


class GrokBuildAuthError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class GrokBuildPrivacyError(Exception):
    """The Grok account is in Legacy mode, which cannot call Grok Build."""

    def __init__(self, model: str, original: BaseException | None = None) -> None:
        self.model = model
        self.original = original
        super().__init__(
            f"'{model}' was refused because this Cursor account is in Legacy (no storage). "
            "Open Cursor Settings, then Privacy, and switch to Privacy Mode (no training). "
            "Grok Build cannot run until that account setting is saved."
        )


_PRIVACY_MARKERS = (
    "privacy-mode:legacy",
    "legacy (no storage)",
    "not supported in legacy",
)


def is_privacy_mode_error(exc: BaseException) -> bool:
    if isinstance(exc, GrokBuildPrivacyError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _PRIVACY_MARKERS)


def subscription_model(model_name: str | None) -> str | None:
    """The model slug behind a ``grok/<model>`` LLM_MODEL, or None.

    ``xai/grok-*`` stays on the API-key route and is not handled here.
    """
    name = (model_name or "").strip()
    if name.lower() == "grok":
        return DEFAULT_MODEL
    if not name.lower().startswith(SUBSCRIPTION_PREFIX):
        return None
    return name[len(SUBSCRIPTION_PREFIX) :] or None


def skips_api_key(model_name: str | None) -> bool:
    """True when this model bills a subscription login instead of LLM_API_KEY."""
    from Ben.config import codex

    return (
        codex.subscription_model(model_name) is not None
        or subscription_model(model_name) is not None
    )


def _read_store() -> dict[str, Any]:
    try:
        import json

        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(data: dict[str, Any]) -> None:
    import json

    from Ben.utils.secret_files import write_secret_text

    write_secret_text(AUTH_PATH, json.dumps(data, indent=2) + "\n")


def read_record() -> dict[str, Any] | None:
    """The Grok CLI OIDC record, or None when ``grok login`` has not been run."""
    selected = _select_record(_read_store())
    if selected is None:
        return None
    _key, record = selected
    return record


def is_authenticated() -> bool:
    return read_record() is not None


def _select_record(store: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for key, value in store.items():
        if not isinstance(value, dict):
            continue
        if not (value.get("key") and value.get("refresh_token")):
            continue
        issuer = str(value.get("oidc_issuer") or "")
        if issuer.startswith("https://auth.x.ai") or str(key).startswith("https://auth.x.ai"):
            return str(key), value
    return None


def _parse_expiry(value: object) -> float | None:
    """Unix time for Grok's RFC3339 expiry, which may carry nanosecond fractions."""
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, frac = text.split(".", 1)
        digits: list[str] = []
        rest = ""
        for index, char in enumerate(frac):
            if char.isdigit():
                digits.append(char)
            else:
                rest = frac[index:]
                break
        trimmed = "".join(digits[:6])
        text = f"{head}.{trimmed}{rest}" if trimmed else f"{head}{rest}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _near_expiry(record: dict[str, Any]) -> bool:
    expires_at = _parse_expiry(record.get("expires_at"))
    if expires_at is None:
        return True
    return expires_at - _EXPIRY_SKEW_S <= time.time()


def _format_expiry(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@contextlib.contextmanager
def _refresh_guard() -> Iterator[None]:
    """Serialize refresh inside this process and across Ben processes."""
    with _refresh_lock:
        try:
            import fcntl

            lock_path = AUTH_PATH.with_name(AUTH_PATH.name + ".ben.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a")
        except (ImportError, OSError):
            yield
            return
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def refresh_tokens(record: dict[str, Any]) -> dict[str, Any]:
    """Exchange the CLI refresh token and return an updated record copy."""
    import requests

    client_id = record.get("oidc_client_id")
    refresh = record.get("refresh_token")
    if (
        not isinstance(client_id, str)
        or not client_id
        or not isinstance(refresh, str)
        or not refresh
    ):
        raise GrokBuildAuthError(
            "not_authenticated", "Grok login is missing a refresh token; run: grok login"
        )
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
            timeout=_TOKEN_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GrokBuildAuthError(
            "refresh_failed", "could not reach auth.x.ai to refresh the Grok login"
        ) from exc
    if response.status_code != 200:
        raise GrokBuildAuthError(
            "refresh_failed",
            "Grok login could not be refreshed. Run `grok login` and re-run.",
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GrokBuildAuthError(
            "refresh_failed", "auth.x.ai returned a non-JSON token response"
        ) from exc
    access = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access, str) or not access:
        raise GrokBuildAuthError("refresh_failed", "auth.x.ai did not return an access token")
    updated = dict(record)
    updated["key"] = access
    new_refresh = payload.get("refresh_token") if isinstance(payload, dict) else None
    if isinstance(new_refresh, str) and new_refresh:
        updated["refresh_token"] = new_refresh
    expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
    try:
        ttl = int(expires_in)
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_S
    updated["expires_at"] = _format_expiry(time.time() + max(ttl, 1))
    return updated


def get_valid_token() -> str:
    """Return a live access token, refreshing under the cross-process guard if needed."""
    record = read_record()
    if record is None:
        raise GrokBuildAuthError("not_authenticated", "not signed in to Grok; run: grok login")
    if not _near_expiry(record):
        return str(record["key"])
    with _refresh_guard():
        store = _read_store()
        selected = _select_record(store)
        if selected is None:
            raise GrokBuildAuthError("not_authenticated", "not signed in to Grok; run: grok login")
        store_key, record = selected
        if not _near_expiry(record):
            return str(record["key"])
        try:
            refreshed = refresh_tokens(record)
        except GrokBuildAuthError:
            latest = _select_record(_read_store())
            if (
                latest
                and latest[1].get("refresh_token") != record.get("refresh_token")
                and not _near_expiry(latest[1])
            ):
                return str(latest[1]["key"])
            raise
        store[store_key] = refreshed
        _write_store(store)
        return str(refreshed["key"])


def client_version() -> str:
    """Version advertised to the proxy. A missing version is rejected as ``(none)``."""
    try:
        import json

        data = json.loads(VERSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _FALLBACK_CLIENT_VERSION
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    return _FALLBACK_CLIENT_VERSION


def client_headers() -> dict[str, str]:
    version = client_version()
    return {
        "User-Agent": f"grok-cli/{version}",
        "x-grok-client-version": version,
        "x-grok-client-identifier": "grok-cli",
    }


def build_openai_client() -> AsyncOpenAI:
    """An ``AsyncOpenAI`` for the Grok CLI proxy. Each request re-stamps the bearer."""
    import asyncio

    import httpx
    from openai import AsyncOpenAI

    from Ben.llm import request_log

    get_valid_token()
    headers = client_headers()

    async def _auth_hook(request: httpx.Request) -> None:
        access = await asyncio.to_thread(get_valid_token)
        request.headers["Authorization"] = f"Bearer {access}"
        for name, value in client_headers().items():
            request.headers[name] = value

    # 代理宣告 ALPN h2。HTTP/1.1 下每个 agent 各开一条 TLS，经本地转发
    # （常见 198.18.0.0/15 fake-ip）时第二条握手会 ConnectError，SDK 记成
    # "Error streaming response"。HTTP/2 让并发回合复用同一条连接。
    try:
        transport = httpx.AsyncHTTPTransport(retries=1, http2=True)
    except ImportError:
        logger.warning("未安装 h2，Grok 客户端退回 HTTP/1.1")
        transport = httpx.AsyncHTTPTransport(retries=1)
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=30.0),
        event_hooks={"request": [_auth_hook]},
        transport=transport,
    )
    request_log.observe_http_client(http_client)
    return AsyncOpenAI(
        api_key="ben-grok-build",  # placeholder; the hook overwrites Authorization
        base_url=PROXY_BASE_URL,
        default_headers=headers,
        http_client=http_client,
    )


_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client  # noqa: PLW0603
    if _client is None:
        _client = build_openai_client()
    return _client
