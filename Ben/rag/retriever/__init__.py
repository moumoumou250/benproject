import hashlib
import json
import math
from langchain_text_splitters import RecursiveCharacterTextSplitter
from Ben.rag.config import RagSettings
from Ben.rag.embeddings import create_embeddings
from Ben.rag.knowledge_base import load_documents
from Ben.rag.vector_store import VectorStore


class KnowledgeRetriever:
    def __init__(self, settings=None, embeddings=None):
        self.settings = settings or RagSettings()
        self.embeddings = embeddings or create_embeddings(self.settings)
        self.store = VectorStore(self.settings.database)
        self.signature = json.dumps(
            [
                self.settings.embedding_provider,
                self.settings.embedding_model
                if self.settings.embedding_provider != "local"
                else "hash-v1-2048",
                self.settings.embedding_base_url,
            ],
            ensure_ascii=False,
        )

    def ingest(self):
        s = self.settings
        if s.chunk_overlap >= s.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=s.chunk_size,
            chunk_overlap=s.chunk_overlap,
            add_start_index=True,
            separators=["\n\n", "\n", "。", "；", " ", ""],
        )
        docs = splitter.split_documents(list(load_documents(s.knowledge_dir)))
        rows = []
        dimensions = None
        for start in range(0, len(docs), 32):
            batch = docs[start : start + 32]
            vectors = self.embeddings.embed_documents([d.page_content for d in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding response count mismatch")
            for doc, vector in zip(batch, vectors, strict=True):
                if not vector or not all(math.isfinite(v) for v in vector):
                    raise ValueError("Invalid embedding vector")
                dimensions = dimensions or len(vector)
                if len(vector) != dimensions:
                    raise ValueError("Inconsistent embedding dimensions")
                meta = json.dumps(doc.metadata, ensure_ascii=False, sort_keys=True)
                chunk_id = hashlib.sha256((meta + doc.page_content).encode()).hexdigest()
                rows.append((chunk_id, doc.page_content, meta, json.dumps(vector)))
        self.store.replace(rows, self.signature)
        return {"chunks": len(rows), "sources": len({d.metadata["source"] for d in docs})}

    def search(self, query, k=5, source=None, min_score=0.0):
        if not query.strip() or len(query) > 10000:
            raise ValueError("query must contain 1 to 10000 characters")
        if not 1 <= k <= 20 or not -1 <= min_score <= 1:
            raise ValueError("k must be 1..20 and min_score -1..1")
        vector = self.embeddings.embed_query(query)
        if not vector or not all(math.isfinite(v) for v in vector):
            raise ValueError("Invalid query embedding")
        return self.store.search(vector, self.signature, k, source, min_score)
