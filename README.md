# deepagents_mongodb_fs

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**MongoDB Atlas-backed filesystem search adapter for LangChain DeepAgents.**

`deepagents_mongodb_fs` implements DeepAgents' `BackendProtocol`, routing `grep`, `glob`, and `ls` through MongoDB Atlas (vector search + full-text search + hybrid `$rankFusion`) while forwarding all other file operations (`read`, `write`, `edit`, `upload_files`, `download_files`) directly to S3.

## Architecture

```
DeepAgent
    │  grep / glob / ls
    ▼
MongoFilesystemBackend
    ├── SearchRouter ──► MongoDB Atlas  (vector + full-text + hybrid)
    └── S3Backend    ──► S3 bucket      (read / write / edit / upload / download)

Background:
    S3 ──(change event)──► S3Watcher ──► Chunker ──► Embedder ──► MongoDB
```

## Install

```bash
pip install deepagents_mongodb_fs
```

## Quickstart

```python
from deepagents_mongodb_fs import MongoFilesystemBackend

backend = MongoFilesystemBackend(
    s3_bucket_name="my-docs-bucket",
    mongodb_connection_string="mongodb+srv://user:pass@cluster.mongodb.net/",
)

# Hybrid search (full-text + vector)
result = backend.grep("authentication flow", path="docs/")
for match in result.matches:
    print(match.path, match.line, match.content[:80])

# Glob by filename pattern
pdfs = backend.glob("*.pdf", path="reports/")
print(pdfs.paths)

# List directory
ls = backend.ls("docs/")
for entry in ls.entries:
    print(entry.name, "DIR" if entry.is_dir else "FILE")

# Pass-through file operations
backend.write("docs/new.txt", "hello world")
content = backend.read("docs/new.txt").content
```

## Configuration

| Parameter | Required | Default | Description |
|---|---|---|---|
| `s3_bucket_name` | Yes | — | S3 bucket name |
| `mongodb_connection_string` | Yes | — | Atlas connection string |
| `embedding_model` | No | `OpenAIEmbeddings(text-embedding-3-small)` | LangChain Embeddings instance |
| `embedding_dimensions` | No | `1024` | Vector dimensions |
| `llm` | No | — | Reserved for future agent LLM integration |
| `watcher` | No | `"polling"` | `"polling"` or `"sqs"` |
| `sqs_queue_url` | If `watcher="sqs"` | — | Full SQS queue URL |
| `aws_region` | No | `AWS_DEFAULT_REGION` env | AWS region |
| `s3_prefix` | No | `""` | Only sync objects under this prefix |
| `debug` | No | `False` | Re-raise exceptions (local dev) |

## Design Decisions

### Chunking strategy
- **512 tokens / chunk, 64-token overlap** (tiktoken `cl100k_base`)
- Each chunk stores `source_path`, `chunk_index`, `page_number`, `char_start`, `char_end`, `line_start`
- `line_start` is what makes `grep` return DeepAgents-compatible `GrepMatch.line`

### Embedding model
- **`text-embedding-3-small` @ 1024 dimensions** — 10× cheaper than `ada-002`, better MTEB benchmarks
- 1024 dims balances semantic fidelity with storage cost at 100k-document scale

### Watcher
- **PollingWatcher** (default): ETag diff every 5 min, zero AWS infra required
- **SQSWatcher** (production): S3 event notifications via SQS long-polling, near real-time

## Error Handling

Every public method returns a DTO. If an error occurs, the `error` field contains a stable `ErrorCode` and a human-readable message — no raw stack traces ever surface.

```python
result = backend.grep("query")
if result.error:
    print(result.error)  # "[E5001] The grep search operation failed. Detail: ..."
```

See [docs/ERROR_CODES.md](docs/ERROR_CODES.md) for the full catalog.

## Requirements

- Python 3.10+
- MongoDB Atlas M0+ (Vector Search requires M10+ for production; Atlas Local works for dev)
- AWS S3 bucket + appropriate IAM permissions
- `OPENAI_API_KEY` env var (if using the default embedding model)

## Contributing

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — especially the "Adding an Azure Blob backend" walkthrough.
