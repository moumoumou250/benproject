"""厂商 API Key 池：为总节点/子节点保存多套连接，并在扫描时切换生效。

存储 ``~/.ben/keypool.json``（0600，复用 write_secret_text）。每个 entry 完整
描述一条连接：tier（root=总节点 / flash=子节点）+ model + api_key + 可选
base_url。某 tier 的 active entry **整体替换**该 tier 的连接（模型、Key、
端点三者一起迁，杜绝"sapper 军团换 key 不换 model"式的错配），在下一次
扫描子进程启动时生效（:func:`apply_to_env` 注入环境变量，扫描进程内零侵
入）；进行中的扫描不受影响。

安全约定：任何对外 payload（:func:`status_payload` / :func:`entry_view`）
只含 key 尾 4 位，明文 key 只落 0600 文件，不出 Web 边界。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from Ben.utils.secret_files import write_secret_text

logger = logging.getLogger(__name__)

TIERS = ("root", "flash")
TIER_LABELS = {"root": "总节点", "flash": "子节点"}
_MODEL_MAX = 200
_URL_MAX = 500
_LABEL_MAX = 60
_KEY_MAX = 500
_ID_LEN = 12

# 前缀路由包装：真正进 LiteLLM 目录的是剥壳后的名字。
_ROUTING_PREFIXES = ("litellm/", "any-llm/")


def pool_path() -> Path:
    return Path.home() / ".ben" / "keypool.json"


def _empty_pool() -> dict[str, Any]:
    return {"entries": [], "active": {}}


def read_pool() -> dict[str, Any]:
    """读池文件；缺失/损坏一律回空池（显示层宁可空也不抛错）。"""
    path = pool_path()
    if not path.is_file():
        return _empty_pool()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("keypool 文件损坏，按空池处理：%s", path)
        return _empty_pool()
    if not isinstance(data, dict):
        return _empty_pool()
    entries = data.get("entries")
    active = data.get("active")
    return {
        "entries": [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else [],
        "active": {str(k): str(v) for k, v in active.items()} if isinstance(active, dict) else {},
    }


def write_pool(pool: dict[str, Any]) -> None:
    write_secret_text(pool_path(), json.dumps(pool, indent=2, ensure_ascii=False))


def _entry_for(pool: dict[str, Any], tier: str) -> dict[str, Any] | None:
    entry_id = pool["active"].get(tier)
    if not entry_id:
        return None
    for entry in pool["entries"]:
        if entry.get("id") == entry_id and entry.get("tier") == tier:
            return entry
    return None


def active_entry(tier: str) -> dict[str, Any] | None:
    if tier not in TIERS:
        return None
    return _entry_for(read_pool(), tier)


def get_entry(pool: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
    for entry in pool["entries"]:
        if entry.get("id") == entry_id:
            return entry
    return None


def _validate_connection(model: str, api_key: str, base_url: str) -> None:
    if not model.strip():
        raise ValueError("模型名不能为空")
    if len(model) > _MODEL_MAX or len(api_key) > _KEY_MAX or len(base_url) > _URL_MAX:
        raise ValueError("模型名 / Key / 端点长度超限")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError("端点必须是 http(s) URL")


def _new_entry(
    tier: str,
    model: str,
    api_key: str,
    base_url: str,
    label: str | None,
) -> dict[str, Any]:
    return {
        "id": secrets.token_urlsafe(9)[:_ID_LEN],
        "tier": tier,
        "label": (label or "").strip()[:_LABEL_MAX] or None,
        "model": model.strip(),
        "api_key": api_key.strip(),
        "base_url": base_url.strip() or None,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_status": None,
        "last_checked_at": None,
        "last_error": None,
    }


def add_entry(
    tier: str,
    model: str,
    api_key: str = "",
    base_url: str = "",
    label: str | None = None,
) -> dict[str, Any]:
    """新增一条连接并立即使其成为该 tier 的 active（新增即切换，符合直觉）。"""
    if tier not in TIERS:
        raise ValueError("tier 必须是 root（总节点）或 flash（子节点）")
    model = str(model or "")
    api_key = str(api_key or "")
    base_url = str(base_url or "")
    _validate_connection(model, api_key, base_url)
    pool = read_pool()
    entry = _new_entry(tier, model, api_key, base_url, str(label) if label else None)
    pool["entries"].append(entry)
    pool["active"][tier] = entry["id"]
    write_pool(pool)
    logger.info("keypool: 已添加 %s 连接 model=%s tail=%s", TIER_LABELS[tier], entry["model"], key_tail(entry["api_key"]))
    return entry


def switch_entry(entry_id: str) -> dict[str, Any]:
    pool = read_pool()
    entry = get_entry(pool, entry_id)
    if entry is None:
        raise ValueError("Key 池中不存在该连接")
    pool["active"][str(entry["tier"])] = entry_id
    write_pool(pool)
    return entry


def clear_tier(tier: str) -> None:
    """取消该 tier 的 active entry，回落到 .env 默认连接。"""
    if tier not in TIERS:
        raise ValueError("tier 必须是 root 或 flash")
    pool = read_pool()
    pool["active"].pop(tier, None)
    write_pool(pool)


def delete_entry(entry_id: str) -> dict[str, Any]:
    pool = read_pool()
    entry = get_entry(pool, entry_id)
    if entry is None:
        raise ValueError("Key 池中不存在该连接")
    pool["entries"] = [e for e in pool["entries"] if e.get("id") != entry_id]
    if pool["active"].get(str(entry["tier"])) == entry_id:
        pool["active"].pop(str(entry["tier"]), None)
    write_pool(pool)
    return entry


# ---------------------------------------------------------------------------
# 生效注入：扫描子进程启动前把 active entry 写进环境变量映射
# ---------------------------------------------------------------------------

# 每个 tier 的（模型, Key, 端点）环境变量，与 settings.py 的别名一致。
_TIER_ENV = {
    "root": ("LLM_MODEL", "LLM_API_KEY", "LLM_API_BASE"),
    "flash": ("BEN_FLASH_MODEL", "BEN_FLASH_API_KEY", "BEN_FLASH_API_BASE"),
}


def apply_to_env(env: MutableMapping[str, str] | None = None) -> MutableMapping[str, str]:
    """把 active entry 注入环境变量映射，返回该映射。

    ``env=None`` 时直接改写 ``os.environ``（CLI 扫描 / 自检路径）；传入 dict
    时返回副本语义的映射（Web launcher 给扫描子进程构造 env 用）。

    - 有 active entry：三个变量整体替换；端点变量写空串表示"显式清除"
      （pydantic-settings 读到空串为 ""，下游 ``or None`` 视为未设置），
      避免回落到 .env 里残留的旧端点；
    - entry 带 Key 时同步厂商专属变量（如 DEEPSEEK_API_KEY），覆盖 shell
      环境里可能残留的旧值——LiteLLM 侧 setdefault 镜像对已存在的变量不
      生效，不主动覆盖就会用错 key；
    - 无 active entry：该 tier 的变量一律不动（保持 .env 默认行为）。
    """
    target: MutableMapping[str, str] = os.environ if env is None else dict(env)
    pool = read_pool()
    for tier, (model_var, key_var, base_var) in _TIER_ENV.items():
        entry = _entry_for(pool, tier)
        if entry is None:
            continue
        model = str(entry.get("model") or "")
        key = str(entry.get("api_key") or "")
        base = str(entry.get("base_url") or "")
        target[model_var] = model
        target[key_var] = key
        target[base_var] = base  # "" = 显式清除
        if key:
            _sync_vendor_key(target, model, key)
        if tier == "root" and not base:
            # 根节点换回厂商默认端点时，shell 里残留的 OPENAI_BASE_URL 会
            # 把请求指去旧端点，必须清掉。
            target.pop("OPENAI_BASE_URL", None)
    return target


def _strip_routing_prefix(model: str) -> str:
    name = model.strip()
    lowered = name.lower()
    for prefix in _ROUTING_PREFIXES:
        if lowered.startswith(prefix):
            return name[len(prefix):]
    return name


def _sync_vendor_key(env: MutableMapping[str, str], model: str, api_key: str) -> None:
    """把 LiteLLM 路由所需厂商变量写为 entry 的 key（仅在能确定厂商时）。"""
    name = _strip_routing_prefix(model)
    provider: str | None = None
    try:
        from litellm import get_llm_provider

        # 返回 (剥壳模型名, provider, api_key 元组, 自定义端点)
        _, provider, _, _ = get_llm_provider(model=name)
    except Exception:  # noqa: BLE001 - 未知厂商名/离线目录都按前缀兜底
        provider = name.split("/", 1)[0].lower() if "/" in name else None
    if not provider or provider in {"openai", "custom_openai", "hosted_vllm", "text-completion-openai"}:
        return
    env[f"{provider.upper().replace('-', '_')}_API_KEY"] = api_key


# ---------------------------------------------------------------------------
# Web 控制台的读取视图（明文 key 不出边界）
# ---------------------------------------------------------------------------


def key_tail(api_key: str | None) -> str | None:
    if not api_key:
        return None
    return f"…{api_key[-4:]}" if len(api_key) >= 8 else "…••••"


def vendor_name(model: str | None, base_url: str | None = None) -> str | None:
    if not model:
        return None
    name = _strip_routing_prefix(model)
    if "/" in name:
        vendor = name.split("/", 1)[0]
        return vendor or None
    if base_url:
        try:
            from urllib.parse import urlsplit

            host = urlsplit(base_url).hostname
            if host:
                return host
        except ValueError:
            pass
    return "openai-compatible"


def entry_view(entry: dict[str, Any]) -> dict[str, Any]:
    """对外视图：key 只留尾 4 位。"""
    return {
        "id": entry.get("id"),
        "tier": entry.get("tier"),
        "label": entry.get("label"),
        "model": entry.get("model"),
        "vendor": vendor_name(entry.get("model"), entry.get("base_url")),
        "key_tail": key_tail(entry.get("api_key")),
        "base_url": entry.get("base_url"),
        "added_at": entry.get("added_at"),
        "last_status": entry.get("last_status"),
        "last_checked_at": entry.get("last_checked_at"),
        "last_error": entry.get("last_error"),
        "active": False,  # 由 status_payload 统一标注
    }


def _tier_status(tier: str) -> dict[str, Any]:
    """某 tier 的当前生效连接：active entry 优先，否则 .env 默认。"""
    pool = read_pool()
    entry = _entry_for(pool, tier)
    from Ben.config import load_settings
    from Ben.config.grok_build import skips_api_key

    settings = load_settings(require_api_key=False)
    if tier == "root":
        model = settings.llm.model
        key = settings.llm.api_key
        base = settings.llm.api_base
    else:
        model = settings.llm.flash_model
        key = settings.llm.flash_api_key
        base = settings.llm.flash_api_base
    if entry is not None:
        model = entry.get("model")
        key = entry.get("api_key")
        base = entry.get("base_url")
    subscription = bool(model and skips_api_key(str(model)))
    return {
        "tier": tier,
        "model": model,
        "vendor": vendor_name(model, base),
        "key_tail": key_tail(key),
        "base_url": base or None,
        "subscription": subscription,
        "source": "keypool" if entry else "env",
        "entry_id": entry.get("id") if entry else None,
        "entry_label": entry.get("label") if entry else None,
    }


def status_payload() -> dict[str, Any]:
    """GET /api/keypool 聚合视图：两档生效连接 + entry 列表（标 active）。"""
    pool = read_pool()
    active_ids = {tier: pool["active"].get(tier) for tier in TIERS}
    entries = []
    for entry in pool["entries"]:
        view = entry_view(entry)
        view["active"] = active_ids.get(str(entry.get("tier"))) == entry.get("id")
        entries.append(view)
    return {
        "root": _tier_status("root"),
        "flash": _tier_status("flash"),
        "entries": entries,
        "pool_path": str(pool_path()),
    }


# ---------------------------------------------------------------------------
# 连通性测试：直接跑正式链路的最小往返
# ---------------------------------------------------------------------------


def test_entry(entry_id: str) -> dict[str, Any]:
    """对一条 entry 发最小真实请求并记录结果（复用开扫前的预检链路）。"""
    pool = read_pool()
    entry = get_entry(pool, entry_id)
    if entry is None:
        raise ValueError("Key 池中不存在该连接")
    started = time.monotonic()
    detail = ""
    status = "fail"
    try:
        from Ben.config import load_settings
        from Ben.interface.cli.scan_setup import preflight_model_connection

        settings = load_settings(require_api_key=False)
        asyncio.run(
            preflight_model_connection(
                str(entry["model"]),
                settings=settings,
                reuse=False,
                api_key=str(entry.get("api_key") or "") or None,
                api_base=str(entry.get("base_url") or "") or None,
            )
        )
        status = "ok"
        detail = f"连通正常（{time.monotonic() - started:.1f}s）"
    except Exception as exc:  # noqa: BLE001 - 失败原因要展示给操作者
        detail = str(exc)[:500] or exc.__class__.__name__
    # 重新读取（测试期间池可能被并发改写），只更新该 entry 的状态字段。
    pool = read_pool()
    entry = get_entry(pool, entry_id)
    if entry is not None:
        entry["last_status"] = status
        entry["last_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["last_error"] = None if status == "ok" else detail
        write_pool(pool)
    logger.info("keypool: 测试 %s -> %s (%s)", entry_id, status, detail[:120])
    return {"id": entry_id, "status": status, "detail": detail}
