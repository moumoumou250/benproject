import argparse
import json
from Ben.rag.config import RagSettings
from Ben.rag.retriever import KnowledgeRetriever
from Ben.rag.vector_store import VectorStore


def main():
    parser = argparse.ArgumentParser(description="Ben RAG knowledge management")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ingest", help="原子重建索引，反映新增、修改和删除的资料")
    commands.add_parser("status", help="显示索引状态")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--source")
    search.add_argument("--min-score", type=float, default=0.0)
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = VectorStore(RagSettings().database).status()
        else:
            rag = KnowledgeRetriever()
            result = (
                rag.ingest()
                if args.command == "ingest"
                else rag.search(args.query, args.top_k, args.source, args.min_score)
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        parser.exit(1, f"RAG error: {exc}\n")


if __name__ == "__main__":
    main()
