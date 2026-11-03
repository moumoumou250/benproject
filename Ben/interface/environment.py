"""Startup environment validation and Docker image management."""

from __future__ import annotations

import logging
import shutil
import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from Ben.config import load_settings


logger = logging.getLogger(__name__)


def _missing_required_vars() -> list[str]:
    settings = load_settings()
    missing: list[str] = []
    if not settings.llm.model:
        missing.append("LLM_MODEL")
    if not settings.llm.api_key and not settings.llm.vendor_key_sufficient():
        missing.append("LLM_API_KEY")
    return missing


def _missing_web_search_vars(integrations: object) -> list[str]:
    """按 web_search 的 provider 规则返回缺失的检索 Key（与工具侧判定一致）。

    返回空列表表示已配置；``web_search_provider`` 为 ``exa``/``perplexity`` 时
    只要求对应那一个 Key，未显式指定时任一 Key 即可。
    """
    provider = str(getattr(integrations, "web_search_provider", "") or "").lower()
    exa_key = getattr(integrations, "exa_api_key", None)
    perplexity_key = getattr(integrations, "perplexity_api_key", None)

    if provider == "exa":
        return [] if exa_key else ["EXA_API_KEY"]
    if provider == "perplexity":
        return [] if perplexity_key else ["PERPLEXITY_API_KEY"]
    if exa_key or perplexity_key:
        return []
    return ["EXA_API_KEY", "PERPLEXITY_API_KEY"]


def validate_environment() -> None:
    """Fail fast with an actionable panel instead of dying inside the first LLM call."""
    logger.info("Validating environment")
    console = Console()
    missing = _missing_required_vars()
    if not missing:
        logger.info("Environment OK")
        return

    error_text = Text()
    error_text.append("MISSING REQUIRED ENVIRONMENT VARIABLES", style="bold red")
    error_text.append("\n\n", style="white")
    for var in missing:
        error_text.append(f"• {var}", style="bold yellow")
        error_text.append(" is not set\n", style="white")
    error_text.append(
        "\nSet them in .env (LLM_MODEL / LLM_API_KEY / LLM_API_BASE) or via the shell.\n",
        style="dim white",
    )
    console.print("\n")
    console.print(
        Panel(
            error_text,
            title="[bold white]BEN",
            title_align="left",
            border_style="red",
            padding=(1, 2),
        )
    )
    console.print()
    sys.exit(1)


def check_docker_installed() -> None:
    if shutil.which("docker") is None:
        console = Console()
        error_text = Text()
        error_text.append("DOCKER NOT INSTALLED", style="bold red")
        error_text.append("\n\n", style="white")
        error_text.append("The 'docker' CLI was not found in your PATH.\n", style="white")
        error_text.append(
            "Please install Docker and ensure the 'docker' command is available.\n", style="white"
        )
        console.print(
            "\n",
            Panel(
                error_text,
                title="[bold white]BEN",
                title_align="left",
                border_style="red",
                padding=(1, 2),
            ),
            "\n",
        )
        sys.exit(1)
    logger.debug("Docker CLI present")


def check_docker_connection():  # noqa: ANN201
    """Return a Docker client, exiting with a readable panel when unreachable."""
    import docker as docker_sdk
    from docker.errors import DockerException

    console = Console()
    try:
        client = docker_sdk.from_env()
        client.ping()
    except DockerException as exc:
        error_text = Text()
        error_text.append("DOCKER DAEMON UNREACHABLE", style="bold red")
        error_text.append("\n\n", style="white")
        error_text.append(f"{exc}\n", style="dim red")
        error_text.append(
            "Start Docker Desktop (or the docker daemon) and try again.", style="white"
        )
        console.print(
            "\n",
            Panel(
                error_text,
                title="[bold white]BEN",
                title_align="left",
                border_style="red",
                padding=(1, 2),
            ),
            "\n",
        )
        sys.exit(1)
    return client


def image_exists(client, image: str) -> bool:  # noqa: ANN001
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001 - NotFound and daemon errors both mean "not local"
        return False
    return True


def process_pull_line(  # noqa: ANN001, PLR0913
    line: dict,
    layers: dict[str, str],
    status,  # noqa: ANN001
    last_update: str,
) -> str:
    layer_id = str(line.get("id") or "")
    message = str(line.get("status") or "")
    if layer_id:
        layers[layer_id] = message
    if message and message != last_update:
        status.update(f"[bold cyan]{message}[/]")
        return message
    return last_update


def pull_docker_image() -> None:
    from docker.errors import DockerException

    console = Console()
    client = check_docker_connection()
    image = load_settings().executor_image

    if image_exists(client, image):
        logger.debug("Docker image already present locally: %s", image)
        return

    logger.info("Pulling docker image: %s", image)
    console.print()
    console.print(f"[dim]Pulling image[/] {image}")
    console.print("[dim yellow]This only happens on first run and may take a few minutes...[/]")
    console.print()

    with console.status("[bold cyan]Downloading image layers...", spinner="dots") as status:
        try:
            layers_info: dict[str, str] = {}
            last_update = ""
            for line in client.api.pull(image, stream=True, decode=True):
                last_update = process_pull_line(line, layers_info, status, last_update)
        except DockerException as exc:
            error_text = Text()
            error_text.append("FAILED TO PULL IMAGE", style="bold red")
            error_text.append("\n\n", style="white")
            error_text.append(f"Could not download: {image}\n", style="white")
            error_text.append(str(exc), style="dim red")
            console.print(
                Panel(
                    error_text,
                    title="[bold white]BEN",
                    title_align="left",
                    border_style="red",
                    padding=(1, 2),
                ),
                "\n",
            )
            sys.exit(1)

    logger.info("Docker image %s ready", image)
    console.print(Text("Docker image ready", style="#22c55e"))
    console.print()
