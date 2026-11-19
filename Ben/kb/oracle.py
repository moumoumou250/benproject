"""正样本 oracle 预检：发已知可复现请求并落盘。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from Ben.kb.contract import ExitCode, SubReason
from Ben.kb.profile import OracleStep, Profile


@dataclass
class OracleResult:
    ok: bool
    code: ExitCode
    subreason: SubReason | None
    message: str
    path: str
    status: int | None = None
    detail: str = ""


def _join(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _run_step(
    base: str, step: OracleStep, *, timeout: float = 10.0
) -> tuple[int, str, dict[str, str], bytes, bytes]:
    """同步发请求；返回 status, url, resp_headers, raw_req, raw_resp_body。"""
    import httpx

    url = _join(base, step.path)
    headers = dict(step.headers)
    body = step.body
    raw_req = (
        f"{step.method} {url} HTTP/1.1\n"
        + "".join(f"{k}: {v}\n" for k, v in headers.items())
        + (f"\n{body}" if body else "\n")
    ).encode("utf-8", errors="replace")

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        resp = client.request(step.method, url, headers=headers, content=body)
    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status_code, url, resp_headers, raw_req, resp.content


def _check_expect(
    step: OracleStep, status: int, body: bytes, headers: dict[str, str]
) -> str | None:
    """返回失败原因；成功返回 None。"""
    text = body.decode("utf-8", errors="replace")
    if step.expect_status is not None and status != step.expect_status:
        return f"status={status} 期望 {step.expect_status}"
    if step.expect_body_regex:
        if not re.search(step.expect_body_regex, text, re.I | re.S):
            return f"body 未匹配 /{step.expect_body_regex}/"
    for hname, pattern in step.expect_header_regex.items():
        value = headers.get(hname.lower(), "")
        if not re.search(pattern, value, re.I):
            return f"header {hname}={value!r} 未匹配 /{pattern}/"
    return None


def run_oracle(
    profile: Profile,
    *,
    evidence_dir: Path,
    timeout: float = 10.0,
) -> OracleResult:
    """扫描前预检。失败 → ORACLE_FAILED（修靶场，不修 skill）。"""
    if not profile.expects_vuln:
        return OracleResult(
            ok=True,
            code=ExitCode.OK,
            subreason=None,
            message="负样本跳过 oracle",
            path="",
        )
    if profile.oracle is None:
        return OracleResult(
            ok=False,
            code=ExitCode.USAGE,
            subreason=None,
            message="正样本缺 oracle",
            path="",
        )

    base = profile.targets[0]
    oracle_dir = evidence_dir / "oracle"
    oracle_dir.mkdir(parents=True, exist_ok=True)

    steps: list[tuple[str, OracleStep]] = [
        *(("setup", s) for s in profile.setup),
        ("precheck", profile.oracle),
    ]

    last_path = profile.oracle.path
    try:
        for idx, (kind, step) in enumerate(steps, 1):
            status, url, headers, raw_req, raw_body = _run_step(base, step, timeout=timeout)
            prefix = f"{idx:02d}_{kind}_{step.method}_{step.path.strip('/').replace('/', '_') or 'root'}"
            (oracle_dir / f"{prefix}.req").write_bytes(raw_req)
            header_blob = "".join(f"{k}: {v}\n" for k, v in headers.items()).encode()
            (oracle_dir / f"{prefix}.resp").write_bytes(
                f"HTTP {status}\n".encode() + header_blob + b"\n" + raw_body
            )
            # setup 步：有 expect 才校验；无则只要求非 5xx
            if kind == "setup" and step.expect_status is None and not step.expect_body_regex:
                if status >= 500:
                    return OracleResult(
                        ok=False,
                        code=ExitCode.INCONCLUSIVE,
                        subreason=SubReason.ORACLE_FAILED,
                        message=f"setup 失败：HTTP {status} {url}",
                        path=step.path,
                        status=status,
                    )
                continue
            err = _check_expect(step, status, raw_body, headers)
            if err:
                return OracleResult(
                    ok=False,
                    code=ExitCode.INCONCLUSIVE,
                    subreason=SubReason.ORACLE_FAILED,
                    message=f"oracle 预检失败：{err} @ {url}",
                    path=step.path,
                    status=status,
                    detail=err,
                )
            last_path = step.path
    except Exception as exc:  # noqa: BLE001 — 预检失败不抛穿
        return OracleResult(
            ok=False,
            code=ExitCode.INCONCLUSIVE,
            subreason=SubReason.ORACLE_FAILED,
            message=f"oracle 请求异常：{exc}",
            path=last_path,
        )
    finally:
        for step in profile.teardown:
            try:
                _run_step(base, step, timeout=timeout)
            except Exception:  # noqa: BLE001
                pass

    return OracleResult(
        ok=True,
        code=ExitCode.OK,
        subreason=None,
        message="oracle 预检通过",
        path=profile.oracle.path,
        status=profile.oracle.expect_status,
    )


def oracle_to_dict(result: OracleResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "path": result.path,
        "status": result.status,
        "message": result.message,
        "detail": result.detail,
        "subreason": str(result.subreason) if result.subreason else None,
    }
