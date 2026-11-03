"""LangChain embedding adapters; local hashing works without credentials."""

import hashlib
from itertools import pairwise
import math
import re
from langchain_core.embeddings import Embeddings


class LocalEmbeddings(Embeddings):
    dimensions = 2048

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower())
        tokens += [a + b for a, b in pairwise(tokens)]
        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1
        norm = math.sqrt(sum(v * v for v in vector)) or 1
        return [v / norm for v in vector]


class APIEmbeddings(Embeddings):
    def __init__(self, settings):
        from openai import OpenAI

        if not settings.embedding_api_key:
            raise ValueError("BEN_RAG_EMBEDDING_API_KEY is required for openai embeddings")
        self.client = OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            timeout=60,
            max_retries=2,
        )
        self.model = settings.embedding_model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        result = self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in sorted(result.data, key=lambda item: item.index)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def create_embeddings(settings):
    if settings.embedding_provider == "local":
        return LocalEmbeddings()
    if settings.embedding_provider == "openai":
        return APIEmbeddings(settings)
    raise ValueError("embedding_provider must be local or openai")
