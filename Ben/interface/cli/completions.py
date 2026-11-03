"""Shell completion scripts and candidates for the Ben CLI."""

from __future__ import annotations

import sys
from typing import Any

from Ben.core.paths import run_record_path, runs_base_dir
from Ben.interface.terminal.text import has_terminal_control, sanitize_terminal_text

_ROOT_COMMANDS = ("view", "completions", "completion")
_VIEW_FLAGS = ("--port", "--host", "--no-open", "-h", "--help")
_VIEW_VALUE_FLAGS = frozenset({"--port", "--host"})


def run_completions(argv: list[str]) -> int:
    """Print a shell integration script or hidden completion candidates."""
    if argv and argv[0] == "--candidates":
        for candidate in completion_candidates(argv[1:]):
            sys.stdout.write(candidate + "\n")
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(
            "Usage: ben completions <zsh|bash|fish>\n\n"
            "Enable tab completion for the current shell:\n"
            "  zsh:  source <(ben completions zsh)\n"
            "  bash: source <(ben completions bash)\n"
            "  fish: ben completions fish | source\n"
        )
        return 0
    shell = argv[0].lower()
    scripts = {"zsh": _zsh_script, "bash": _bash_script, "fish": _fish_script}
    generator = scripts.get(shell)
    if generator is None:
        sys.stderr.write(
            f"Unknown shell: {sanitize_terminal_text(shell)}. Choose zsh, bash, or fish.\n"
        )
        return 2
    sys.stdout.write(generator())
    return 0


def completion_candidates(words: list[str]) -> list[str]:
    """Return candidates for words after the ``ben`` executable."""
    prior, current = _split_cursor(words)
    if not prior:
        candidates = _matching(_ROOT_COMMANDS, current)
    elif prior[0] == "view":
        candidates = _view_candidates(prior[1:], current)
    else:
        candidates = []
    # The line-oriented shell protocol cannot represent these names safely.
    # Omitting them is preferable to returning a sanitized path that does not exist.
    return [candidate for candidate in candidates if not has_terminal_control(candidate)]


def _view_candidates(prior: list[str], current: str) -> list[str]:
    """Flags plus the names of runs under ./strix_runs."""
    if prior and prior[-1] in _VIEW_VALUE_FLAGS:
        return []
    run_names = _run_names()
    if current.startswith("-"):
        return _matching(_VIEW_FLAGS, current)
    return _matching(run_names, current)


def _run_names() -> list[str]:
    base = runs_base_dir()
    if not base.is_dir():
        return []
    return sorted(
        (child.name for child in base.iterdir() if run_record_path(child).is_file()),
        reverse=True,
    )


def _split_cursor(words: list[str]) -> tuple[list[str], str]:
    if not words:
        return [], ""
    return words[:-1], words[-1]


def _matching(candidates: Any, prefix: str) -> list[str]:
    return sorted({str(candidate) for candidate in candidates if str(candidate).startswith(prefix)})


def _zsh_script() -> str:
    return r"""#compdef ben
_ben() {
  local -a candidates
  candidates=("${(@f)$($words[1] completions --candidates "${words[@]:2}")}")
  _describe 'ben' candidates
}
compdef _ben ben
"""


def _bash_script() -> str:
    return r"""_ben_completion() {
  local -a candidates
  local candidate
  while IFS= read -r candidate; do
    candidates+=("$candidate")
  done < <(ben completions --candidates "${COMP_WORDS[@]:1:$COMP_CWORD}")
  COMPREPLY=("${candidates[@]}")
  for candidate in "${COMPREPLY[@]}"; do
    if [[ $candidate == */ ]]; then
      if type compopt >/dev/null 2>&1; then
        compopt -o nospace
      fi
      break
    fi
  done
}
complete -F _ben_completion ben
"""


def _fish_script() -> str:
    return r"""function __ben_candidates
  set -l words (commandline -opc)
  set -e words[1]
  command ben completions --candidates $words (commandline -ct)
end
complete -c ben -f -a '(__ben_candidates)'
"""


__all__ = ["completion_candidates", "run_completions"]
