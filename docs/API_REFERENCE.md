# API Reference

## `MongoFilesystemBackend`

```python
from deepagents_mongodb_fs import MongoFilesystemBackend
```

The main entry point. Implements DeepAgents' `BackendProtocol`.

### Constructor

```python
MongoFilesystemBackend(
    s3_bucket_name: str,
    mongodb_connection_string: str,
    llm: Any = None,
    embedding_model: Embeddings | None = None,
    embedding_dimensions: int = 1024,
    watcher: Literal["polling", "sqs"] = "polling",
    sqs_queue_url: str | None = None,
    aws_region: str | None = None,
    s3_prefix: str = "",
    debug: bool = False,
)
```

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `s3_bucket_name` | `str` | Yes | — | S3 bucket name |
| `mongodb_connection_string` | `str` | Yes | — | Atlas or compatible Mongo URI |
| `llm` | `Any` | No | `None` | Reserved for future LLM integration |
| `embedding_model` | `Embeddings` | No | OpenAI `text-embedding-3-small` | LangChain Embeddings instance |
| `embedding_dimensions` | `int` | No | `1024` | Must match the embedding model's output |
| `watcher` | `str` | No | `"polling"` | `"polling"` or `"sqs"` |
| `sqs_queue_url` | `str` | If `watcher="sqs"` | `None` | Full SQS queue URL |
| `aws_region` | `str` | No | env var | AWS region |
| `s3_prefix` | `str` | No | `""` | Only sync keys under this prefix |
| `debug` | `bool` | No | `False` | Re-raise exceptions (local dev) |

**Raises:** `AdapterError(E1001)` if required parameters are missing.

---

### `grep(pattern, path="", glob="") → GrepResult`

Hybrid search over file contents (Atlas Full-Text + Vector Search + `$rankFusion`).
Falls back to regex on non-Atlas MongoDB.

| Parameter | Type | Description |
|---|---|---|
| `pattern` | `str` | Natural-language or keyword query |
| `path` | `str` | Restrict to source_paths starting with this prefix |
| `glob` | `str` | Further restrict to filenames matching this glob |

**Returns:** `GrepResult`

| Field | Type | Description |
|---|---|---|
| `matches` | `list[GrepMatch]` | Ranked results, deduplicated by `(path, line)` |
| `error` | `str | None` | Error message if operation failed |

`GrepMatch` fields: `path: str`, `line: int` (1-indexed line number), `content: str`, `score: float`

**Error codes:** E5001, E4001, E4003

---

### `glob(pattern, path="") → GlobResult`

Find files by filename pattern (Atlas Search wildcard or fnmatch fallback).

| Parameter | Type | Description |
|---|---|---|
| `pattern` | `str` | Glob pattern applied to the filename (e.g. `"*.pdf"`) |
| `path` | `str` | Restrict to this path prefix |

**Returns:** `GlobResult`

| Field | Type | Description |
|---|---|---|
| `paths` | `list[str]` | Sorted list of matching source paths |
| `error` | `str | None` | Error message if operation failed |

**Error codes:** E5002

---

### `ls(path) → LsResult`

List immediate children of a virtual directory path.

| Parameter | Type | Description |
|---|---|---|
| `path` | `str` | Directory path (e.g. `"docs/"`) |

**Returns:** `LsResult`

| Field | Type | Description |
|---|---|---|
| `path` | `str` | The queried path |
| `entries` | `list[LsEntry]` | Sorted list of child entries |
| `error` | `str | None` | Error message if operation failed |

`LsEntry` fields: `name: str`, `is_dir: bool`, `size: int`

**Error codes:** E5003

---

### `read(path, offset=0, limit=-1) → ReadResult`

Read bytes from S3 and decode as UTF-8.

| Parameter | Type | Description |
|---|---|---|
| `path` | `str` | Object key |
| `offset` | `int` | Byte offset to start from |
| `limit` | `int` | Maximum bytes to return (-1 = all) |

**Returns:** `ReadResult`

| Field | Type | Description |
|---|---|---|
| `path` | `str` | The requested path |
| `content` | `str` | Decoded file content |
| `error` | `str | None` | Error message if operation failed |

**Error codes:** E2001, E2002

---

### `write(path, content) → WriteResult`

