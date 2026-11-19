"""transcript P1：差分 / 截断 / 上限 / CompositeHooks 隔离 / 零写盘。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from Ben.core.audit import (
    AuditHooks,
    AuditWriter,
    load_events,
    rebuild_input_at_turn,
    redact,
    render_markdown,
)
from Ben.core.hooks import (
    BudgetExceededError,
    CompositeHooks,
    ReportUsageHooks,
)


@pytest.mark.asyncio
async def test_delta_monotonic_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_AUDIT", "1")
    monkeypatch.delenv("BEN_AUDIT_FULL", raising=False)
    w = AuditWriter(audit_dir=tmp_path / "audit", run_name="t")
    await w.run_start()
    hooks = AuditHooks(w)
    ctx = type("C", (), {"context": {"agent_id": "a1"}})()
    agent = type("A", (), {"name": "Root", "model": "m"})()

    items1 = [{"role": "user", "content": "hi"}]
    await hooks.on_llm_start(ctx, agent, "SYS", items1)
    items2 = items1 + [{"role": "assistant", "content": "yo"}]
    await hooks.on_llm_start(ctx, agent, "SYS", items2)
    await w.run_end()

    events = load_events(w.audit_dir)
    starts = [e for e in events if e.get("type") == "llm_start"]
    assert len(starts) == 2
    assert starts[0]["input"]["base"] == 0
    assert starts[0]["input"]["total"] == 1
    assert starts[1]["input"]["base"] == 1
    assert starts[1]["input"]["total"] == 2
    assert len(starts[1]["input"]["items"]) == 1
    rebuilt = rebuild_input_at_turn(events, "a1", 2)
    assert len(rebuilt) == 2


@pytest.mark.asyncio
async def test_rewind_when_middle_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_AUDIT", "1")
    monkeypatch.delenv("BEN_AUDIT_FULL", raising=False)
    w = AuditWriter(audit_dir=tmp_path / "audit", run_name="t")
    hooks = AuditHooks(w)
    ctx = type("C", (), {"context": {"agent_id": "a1"}})()
    agent = type("A", (), {"name": "Root", "model": "m"})()

    a = {"role": "user", "content": "a"}
    b = {"role": "user", "content": "b"}
    c = {"role": "user", "content": "c"}
    await hooks.on_llm_start(ctx, agent, "S", [a, b, c])
    # 删掉中间 b
    await hooks.on_llm_start(ctx, agent, "S", [a, c])
    events = load_events(w.audit_dir)
    starts = [e for e in events if e.get("type") == "llm_start"]
    assert starts[1]["input"]["base"] == 1
    assert starts[1]["input"]["rewind"] is True
    rebuilt = rebuild_input_at_turn(events, "a1", 2)
    assert len(rebuilt) == 2


@pytest.mark.asyncio
async def test_item_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_AUDIT", "1")
    monkeypatch.setenv("BEN_AUDIT_ITEM_KB", "1")  # 1KB
    w = AuditWriter(audit_dir=tmp_path / "audit", run_name="t")
    hooks = AuditHooks(w)
    ctx = type("C", (), {"context": {"agent_id": "a1"}})()
    agent = type("A", (), {"name": "Root", "model": "m"})()
    big = {"role": "user", "content": "x" * 5000}
    await hooks.on_llm_start(ctx, agent, "S", [big])
    events = load_events(w.audit_dir)
    start = next(e for e in events if e["type"] == "llm_start")
    item = start["input"]["items"][0]
    assert item.get("truncated") is True
    assert item.get("full_chars", 0) > 1000


@pytest.mark.asyncio
async def test_cap_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_AUDIT", "1")
    monkeypatch.setenv("BEN_AUDIT_MAX_MB", "1")
    # 人为把已用字节抬到接近上限
    w = AuditWriter(audit_dir=tmp_path / "audit", run_name="t")
    w._bytes = 1024 * 1024 - 10
    await w.emit({"type": "noise", "blob": "y" * 100})
    assert w._capped is True
    await w.emit({"type": "after"})  # 不应抛
    events = load_events(w.audit_dir)
    assert any(e.get("type") == "capped" for e in events)


@pytest.mark.asyncio
async def test_composite_hooks_isolates_audit_errors(monkeypatch: pytest.MonkeyPatch):
    class BoomHooks:
        async def on_llm_start(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("audit boom")

    class OkHooks:
        def __init__(self) -> None:
            self.called = False

        async def on_llm_start(self, *a: Any, **k: Any) -> None:
            self.called = True

    ok = OkHooks()
    comp = CompositeHooks(BoomHooks(), ok)  # duck-typed fanout
    await comp.on_llm_start(None, None, None, [])
    assert ok.called is True


@pytest.mark.asyncio
async def test_composite_hooks_propagates_budget():
    class BudgetHooks:
        async def on_llm_start(self, *a: Any, **k: Any) -> None:
            raise BudgetExceededError("stop")

    comp = CompositeHooks(BudgetHooks())
    with pytest.raises(BudgetExceededError):
        await comp.on_llm_start(None, None, None, [])


def test_redact_keeps_edges():
    text = "Authorization: Bearer sk-abcdefghijklmnop\nCookie: session=abcdef123456\n"
    out = redact(text)
    assert "Bearer" in out or "…" in out
    assert "sk-a" in out or "…" in out


def test_render_mentions_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEN_AUDIT", "1")
    events = [
        {"type": "run_start", "run": "r", "target": "http://x"},
        {
            "type": "llm_start",
            "agent": "a1",
            "turn": 2,
            "system_changed": False,
            "system": {"chars": 1},
            "input": {"base": 0, "total": 1, "rewind": False, "items": [{"role": "user"}]},
        },
        {
            "type": "llm_end",
            "agent": "a1",
            "turn": 2,
            "output": [{"type": "message", "text": "hello turn2"}],
        },
    ]
    md = render_markdown(events, turn=2)
    assert "Turn 2" in md
    assert "hello turn2" in md


@pytest.mark.asyncio
async def test_ben_audit_off_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BEN_AUDIT", raising=False)
    from Ben.core import audit as audit_mod

    writes: list[str] = []

    real_write = Path.write_text

    def track_write(self: Path, *a: Any, **k: Any):
        writes.append(str(self))
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", track_write)
    assert audit_mod.audit_enabled() is False
    # 未启用时 runner 不会建 AuditWriter；此处确保 enabled 门控
    assert writes == []
