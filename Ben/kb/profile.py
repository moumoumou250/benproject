"""靶场 profile 加载与闸门校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_BUILTIN = Path(__file__).resolve().parent / "profiles"
_USER = Path.home() / ".ben" / "kb-profiles"

_SECRET_LITERAL = re.compile(
    r"(?i)(Cookie|Authorization|Proxy-Authorization)\s*:|(Bearer\s+[A-Za-z0-9._\-]+)"
)


class ProfileError(ValueError):
    """profile 不合法；消息面向用户。"""


@dataclass(frozen=True)
class OracleStep:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    expect_status: int | None = None
    expect_body_regex: str | None = None
    expect_header_regex: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    expects_vuln: bool
    targets: tuple[str, ...]
    allowed_vulns: tuple[str, ...]
    max_budget_usd: float
    max_turns: int
    minutes: int
    baseline: tuple[dict[str, Any], ...]
    oracle: OracleStep | None
    setup: tuple[OracleStep, ...]
    teardown: tuple[OracleStep, ...]
    expected_criteria: tuple[int, ...]
    accounts: tuple[str, ...]
    source: Path
    # 注入给 agent 的短提示（禁止在 verify.py 写死某个 CVE）
    verify_hint: str = ""


def _as_steps(raw: Any, *, field_name: str) -> tuple[OracleStep, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProfileError(f"{field_name} 必须是列表")
    steps: list[OracleStep] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProfileError(f"{field_name}[{i}] 必须是映射")
        steps.append(_parse_oracle(item, required=False, label=f"{field_name}[{i}]"))
    return tuple(steps)


def _parse_oracle(raw: dict[str, Any], *, required: bool, label: str) -> OracleStep:
    method = str(raw.get("method") or "GET").upper()
    path = str(raw.get("path") or "").strip()
    if required and not path:
        raise ProfileError(f"{label}.path 必填")
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        raise ProfileError(f"{label}.headers 必须是映射")
    expect_header = raw.get("expect_header_regex") or {}
    if not isinstance(expect_header, dict):
        raise ProfileError(f"{label}.expect_header_regex 必须是映射")
    return OracleStep(
        method=method,
        path=path,
        headers={str(k): str(v) for k, v in headers.items()},
        body=None if raw.get("body") is None else str(raw.get("body")),
        expect_status=int(raw["expect_status"]) if raw.get("expect_status") is not None else None,
        expect_body_regex=(
            str(raw["expect_body_regex"]) if raw.get("expect_body_regex") is not None else None
        ),
        expect_header_regex={str(k): str(v) for k, v in expect_header.items()},
    )


def _scan_secret_literals(text: str, source: Path) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        if _SECRET_LITERAL.search(line):
            raise ProfileError(
                f"profile 禁止凭据字面量：{source}:{i}（用 accounts.ref / 密钥文件）"
            )


def load_profile(name: str) -> Profile:
    """用户级同名覆盖内置。"""
    candidates = [
        _USER / f"{name}.yaml",
        _USER / f"{name}.yml",
        _BUILTIN / f"{name}.yaml",
        _BUILTIN / f"{name}.yml",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise ProfileError(f"找不到 profile：{name}（查 Ben/kb/profiles/ 或 ~/.ben/kb-profiles/）")

    text = path.read_text(encoding="utf-8")
    _scan_secret_literals(text, path)
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ProfileError(f"profile YAML 非法：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"profile 根必须是映射：{path}")

    expects = bool(raw.get("expects_vuln", True))
    targets = tuple(str(t).rstrip("/") for t in (raw.get("targets") or []))
    if not targets:
        raise ProfileError("profile.targets 不能为空")
    allowed = tuple(str(v) for v in (raw.get("allowed_vulns") or []))
    if not allowed:
        raise ProfileError("profile.allowed_vulns 不能为空")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ProfileError("defaults 必须是映射")

    oracle_raw = raw.get("oracle")
    oracle: OracleStep | None = None
    if expects:
        if not isinstance(oracle_raw, dict):
            raise ProfileError("正样本 profile 必须含 oracle 映射")
        oracle = _parse_oracle(oracle_raw, required=True, label="oracle")
        if oracle.expect_status is None and not oracle.expect_body_regex and not oracle.expect_header_regex:
            raise ProfileError("oracle 至少声明 expect_status / expect_body_regex / expect_header_regex 之一")
    elif isinstance(oracle_raw, dict):
        oracle = _parse_oracle(oracle_raw, required=False, label="oracle")

    expected = raw.get("expected_criteria") or []
    if expected and not isinstance(expected, list):
        raise ProfileError("expected_criteria 必须是整数列表")

    accounts_raw = raw.get("accounts") or []
    accounts: list[str] = []
    for item in accounts_raw:
        if isinstance(item, dict):
            accounts.append(str(item.get("ref") or ""))
        else:
            accounts.append(str(item))

    baseline = raw.get("baseline") or []
    if not isinstance(baseline, list):
        raise ProfileError("baseline 必须是列表")

    hint = str(raw.get("verify_hint") or "").strip()
    if not hint:
        hint = str(raw.get("description") or "").strip().split("\n")[0][:160]

    return Profile(
        name=str(raw.get("name") or name),
        description=str(raw.get("description") or ""),
        expects_vuln=expects,
        targets=targets,
        allowed_vulns=allowed,
        max_budget_usd=float(defaults.get("max_budget_usd") or 0.30),
        max_turns=int(defaults.get("max_turns") or 10),
        minutes=int(defaults.get("minutes") or 8),
        baseline=tuple(baseline),
        oracle=oracle,
        setup=_as_steps(raw.get("setup"), field_name="setup"),
        teardown=_as_steps(raw.get("teardown"), field_name="teardown"),
        expected_criteria=tuple(int(x) for x in expected),
        accounts=tuple(a for a in accounts if a),
        source=path,
        verify_hint=hint,
    )


def assert_profile_allowed(profile: Profile, *, term: str) -> None:
    """双闸：目标 ∈ ALLOWED_TARGETS，且 term ∈ allowed_vulns。"""
    from Ben.config.settings import load_settings, require_allowed_targets
    from Ben.kb.vocab import canonical, normalize

    settings = load_settings(require_api_key=False)
    allowed = require_allowed_targets(settings)
    allowed_norm = {a.rstrip("/").lower() for a in allowed}
    for target in profile.targets:
        t = target.rstrip("/").lower()
        if t not in allowed_norm and f"{t}/" not in {a + "/" for a in allowed_norm}:
            # 也允许白名单项是前缀
            if not any(t.startswith(a.rstrip("/")) or a.rstrip("/").startswith(t) for a in allowed_norm):
                raise ProfileError(
                    f"profile 目标不在 ALLOWED_TARGETS：{target}（.env 白名单：{', '.join(allowed)}）"
                )

    resolved = canonical(term) or normalize(term)
    allowed_set = {normalize(v) for v in profile.allowed_vulns}
    if resolved not in allowed_set and normalize(term) not in allowed_set:
        raise ProfileError(
            f"term {term!r} 不在 profile.allowed_vulns：{list(profile.allowed_vulns)}"
        )