Write a string to S3 (creates or replaces).

| Parameter | Type | Description |
|---|---|---|
| `path` | `str` | Object key |
| `content` | `str` | Content to write (UTF-8 encoded) |

**Returns:** `WriteResult`

| Field | Type | Description |
|---|---|---|
| `path` | `str` | The written path |
| `success` | `bool` | True if write succeeded |
| `error` | `str | None` | Error message if operation failed |

**Error codes:** E2003

---

### `edit(path, old, new, replace_all=False) → EditResult`

Conditional read-modify-write with ETag verification.

| Parameter | Type | Description |
|---|---|---|
| `path` | `str` | Object key |
| `old` | `str` | Substring to find |
| `new` | `str` | Replacement string |
| `replace_all` | `bool` | Replace every occurrence if True |

**Returns:** `EditResult`

| Field | Type | Description |
|---|---|---|
| `path` | `str` | The edited path |
| `success` | `bool` | True if edit succeeded |
| `error` | `str | None` | Error message if operation failed |

**Error codes:** E2001, E2008

---

### `upload_files(files) → UploadResult`

Upload multiple `(path, bytes)` pairs to S3.

| Parameter | Type | Description |
|---|---|---|
| `files` | `list[tuple[str, bytes]]` | List of `(key, content)` tuples |

**Returns:** `UploadResult`

| Field | Type | Description |
|---|---|---|
| `uploaded` | `list[str]` | Successfully uploaded paths |
| `failed` | `list[str]` | Failed paths |
| `error` | `str | None` | Error message if any upload failed |

**Error codes:** E2006

---

### `download_files(paths) → DownloadResult`

Download multiple objects from S3.

| Parameter | Type | Description |
|---|---|---|
| `paths` | `list[str]` | List of object keys |

**Returns:** `DownloadResult`

| Field | Type | Description |
|---|---|---|
| `downloaded` | `list[str]` | Successfully downloaded paths |
| `failed` | `list[str]` | Failed paths |
| `error` | `str | None` | Error message if any download failed |

**Error codes:** E2007

---

### `stop() → None`

Gracefully stop the background watcher.

---

## `AdapterError`

```python
from deepagents_mongodb_fs import AdapterError

raise AdapterError(ErrorCode.E2001_OBJECT_NOT_FOUND, "Key 'docs/x.txt' not found")
```

| Attribute | Type | Description |
|---|---|---|
| `code` | `ErrorCode` | Stable error code enum value |
| `message` | `str` | Human-readable description |
| `cause` | `BaseException | None` | Original exception |

---

## `ErrorCode`

```python
from deepagents_mongodb_fs import ErrorCode

# Example: check error code from a DTO
if result.error and ErrorCode.E4003_EMBEDDING_RATE_LIMITED.value in result.error:
    print("Rate limited")
```

See [ERROR_CODES.md](ERROR_CODES.md) for the full catalog.

---

## Internal Components (not part of the public API)

These classes are documented for contributors extending the package.

### `ObjectStoreBackend` (ABC)

Extensibility seam for object store backends. Implement this to add Azure Blob, GCS, or local disk support. See [CONTRIBUTING.md](CONTRIBUTING.md).

### `Chunker`

`Chunker(token_limit=512, overlap=64)`

Converts raw bytes to `list[Chunk]` using format-specific extractors and token-aware recursive splitting.

### `Embedder`

`Embedder(model=None, model_name="text-embedding-3-small", dimensions=1024, batch_size=100)`

Wraps a LangChain `Embeddings` model with batching and exponential-backoff retry.

### `IndexManager`

`IndexManager(collection, embedding_dimensions=1024, wait_timeout=600)`

Idempotently provisions Atlas Vector Search, Atlas Full-Text Search, and a compound native index.

### `InitialSync`

`InitialSync(store, chunker, embedder, collection)`

`run(prefix="", dry_run=False) → SyncReport`

### `PollingWatcher` / `SQSWatcher`

Both extend `S3Watcher` and share `on_created` / `on_updated` / `on_deleted` callback logic.

### `SearchRouter`

`SearchRouter(collection, embedder, atlas_available=None)`

Routes `ls`, `glob`, `grep` to the appropriate MongoDB query strategy.
