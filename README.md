> # ⚠️ **This repository has been archived. Kindly refer this [repository](https://github.com/langchain-ai/langchain-mongodb/tree/main/libs/langchain-mongodb-deepagents-vfs) for detailed instructions on how to use and contribute.**

# deepagents_mongodb_fs

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**MongoDB Atlas-backed filesystem search adapter for LangChain DeepAgents.**

`deepagents_mongodb_fs` implements DeepAgents' `BackendProtocol`, routing `grep`, `glob`, and `ls` through MongoDB Atlas (vector search + full-text search + hybrid `$rankFusion`) while forwarding all other file operations (`read`, `write`, `edit`, `upload_files`, `download_files`) directly to S3.

## Architecture

![High-level architecture](./docs/images/high-level-arch-draw-io.png)

## Install

```bash
pip install deepagents_mongodb_fs
```

### Embedding providers

The adapter supports two embedding providers via optional extras:

```bash
# AWS Bedrock (default — uses boto3 credential chain, no extra API key needed)
pip install "deepagents_mongodb_fs[bedrock]"

# OpenAI
pip install "deepagents_mongodb_fs[openai]"
```

## Quickstart

```python
from deepagents_mongodb_fs import MongoFilesystemBackend

# Non-blocking: index provisioning, initial sync, and the watcher
# all start in a background thread. grep/glob/ls block until ready.
backend = MongoFilesystemBackend(
    s3_bucket_name="my-docs-bucket",
    mongodb_connection_string="mongodb+srv://user:pass@cluster.mongodb.net/",
)

# Hybrid search (full-text + vector via $rankFusion)
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

# Conditional in-place edit (ETag-verified read-modify-write)
backend.edit("docs/new.txt", old="hello", new="goodbye")

# Graceful shutdown (or use as a context manager)
backend.stop()
```

### Context manager

```python
with MongoFilesystemBackend(
    s3_bucket_name="my-docs-bucket",
    mongodb_connection_string="mongodb+srv://...",
) as backend:
    result = backend.grep("authentication flow")
# watcher is stopped automatically on exit
```

## Configuration

| Parameter | Required | Default | Description |
|---|---|---|---|
| `s3_bucket_name` | Yes | — | S3 bucket name |
| `mongodb_connection_string` | Yes | — | Atlas connection string |
| `embedding_model` | No | `BedrockEmbeddings(titan-embed-text-v2:0)` | Any LangChain `Embeddings` instance |
| `embedding_dimensions` | No | `1024` | Vector dimensions; must match the model |
| `llm` | No | — | Reserved for future agent LLM integration |
| `watcher` | No | `"polling"` | `"polling"` or `"sqs"` |
| `sqs_queue_url` | If `watcher="sqs"` | — | Full SQS queue URL |
| `aws_region` | No | `AWS_DEFAULT_REGION` env | AWS region for S3 and SQS clients |
| `s3_prefix` | No | `""` | Only sync/watch objects under this S3 prefix |
| `debug` | No | `False` | Re-raise exceptions instead of returning error DTOs (local dev) |

### Embedding provider selection

The provider is resolved at construction time from the `EMBEDDING_PROVIDER` environment variable (default `bedrock`). Override the model with `EMBEDDING_MODEL`.

| `EMBEDDING_PROVIDER` | Default model | Required credential |
|---|---|---|
| `bedrock` (default) | `amazon.titan-embed-text-v2:0` | boto3 credential chain (IAM role, `~/.aws/credentials`, etc.) |
| `openai` | `text-embedding-3-small` | `OPENAI_API_KEY` env var |

Pass a `LangChain Embeddings` instance directly to `embedding_model` to bypass provider lookup entirely.

## Design Decisions

### Non-blocking constructor

`MongoFilesystemBackend.__init__` returns immediately. Index provisioning, initial sync, and the background watcher all start in a single daemon thread. Search methods (`grep`, `glob`, `ls`) block internally until the first sync completes, so the first call may take longer than subsequent ones.

### Supported file formats

The chunker extracts text from the following formats before embedding:

