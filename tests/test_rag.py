import json
import pytest
from agents.tool_context import ToolContext
from Ben.rag.config import RagSettings
from Ben.rag.retriever import KnowledgeRetriever
from Ben.rag.tools import search_knowledge


def settings(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    return RagSettings(
        knowledge_dir=kb,
        database=tmp_path / "index.sqlite3",
        embedding_provider="local",
        _env_file=None,
    )


def test_persistence_replacement_and_filter(tmp_path):
    s = settings(tmp_path)
    (s.knowledge_dir / "auth.txt").write_text("认证令牌 JWT authentication token", encoding="utf-8")
    (s.knowledge_dir / "sql.md").write_text("SQL database injection 数据库", encoding="utf-8")
    rag = KnowledgeRetriever(s)
    assert rag.ingest()["sources"] == 2
    results = KnowledgeRetriever(s).search("认证令牌 JWT", k=1)
    assert results[0]["source"] == "auth.txt"
    assert results[0]["chunk_id"] and results[0]["score"] > 0
    assert rag.search("JWT", source="missing") == []
    (s.knowledge_dir / "auth.txt").unlink()
    rag.ingest()
    assert rag.search("JWT", source="auth.txt") == []
    (s.knowledge_dir / "sql.md").unlink()
    assert rag.ingest()["chunks"] == 0


def test_failure_preserves_index_and_model_mismatch(tmp_path):
    s = settings(tmp_path)
    path = s.knowledge_dir / "a.txt"
    path.write_text("hello", encoding="utf-8")
    rag = KnowledgeRetriever(s)
    rag.ingest()
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        rag.ingest()
    assert rag.store.status()["chunks"] == 1
    rag.signature = "changed"
    with pytest.raises(ValueError, match="configuration changed"):
        rag.search("hello")


@pytest.mark.asyncio
async def test_agent_tool(tmp_path, monkeypatch):
    s = settings(tmp_path)
    (s.knowledge_dir / "test.txt").write_text("knowledge retrieval", encoding="utf-8")
    KnowledgeRetriever(s).ingest()
    monkeypatch.setenv("BEN_RAG_KNOWLEDGE_DIR", str(s.knowledge_dir))
    monkeypatch.setenv("BEN_RAG_DATABASE", str(s.database))
    monkeypatch.setenv("BEN_RAG_EMBEDDING_PROVIDER", "local")
    result = json.loads(
        await search_knowledge.on_invoke_tool(
            ToolContext(
                context={},
                tool_name="search_knowledge",
                tool_call_id="rag-test",
                tool_arguments="{}",
            ),
            '{"query":"knowledge","top_k":1,"source":null}',
        )
    )
    assert result["ok"] and result["results"][0]["source"] == "test.txt"
