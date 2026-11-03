# RAG 知识检索

操作规范、执行步骤、工具使用约定继续放在 `Ben/skills/`，由现有 Skill 机制加载。
知识资料放在 `knowledge_base/`，经切分和向量化写入向量知识库，通过 RAG 按需检索。
LangChain 仅用于 RAG 的资料处理、向量化、向量库接入和检索，不承担 Agent 编排；
Agent 生命周期、工具调度和多轮执行仍由现有 Agent SDK 与 `Ben/core/` 负责。

```text
rag/
├── knowledge_base/   # 知识资料源文件与来源元数据
├── embeddings/       # 向量化适配与配置
├── vector_store/     # 向量数据库、索引与持久化
├── retriever/        # 检索、过滤和结果整理
└── tools/            # 提供给 Agent 的检索工具
```

预期资料流：知识资料 → 切分 → embeddings → vector_store → retriever → tools → Agent 上下文。
检索结果应保留来源、片段标识和相关性信息；检索文本作为参考资料，不作为操作规范执行。

## 使用

将 UTF-8 `.md`、`.txt` 或可提取文本的 `.pdf` 放入 `knowledge_base/`（递归扫描，跳过 README.md）。
操作规范继续放在 `Ben/skills/`，不会自动迁移或索引 Skill。

本次已建立独立 `.venv-rag` 环境，避免现有 `.venv` 的扩展文件占用。在项目根目录执行：

```powershell
.venv-rag/Scripts/python.exe -m Ben.rag ingest
.venv-rag/Scripts/python.exe -m Ben.rag status
.venv-rag/Scripts/python.exe -m Ben.rag search "认证漏洞" --top-k 5
.venv-rag/Scripts/python.exe -m Ben.rag search "认证漏洞" --source reference.md --min-score 0.1
```

新增、修改或删除资料后重新运行 `ingest`；它原子替换索引，处理失败保留旧索引。
空资料目录会清空索引。扫描型 PDF 需先 OCR。本实现适合本地中小型知识库，使用 SQLite
保存向量并精确计算余弦相似度，检索复杂度随片段数线性增长。

默认 `BEN_RAG_EMBEDDING_PROVIDER=local` 使用确定性词项哈希向量，支持中英文关键词，
无需下载模型或联网，但不具备深度语义、跨语言同义检索能力。需要语义检索时配置：

```dotenv
BEN_RAG_EMBEDDING_PROVIDER=openai
BEN_RAG_EMBEDDING_MODEL=text-embedding-3-small
BEN_RAG_EMBEDDING_API_KEY=你的Embedding密钥
BEN_RAG_EMBEDDING_BASE_URL=https://api.openai.com/v1
```

可指向兼容 OpenAI Embeddings API 的服务；资料片段会发送到该服务。
配置从项目 `.env` 和环境变量读取，支持 `BEN_ENV_FILE`；环境变量优先。
更换 Embedding 提供方、模型或端点后重新入库，禁止混用不同向量空间。
默认数据库位于 `Ben/rag/vector_store/knowledge.sqlite3`。运行账户须有目录写入权限（包括 SQLite journal 文件）；可用 `BEN_RAG_DATABASE` 指定其他可写路径。
其他参数见项目 `.env.example`。

root 和 child Agent 均已注册 `search_knowledge(query, top_k, source)`，结果含
`content`、`source`、`chunk_id`、`score`，PDF 另含 `page`。空库返回空列表；错误作为工具结果返回。
Agent 通过现有 SDK 将检索结果加入上下文，LangChain 仅提供 Embeddings 接口和文本切分。
参考接口：[Embeddings](https://reference.langchain.com/python/langchain-core/embeddings/embeddings/Embeddings)。