| Extension | Parser |
|---|---|
| `.txt`, `.md`, `.rst`, `.csv` | UTF-8 decode |
| `.pdf` | pypdf (layout mode + plain fallback) |
| `.docx` | python-docx |
| `.xlsx` | openpyxl (one page per sheet) |
| `.xls` | xlrd (one page per sheet) |
| `.pptx` | python-pptx (one page per slide, includes speaker notes) |
| `.ppt` | olefile OLE stream scan (best-effort) |

Format is detected by file extension; magic bytes are used as a fallback for extensionless objects.

### Chunking strategy

- **512 tokens / chunk, 64-token overlap** (tiktoken `cl100k_base`)
- Each chunk stores `source_path`, `chunk_index`, `page_number`, `char_start`, `char_end`, `line_start`
- `line_start` is what makes `grep` return DeepAgents-compatible `GrepMatch.line`

### Embedding model

- **AWS Bedrock `amazon.titan-embed-text-v2:0` @ 1024 dimensions** — default; uses the boto3 credential chain, no extra API key needed
- **OpenAI `text-embedding-3-small` @ 1024 dimensions** — alternative; 10× cheaper than `ada-002`, strong MTEB benchmarks
- 1024 dims balances semantic fidelity with storage cost at 100k-document scale

### Search

`grep` uses `$rankFusion` (equal 0.5/0.5 weights) combining:
- **Atlas Full-Text Search** (`lucene.standard` on `content`)
- **Atlas Vector Search** (cosine similarity on `embedding`)

If the embedding API is unavailable at query time, `grep` falls back to full-text only. On non-Atlas MongoDB (mongomock, community server), both `grep` and `glob` fall back to regex queries.

### Watcher

- **PollingWatcher** (default): ETag diff on a 10-second interval, zero AWS infra required. Backs off gracefully on S3 list failures.
- **SQSWatcher** (production): S3 event notifications via SQS long-polling (20s), near real-time. Requires S3 → SQS event notifications configured in AWS.

### ETag-based idempotency

Initial sync and every watcher ingest pass are idempotent: objects whose ETag hasn't changed since the last run are skipped. Restarting the backend after a partial failure resumes cheaply without re-embedding unchanged files.

## Error Handling

Every public method returns a DTO. If an error occurs, the `error` field contains a stable `ErrorCode` and a human-readable message — no raw stack traces ever surface to the caller.

```python
result = backend.grep("query")
if result.error:
    print(result.error)  # "[E5001] The grep search operation failed. Detail: ..."
```

Enable `debug=True` during local development to re-raise the original exception with a full traceback instead.

See [docs/ERROR_CODES.md](docs/ERROR_CODES.md) for the full catalog.

## Requirements

- Python 3.10+
- MongoDB Atlas M0+ (Vector Search and Full-Text Search require M10+ for production; Atlas Local works for dev)
- AWS S3 bucket + appropriate IAM permissions (`s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:DeleteObject`)
- AWS credentials accessible via the boto3 credential chain (required for S3 and for Bedrock embeddings)
- `OPENAI_API_KEY` env var — only if using `EMBEDDING_PROVIDER=openai`

### IAM permissions

Minimum policy for the S3 backend:

```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
  "Resource": ["arn:aws:s3:::my-docs-bucket", "arn:aws:s3:::my-docs-bucket/*"]
}
```

When using `watcher="sqs"`, also grant `sqs:ReceiveMessage` and `sqs:DeleteMessage` on the queue.

When using Bedrock embeddings, also grant `bedrock:InvokeModel` for `amazon.titan-embed-text-v2:0`.

## Testing

```bash
pytest -m unit            # fast, no external services (92 tests)
pytest -m integration     # requires moto + mongomock (auto-installed via dev extras)
pytest -m e2e             # full stack, moto + mongomock (no real credentials needed)
pytest -m real_e2e        # requires real Atlas + S3 + embedding provider credentials
pytest -m watcher_e2e     # PollingWatcher E2E; subset of real_e2e
```

Copy `.env.e2e.template` to `.env.e2e`, fill in your credentials, then:

```bash
set -a && source .env.e2e && set +a
pytest -m real_e2e -v
```

## Contributing

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — especially the "Adding an Azure Blob backend" walkthrough.
