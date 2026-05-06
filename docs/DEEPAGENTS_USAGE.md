# DeepAgents Subagent Usage Guide

This document explains how DeepAgents subagents interact with `MongoFilesystemBackend` and the use cases the adapter is designed to cover.

---

## How Subagents Use This Adapter

DeepAgents exposes a `BackendProtocol` — a typed interface defining the filesystem tools available to agents. `MongoFilesystemBackend` implements that protocol. The framework wraps each method as a **callable tool** that any subagent can invoke during its reasoning loop.

```
Subagent reasoning loop
  └─ "I need to find authentication code"
       ├─ tool_call: grep("authentication flow", path="src/")
       ├─ tool_call: ls("src/auth/")
       └─ tool_call: read("src/auth/middleware.py")
```

The subagent receives clean DTOs in return — `GrepResult`, `LsResult`, `ReadResult` — which it can reason over directly. It never touches MongoDB, S3, or embeddings.

---

## Full Tool Surface Available to Subagents

| Method | What it does | Return type |
|---|---|---|
| `grep(pattern, path, glob)` | Semantic + keyword hybrid search across all file content | `GrepResult` → ranked `GrepMatch(path, line, content, score)` |
| `glob(pattern, path)` | Find files by name pattern (e.g. `"*.py"`, `"test_*.md"`) | `GlobResult` → list of matching paths |
| `ls(path)` | List immediate children of a directory | `LsResult` → `LsEntry(name, is_dir)` |
| `read(path, offset, limit)` | Read raw file content from S3 | `ReadResult` → decoded string |
| `write(path, content)` | Write/overwrite a file in S3 | `WriteResult` |
| `edit(path, old, new)` | Surgical string replacement with ETag conflict detection | `EditResult` |
| `upload_files([(path, bytes)])` | Bulk upload | `UploadResult` |
| `download_files([paths])` | Bulk download | `DownloadResult` |

---

## Use Cases

### 1. Codebase-aware coding agents

An agent working on a large repo it has never seen can navigate it progressively:

```python
backend.ls("")                                      # explore top-level structure
backend.glob("*.py", path="src/")                  # find all Python files
backend.grep("rate limiting logic")                 # jump to the relevant chunk
backend.read("src/middleware.py")                   # fetch the full file
backend.edit("src/middleware.py", old_code, new)    # apply a change
```

The hybrid search (`$rankFusion`) means `grep` works on both natural-language queries ("how does retry logic work") and literal patterns ("MAX_RETRIES"), covering the full range of queries a coding agent would issue.

### 2. Document Q&A / RAG over private knowledge bases

An agent answering questions over internal PDFs, docs, or wikis stored in S3:

```python
backend.grep("refund policy for enterprise customers", path="policies/")
# → semantic search finds the right chunk even if the exact words don't appear
backend.read("policies/enterprise_tos.pdf")
# → retrieve the full document for detailed reading
```

### 3. Multi-agent pipelines with specialised subagents

A coordinator spawns subagents with scoped access to different parts of the filesystem:

- **Security review agent**: `grep("eval(", glob="*.py")` — scan for dangerous patterns
- **Docs agent**: `glob("*.md")` + `grep("deployment steps")`
- **Test agent**: `glob("test_*.py")` + `read(...)` for each file

Each subagent operates independently on the same shared index.

### 4. Long-running background monitoring

The SQS watcher keeps the index live as S3 changes. An agent working hours later gets fresh results — files added, modified, or deleted since the last run are automatically re-indexed with no manual refresh required.

### 5. Progressive narrowing (the natural agent search pattern)

This is the most common pattern: each tool call provides just enough signal to drive the next decision, avoiding large context dumps.

```
ls("")
  → finds "services/" directory
glob("*.yaml", path="services/")
  → finds config files
grep("timeout: 30", path="services/", glob="*.yaml")
  → finds where timeout is configured
read("services/api-gateway.yaml")
  → reads the exact file
edit("services/api-gateway.yaml", 'timeout: 30', 'timeout: 60')
  → patches it
```

---

## What Makes This Different From Plain RAG

Standard RAG pipelines retrieve chunks and feed them to an LLM in one shot. This adapter gives agents **interactive, iterative filesystem navigation**:

- Agents can drill down, cross-reference, read exact bytes, and write back.
- `grep` handles both semantic queries and literal keyword searches via `$rankFusion`.
- `ls` and `glob` let agents orient themselves in an unfamiliar codebase before querying content.
- `edit` with ETag-based conflict detection means multiple agents can write concurrently without silently clobbering each other.
- The background watcher ensures the index reflects the live state of S3 at all times.
